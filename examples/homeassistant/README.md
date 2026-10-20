# Proposed C-Bus Observatory for Home Assistant

These files are review artifacts, NOT installed configuration. The user requires
explicit approval before any live HA YAML changes. Bridge deployment does not copy
these files or publish MQTT discovery configuration.

## Proposed installation after approval

The supplied live configuration already has
`homeassistant.packages: !include_dir_named packages`. Add just one new file:
`/config/packages/cbus_diagnostics.yaml`, using cbus_diagnostics_package.yaml.
Do not replace configuration.yaml, its existing mqtt section or recorder settings.
The package adds 32 diagnostic sensors and merges two recorder exclusions for the
large live-traffic/group attribute sensors. Keep numeric metrics recorded.
Confirm the actual assigned entity IDs; if HA adds suffixes, adjust dashboard
references and recorder exclusions accordingly. Check configuration in HA before
reloading/restarting it. HA's own schema validation has NOT yet been run.

Create a separate dashboard called C-Bus Observatory and use cbus_dashboard.yaml
as its raw configuration. This is a complete dashboard, not a single card. It uses
built-in cards only. Do not overwrite an existing dashboard.

For HA rollback, remove only the added package and dashboard, validate and reload
HA. Removing the package also removes its two recorder exclusions; it does not
purge existing history. Confirm entity-registry behavior before manually deleting
entities. Bridge rollback remains independent, using the saved cgate snapshot.

## What you will see

- CNI/MQTT connection ages, RX freshness and reconnect counts.
- RX token rate and TX frame rate over the last 60 seconds, with history graphs.
  RX tokens include ACKs and echoes; these are not C-Bus bus utilization percentages.
- Distinct command ACK failures, timeouts, checksum/format failures, invalid status
  encodings, and unsupported traffic. Unsupported is not an error counter.
- Pending/overdue status reads and groups never observed. An absent/no-output group
  may never report a valid level; pending does NOT automatically mean a faulty unit.
- A one-second live wire table with the most recent 20 events: RX/TX, decoded group,
  unit, command/target, ACK result and bounded raw hex. Recent rows remain during quiet
  periods with their original timestamps; the header heartbeat keeps updating.
- A 30-second group table: published level, observation age, confidence and status
  quality. Reported level means a C-Bus status report, not independent load sensing.
  Observed command means commanded/target level; transmitted means command ACK only.

The live stream is a sampled visualization, NOT an automation-grade event bus or
an archive. Packet rows and decoded command rows are separate events; one packet
can generate several rows. Sampling/stream publish counters are distinct from
C-Bus quality counters. Use the exact counters, not the table, for trends.

## Freshness and history

Proposals use default topics:
- cbus/bridge/status/diagnostics: non-retained fresh status, normally every 5s.
- cbus/bridge/traffic: non-retained QoS 0 live window/heartbeat, normally every 1s.
- cbus/bridge/status/groups: non-retained QoS 0 group snapshot, every 30s.
- cbus/bridge/availability: existing bridge online/offline topic.

If production uses custom status/availability/traffic topics, adjust this package
before installation. Expiry is 30s for metrics, 5s for traffic and 90s for groups;
adjust if changing the publishing intervals. The existing retained legacy status
topic is preserved separately and is not used for the freshness-sensitive sensors.

Counters reset on process restart. total_increasing is used for cumulative counters;
measurement is used for rates, ages, rolling errors and pending counts. The rolling
five-minute error count is not cumulative. Aggregate observed errors cover known
parse/encoding problems, negative ACKs, send failures and command timeouts;
connection history is tracked separately. No percentage health score is invented.

32 sensors are proposed, not 32 required. The entities card shows the useful subset;
other counters are available for troubleshooting. The live and group snapshot
entities carry arrays of attributes and are excluded from Recorder to avoid storing
those arrays every second. Do not add force_update to all metrics.

## Verification performed

YAML parses and entity IDs are internally consistent; there are no unique-ID
collisions with the supplied live mqtt.sensor entries. Sensor and Markdown templates
render locally with populated, unknown and unavailable sample data. This does not
replace validation/rendering in the user's actual HA version. No live broker test
or installed dashboard validation has been performed for this candidate.

Official references:
https://www.home-assistant.io/docs/configuration/packages/
https://www.home-assistant.io/integrations/sensor.mqtt/
https://www.home-assistant.io/dashboards/markdown/
https://www.home-assistant.io/integrations/recorder/
