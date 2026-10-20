#!/usr/bin/env bash
set -euo pipefail

cd /opt/cbus_bridge
source /opt/cbus_bridge/.venv/bin/activate

# Release 7 defaults to the agreed passive-monitoring trial. Set CNI_MONITOR=0
# to retain the earlier initialization. Invalid values fail before any connection.
case "${CNI_MONITOR:-1}" in
  1) monitor_args=(--monitor) ;;
  0) monitor_args=(--no-monitor) ;;
  *) echo 'CNI_MONITOR must be 0 or 1' >&2; exit 1 ;;
esac

exec python /opt/cbus_bridge/run_bridge.py \
  --cni-host "${CNI_HOST}" \
  --cni-port "${CNI_PRIMARY_PORT}" \
  --mqtt-host "${MQTT_HOST}" \
  --mqtt-port "${MQTT_PORT}" \
  --mqtt-user "${MQTT_USER}" \
  --mqtt-pass "${MQTT_PASS}" \
  --ha-config "${HA_CONFIG}" \
  --log-level "${LOG_LEVEL}" \
  "${monitor_args[@]}" \
  ${INIT} ${STATUS} ${STATUS_96} \
  --resync-every "${RESYNC_EVERY}" \
  --resync-mode "${RESYNC_MODE}" \
  --availability-topic "${AVAIL_TOPIC}" \
  --status-topic "${STATUS_TOPIC}" \
  --metrics-every "${METRICS_EVERY}" \
  --diagnostics-every "${DIAGNOSTICS_EVERY:-5}" \
  --traffic-every "${TRAFFIC_EVERY:-1}" \
  --ramp-readback-every "${RAMP_READBACK_EVERY:-10}"
