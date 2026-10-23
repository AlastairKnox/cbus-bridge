# Release 20: opt-in paced device monitoring and HA health dashboard

Baseline: 5269eee (Release 19). Production and HA files remain unchanged during
preparation. No new dependencies, CNI connections, interface options or runtime
module files. Existing full-app backup/rollback covers the default roster path.

A reviewed JSON roster enables read-only health polling of known responders only.
This site's survey produces 43 inventory devices and 21 eligible for routine reads.
One unit per ~43 seconds spreads polling over 15 minutes, yielding to controls and
ramps. The manual refresh button queues a paced responder batch without cancelling
manual surveys or bypassing the existing three-second query budget. Missing/invalid
configuration disables monitoring while the bridge continues normal operation.

Per-device MQTT states report fresh/stale/awaiting/unknown distinctly. Link loss
invalidates freshness; reconnect cannot revive cached observations. Staleness uses
monotonic time. Metadata and saved error baselines persist in the reviewed roster;
measurements do not survive restart as fresh data. Current bits absent from the
baseline are shown separately, without clearing flags or claiming an error rate.

The offline generator prepares native MQTT HA devices/sensors, a refresh button,
Recorder timestamp exclusions and an additive Observatory Devices view. Numeric
voltage and meaningful flag/status changes retain normal history. Routine times
and diagnostic JSON attributes do not create history. No light definitions,
schedules or automations are changed. HA installation follows bridge acceptance.

See [monitoring guide](../docs/DEVICE-MONITORING.md) for config validation, scheduling,
freshness, baseline limits, generation, ordered installation and separate HA rollback.

## Validation

307 offline tests cover existing bridge/release behavior plus monitor pacing,
no polling for configured nonresponders, read-only/unready admission, manual-refresh
coalescing, survey priority, stale/failure/session semantics, wall-clock corrections,
new vs baseline flags, bounded heartbeat/state maps, malformed config and native
HA artifact structure/Recorder separation. A fake-clock 30-day continuous run
keeps monitor state bounded without introducing scans or non-read commands.

A full-process localhost integration exercises the real bridge with a reviewed
fixture roster and disposable MQTT/CNI simulators: retained refresh rejection,
manual responder-only refresh, unknown-data handling, historical flags not new,
lighting during monitoring, reconnect freshness invalidation without replay and
ordinary control after reconnect. Previous discovery/full-health integration is
also checked with monitoring disabled. No installed CNI/MQTT faults are injected.
HA YAML requires live configuration checking and UI acceptance after deployment.
