#!/usr/bin/env bash
set -euo pipefail

cd /opt/cbus_bridge
source /opt/cbus_bridge/.venv/bin/activate

exec python /opt/cbus_bridge/run_bridge.py \
  --cni-host "${CNI_HOST}" \
  --cni-port "${CNI_PRIMARY_PORT}" \
  --mqtt-host "${MQTT_HOST}" \
  --mqtt-port "${MQTT_PORT}" \
  --mqtt-user "${MQTT_USER}" \
  --mqtt-pass "${MQTT_PASS}" \
  --ha-config "${HA_CONFIG}" \
  --log-level "${LOG_LEVEL}" \
  ${INIT} ${STATUS} ${STATUS_96} \
  --resync-every "${RESYNC_EVERY}" \
  --resync-mode "${RESYNC_MODE}" \
  --availability-topic "${AVAIL_TOPIC}" \
  --status-topic "${STATUS_TOPIC}" \
  --metrics-every "${METRICS_EVERY}"
