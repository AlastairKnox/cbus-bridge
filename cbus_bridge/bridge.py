from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Set, List

import paho.mqtt.client as mqtt

from .cni_connection import CniConnection
from .cni_protocol import parse_monitored_sals, parse_status_reply, diagnose_frame
from .ha_profiles import GroupProfile
from .utils import (
    BridgeDiagnostics,
    clamp,
    checksum_2s_complement,
    level255_to_pct,
    normalize_hex_from_bytes,
    parse_first_int,
    pct_to_level255,
    quantize_fan_level,
)

LOG = logging.getLogger("cbus_bridge.bridge")

# ACK tokens are 2 bytes: <tag><code>
# tag is g..z, code is one of: . # $ %
ACK_LINE_RE = re.compile(r"^[g-z][.\#$%]$")
BANG_LINE_RE = re.compile(r"^!$")


@dataclass
class AckWaiter:
    event: threading.Event
    result: Optional[str] = None  # '.', '#', '$', '%', or '!'


class CbusMqttBridge:
    """
    C-Bus CNI <-> MQTT bridge.

    STRICT ACK POLICY (mode A):
      - We only publish an "optimistic" state/level update to MQTT if we observe a tagged ACK success
        for the write we sent (tag '.').
      - If there is no ACK or a failure ACK (#/$/%/!), we do not publish a commanded-state update.
        Home Assistant will converge via actual C-Bus monitored/status traffic instead.

    Notes:
      - C-Bus traffic is still published based on monitored SAL/status messages.
      - Resync remains available via request_resync().
    """

    def __init__(
        self,
        cni: CniConnection,
        mqtt_client: mqtt.Client,
        profiles: Dict[Tuple[int, int, int], GroupProfile],
        readonly: bool,
        dimmer_ramp_cmd: int = 0x0A,  # ramp code
        ack_timeout_s: float = 2.5,
        diagnostics=None,
    ):
        self.diagnostics = diagnostics if diagnostics is not None else BridgeDiagnostics(profiles)
        self.cni = cni
        self.mqtt = mqtt_client
        self.profiles = profiles
        self.readonly = readonly
        self.dimmer_ramp_cmd = dimmer_ramp_cmd
        self.ack_timeout_s = float(ack_timeout_s)

        # Index profiles by (app, group) to:
        #  - publish efficiently
        #  - provide human-readable names in logs
        self._profiles_by_app_group: Dict[Tuple[int, int], List[GroupProfile]] = {}
        for (_net, app, group), p in self.profiles.items():
            self._profiles_by_app_group.setdefault((app, group), []).append(p)

        # Track last inbound level per (app, group) so INFO logs show only changes.
        self._last_rx_level: Dict[Tuple[int, int], int] = {}

        # Some C-Bus dimmer switches emit plain ON/OFF SAL while dimming, followed by
        # the actual level. Delay those synthetic 0/255 updates so the real level wins.
        self._pending_synthetic_lock = threading.Lock()
        self._pending_synthetic_timers: Dict[Tuple[int, int], threading.Timer] = {}

        # Tag generator (g..z)
        self._tag_lock = threading.Lock()
        self._tag_index = 0

        # Waiters keyed by tag
        self._ack_lock = threading.Lock()
        self._ack_waiters: Dict[str, AckWaiter] = {}

        # Ensure only one write is in flight at a time (simplifies ACK attribution)
        self._write_lock = threading.Lock()

    # ----------------------------
    # Profile helpers (including friendly names for logging)
    # ----------------------------
    def _get_profile(self, net: int, app: int, group: int) -> Optional[GroupProfile]:
        return self.profiles.get((net, app, group))

    def _topic_for(self, p: GroupProfile, kind: str) -> str:
        if kind == "state":
            return p.state_topic
        if kind == "level" and p.level_state_topic:
            return p.level_state_topic
        return f"cbus/read/{p.net}/{p.app}/{p.group}/{kind}"

    def _profile_display_name(self, p: GroupProfile) -> str:
        """
        Best-effort friendly name for logs based on what your YAML ingestion provides.
        Prefer YAML `name`, then `unique_id`, then other common fields, then topic fallback.
        """
        for attr in ("name", "unique_id", "friendly_name", "entity_name", "entity_id", "ha_entity_id"):
            v = getattr(p, attr, None)
            if isinstance(v, str) and v.strip():
                return v.strip()

        # Fall back to something stable and useful
        st = getattr(p, "state_topic", None)
        if isinstance(st, str) and st.strip():
            return st.strip()

        return f"net={getattr(p, 'net', '?')} app={getattr(p, 'app', '?')} group={getattr(p, 'group', '?')}"

    def _labels_for(self, app: int, group: int) -> str:
        ps = self._profiles_by_app_group.get((int(app), int(group)), [])
        if not ps:
            return ""
        names = [self._profile_display_name(p) for p in ps]
        # De-dup while preserving order
        seen = set()
        out = []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return ", ".join(out)

    def _label_suffix_for(self, app: int, group: int) -> str:
        """
        Returns a stable suffix like: ' name="Upstairs Dimmer"' or '' if unknown.
        Uses the ingested HA profiles.
        """
        labels = self._labels_for(app, group)
        return f' name="{labels}"' if labels else ""

    def _label_suffix_for_profile(self, p: GroupProfile) -> str:
        n = self._profile_display_name(p)
        return f' name="{n}"' if n else ""

    def _has_pct_profile(self, app: int, group: int) -> bool:
        return any(
            p.level_mode == "pct"
            for p in self._profiles_by_app_group.get((int(app), int(group)), [])
        )

    def _cancel_pending_synthetic_level(self, app: int, group: int) -> None:
        key = (int(app), int(group))
        with self._pending_synthetic_lock:
            timer = self._pending_synthetic_timers.pop(key, None)
        if timer:
            timer.cancel()

    def _delay_synthetic_level_update(
        self,
        app: int,
        group: int,
        level255: int,
        *,
        source: str,
        delay_s: float = 0.75,
    ) -> None:
        key = (int(app), int(group))

        def publish_if_still_pending() -> None:
            with self._pending_synthetic_lock:
                if self._pending_synthetic_timers.pop(key, None) is not timer:
                    return
            self.handle_cbus_level_update(app=app, group=group, level255=level255, source=source)

        timer = threading.Timer(delay_s, publish_if_still_pending)
        timer.daemon = True

        with self._pending_synthetic_lock:
            old = self._pending_synthetic_timers.pop(key, None)
            self._pending_synthetic_timers[key] = timer
        if old:
            old.cancel()
        timer.start()

    # ----------------------------
    # Publishing helpers
    # ----------------------------
    def publish_state(self, p: GroupProfile, is_on: bool) -> None:
        payload = "ON" if is_on else "OFF"
        self.mqtt.publish(self._topic_for(p, "state"), payload=payload, retain=True, qos=1)

    def publish_level(self, p: GroupProfile, level255: int, *, source: str = "ACK") -> None:
        """
        Publish readbacks matching HA expectations.

        pct mode: publish brightness topic (0..pct_scale) and state ON/OFF.
        fan mode: publish preset numeric level and state ON/OFF.
        none: publish state ON/OFF only.
        """
        level255 = clamp(int(level255), 0, 255)
        self.diagnostics.published(p, level255, source)

        if p.level_mode == "pct":
            pct = level255_to_pct(level255, p.pct_scale)
            if pct > 0:
                p.last_nonzero_255 = level255
            if p.level_state_topic:
                self.mqtt.publish(self._topic_for(p, "level"), payload=str(pct), retain=True, qos=1)
            if self._topic_for(p, "state") != self._topic_for(p, "level"):
                self.publish_state(p, pct > 0)
            return

        if p.level_mode == "fan":
            q = quantize_fan_level(level255)
            if q > 0:
                p.last_nonzero_255 = q
            out = p.fan_off_value_for_ha if (q == 0 and p.fan_off_value_for_ha == 5) else q
            if p.level_state_topic:
                self.mqtt.publish(self._topic_for(p, "level"), payload=str(out), retain=True, qos=1)
            if self._topic_for(p, "state") != self._topic_for(p, "level"):
                self.publish_state(p, q > 0)
            return

        self.publish_state(p, level255 > 0)

    def handle_cbus_level_update(self, app: int, group: int, level255: int, *, source: str = "") -> None:
        """
        Publish updates for configured groups and emit useful logs.

        Logging policy:
          - INFO: changes for configured groups
          - DEBUG: unchanged updates, and updates for unconfigured groups (to avoid log spam)
        """
        app_int = int(app)
        group_int = int(group)
        level_int = clamp(int(level255), 0, 255)

        ps = self._profiles_by_app_group.get((app_int, group_int), [])
        suffix = self._label_suffix_for(app_int, group_int)

        key = (app_int, group_int)
        prev = self._last_rx_level.get(key)
        changed = (prev is None) or (prev != level_int)
        self._last_rx_level[key] = level_int

        src = source or "CBUS"

        if ps:
            if changed:
                LOG.info("RX %s app=0x%02X group=%d level=%d%s", src, app_int, group_int, level_int, suffix)
            else:
                LOG.debug("RX %s app=0x%02X group=%d level=%d (unchanged)%s", src, app_int, group_int, level_int, suffix)

            for p in ps:
                self.publish_level(p, level_int, source=src)
        else:
            # Unconfigured: keep noise low
            if changed:
                LOG.debug("RX %s app=0x%02X group=%d level=%d (unconfigured)", src, app_int, group_int, level_int)

    # ----------------------------
    # ACK wait infrastructure
    # ----------------------------
    def _next_tag(self) -> str:
        with self._tag_lock:
            tag = chr(ord("g") + (self._tag_index % 20))
            self._tag_index += 1
            return tag

    def _register_waiter(self, tag: str) -> AckWaiter:
        w = AckWaiter(event=threading.Event(), result=None)
        with self._ack_lock:
            self._ack_waiters[tag] = w
        return w

    def _complete_ack(self, tag: str, result: str) -> None:
        outcome = {".": "success", "#": "retries_exhausted", "$": "corruption", "%": "clock_loss", "!": "bang"}.get(result, "unknown")
        self.diagnostics.bump("ack_" + outcome + "_tokens_total")
        with self._ack_lock:
            w = self._ack_waiters.get(tag)
            if not w:
                self.diagnostics.bump("ack_unmatched_total")
                return
            w.result = result
            w.event.set()

    def _complete_bang(self) -> None:
        # With serialized writes, there should be at most one pending command.
        with self._ack_lock:
            tags = list(self._ack_waiters.keys())
        if len(tags) != 1:
            self.diagnostics.bump("ack_bang_tokens_total")
        if len(tags) == 1:
            self._complete_ack(tags[0], "!")
        elif len(tags) == 0:
            LOG.debug("RX '!' but no pending command")
        else:
            LOG.warning("RX '!' but multiple pending commands exist (%d) (unexpected)", len(tags))

    def _await_ack(self, tag: str, waiter: AckWaiter) -> Tuple[bool, Optional[str]]:
        got = waiter.event.wait(self.ack_timeout_s)
        res = waiter.result if got else None
        with self._ack_lock:
            self._ack_waiters.pop(tag, None)
        return got, res

    # ----------------------------
    # PCI/CNI transmit
    # ----------------------------
    def _send_lighting_command(self, app_byte: int, cmd: int, group: int, level: Optional[int]) -> bool:
        """
        Send a PCI/CNI lighting command:
          bytes: 05 <app> 00 <cmd> <group> [<level>]
          checksum: 2's complement of byte-sum
          tag: g..z (so we can read ack)

        Returns True only if ACK is tag '.'.
        """
        if self.readonly:
            LOG.info(
                "[readonly] Would send cmd=0x%02X group=%d level=%s%s",
                cmd, group, level, self._label_suffix_for(app_byte, group)
            )
            self.diagnostics.bump("readonly_commands_blocked_total")
            return False

        group = clamp(int(group), 0, 255)
        b = bytearray([0x05, app_byte, 0x00, cmd, group])
        if level is not None:
            b.append(clamp(int(level), 0, 255))

        cks = checksum_2s_complement(bytes(b))
        tag = self._next_tag()

        with self._write_lock:
            waiter = self._register_waiter(tag)
            payload = b.hex().upper() + f"{cks:02X}" + tag
            self.diagnostics.bump("command_attempts_total")
            started = time.monotonic()
            try:
                self.cni.send_hex_frame(payload)
                self.diagnostics.tx("lighting_command", app=app_byte, group=group, command=cmd,
                                    level=level, tag=tag, hex=payload[:-1])
                got, res = self._await_ack(tag, waiter)
            except Exception:
                self.diagnostics.bump("command_send_failures_total")
                self.diagnostics.event("command_result", "LOCAL", result="send_failure", tag=tag)
                raise
            finally:
                # A failed send must not leave a waiter attributed to a later command.
                with self._ack_lock:
                    self._ack_waiters.pop(tag, None)

        outcome = "timeout" if not got else {".": "success", "#": "retries_exhausted", "$": "corruption", "%": "clock_loss", "!": "bang"}.get(res, "unknown")
        self.diagnostics.bump("command_" + outcome + "_total")
        if got:
            self.diagnostics.bump("command_acks_completed_total")
            self.diagnostics.bump("command_ack_latency_ms_sum", (time.monotonic() - started) * 1000)
        self.diagnostics.event("command_result", "LOCAL", app=app_byte, group=group, tag=tag,
                               result=outcome, elapsed_ms=round((time.monotonic() - started) * 1000, 1))
        if not got:
            LOG.warning(
                "No ACK from CNI for tag=%s cmd=0x%02X group=%d level=%s%s",
                tag, cmd, group, level, self._label_suffix_for(app_byte, group)
            )
            return False

        if res == ".":
            LOG.info(
                "CNI ACK %s. (success) cmd=0x%02X group=%d level=%s%s",
                tag, cmd, group, level, self._label_suffix_for(app_byte, group)
            )
            return True

        LOG.warning(
            "CNI ACK %s%s (failure) cmd=0x%02X group=%d level=%s%s",
            tag, res, cmd, group, level, self._label_suffix_for(app_byte, group)
        )
        return False

    def write_off(self, app_byte: int, group: int) -> bool:
        return self._send_lighting_command(app_byte=app_byte, cmd=0x01, group=group, level=None)

    def write_on(self, app_byte: int, group: int) -> bool:
        return self._send_lighting_command(app_byte=app_byte, cmd=0x79, group=group, level=None)

    def write_ramp_to_level(self, app_byte: int, group: int, level255: int, ramp_cmd: int) -> bool:
        return self._send_lighting_command(app_byte=app_byte, cmd=ramp_cmd, group=group, level=level255)

    # ----------------------------
    # MQTT command handling (publish ONLY on ACK success)
    # ----------------------------
    def handle_mqtt_command(self, topic: str, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="ignore").strip()
        LOG.info("MQTT %s payload=%r", topic, text)

        m = re.match(r"^cbus/write/(\d+)/(\d+)/(\d+)/(switch|ramp)$", topic)
        if not m:
            return

        net = int(m.group(1))
        app = int(m.group(2))
        group = int(m.group(3))
        action = m.group(4)

        p = self._get_profile(net, app, group)
        if p is None:
            LOG.warning("Ignoring write to unknown group=%d (net=%d app=%d)", group, net, app)
            return

        allowed_topic = p.command_topic if action == "switch" else p.level_command_topic
        if topic != allowed_topic:
            self.diagnostics.bump("commands_not_enabled_total")
            LOG.warning("Ignoring command not enabled for this profile")
            return

        is_fan = (p.level_mode == "fan")
        is_dimmer = (p.level_mode == "pct")

        # Lighting application in your setup is decimal 56 => hex 0x38 for PCI payload.
        app_byte = 0x38 if app == 56 else app
        p_suffix = self._label_suffix_for_profile(p)

        if action == "switch":
            if text.upper() == "OFF":
                ok = self.write_off(app_byte, group)
                if ok:
                    self.publish_level(p, 0)
                return

            if text.upper() == "ON":
                if is_fan:
                    ok = self.write_ramp_to_level(app_byte, group, level255=10, ramp_cmd=self.dimmer_ramp_cmd)
                    if ok:
                        self.publish_level(p, 10)
                    return

                if is_dimmer:
                    level255 = p.last_nonzero_255 if p.last_nonzero_255 > 0 else 255
                    ok = self.write_ramp_to_level(app_byte, group, level255=level255, ramp_cmd=self.dimmer_ramp_cmd)
                    if ok:
                        self.publish_level(p, level255)
                    return

                ok = self.write_on(app_byte, group)
                if ok:
                    self.publish_level(p, 255)
                return

            LOG.warning("Unexpected switch payload for group=%d%s: %r", group, p_suffix, text)
            return

        # action == "ramp"
        val = parse_first_int(text)
        if val is None:
            LOG.warning("Bad ramp payload for group=%d%s: %r", group, p_suffix, text)
            return

        if is_fan:
            level255 = clamp(val, 0, 255)
            if level255 <= 0:
                ok = self.write_off(app_byte, group)
                if ok:
                    self.publish_level(p, 0)
            else:
                ok = self.write_ramp_to_level(app_byte, group, level255=level255, ramp_cmd=self.dimmer_ramp_cmd)
                if ok:
                    self.publish_level(p, level255)
            return

        if is_dimmer:
            level_pct = clamp(val, 0, p.pct_scale)
            if level_pct <= 0:
                ok = self.write_off(app_byte, group)
                if ok:
                    self.publish_level(p, 0)
                return

            level255 = pct_to_level255(level_pct, p.pct_scale)
            ok = self.write_ramp_to_level(app_byte, group, level255=level255, ramp_cmd=self.dimmer_ramp_cmd)
            if ok:
                self.publish_level(p, level255)
            return

        # relay-like ramp
        if val <= 0:
            ok = self.write_off(app_byte, group)
            if ok:
                self.publish_level(p, 0)
        else:
            ok = self.write_on(app_byte, group)
            if ok:
                self.publish_level(p, 255)

    # ----------------------------
    # Init + status + resync (manual use)
    # ----------------------------
    def send_init(self, app: int) -> None:
        """
        PCI init sequence used in your setup.
        """
        if self.readonly:
            LOG.info("[readonly] Skipping CNI initialization")
            return
        self.cni.send_line("~~~")
        self.diagnostics.tx("initialization")
        time.sleep(0.1)
        if app != 56:
            LOG.warning("Init strings are hard-coded for app=56 (0x38). You passed app=%d.", app)

        self.cni.send_line("A3210038g")
        self.diagnostics.tx("initialization")
        time.sleep(0.05)
        self.cni.send_line("A3420002g")
        self.diagnostics.tx("initialization")
        time.sleep(0.05)
        self.cni.send_line("A3300059g")
        self.diagnostics.tx("initialization")
        time.sleep(0.05)

    def request_status_blocks(self, app_byte: int, groups: Set[int], include_96: bool = False) -> None:
        if self.readonly:
            LOG.info("[readonly] Skipping CNI status requests")
            return
        if not groups:
            return
        # include_96 is retained for CLI compatibility; every configured block is queried.
        starts = sorted({(group // 32) * 32 for group in groups})
        for start in starts:
            frame = bytes([0x05, 0xFF, 0x00, 0x73, 0x07, app_byte, start])
            cks = checksum_2s_complement(frame)
            payload = frame.hex().upper() + f"{cks:02X}"
            LOG.info("Requesting status start_group=%d (0x%02X)", start, start)
            self.diagnostics.request(app_byte, start)
            try:
                self.cni.send_hex_frame(payload)
            except Exception:
                self.diagnostics.bump("status_send_failures_total")
                raise
            self.diagnostics.tx("status_request", app=app_byte, start_group=start, hex=payload)
            time.sleep(0.2)

    def request_resync(self, mode: str = "configured") -> None:
        if self.readonly:
            LOG.info("[readonly] Skipping CNI resync")
            return
        apps = {k[1] for k in self.profiles.keys()}
        app = 56 if 56 in apps else (next(iter(apps)) if apps else 56)
        app_byte = 0x38 if app == 56 else app

        if mode == "full":
            starts = list(range(0, 256, 32))
        else:
            groups = {k[2] for k in self.profiles.keys() if k[1] == app}
            starts = sorted({(g // 32) * 32 for g in groups})
            if not starts:
                return

        for start in starts:
            frame = bytes([0x05, 0xFF, 0x00, 0x73, 0x07, app_byte, start])
            cks = checksum_2s_complement(frame)
            payload = frame.hex().upper() + f"{cks:02X}"
            LOG.info("Resync: requesting status start_group=%d (mode=%s)", start, mode)
            self.diagnostics.request(app_byte, start)
            try:
                self.cni.send_hex_frame(payload)
            except Exception:
                self.diagnostics.bump("status_send_failures_total")
                raise
            self.diagnostics.tx("status_request", app=app_byte, start_group=start, hex=payload)
            time.sleep(0.2)

    # ----------------------------
    # Read loop: process ACK tokens + normal C-Bus traffic
    # ----------------------------
    def run_forever(self, init: bool, status: bool, status_include_96: bool = False) -> None:
        apps = {k[1] for k in self.profiles.keys()}
        app = 56 if 56 in apps else (next(iter(apps)) if apps else 56)

        if init:
            LOG.info("Sending init sequence")
            self.send_init(app=app)

        if status:
            groups = {k[2] for k in self.profiles.keys() if k[1] == app}
            self.request_status_blocks(app_byte=0x38 if app == 56 else app, groups=groups, include_96=status_include_96)

        LOG.info("Entering read loop")
        while True:
            raw = self.cni.recv_line_bytes()
            if raw is None or not raw:
                continue

            self.diagnostics.rx()
            # ACK tokens are returned as standalone "lines" by cni_connection
            text_line = raw.decode("ascii", errors="ignore").strip()
            if text_line:
                if BANG_LINE_RE.match(text_line):
                    self.diagnostics.bump("ack_tokens_total")
                    self.diagnostics.event("ack", "RX", code="!")
                    LOG.debug("RX: '!'")
                    self._complete_bang()
                    continue

                if ACK_LINE_RE.match(text_line):
                    tag = text_line[0]
                    res = text_line[1]
                    self.diagnostics.bump("ack_tokens_total")
                    self.diagnostics.event("ack", "RX", tag=tag, code=res)
                    LOG.debug("RX ACK token: %s%s", tag, res)
                    self._complete_ack(tag, res)
                    continue

                # Some devices may echo commands; ignore echoes
                if text_line.startswith("\\"):
                    self.diagnostics.bump("echo_tokens_total")
                    self.diagnostics.event("echo", "RX")
                    LOG.debug("RX echo ignored: %r", text_line)
                    continue

            # Normal hex traffic
            norm = normalize_hex_from_bytes(raw)
            if not norm:
                self.diagnostics.bump("nonhex_tokens_total")
                self.diagnostics.event("unclassified", "RX")
                LOG.debug("RX non-hex/empty after normalize: raw=%r text=%r", raw, text_line)
                continue

            # DEBUG: show every inbound non-ACK frame (parsed or not)
            LOG.debug("RX frame: raw=%r norm=%s", raw, norm)

            category = diagnose_frame(norm)
            self.diagnostics.bump("rx_" + category + "_total")
            self.diagnostics.event(category + "_frame", "RX", hex=norm[:160], truncated=len(norm) > 160)
            if norm != text_line.upper():
                self.diagnostics.bump("normalized_tokens_total")
            sr = parse_status_reply(norm)
            if sr and sr.app == 0x38:
                self.diagnostics.status(sr)
                LOG.debug(
                    "RX status-reply: hdr=0x%02X coding=0x%02X app=0x%02X start_group=%d",
                    sr.hdr, sr.coding, sr.app, sr.start_group
                )
                for g, lvl in sr.levels.items():
                    if lvl is None:
                        continue
                    self._cancel_pending_synthetic_level(sr.app, g)
                    self.handle_cbus_level_update(app=sr.app, group=g, level255=lvl, source="STATUS")
                continue

            sals = parse_monitored_sals(norm)
            for sal in (item for item in sals if item.app == 0x38):
                self.diagnostics.bump("lighting_commands_observed_total")
                self.diagnostics.observed(sal.app, sal.group)
                self.diagnostics.event("lighting", "RX", app=sal.app, group=sal.group,
                                       unit=sal.dev, command=sal.cmd, level=sal.level)
                if sal.level is not None:
                    lvl = sal.level

                    if (
                        self._has_pct_profile(sal.app, sal.group)
                        and sal.cmd == 0x0A
                        and (lvl <= 1 or lvl >= 254)
                    ):
                        LOG.debug(
                            "RX SAL cmd=0x%02X group=%d edge level=%d; delaying for final dim level%s raw=%s",
                            sal.cmd,
                            sal.group,
                            lvl,
                            self._label_suffix_for(sal.app, sal.group),
                            sal.raw_hex,
                        )
                        self._delay_synthetic_level_update(
                            app=sal.app,
                            group=sal.group,
                            level255=lvl,
                            source=f"SAL cmd=0x{sal.cmd:02X}",
                            delay_s=5.0,
                        )
                        continue

                    self._cancel_pending_synthetic_level(sal.app, sal.group)
                else:
                    # cmd 0x79 is ON, 0x01 is OFF
                    if sal.cmd == 0x79:
                        lvl = 255
                    elif sal.cmd == 0x01:
                        lvl = 0
                    else:
                        LOG.debug("RX SAL cmd=0x%02X without level; ignored (raw=%s)", sal.cmd, sal.raw_hex)
                        continue

                    if self._has_pct_profile(sal.app, sal.group):
                        LOG.debug(
                            "RX SAL cmd=0x%02X group=%d has no explicit level; delaying synthetic level=%d%s raw=%s",
                            sal.cmd,
                            sal.group,
                            lvl,
                            self._label_suffix_for(sal.app, sal.group),
                            sal.raw_hex,
                        )
                        self._delay_synthetic_level_update(
                            app=sal.app,
                            group=sal.group,
                            level255=lvl,
                            source=f"SAL cmd=0x{sal.cmd:02X}",
                        )
                        continue

                LOG.debug(
                    "RX SAL: dev=0x%02X app=0x%02X cmd=0x%02X group=%d level=%d%s raw=%s",
                    sal.dev, sal.app, sal.cmd, sal.group, lvl,
                    self._label_suffix_for(sal.app, sal.group),
                    sal.raw_hex
                )
                self.handle_cbus_level_update(
                    app=sal.app,
                    group=sal.group,
                    level255=lvl,
                    source=f"SAL cmd=0x{sal.cmd:02X}",
                )
                continue

            if sals:
                continue
            LOG.debug("Unparsed RX line: raw=%r norm=%s", raw, norm)
