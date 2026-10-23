# Release 18: read-only device voltage and latched-diagnostics trial

Baseline: a34093b (Release 17). Production and HA files are not changed during
preparation. Runtime layout, dependencies and interface options remain unchanged.

## Behavior

- Explicit health batches of 1–16 known unit addresses, or all typed rows in the
  process's discovered inventory. One IDENTIFY 04 read per selected unit.
- Exact local long CAL reply validation; decode voltage, common status and latched
  error flags. Preserve all twelve raw data bytes for installed-hardware review.
- Independent health outcomes, scan IDs and queued/sent/completed/observed timestamps.
  Identity stays separate; pending/timeouts cannot masquerade as zero voltage or
  fresh healthy readings. Identity rescans preserve historical health timestamps.
- Reuse lighting priority, shared three-second read budget, ramp pause, one pending
  query, cancellation, connection loss invalidation and 30-second job cooldown.
- No startup/periodic polling, retries, new CNI connection, RESET, writes or error
  clears. No automatic hardware-health verdicts, new HA entities or Recorder data.

See [device health](../docs/DEVICE-HEALTH.md) for commands, interpretation, limitations
and the first CNI-only hardware trial. Per-device HA monitoring follows validation
of actual diagnostic responses; simulated values are not evidence of site health.

## Validation

276 offline tests pass, including the existing lighting, ramps, discovery,
deployment/rollback and recovery tests. New checks cover frame length/count/checksum,
voltage conversion, each flag, wrong addressing/attribute/late replies, timeout
semantics, bounds/admission, cancellation/send failure/deadlines, identity preservation,
43-device completion, stale measurements, ramp pause and ordinary-control priority.

The Release 18 localhost full-process test runs the real bridge against disposable
MQTT/CNI simulators: fragmented replies, unrelated and malformed replies, missing
unit, retained-command rejection, three-second spacing, ordinary lighting during
health reads, reconnect cancellation without replay, and control after recovery.
The Release 17 full-process discovery/partial-map regression also passes.
Neither test contacts the installed CNI, MQTT broker or production service.

## Deployment

Use tools/release.sh check/apply with the exact commit after pushing from desktop.
Keep the printed backup and saved /home/ack/cbus-bridge-rollback.sh helper.
Check normal HA/physical controls and fades, then use the guide's unit-2 health
trial and share its final JSON. No HA restart or configuration reload is required.
