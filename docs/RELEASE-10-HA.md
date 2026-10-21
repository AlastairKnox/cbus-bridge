# Release 10 optional Home Assistant updates

Apply the bridge first. These are proposed YAML changes; live HA activation requires
user approval. No C-Bus device mappings, lighting definitions or automations change.

Files:
- examples/homeassistant/cbus_diagnostics_package.yaml → packages/cbus_diagnostics.yaml
- examples/homeassistant/cbus_dashboard.yaml → dashboards/cbus_observatory.yaml

Preserve existing entity unique IDs. The package has 57 sensors: the installed 38,
six previously prepared MONITOR metrics, and 13 additional Release 10 metrics.
The dashboard adds Ramp feedback, current-connection numeric coverage, numeric
report age per group, and a separate Detailed traffic view. Default Live wire uses
the activity buffer; it falls back to detailed events with an older bridge payload.

No raw events, group arrays or unsupported samples get a new recorder-enabled
entity. Existing sensor.cbus_diag_traffic and sensor.cbus_diag_groups remain
excluded by the package. Numeric counters and ordinary light states remain recorded.
Counts reset on bridge restart; missing usable ramp replies are not automatically
C-Bus electrical errors. A pending request does not erase historical numeric evidence.
Coverage means a numeric report has been seen in this CNI session, not a constant
polling guarantee. Historical published-level confidence is distinct from that metric.

Before applying, compare current live files and save exact backups/hashes. Copy only
the two approved files. Check HA configuration, reload manually configured MQTT
entities, and refresh the dashboard. If validation fails, restore backups before
reloading. If rolling the bridge back, the previous HA files may also be restored;
new coverage sensors will otherwise show unknown on the older bridge.

Pending mapping audit: groups 65, 73, 90, 99–103, 250 currently report absent. Confirm
hardware/programming and intended HA use before removing or changing any mapping.
Scene Trigger Control application 202 is outside this release; lighting monitoring
remains application 56 only.
