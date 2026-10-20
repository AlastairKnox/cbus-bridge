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
        self.recent = deque(maxlen=20)
        self.buckets = deque(maxlen=61)
        self.tx_buckets = deque(maxlen=61)
        self.error_buckets = deque(maxlen=301)
        self.last_observed_error_ts = None
        self.last_observed_error_kind = None
        self.sequence = 0
        self.last_stream_sequence = 0
        self.last_rx = None
        self.last_tx = None
        self.last_rx_mono = None
        self.last_tx_mono = None
        self.groups = {f'{p.net}/{p.app}/{p.group}': {
            'net': p.net, 'app': p.app, 'group': p.group,
            'last_bus_ts': None, 'last_bus_mono': None, 'last_publish_ts': None,
            'published_level': None, 'source': 'unknown', 'confidence': 'unknown',
            'pending_since': None, 'status_quality': 'not_requested',
        } for p in profiles.values()}

    def bump(self, name, count=1):
        with self.lock:
            self.counters[name] += count
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

    def request(self, app, start):
        with self.lock:
            self.counters['status_requests_total'] += 1
            for state in self.groups.values():
                if state['app'] == app and start <= state['group'] < start + 32:
                    state['pending_since'] = self.clock()
                    state['status_quality'] = 'awaiting_valid_level'

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
                        if level is not None:
                            state['pending_since'] = None
                            state['last_bus_ts'] = int(self.wall())
                            state['last_bus_mono'] = self.clock()

    def reset_connection(self):
        # Pending synchronization belongs to the old TCP session. Historical group
        # readings stay visible with timestamps; their age is not a fault assertion.
        with self.lock:
            for state in self.groups.values():
                state['pending_since'] = None
                state['status_quality'] = 'not_requested'

    def snapshot(self):
        with self.lock:
            now = self.clock()
            pending = [s for s in self.groups.values() if s['pending_since'] is not None]
            counts = dict(self.counters)
            elapsed = max(1., min(60., now - self.started))
            return {'schema': 1, 'scope': 'observed_by_bridge', 'counters': counts,
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
                    'configured_groups': len(self.groups), 'status_pending_groups': len(pending),
                    'status_overdue_groups': sum(now - s['pending_since'] >= 15 for s in pending),
                    'groups_never_observed': sum(s['last_bus_ts'] is None for s in self.groups.values()),
                    'groups_with_reported_level': sum(s['confidence'] == 'reported_level' for s in self.groups.values())}

    def group_snapshot(self):
        with self.lock:
            now = self.clock()
            groups = []
            for state in self.groups.values():
                row = {k: v for k, v in state.items() if k not in ('last_bus_mono', 'pending_since')}
                row['observation_age_s'] = None if state['last_bus_mono'] is None else round(now - state['last_bus_mono'], 1)
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
                return None
            self.counters['traffic_events_sampled_out_total'] += max(0, unseen - 20)
            return {'schema': 1, 'ts': int(self.wall()), 'sequence': self.sequence,
                    'events_since_publish': unseen, 'events': list(self.recent)}

    def publish_traffic(self, client, topic, connected):
        import json
        view = self.traffic_snapshot(connected=connected)
        if view is None:
            return
        result = client.publish(topic, json.dumps(view), qos=0, retain=False)
        if result.rc != 0:
            self.bump('traffic_publish_failures_total')
