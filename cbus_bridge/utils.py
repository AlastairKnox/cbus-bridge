from __future__ import annotations

import logging
import re
from typing import Optional

LOG = logging.getLogger("cbus_bridge.utils")

HEX_RE = re.compile(r"[0-9A-Fa-f]")

# ----------------------------
# Utilities
# ----------------------------

def normalize_hex_from_bytes(raw: bytes) -> str:
    """
    Keep only hex characters; uppercase result; enforce even nibble count.

    Why enforce even nibble count:
      Some CNI/PCI streams can include non-hex terminators or markers (e.g. 'cn').
      Because 'c' is a hex character but 'n' is not, naive filtering can leave an
      odd number of hex characters, which breaks bytes.fromhex() and causes drops
      of otherwise valid frames.

      The Arduino parser naturally reads in *pairs* (bytes), so dropping a trailing
      nibble matches Arduino behaviour better than rejecting the whole line.
    """
    chars: list[str] = []
    for b in raw:
        c = chr(b)
        if HEX_RE.match(c):
            chars.append(c.upper())

    # Enforce byte alignment (2 hex chars per byte).
    if len(chars) % 2 == 1:
        LOG.debug(
            "normalize_hex_from_bytes: dropping trailing nibble %r (raw=%r)",
            chars[-1],
            raw,
        )
        chars.pop()

    return "".join(chars)


def checksum_2s_complement(frame_bytes: bytes) -> int:
    """C-Bus checksum used in your status request examples: (-sum(bytes)) mod 256."""
    return (-sum(frame_bytes)) & 0xFF


def clamp(n: int, lo: int, hi: int) -> int:
    return lo if n < lo else hi if n > hi else n


def level255_to_pct(level255: int, scale: int = 100) -> int:
    return clamp(int(round((level255 / 255.0) * scale)), 0, scale)


def pct_to_level255(pct: int, scale: int = 100) -> int:
    pct = clamp(pct, 0, scale)
    return clamp(int(round((pct / scale) * 255.0)), 0, 255)


def quantize_fan_level(level255: int) -> int:
    """Map arbitrary level to nearest of (0,10,128,255)."""
    if level255 <= 0:
        return 0
    choices = [10, 128, 255]
    return min(choices, key=lambda x: abs(x - level255))


def parse_first_int(payload_text: str) -> Optional[int]:
    """
    HA (or other clients) sometimes send ramp payloads like "255,34".
    We take the first integer token.
    """
    s = payload_text.strip()
    if not s:
        return None
    first = s.split(",", 1)[0].strip()
    if not first:
        return None
    try:
        return int(first)
    except ValueError:
        return None




class BridgeDiagnostics:
    """Bounded, process-local observations; no claim of physical load feedback."""
    ERROR_COUNTERS = frozenset({
        'rx_checksum_failure_total', 'rx_malformed_total', 'command_timeout_total',
        'command_send_failures_total', 'status_send_failures_total', 'ack_retries_exhausted_tokens_total',
        'ack_corruption_tokens_total', 'ack_clock_loss_tokens_total', 'ack_bang_tokens_total',
        'status_invalid_encoding_groups_total',
    })

    def __init__(self, profiles, clock=None, wall=None):
        import threading
        import time
        from collections import Counter, deque
        self.lock = threading.RLock()
        self.clock = clock or time.monotonic
        self.wall = wall or time.time
        self.started = self.clock()
        self.counters = Counter()
        self.unsupported_samples = {}
        self.recent = deque(maxlen=20)
        self.activity = deque(maxlen=20)
        self.buckets = deque(maxlen=61)
        self.tx_buckets = deque(maxlen=61)
        self.error_buckets = deque(maxlen=301)
        self.last_command_failure = None
        self.last_observed_error_ts = None
        self.last_observed_error_kind = None
        self.sequence = 0
        self.last_stream_sequence = 0
        self.last_rx = None
        self.last_tx = None
        self.last_rx_mono = None
        self.last_tx_mono = None
        self.last_binary_report_ts = None
        self.last_binary_report_mono = None
        self.groups = {f'{p.net}/{p.app}/{p.group}': {
            'net': p.net, 'app': p.app, 'group': p.group,
            'last_bus_ts': None, 'last_bus_mono': None, 'last_publish_ts': None,
            'publication_semantics': 'accepted_locally_not_ha_receipt',
            'published_level': None, 'source': 'unknown', 'confidence': 'unknown',
            'pending_since': None, 'status_quality': 'not_requested',
            'binary_status': 'not_observed', 'last_binary_status_ts': None,
            'last_binary_status_mono': None, 'last_binary_publish_ts': None,
            'level_mode': p.level_mode, 'level_report_ts': None, 'level_report_mono': None,
            'session_level_seen': False,
            'profile_kind': 'writable' if p.command_topic else 'read_only',
        } for p in profiles.values()}

    def command_failure(self, reason, *, app=None, group=None):
        with self.lock:
            self.counters['command_failures_total'] += 1
            self.last_command_failure = {'reason': reason, 'app': app, 'group': group,
                                         'timestamp': int(self.wall())}

    def bump(self, name, count=1):
        with self.lock:
            self.counters[name] += count
            if count and name in {'queue_busy_rejected_total', 'queue_not_ready_rejected_total',
                                  'queue_full_rejected_total', 'queue_expired_discarded_total',
                                  'queue_session_changed_discarded_total', 'queue_disconnect_discarded_total',
                                  'readonly_commands_ignored_total'}:
                self.command_failure(name.removesuffix('_total'))
            if name in self.ERROR_COUNTERS:
                self.last_observed_error_ts = int(self.wall())
                self.last_observed_error_kind = name
                second = int(self.clock())
                if not self.error_buckets or self.error_buckets[-1][0] != second:
                    self.error_buckets.append([second, 0])
                self.error_buckets[-1][1] += count

    def event(self, kind, direction, **details):
        with self.lock:
            self.sequence += 1
            event = {'seq': self.sequence, 'ts': round(self.wall(), 3),
                     'kind': kind, 'direction': direction, **details}
            self.recent.append(event)
            routine = kind in {'binary_status_frame', 'binary_status_report', 'ack', 'echo', 'interface_setup_reply'}
            if kind == 'ack' and details.get('code') != '.':
                routine = False
            if kind == 'binary_status_report' and details.get('error', 0):
                routine = False
            if routine:
                self.counters['traffic_routine_events_total'] += 1
            else:
                self.activity.append(event)

    def rx(self):
        with self.lock:
            self.counters['cni_rx_tokens_total'] += 1
            self.last_rx = int(self.wall())
            self.last_rx_mono = self.clock()
            second = int(self.clock())
            if not self.buckets or self.buckets[-1][0] != second:
                self.buckets.append([second, 0])
            self.buckets[-1][1] += 1

    def tx(self, kind, **details):
        with self.lock:
            self.counters['cni_tx_frames_total'] += 1
            self.last_tx = int(self.wall())
            self.last_tx_mono = self.clock()
            second = int(self.clock())
            if not self.tx_buckets or self.tx_buckets[-1][0] != second:
                self.tx_buckets.append([second, 0])
            self.tx_buckets[-1][1] += 1
        self.event(kind, 'TX', **details)

    def observed(self, app, group):
        with self.lock:
            for state in self.groups.values():
                if state['app'] == app and state['group'] == group:
                    state['last_bus_ts'] = int(self.wall())
                    state['last_bus_mono'] = self.clock()

    def published(self, profile, level, source):
        key = f'{profile.net}/{profile.app}/{profile.group}'
        with self.lock:
            if key not in self.groups:
                return
            state = self.groups[key]
            state.update(last_publish_ts=int(self.wall()), published_level=level, source=source,
                         confidence='reported_level' if source == 'STATUS' else
                         'transmitted' if source == 'ACK' else 'observed_command')

    def transition_transmitted(self, profile, target, seconds, *, stopped=False):
        key = f'{profile.net}/{profile.app}/{profile.group}'
        with self.lock:
            if key in self.groups:
                self.groups[key]['last_transition'] = {
                    'ts': int(self.wall()), 'target_level': target,
                    'full_scale_seconds': seconds,
                    'result': 'stop_transmitted' if stopped else 'target_transmitted',
                }
        self.bump('transition_stop_transmitted_total' if stopped else 'transition_transmitted_total')
        self.event('transition_stop_transmitted' if stopped else 'transition_transmitted', 'LOCAL',
                   app=profile.app, group=profile.group, level=target, full_scale_seconds=seconds)

    def duration_transmitted(self, profile, start, target, requested, rate, nominal):
        with self.lock:
            row = self.groups.get(f'{profile.net}/{profile.app}/{profile.group}')
            if row is not None and 'last_transition' in row:
                row['last_transition'].update(requested_duration_s=requested,
                    start_reported_level=start,nominal_duration_s=round(nominal,3))
        self.bump('duration_transitions_transmitted_total')
        self.event('duration_transition_selected','LOCAL',app=profile.app,group=profile.group,
            requested_seconds=requested,start_level=start,target_level=target,
            full_scale_seconds=rate,nominal_duration_s=round(nominal,3))

    def request(self, app, start):
        with self.lock:
            self.counters['status_requests_total'] += 1
            for state in self.groups.values():
                if state['app'] == app and start <= state['group'] < start + 32:
                    state['pending_since'] = self.clock()
                    state['status_quality'] = 'awaiting_reply'

    def status(self, reply):
        with self.lock:
            self.counters['status_replies_total'] += 1
            for group, level in reply.levels.items():
                offset = group - reply.start_group
                packed = reply.raw_hex[8 + offset * 4:12 + offset * 4].upper()
                quality = 'valid' if level is not None else 'absent' if packed == '0000' else 'invalid_encoding'
                self.bump('status_' + quality + '_groups_total')
                for state in self.groups.values():
                    if state['app'] == reply.app and state['group'] == group:
                        state['status_quality'] = quality
                        # A reply was received, even if it reports no output or a
                        # corrupt encoding. Keep those distinct from no response.
                        state['pending_since'] = None
                        if level is not None:
                            state['level_report_ts'] = int(self.wall())
                            state['level_report_mono'] = self.clock()
                            state['session_level_seen'] = True
                            state['last_bus_ts'] = int(self.wall())
                            state['last_bus_mono'] = self.clock()

    def binary_status(self, reply):
        # Binary evidence cannot complete a pending LEVEL request or refresh the
        # age/confidence of a historical numeric brightness reading.
        with self.lock:
            self.counters['binary_status_reports_total'] += 1
            self.last_binary_report_ts = int(self.wall())
            self.last_binary_report_mono = self.clock()
            for group, value in reply.states.items():
                self.counters['binary_status_' + value + '_groups_total'] += 1
                key = f'254/{reply.app}/{group}'
                if key in self.groups:
                    self.groups[key].update(binary_status=value,
                        last_binary_status_ts=int(self.wall()), last_binary_status_mono=self.clock())
            self.event('binary_status_report', 'RX', app=reply.app,
                       start_group=reply.start_group, groups=len(reply.states),
                       on=sum(s == 'on' for s in reply.states.values()),
                       off=sum(s == 'off' for s in reply.states.values()),
                       absent=sum(s == 'absent' for s in reply.states.values()),
                       error=sum(s == 'error' for s in reply.states.values()))

    def binary_published(self, profile):
        with self.lock:
            state = self.groups.get(f'{profile.net}/{profile.app}/{profile.group}')
            if state is not None:
                state['last_binary_publish_ts'] = int(self.wall())
            self.counters['binary_state_publishes_total'] += 1

    def reset_connection(self):
        # Pending synchronization belongs to the old TCP session. Historical group
        # readings stay visible with timestamps; their age is not a fault assertion.
        with self.lock:
            for state in self.groups.values():
                state['pending_since'] = None
                state['status_quality'] = 'not_requested'
                state['session_level_seen'] = False

    def snapshot(self):
        with self.lock:
            now = self.clock()
            pending = [s for s in self.groups.values() if s['pending_since'] is not None]
            counts = dict(self.counters)
            elapsed = max(1., min(60., now - self.started))
            return {'schema': 1, 'scope': 'observed_by_bridge', 'counters': counts,
                    'last_command_failure': dict(self.last_command_failure) if self.last_command_failure else None,
                    'errors_observed_total': sum(counts.get(n, 0) for n in self.ERROR_COUNTERS),
                    'errors_observed_5min': sum(n for sec, n in self.error_buckets if sec > int(now) - 300),
                    'last_observed_error_ts': self.last_observed_error_ts,
                    'last_observed_error_kind': self.last_observed_error_kind,
                    'mean_command_ack_ms': round(counts.get('command_ack_latency_ms_sum', 0) / counts['command_acks_completed_total'], 1) if counts.get('command_acks_completed_total') else None,
                    'last_cni_rx_ts': self.last_rx, 'last_cni_tx_ts': self.last_tx,
                    'last_cni_rx_age_s': None if self.last_rx_mono is None else round(now - self.last_rx_mono, 1),
                    'last_cni_tx_age_s': None if self.last_tx_mono is None else round(now - self.last_tx_mono, 1),
                    'rx_tokens_per_second_60s': round(sum(n for sec, n in self.buckets if sec > int(now) - 60) / elapsed, 3),
                    'tx_frames_per_second_60s': round(sum(n for sec, n in self.tx_buckets if sec > int(now) - 60) / elapsed, 3),
                    'last_binary_report_ts': self.last_binary_report_ts,
                    'last_binary_report_age_s': None if self.last_binary_report_mono is None else round(now - self.last_binary_report_mono, 1),
                    'binary_error_configured_groups': sum(s['binary_status'] == 'error' for s in self.groups.values()),
                    'binary_absent_configured_groups': sum(s['binary_status'] == 'absent' for s in self.groups.values()),
                    'session_level_reported_groups': sum(s['session_level_seen'] for s in self.groups.values()),
                    'session_level_unconfirmed_groups': sum(not s['session_level_seen'] for s in self.groups.values()),
                    'configured_groups': len(self.groups), 'status_pending_groups': len(pending),
                    'status_overdue_groups': sum(now - s['pending_since'] >= 15 for s in pending),
                    'status_absent_configured_groups': sum(s['status_quality'] == 'absent' for s in self.groups.values()),
                    'status_invalid_configured_groups': sum(s['status_quality'] == 'invalid_encoding' for s in self.groups.values()),
                    'unsupported_samples': [dict(s) for s in self.unsupported_samples.values()],
                    'groups_never_observed': sum(s['last_bus_ts'] is None for s in self.groups.values()),
                    'groups_with_reported_level': sum(s['confidence'] == 'reported_level' for s in self.groups.values())}

    def group_snapshot(self):
        with self.lock:
            now = self.clock()
            groups = []
            for state in self.groups.values():
                row = {k: v for k, v in state.items() if k not in ('last_bus_mono', 'pending_since', 'last_binary_status_mono', 'level_report_mono')}
                row['level_report_age_s'] = None if state['level_report_mono'] is None else round(now - state['level_report_mono'], 1)
                row['binary_observation_age_s'] = None if state['last_binary_status_mono'] is None else round(now - state['last_binary_status_mono'], 1)
                row['observation_age_s'] = None if state['last_bus_mono'] is None else round(now - state['last_bus_mono'], 1)
                if 'last_transition' in row:
                    row['last_transition'] = dict(row['last_transition'])
                row['status_pending_age_s'] = None if state['pending_since'] is None else round(now - state['pending_since'], 1)
                groups.append(row)
            return {'ts': int(self.wall()), 'groups': groups}

    def traffic_snapshot(self, connected=True):
        with self.lock:
            unseen = self.sequence - self.last_stream_sequence
            self.last_stream_sequence = self.sequence
            if not connected:
                self.counters['traffic_events_not_streamed_total'] += unseen
                self.recent.clear()
                self.activity.clear()
                return None
            self.counters['traffic_events_sampled_out_total'] += max(0, unseen - 20)
            return {'schema': 1, 'ts': int(self.wall()), 'sequence': self.sequence,
                    'events_since_publish': unseen, 'events': list(self.recent),
                    'activity_events': list(self.activity)}

    def sample_unsupported(self, hexline):
        import hashlib
        if not re.fullmatch(r'[0-9A-Fa-f]+', hexline) or len(hexline) % 2:
            return
        data = bytes.fromhex(hexline)
        key = hashlib.sha256(data).hexdigest()
        with self.lock:
            if key not in self.unsupported_samples:
                if len(self.unsupported_samples) >= 16:
                    self.unsupported_samples.pop(next(iter(self.unsupported_samples)))
                    self.counters['unsupported_sample_evictions_total'] += 1
                self.unsupported_samples[key] = {
                    'id': key[:12], 'hex': hexline[:160].upper(), 'bytes': len(data),
                    'truncated': len(hexline) > 160, 'header': data[0],
                    'application': data[2] if data[0] == 5 and len(data) > 2 else None,
                    'first_ts': int(self.wall()), 'count': 0,
                }
            sample = self.unsupported_samples[key]
            sample['count'] += 1
            sample['last_ts'] = int(self.wall())

    def publish_traffic(self, client, topic, connected):
        import json
        view = self.traffic_snapshot(connected=connected)
        if view is None:
            return
        result = client.publish(topic, json.dumps(view), qos=0, retain=False)
        if result.rc != 0:
            self.bump('traffic_publish_failures_total')


class CommandRejected(Exception):
    """A queued operation is no longer authorized for this connection session."""


class CommandWorker:
    """Bounded priority queue; no replay across link loss or expired deadlines."""
    def __init__(self, diagnostics, capacity=32, ttl=5., clock=None, cancel=None,
                 duration_capacity=8, duration_ttl=60.):
        import threading
        import time
        from collections import deque
        self.condition = threading.Condition(threading.RLock())
        self.items = deque()
        self.capacity, self.ttl = capacity, ttl
        self.duration_capacity, self.duration_ttl = duration_capacity, duration_ttl
        self.clock = clock or time.monotonic
        self.diagnostics = diagnostics
        self.cancel = cancel or (lambda: None)
        self.links = {'mqtt': False, 'cni': False}
        self.generation = 0
        self.stopped = False
        self.active = False
        self.active_duration = False
        self.health_progress = lambda: None
        self.high_water = 0

    def set_link(self, name, up):
        with self.condition:
            self.links[name] = up
            if not up:
                # Link loss closes the CNI session; MQTT recovery alone must not
                # authorize writes on the old or not-yet-initialized socket.
                self.links['cni'] = False
                self.generation += 1
                self.diagnostics.bump('queue_disconnect_discarded_total', len(self.items))
                self.items.clear()
            self.condition.notify_all()
        if not up:
            self.cancel()

    def session_token(self):
        with self.condition:
            return self.generation

    def ready_cni(self, token):
        with self.condition:
            if token != self.generation or self.stopped:
                raise CommandRejected('session_changed')
            self.links['cni'] = True

    def submit(self, operation, *, interactive=False, key=None, duration=False):
        # Never wait behind a socket send in the MQTT callback. Reject rather than
        # block if a short, bounded guarded send is currently in progress.
        if not self.condition.acquire(blocking=False):
            self.diagnostics.bump('queue_busy_rejected_total')
            return False
        try:
            if self.stopped or not all(self.links.values()):
                self.diagnostics.bump('queue_not_ready_rejected_total')
                return False
            pending_durations = sum(item[5] and (key is None or item[4] != key)
                                    for item in self.items)
            if duration and pending_durations + int(self.active_duration) >= self.duration_capacity:
                self._failure('duration_queue_full', key)
                return False
            if key is not None:
                old = len(self.items)
                self.items = type(self.items)(item for item in self.items if item[4] != key)
                self.diagnostics.bump('queue_coalesced_total', old-len(self.items))
            if interactive and len(self.items) >= self.capacity:
                discard = next((i for i,item in enumerate(self.items) if not item[3] and not item[5]), None)
                if discard is not None:
                    del self.items[discard]
                    self.diagnostics.command_failure('queue_background_displaced')
            if len(self.items) >= self.capacity:
                self.diagnostics.bump('queue_full_rejected_total')
                return False
            lifetime = self.duration_ttl if duration else self.ttl
            item = (self.generation, self.clock() + lifetime, operation, interactive, key, duration)
            if interactive:
                index = next((i for i, queued in enumerate(self.items) if not queued[3]), len(self.items))
                self.items.insert(index, item)
            elif duration:
                index = next((i for i, queued in enumerate(self.items) if not queued[3] and not queued[5]), len(self.items))
                self.items.insert(index, item)
            else:
                self.items.append(item)
            self.high_water = max(self.high_water, len(self.items))
            self.diagnostics.bump('queue_accepted_total')
            self.condition.notify()
            return True
        finally:
            self.condition.release()

    def _failure(self, reason, key):
        app = group = None
        try:
            parts = key.split('/')
            app, group = int(parts[-2]), int(parts[-1])
        except (AttributeError, ValueError, IndexError):
            pass
        self.diagnostics.command_failure(reason, app=app, group=group)
        import logging
        logging.getLogger('cbus_bridge').warning('Command rejected: %s app=%s group=%s', reason, app, group)

    def snapshot(self):
        with self.condition:
            return {'depth': len(self.items), 'capacity': self.capacity,
                    'high_water': self.high_water, 'active': self.active,
                    'ready': not self.stopped and all(self.links.values()), 'ttl_s': self.ttl,
                    'duration_pending': sum(item[5] for item in self.items) + int(self.active_duration),
                    'duration_capacity': self.duration_capacity, 'duration_ttl_s': self.duration_ttl}

    def run_one(self):
        with self.condition:
            if not self.items:
                return False
            generation, deadline, operation, interactive, key, duration = self.items.popleft()
            self.active = True
            self.active_duration = duration
        worker = self

        class Lease:
            def yield_to_interactive(self):
                with worker.condition:
                    if key is not None and any(item[4] == key for item in worker.items):
                        raise CommandRejected('superseded')
                    if any(item[3] for item in worker.items):
                        raise CommandRejected('duration_deferred')

            def atomic(self, action, expire=True):
                with worker.condition:
                    if generation != worker.generation or worker.stopped or not all(worker.links.values()):
                        raise CommandRejected('session_changed')
                    if expire and worker.clock() >= deadline:
                        raise CommandRejected('expired')
                    return action()

            def invalidate(self):
                worker.set_link('cni', False)

        lease = Lease()
        try:
            lease.atomic(lambda: None)
            operation(lease)
            self.diagnostics.bump('queue_completed_total')
        except CommandRejected as exc:
            reason = str(exc)
            with self.condition:
                # Yield only before the lighting write. Keep the original deadline;
                # disconnect, newer same-group work or sustained controls cancel it.
                can_resume = (reason == 'duration_deferred' and duration and
                              generation == self.generation and not self.stopped and
                              all(self.links.values()) and self.clock() < deadline and
                              len(self.items) < self.capacity and
                              not any(key is not None and item[4] == key for item in self.items))
                if can_resume:
                    self.active_duration = False
                    index = next((i for i, queued in enumerate(self.items) if not queued[3]), len(self.items))
                    self.items.insert(index, (generation, deadline, operation, interactive, key, duration))
                    self.diagnostics.bump('duration_deferred_total')
                    self.condition.notify()
                else:
                    self.diagnostics.bump('queue_' + reason + '_discarded_total')
                    if reason == 'expired' or (reason == 'duration_deferred' and self.clock() >= deadline):
                        self._failure('command_expired', key)
                    elif reason == 'duration_deferred' and len(self.items) >= self.capacity:
                        self._failure('duration_queue_full', key)
        except Exception:
            import logging
            logging.getLogger("cbus_bridge").warning("Queued operation failed; discarding session work")
            self.diagnostics.command_failure('operation_failed')
            self.diagnostics.bump('queue_operation_failures_total')
            # Do not execute remaining work after an ambiguous transport failure.
            self.set_link('cni', False)
        finally:
            with self.condition:
                self.active = False
                self.active_duration = False
        return True

    def run(self):
        while True:
            self.health_progress()
            with self.condition:
                self.condition.wait_for(lambda: self.stopped or bool(self.items), timeout=1)
                if self.stopped:
                    return
            self.run_one()

    def stop(self):
        with self.condition:
            self.stopped = True
        self.set_link('cni', False)


def command_requires_duration_wait(command):
    """Zero-time UI controls need neither fresh-level acquisition nor fade queueing."""
    if not isinstance(command, dict):
        return False
    fields = [name for name in ('transition_seconds', 'ramp_seconds') if name in command]
    if not fields:
        return False
    return not (len(fields) == 1 and type(command[fields[0]]) in (int, float)
                and command[fields[0]] == 0)


def parse_transition_payload(text, scale):
    """Strict opt-in JSON; legacy numeric/comma payloads are handled separately."""
    import json
    from .cni_protocol import RAMP_CODES

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate transition field')
            result[key] = value
        return result

    try:
        obj = json.loads(text, object_pairs_hook=unique_object)
    except (ValueError, RecursionError) as exc:
        raise ValueError('Invalid transition JSON') from exc
    if not isinstance(obj, dict):
        raise ValueError('Transition must be an object')
    if set(obj) == {'stop'} and obj['stop'] is True:
        return 'stop', None, None
    if set(obj) in ({'level', 'transition_seconds'}, {'restore', 'transition_seconds'}):
        import math
        seconds = obj['transition_seconds']
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 3600:
            raise ValueError('Duration must be finite and within 0..3600 seconds')
        if 'restore' in obj:
            if obj['restore'] is not True:
                raise ValueError('restore must be true')
            return 'duration_restore', None, seconds
        level = obj['level']
        if type(level) is not int or not 0 <= level <= scale:
            raise ValueError('Transition level outside profile brightness scale')
        return 'duration', level, seconds
    if set(obj) != {'level', 'ramp_seconds'}:
        raise ValueError('Expected level and ramp_seconds, or stop=true')
    level, seconds = obj['level'], obj['ramp_seconds']
    if type(level) is not int or not 0 <= level <= scale:
        raise ValueError('Transition level outside profile brightness scale')
    if type(seconds) is not int or seconds not in RAMP_CODES:
        raise ValueError('Unsupported full-scale ramp rate')
    return 'ramp', level, seconds


def nearest_duration_rate(start, target, seconds):
    """Minimize nominal partial-travel duration error; ties prefer faster rate."""
    from .cni_protocol import RAMP_SECONDS
    fraction = abs(target-start)/255
    if not fraction or not seconds:
        return 0, 0.
    rate = min(RAMP_SECONDS, key=lambda rate: (abs(rate*fraction-seconds), rate))
    return rate, rate*fraction


class RampTracker:
    """Session-local watches bounded by configured dimmers and a bus budget."""
    def __init__(self, diagnostics, keys, interval=0, clock=None):
        import threading
        import time
        self.lock = threading.RLock()
        self.diagnostics, self.keys = diagnostics, set(keys)
        self.interval, self.clock = interval, clock or time.monotonic
        self.entries = {}
        self.numeric_authority = set()
        self.epoch = self.version = 0
        self.pending = None
        self.next_bus = 0.
        self.last_sent = float("-inf")

    @property
    def enabled(self):
        return self.interval > 0

    def _finish(self, key, result):
        entry = self.entries.pop(key)
        self.diagnostics.bump('ramp_tracking_' + result + '_total')
        self.diagnostics.event('ramp_readback_' + result, 'LOCAL', app=key[0], group=key[1],
                               level=entry['last_level'])

    def reset(self):
        with self.lock:
            self.diagnostics.bump('ramp_tracking_disconnect_cancellations_total', len(self.entries))
            self.epoch += 1
            self.entries.clear()
            self.numeric_authority.clear()
            self.pending = None
            self.next_bus = self.clock()
            self.last_sent = float("-inf")

    def begin(self, app, group, target, seconds, *, observed=False):
        key = (app, group)
        with self.lock:
            if not self.enabled or key not in self.keys:
                return None
            now, old = self.clock(), self.entries.get(key)
            # Do not restart a locally initiated command when its SAL is echoed.
            if observed and old and old['target'] == target and old['seconds'] == seconds and now-old['started'] < 2:
                old['observed'] = old['ready'] = True
                return None
            self.version += 1
            self.entries[key] = {
                'version': self.version, 'target': target, 'seconds': seconds,
                'started': now, 'deadline': now+max(90,(seconds or 0)+30),
                'due': now+1, 'ready': observed, 'observed': observed, 'waiting': None, 'misses': 0,
                'last_level': None, 'confirmations': 0, 'phase': 'stop' if target is None else 'ramp',
            }
            self.diagnostics.bump('ramp_tracking_started_total')
            return self.epoch, key, self.version, old

    def ack(self, token, success):
        if token is None:
            return
        epoch, key, version, old = token
        with self.lock:
            entry = self.entries.get(key)
            if epoch != self.epoch or not entry or entry['version'] != version:
                return
            if success or entry['observed']:
                entry['ready'] = True
            elif old is not None:
                self.entries[key] = old
            else:
                self.entries.pop(key)

    def suppress_binary(self, app, group):
        with self.lock:
            return (app,group) in self.entries or (app,group) in self.numeric_authority

    def remember_level(self, app, group, level):
        with self.lock:
            entry = self.entries.get((app,group))
            # Intermediate fading samples must not replace the remembered ON
            # brightness. A reached positive target or a confirmed stop can.
            return entry is None or (entry['target'] is not None and level == entry['target'] and level > 0)

    def observe(self, app, group, level):
        with self.lock:
            now = self.clock()
            key = (app, group)
            if not self.enabled or key not in self.keys:
                return
            self.numeric_authority.add(key)
            entry = self.entries.get(key)
            if not entry or entry['waiting'] is None or now-entry['waiting'] > 3:
                return
            # Only requests sent for this command generation advance completion.
            entry['waiting'] = None
            entry['misses'] = 0
            self.diagnostics.bump('ramp_readback_samples_total')
            last, entry['last_level'] = entry['last_level'], level
            matched = last is not None and level == last if entry['target'] is None else level == entry['target']
            entry['confirmations'] = entry['confirmations']+1 if matched else (1 if entry['target'] is None else 0)
            if entry['confirmations'] >= 2:
                self._finish(key, 'completed')
            elif entry['target'] is None or entry['confirmations']:
                entry['due'] = now+1
            else:
                remaining = entry['seconds'] * abs(entry['target']-level) / 255
                entry['due'] = now+min(self.interval,max(1,remaining+1))

    def reserve(self):
        with self.lock:
            now = self.clock()
            for key, entry in list(self.entries.items()):
                if now >= entry['deadline']:
                    self._finish(key, 'expired')
                    continue
                if entry['waiting'] is not None and now-entry['waiting'] >= 3:
                    entry['waiting'] = None
                    entry['misses'] += 1
                    self.diagnostics.bump('ramp_readback_reply_timeouts_total')
                    if entry['misses'] >= 3:
                        # No endless retry loop; retain protection until deadline.
                        entry['phase'] = 'readback_failed'
                        self.diagnostics.bump('ramp_readback_failed_total')
            if self.pending is not None:
                if now < self.pending['expires']:
                    return None
                self.pending = None
                self.diagnostics.bump('ramp_readback_queue_expired_total')
            if now < self.next_bus:
                return None
            due = [(key,e) for key,e in self.entries.items() if e['ready']
                   and e['phase'] != 'readback_failed' and e['waiting'] is None and now >= e['due']]
            if not due:
                return None
            key, _ = min(due,key=lambda item:item[1]['due'])
            block = key[0],key[1]//32*32
            versions = {k:e['version'] for k,e in self.entries.items()
                        if (k[0],k[1]//32*32) == block and e['ready'] and e['phase'] != 'readback_failed'}
            self.pending = {'epoch':self.epoch,'block':block,'versions':versions,'expires':now+6}
            self.next_bus = now+3  # global ceiling: one block per three seconds
            return self.pending

    def release(self, token):
        with self.lock:
            if self.pending is token:
                self.pending = None

    def send(self, token, operation):
        with self.lock:
            now = self.clock()
            if self.pending is not token or token['epoch'] != self.epoch or now >= token['expires']:
                return False
            self.pending = None
            if now < self.last_sent+3:
                self.next_bus = max(self.next_bus,self.last_sent+3)
                return False
            matching = [self.entries[k] for k,v in token['versions'].items()
                        if k in self.entries and self.entries[k]['version'] == v
                        and self.entries[k]['phase'] != 'readback_failed' and now < self.entries[k]['deadline']]
            if not matching:
                return False
            for entry in matching:
                entry['waiting'],entry['due'] = now,now+self.interval
            self.last_sent = now
            operation(*token['block'])
            self.diagnostics.bump('ramp_readback_requests_total')
            return True

    def inventory_probe_ready(self):
        with self.lock:
            return not self.entries and self.clock() >= self.last_sent+3

    def send_inventory_probe(self, operation):
        with self.lock:
            if not self.inventory_probe_ready():
                return False
            result = operation()
            if result:
                self.pending = None
                self.last_sent = self.clock()
                self.next_bus = self.last_sent+3
            return result

    def send_duration_probe(self, app, start, operation):
        # The command worker serializes probes with lighting commands. Supersede
        # an unsent background reservation rather than wait behind our own queue.
        with self.lock:
            now = self.clock()
            if now < self.last_sent+3:
                return False
            self.pending = None
            self.next_bus = now+3
            self.last_sent = now
            operation(app,start)
            self.diagnostics.bump('duration_level_requests_total')
            return True

    def snapshot(self):
        with self.lock:
            now = self.clock()
            return {'enabled':self.enabled,'interval_s':self.interval,'global_min_interval_s':3,
                    'active_groups':len(self.entries),'numeric_authority_groups':len(self.numeric_authority),
                    'groups':[{'app':key[0],'group':key[1],'target_level':e['target'],
                               'full_scale_seconds':e['seconds'],'phase':e['phase'],
                               'age_s':round(now-e['started'],1),'last_reported_level':e['last_level'],
                               'missing_replies':e['misses']} for key,e in self.entries.items()]}


# Process reliability and bounded MQTT state delivery.
import fcntl
import os
import stat
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace


@contextmanager
def instance_lock(path='/tmp/cbus-bridge.lock'):
    # Never unlink: a second inode would allow a second simultaneous lock.
    flags = os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | flags, 0o644)
        os.fchmod(fd, 0o644)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError('Bridge lock must be a regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another local C-Bus bridge is already running') from None
        yield
    finally:
        os.close(fd)


class LatestPublisher:
    """One latest desired value per retained QoS1 topic; one in-flight per topic.

    rc=0 means accepted locally, not broker/HA receipt. Acknowledged versions are
    kept for reconnect replay. Paho's separate queue must also be bounded.
    """
    def __init__(self, client, diagnostics, capacity=1024, inflight=16, clock=None):
        self.client, self.diagnostics = client, diagnostics
        self.capacity, self.limit = capacity, inflight
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        from collections import OrderedDict
        self.latest = OrderedDict()
        self.pending = {}
        self.confirmed = {}
        self.version = 0
        self.connected = False
        self.failures = 0
        self.first_rejection = None

    def set_connected(self, up):
        with self.lock:
            self.connected = up
            if up:
                # Paho owns pending packets across reconnect. Do not duplicate
                # them; send the latest desired version once those are ACKed.
                self.confirmed.clear()

    def publish(self, topic, payload=None, qos=0, retain=False, **kwargs):
        if qos != 1 or not retain:
            result = self.client.publish(topic, payload=payload, qos=qos, retain=retain, **kwargs)
            if result.rc != 0:
                self.diagnostics.bump('mqtt_publication_rejected_total')
            return result
        with self.lock:
            if topic not in self.latest and len(self.latest) >= self.capacity:
                self.diagnostics.bump('mqtt_publication_rejected_total')
                return SimpleNamespace(rc=15)
            self.version += 1
            self.latest[topic] = (self.version, payload)
        return SimpleNamespace(rc=0)

    def pump(self):
        with self.lock:
            for topic, (version, info, started) in list(self.pending.items()):
                if info.is_published() is True:
                    self.first_rejection = None
                    self.confirmed[topic] = version
                    del self.pending[topic]
            if not self.connected:
                return
            for topic, (version, payload) in list(self.latest.items()):
                if len(self.pending) >= self.limit:
                    break
                if topic in self.pending or self.confirmed.get(topic) == version:
                    continue
                info = self.client.publish(topic, payload=payload, qos=1, retain=True)
                if info.rc == 0:
                    self.pending[topic] = (version, info, self.clock())
                    self.latest.move_to_end(topic)
                else:
                    self.failures += 1
                    if self.first_rejection is None:
                        self.first_rejection = self.clock()
                    self.diagnostics.bump('mqtt_publication_rejected_total')
                    # Avoid a hot loop across every topic on a broken transport.
                    break

    def snapshot(self):
        # Dictionary lengths/progress are copied under a nonblocking lock so a
        # watchdog cannot itself be trapped behind a broken publish call.
        if not self.lock.acquire(blocking=False):
            return None
        try:
            return {'topics': len(self.latest), 'capacity': self.capacity,
                    'pending': len(self.pending), 'pending_limit': self.limit,
                    'oldest_pending_s': max((self.clock()-x[2] for x in self.pending.values()), default=0),
                    'transport_rejection_age_s': 0 if self.first_rejection is None else self.clock()-self.first_rejection,
                    'delivery_semantics': 'local_queue_then_broker_ack_not_physical_feedback'}
        finally:
            self.lock.release()


class FunctionalHealth:
    def __init__(self, clock=None, timeout=60):
        self.clock = clock or time.monotonic
        self.timeout = timeout
        self.progress = {}

    def beat(self, name):
        self.progress[name] = self.clock()

    def stalled(self):
        now = self.clock()
        return [name for name, when in self.progress.copy().items() if now-when > self.timeout]

    def snapshot(self):
        now = self.clock()
        return {'progress_timeout_s': self.timeout,
                'progress_age_s': {name: round(max(0,now-when),3) for name,when in self.progress.copy().items()},
                'stalled': self.stalled()}


class DeviceInventory:
    """An explicit, bounded local IDENTIFY survey. No sockets, timers or retries.

    Metrics schedules individual sends on the existing command worker; the CNI
    reader supplies replies independently. No wait for a reply holds that worker.
    """
    FIELDS = {0: 'manufacturer', 1: 'type', 2: 'firmware'}

    def __init__(self, diagnostics, clock=None, wall=None):
        import threading
        import time
        from collections import deque
        self.lock = threading.RLock()
        self.diagnostics = diagnostics
        self.clock, self.wall = clock or time.monotonic, wall or time.time
        self.tasks = deque()
        self.rows, self.observed = {}, set()
        self.interface_unit = None
        self.bridge_start_ts = int(self.wall())
        self.pending = self.reserved = None
        self.job = {'id': 0, 'state': 'idle', 'requested_units': []}
        self.last_request_error = None
        self.last_start = float('-inf')
        self.serial = self.version = 0

    def _change(self):
        self.version += 1

    def observe_unit(self, unit):
        if type(unit) is int and 0 <= unit <= 254:
            with self.lock:
                if unit not in self.observed:
                    self.observed.add(unit); self._change()

    def set_interface_unit(self, unit):
        if type(unit) is int and 0 <= unit <= 254:
            with self.lock:
                if self.interface_unit != unit:
                    self.interface_unit = unit; self._change()

    def request(self, spec, ready, readonly=False):
        with self.lock:
            if spec == 'cancel':
                self.cancel('cancelled'); return True
            if spec == 'interface':
                if self.interface_unit is None:
                    self.last_request_error = 'interface_address_unknown'
                    self.diagnostics.bump('inventory_requests_rejected_total'); self._change()
                    return False
                spec = {'units': [self.interface_unit]}
            if spec == 'observed':
                spec = {'units': sorted(self.observed)[:16]}
            error = None
            units = spec.get('units') if isinstance(spec, dict) and set(spec) == {'units'} else None
            if (not isinstance(units, list) or not 1 <= len(units) <= 16 or
                any(type(u) is not int or not 0 <= u <= 254 for u in units)):
                error = 'expected_1_to_16_local_unit_addresses_0_to_254'
            elif readonly: error = 'readonly'
            elif not ready: error = 'command_path_not_ready'
            elif self.interface_unit is None: error = 'interface_address_unknown'
            elif self.job['state'] == 'running': error = 'inventory_already_running'
            elif self.clock() < self.last_start + 30: error = 'inventory_rate_limited'
            if error:
                self.last_request_error = error
                self.diagnostics.bump('inventory_requests_rejected_total'); self._change()
                return False
            units = sorted(set(units))
            self.last_start = self.clock()
            self.job = {'id': self.job['id']+1, 'state': 'running',
                        'requested_units': units, 'started_ts': int(self.wall()),
                        'deadline': self.clock()+600}
            self.tasks.clear(); self.pending = self.reserved = None
            for unit in units:
                self.rows[unit] = {'unit':unit, 'outcome':'pending', 'scan_id':self.job['id'],
                                   'attempt_ts':int(self.wall()), 'attributes':{}}
                self.tasks.append((unit,1))
            self.last_request_error = None
            self.diagnostics.bump('inventory_surveys_started_total'); self._change()
            return True

    def cancel(self, reason='connection_lost'):
        with self.lock:
            if self.job['state'] == 'running':
                self.job['state'] = reason
                self.last_start = self.clock()
                self.job['finished_ts'] = int(self.wall())
                for unit in self.job['requested_units']:
                    if self.rows[unit]['outcome'] == 'pending':
                        self.rows[unit]['outcome'] = reason
                self.diagnostics.bump('inventory_surveys_cancelled_total'); self._change()
            self.tasks.clear(); self.pending = self.reserved = None
            if reason == 'connection_lost':
                self.interface_unit = None; self._change()

    def _result(self, token, value=None):
        unit, attr = token['unit'], token['attribute']
        row = self.rows[unit]; field = self.FIELDS[attr]
        row['attributes'][field] = {'outcome':'reply' if value is not None else 'no_reply',
                                     'observed_ts':int(self.wall())}
        if value is not None:
            row[field] = value
        if attr == 1 and value is not None:
            # Probe identity first; only responsive units receive the other reads.
            self.tasks.appendleft((unit,2)); self.tasks.appendleft((unit,0))
        elif attr == 1:
            row['outcome'] = 'no_reply'  # Not a diagnosis of absence/offline/fault.
        if attr == 2:
            row['outcome'] = 'identified' if all(
                row['attributes'].get(f,{}).get('outcome') == 'reply' for f in self.FIELDS.values()) else 'partial'
        self._change()

    def reserve(self):
        with self.lock:
            now = self.clock()
            if self.job['state'] != 'running': return None
            if now >= self.job['deadline']:
                self.cancel('survey_deadline'); return None
            if self.pending and now >= self.pending['sent_at']+3:
                self._result(self.pending)
                self.diagnostics.bump('inventory_query_timeouts_total')
                self.pending = None
            if self.reserved and now >= self.reserved['queued_at']+6:
                self.reserved = None
            if self.pending or self.reserved: return None
            if not self.tasks:
                self.job['state'] = 'complete'; self.job['finished_ts'] = int(self.wall())
                self.last_start = self.clock()
                self._change(); return None
            unit, attr = self.tasks[0]
            self.serial += 1
            self.reserved = {'token':self.serial, 'job':self.job['id'], 'unit':unit,
                             'attribute':attr, 'destination':self.interface_unit, 'queued_at':now}
            return dict(self.reserved)

    def release(self, token):
        with self.lock:
            if self.reserved and self.reserved['token'] == token['token']:
                self.reserved = None

    def transmit(self, token, send):
        from .cni_protocol import build_identity_request
        with self.lock:
            if (not self.reserved or self.reserved['token'] != token['token'] or
                self.job['state'] != 'running' or token['job'] != self.job['id'] or
                self.clock() >= self.job['deadline'] or self.clock() >= token['queued_at']+5):
                return False
            self.tasks.popleft(); self.reserved = None
            self.pending = {**token, 'sent_at':self.clock()}
            try:
                send(build_identity_request(token['unit'],token['attribute']))
            except Exception:
                self.cancel('send_failed')
                self.diagnostics.bump('inventory_send_failures_total')
                raise
            self.diagnostics.tx('inventory_identify_request', unit=token['unit'], attribute=token['attribute'])
            self.diagnostics.bump('inventory_queries_sent_total'); self._change()
            return True

    def receive(self, reply):
        with self.lock:
            p = self.pending
            if (not p or self.job['state'] != 'running' or
                self.clock() >= p['sent_at']+3 or self.clock() >= self.job['deadline'] or
                (reply.unit,reply.destination,reply.attribute) != (p['unit'],p['destination'],p['attribute'])):
                self.diagnostics.bump('inventory_unmatched_replies_total'); return False
            self.pending = None
            self._result(p,reply.value)
            self.diagnostics.bump('inventory_replies_matched_total')
            return True

    def revision(self):
        with self.lock:
            return self.version

    def snapshot(self):
        import copy
        with self.lock:
            job = {k:v for k,v in self.job.items() if k != 'deadline'}
            return {'schema':1, 'scope':'local_explicit_identity_survey', 'version':self.version,
                    'bridge_start_ts':self.bridge_start_ts,
                    'interface_unit':self.interface_unit, 'observed_units':sorted(self.observed),
                    'job':copy.deepcopy(job), 'last_request_error':self.last_request_error,
                    'pending':{k:self.pending[k] for k in ('unit','attribute')} if self.pending else None,
                    'devices':copy.deepcopy([self.rows[u] for u in sorted(self.rows)])}
