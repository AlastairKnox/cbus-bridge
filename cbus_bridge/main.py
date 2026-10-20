from __future__ import annotations

import argparse
import json
import logging
import time
import threading
from dataclasses import dataclass
from typing import Optional

import paho.mqtt.client as mqtt

from .ha_profiles import load_ha_mqtt_profiles
from .cni_connection import CniConnection
from .bridge import CbusMqttBridge
from .utils import BridgeDiagnostics, CommandWorker

LOG = logging.getLogger("cbus_bridge")

AVAIL_TOPIC_DEFAULT = "cbus/bridge/availability"
AVAIL_ONLINE = "online"
AVAIL_OFFLINE = "offline"

STATUS_TOPIC_DEFAULT = "cbus/bridge/status"


@dataclass
class Metrics:
    start_time_epoch: int
    start_monotonic: float

    mqtt_connects_total: int = 0
    mqtt_disconnects_total: int = 0
    cni_connects_total: int = 0
    cni_disconnects_total: int = 0
    cni_connect_failures_total: int = 0
    resync_requests_total: int = 0

    last_mqtt_connect_ts: Optional[int] = None
    last_mqtt_connect_mono: Optional[float] = None
    last_mqtt_disconnect_ts: Optional[int] = None
    last_mqtt_rx_ts: Optional[int] = None

    last_cni_connect_ts: Optional[int] = None
    last_cni_connect_mono: Optional[float] = None
    last_cni_disconnect_ts: Optional[int] = None

    last_resync_ts: Optional[int] = None
    last_resync_mode: Optional[str] = None
    last_resync_error: Optional[str] = None

    last_error_ts: Optional[int] = None
    last_error: Optional[str] = None


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cni-host", required=True)
    ap.add_argument("--cni-port", type=int, default=10001, help="Single CNI port (default: 10001).")

    # Failover settings (Mini-project #2)
    ap.add_argument("--cni-secondary-port", type=int, default=0,
                    help="Deprecated; ignored. The CNI has a single endpoint.")
    ap.add_argument("--failover-after-seconds", type=int, default=60,
                    help="Deprecated; ignored.")
    ap.add_argument("--failover-after-attempts", type=int, default=10,
                    help="Deprecated; ignored.")

    ap.add_argument("--mqtt-host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-user", default=None)
    ap.add_argument("--mqtt-pass", default=None)

    ap.add_argument("--ha-config", required=True,
                    help="Path to file containing your HA mqtt: YAML (or just the mqtt: block).")

    ap.add_argument("--init", action="store_true", help="Send CNI init sequence.")
    ap.add_argument("--status", action="store_true", help="Request status blocks for groups found in HA config.")
    ap.add_argument("--status-include-96", action="store_true", help="Legacy flag; --status now queries every configured group block.")

    ap.add_argument("--readonly", action="store_true", help="Do not write to CNI; only publish updates to MQTT.")
    ap.add_argument("--log-level", default="INFO")

    ap.add_argument("--enable-writes", action="store_true", help="Legacy flag; cannot be combined with --readonly.")

    # Resync / get_all fallback
    ap.add_argument("--resync-every", type=int, default=0,
                    help="Periodic status resync interval in seconds (0 disables).")
    ap.add_argument("--resync-mode", choices=["configured", "full"], default="configured")
    ap.add_argument("--resync-topic", default="cbus/bridge/command",
                    help="MQTT topic for on-demand resync commands (payload: resync|resync_full).")

    # Availability + status
    ap.add_argument("--availability-topic", default=AVAIL_TOPIC_DEFAULT)
    ap.add_argument("--status-topic", default=STATUS_TOPIC_DEFAULT)
    ap.add_argument("--metrics-every", type=int, default=0,
                    help="Publish retained status metrics every N seconds (0 disables).")

    ap.add_argument("--diagnostics-every", type=float, default=0,
                    help="Status diagnostics interval, minimum 1s; 0 disables (service default 5s).")
    ap.add_argument("--traffic-every", type=float, default=0,
                    help="Non-retained traffic-view interval, minimum 1s; 0 disables (service default 1s).")
    ap.add_argument("--traffic-topic", default="cbus/bridge/traffic")
    return ap


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.readonly and args.enable_writes:
        parser.error("--readonly and --enable-writes cannot be combined")

    for name in ("diagnostics_every", "traffic_every"):
        value = getattr(args, name)
        if value != 0 and (not 1 <= value <= 3600):
            parser.error(f"--{name.replace('_', '-')} must be 0 or between 1 and 3600 seconds")

    if (any(c in args.traffic_topic for c in ("#", "+", "\x00"))
            or args.traffic_topic.startswith("cbus/write/")
            or args.traffic_topic in (args.status_topic, args.status_topic + "/groups",
                                      args.status_topic + "/diagnostics", args.availability_topic,
                                      args.resync_topic)):
        parser.error("--traffic-topic must be a distinct, non-command MQTT topic")

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    profiles = load_ha_mqtt_profiles(args.ha_config)
    LOG.info("Loaded %d C-Bus group profiles from HA config", len(profiles))

    readonly = args.readonly

    mqtt_up = threading.Event()
    cni_up = threading.Event()
    stop_event = threading.Event()

    metrics = Metrics(start_time_epoch=int(time.time()), start_monotonic=time.monotonic())

    def now_epoch() -> int:
        return int(time.time())

    def set_last_error(msg: str) -> None:
        metrics.last_error = (msg[:240] + "...") if len(msg) > 240 else msg
        metrics.last_error_ts = now_epoch()

    # MQTT client
    try:
        mc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cbus-bridge", clean_session=True)
    except Exception:
        mc = mqtt.Client(client_id="cbus-bridge", clean_session=True)

    if args.mqtt_user:
        mc.username_pw_set(args.mqtt_user, args.mqtt_pass or "")
    mc.reconnect_delay_set(min_delay=2, max_delay=30)

    # LWT for availability
    mc.will_set(args.availability_topic, payload=AVAIL_OFFLINE, qos=1, retain=True)

    secondary_port = args.cni_secondary_port if args.cni_secondary_port and args.cni_secondary_port > 0 else None

    cni = CniConnection(
        host=args.cni_host,
        primary_port=args.cni_port,
        secondary_port=secondary_port,
        timeout=10.0,
        failover_after_seconds=args.failover_after_seconds,
        failover_after_attempts=args.failover_after_attempts,
    )

    diagnostics = BridgeDiagnostics(profiles)
    bridge = CbusMqttBridge(cni=cni, mqtt_client=mc, profiles=profiles, readonly=readonly,
                            diagnostics=diagnostics)

    worker = CommandWorker(diagnostics, cancel=bridge.cancel_session)

    def publish_availability(state: str, reason: str = "") -> None:
        mc.publish(args.availability_topic, payload=state, qos=1, retain=True)
        if reason:
            LOG.info("Availability -> %s (%s)", state, reason)
        else:
            LOG.info("Availability -> %s", state)

    def build_status(reason: str = "") -> dict:
        uptime_s = int(time.monotonic() - metrics.start_monotonic)
        fo = cni.failover_status()
        return {
            "ts": now_epoch(),
            "uptime_s": uptime_s,
            "start_ts": metrics.start_time_epoch,
            "reason": reason,

            "mqtt_connected": mqtt_up.is_set(),
            "cni_connected": cni_up.is_set(),

            # Keep host as configured; port is reported as ACTIVE to reflect failover.
            "cni_host": args.cni_host,
            "cni_port": fo.get("cni_active_port") or args.cni_port,

            "mqtt_host": args.mqtt_host,
            "mqtt_port": args.mqtt_port,

            "readonly": bool(readonly),

            "mqtt_connects_total": metrics.mqtt_connects_total,
            "mqtt_disconnects_total": metrics.mqtt_disconnects_total,
            "cni_connects_total": metrics.cni_connects_total,
            "cni_disconnects_total": metrics.cni_disconnects_total,
            "cni_connect_failures_total": metrics.cni_connect_failures_total,
            "mqtt_connection_age_s": int(time.monotonic() - metrics.last_mqtt_connect_mono) if mqtt_up.is_set() and metrics.last_mqtt_connect_mono is not None else None,
            "cni_connection_age_s": int(time.monotonic() - metrics.last_cni_connect_mono) if cni_up.is_set() and metrics.last_cni_connect_mono is not None else None,
            "diagnostics": diagnostics.snapshot(),
            "command_queue": worker.snapshot(),

            "last_mqtt_connect_ts": metrics.last_mqtt_connect_ts,
            "last_mqtt_disconnect_ts": metrics.last_mqtt_disconnect_ts,
            "last_mqtt_rx_ts": metrics.last_mqtt_rx_ts,

            "last_cni_connect_ts": metrics.last_cni_connect_ts,
            "last_cni_disconnect_ts": metrics.last_cni_disconnect_ts,

            "resync_requests_total": metrics.resync_requests_total,
            "last_resync_ts": metrics.last_resync_ts,
            "last_resync_mode": metrics.last_resync_mode,
            "last_resync_error": metrics.last_resync_error,

            "last_error_ts": metrics.last_error_ts,
            "last_error": metrics.last_error,

            # Failover details (new)
            **fo,
        }

    def publish_status(reason: str = "") -> None:
        payload = json.dumps(build_status(reason), separators=(",", ":"), sort_keys=True)
        mc.publish(args.status_topic, payload=payload, qos=1, retain=True)
        if args.diagnostics_every and mqtt_up.is_set():
            info = mc.publish(args.status_topic + "/diagnostics", payload=payload, qos=0, retain=False)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                diagnostics.bump("diagnostic_publish_failures_total")
        LOG.debug("Published status (%s)", reason or "periodic")

    # ---- MQTT callbacks ----
    def on_connect(client, userdata, flags, reason_code, properties=None):
        rc_val = getattr(reason_code, "value", reason_code)
        if rc_val == 0:
            mqtt_up.set()
            worker.set_link("mqtt", True)
            metrics.mqtt_connects_total += 1
            metrics.last_mqtt_connect_ts = now_epoch()
            metrics.last_mqtt_connect_mono = time.monotonic()

            LOG.info("MQTT connected")
            client.subscribe("cbus/write/+/+/+/+")
            LOG.info("Subscribed to cbus/write/+/+/+/+")
            client.subscribe(args.resync_topic)
            LOG.info("Subscribed to %s", args.resync_topic)

            publish_availability(
                AVAIL_ONLINE if cni_up.is_set() else AVAIL_OFFLINE,
                "MQTT connected; CNI connected" if cni_up.is_set() else "MQTT up, awaiting CNI",
            )

            publish_status("mqtt_connected")
        else:
            name = getattr(reason_code, "getName", lambda: repr(reason_code))()
            msg = f"MQTT connect failed rc={rc_val} ({name})"
            LOG.warning(msg)
            set_last_error(msg)
            publish_status("mqtt_connect_failed")
    
    
    def on_disconnect(client, userdata, *args, **kwargs):
        mqtt_up.clear()
        worker.set_link("mqtt", False)
        metrics.mqtt_disconnects_total += 1
        metrics.last_mqtt_disconnect_ts = now_epoch()

        # Best-effort detail for logs/status
        detail = ""
        if args:
            detail = ", ".join(repr(a) for a in args)
        if kwargs:
            detail = (detail + " " if detail else "") + ", ".join(f"{k}={v!r}" for k, v in kwargs.items())

        publish_availability(AVAIL_OFFLINE, f"MQTT disconnected ({detail})" if detail else "MQTT disconnected")
        publish_status("mqtt_disconnected")


    def perform_resync(mode):
        metrics.resync_requests_total += 1
        metrics.last_resync_ts = now_epoch()
        metrics.last_resync_mode = mode
        metrics.last_resync_error = None
        bridge.request_resync(mode=mode)
        publish_status("resync:" + mode)

    command_topics = {topic for profile in profiles.values()
                      for topic in (profile.command_topic, profile.level_command_topic) if topic}

    def enqueue(operation):
        return worker.submit(lambda lease: bridge.execute_queued(operation, lease))

    def on_message(client, userdata, msg):
        metrics.last_mqtt_rx_ts = now_epoch()
        if msg.retain:
            diagnostics.bump("retained_commands_ignored_total")
            return
        if len(msg.payload) > 256 or len(msg.topic) > 128:
            diagnostics.bump("oversized_commands_ignored_total")
            return
        if readonly:
            diagnostics.bump("readonly_commands_ignored_total")
            return
        if msg.topic == args.resync_topic:
            text = msg.payload.decode("utf-8", errors="ignore").strip().lower()
            mode = ("full" if text in ("resync_full", "full") else
                    args.resync_mode if text in ("resync", "sync", "status") else None)
            if mode:
                enqueue(lambda: perform_resync(mode))
            return
        if msg.topic in command_topics:
            topic, payload = msg.topic, bytes(msg.payload)
            enqueue(lambda: bridge.handle_mqtt_command(topic, payload))

    mc.on_connect = on_connect
    mc.on_disconnect = on_disconnect
    mc.on_message = on_message

    LOG.info("Connecting to MQTT %s:%d", args.mqtt_host, args.mqtt_port)
    mc.connect(args.mqtt_host, args.mqtt_port, keepalive=60)
    mc.loop_start()
    worker_thread = threading.Thread(target=worker.run, name="cbus-command-worker", daemon=True)
    worker_thread.start()

    # ---- Periodic resync thread ----
    def resync_loop():
        if args.resync_every <= 0:
            return
        time.sleep(5)
        while not stop_event.is_set():
            stop_event.wait(args.resync_every)
            if stop_event.is_set():
                break
            enqueue(lambda: perform_resync(args.resync_mode))

    if args.resync_every and args.resync_every > 0:
        threading.Thread(target=resync_loop, name="cbus-resync", daemon=True).start()
        LOG.info("Periodic resync enabled: every %ss (mode=%s)", args.resync_every, args.resync_mode)

    # ---- Bounded diagnostics publishing; no new C-Bus queries ----
    intervals = [v for v in (args.metrics_every, args.diagnostics_every) if v > 0]
    status_interval = min(intervals) if intervals else 0

    def metrics_loop():
        next_status = next_traffic = next_groups = 0.0
        while not stop_event.wait(0.25):
            now = time.monotonic()
            try:
                if status_interval and now >= next_status:
                    next_status = now + status_interval
                    if mqtt_up.is_set():
                        publish_status("periodic_metrics")
                if args.diagnostics_every and now >= next_groups:
                    next_groups = now + 30
                    if mqtt_up.is_set():
                        info = mc.publish(args.status_topic + "/groups", json.dumps(diagnostics.group_snapshot()), qos=0, retain=False)
                        if info.rc != mqtt.MQTT_ERR_SUCCESS:
                            diagnostics.bump("diagnostic_publish_failures_total")
                if args.traffic_every and now >= next_traffic:
                    next_traffic = now + args.traffic_every
                    diagnostics.publish_traffic(mc, args.traffic_topic, mqtt_up.is_set())
            except Exception as e:
                diagnostics.bump("diagnostic_publish_failures_total")
                set_last_error(f"Metrics publish failed: {e!r}")
                LOG.warning("Metrics publish failed: %s", e)

    if status_interval or args.traffic_every:
        threading.Thread(target=metrics_loop, name="cbus-metrics", daemon=True).start()
        LOG.info("Diagnostics intervals: status=%ss, traffic=%ss", status_interval, args.traffic_every)

    # ---- CNI reconnect loop ----
    try:
        while True:
            try:
                session_token = worker.session_token()
                cni.connect()
                diagnostics.reset_connection()
                diagnostics.event("cni_connected", "LOCAL")
                cni_up.set()
                metrics.cni_connects_total += 1
                metrics.last_cni_connect_ts = now_epoch()
                metrics.last_cni_connect_mono = time.monotonic()

                # Availability reason includes the active port (primary or secondary)
                publish_availability(AVAIL_ONLINE, f"CNI connected (port {cni.active_port})")
                publish_status("cni_connected")

                bridge.run_forever(init=args.init, status=args.status, status_include_96=args.status_include_96,
                                   on_ready=lambda: worker.ready_cni(session_token))

            except Exception as e:
                worker.set_link("cni", False)
                was_connected = cni_up.is_set()
                cni_up.clear()
                if was_connected:
                    metrics.cni_disconnects_total += 1
                    metrics.last_cni_disconnect_ts = now_epoch()
                else:
                    metrics.cni_connect_failures_total += 1
                diagnostics.reset_connection()
                diagnostics.event("cni_disconnected" if was_connected else "cni_connect_failure", "LOCAL")

                set_last_error(f"CNI loop error: {e!r}")
                publish_availability(AVAIL_OFFLINE, f"CNI loop error: {e}")
                publish_status("cni_disconnected")

                LOG.warning("CNI loop error: %s; reconnecting in 3s", e)
                try:
                    cni.close()
                except Exception:
                    pass
                time.sleep(3)

    finally:
        stop_event.set()
        worker.stop()
        worker_thread.join(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())


