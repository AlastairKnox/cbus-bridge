# Release 4 candidate: bridge diagnostics and a live traffic view

Baseline runtime: Release 3, 57e4b9e. Not deployed by preparation.

## Scope

Add passive diagnostics using already received traffic and existing writes. Do not
add C-Bus polling, extra CNI connections, discovery writes, ramp controls or changes
to existing HA control mappings. Live HA files remain untouched. HA proposals require
separate approval and are not installed by tools/release.sh.

## Runtime behavior

- Existing status JSON gains a diagnostics object, connection ages and an explicit
  cni_connect_failures_total. cni_disconnects_total now counts actual established
  connection losses; failed initial/reconnect attempts are counted separately.
- Count command attempts, successful transmissions to CNI, correlated ACK results,
  send failures and timeouts. # retries exhausted, $ corruption, % clock loss and
  untagged ! are separate outcomes. Mean ACK turnaround uses monotonic time and
  includes positive/negative ACKs, excluding timeouts. Success means transmission
  onto C-Bus, not physical feedback.
- Negative ACKs are also counted even without a matching pending command (for
  example initialization replies). Aggregate observed errors count each negative
  ACK once rather than double-counting the correlated command outcome.
- Count incoming ACK/echo tokens, supported lighting/status frames, checksum/format
  failures, unsupported and nonhex traffic separately. Normalization count is
  informational. Unsupported opcodes/apps/other reply formats are not declared errors.
- Distinguish valid, absent and invalid-encoded status groups. Invalid encoding is
  an observed quality issue, not proof of a specific electrical fault. A missing
  group is not automatically a network error.
- Track configured groups awaiting valid status after a request and waiting >=15s.
  These are observations since requests, not transaction-correlated guarantees;
  unrelated monitored status can also arrive. Repeated requests reset that group's
  waiting age. Disconnect/reconnect clears pending session state. Last historical
  levels and timestamps are preserved; age alone is not labelled a fault.
- Track each configured group's last bus observation, last MQTT publication attempt,
  published target/level and source: unknown, ACK-transmitted, observed command, or
  status-reported level. No values are fabricated for unobserved groups.
- Keep process-local counters, bounded rate/error buckets, and a 20-event live ring.
  Counts are exact for recorded bridge observations; the live view is sampled.

## MQTT and defaults

The service launcher adds DIAGNOSTICS_EVERY (default 5s) and TRAFFIC_EVERY (default
1s), both optional environment settings; no env edit is required. CLI defaults are
0 (disabled) for compatibility when invoking the app directly. Valid nonzero
intervals are 1..3600s. A separate diagnostic thread emits MQTT only, no CNI reads.

- Existing status topic remains retained and gains nested diagnostics.
- STATUS_TOPIC/diagnostics publishes the same status as non-retained QoS 0, at least
  as often as the smaller positive metrics/diagnostics interval. Fresh status is
  also emitted on connection/status events when MQTT is up.
- STATUS_TOPIC/groups: non-retained QoS 0 configured-group snapshot every 30s while
  diagnostics are enabled.
- cbus/bridge/traffic (customizable by --traffic-topic): non-retained QoS 0 snapshot
  of the most recent 20 RX/TX/local events, with sequence/timestamps and heartbeat.
  Raw packet hex is bounded to 160 characters. Known decoded command details are
  separate rows. Non-protocol text and command payload/credentials are not streamed.

When MQTT is down, periodic diagnostic/traffic publishing is skipped and the live
window is cleared at its next tick; counters continue. Sampling, offline omission
and publish failure counters are distinct from bus quality. No traffic retry queue
or persistent packet archive is created. A failed QoS 0 publish may be omitted;
this interface is unsuitable as a lossless automation trigger stream.

## HA proposal

examples/homeassistant contains a 32-sensor package, a built-in-card dashboard,
Recorder exclusions for the two large attribute buffers, and installation/rollback
notes. The user's existing packages include can load the package without replacing
configuration.yaml. Numeric metrics are history/statistics candidates; raw live
buffers are deliberately not recorded. Approval and actual HA configuration
validation are required before adding it.

## Checks and acceptance

72 offline tests pass locally and on cgate using the production Python environment, including classifications, ACK outcomes,
threaded counter updates, bounded sampling, disconnect behavior, rates/ages,
synchronization quality, passive publishing, QoS/retain policies and HA artifact
consistency. Local Jinja rendering passes for all proposed sensor/dashboard
templates. No new Python dependency is required.

The active 72-profile mapping validates and the proposed HA topics match the
production topic settings. Rerun the normal preflight before activation. Use the normal check/apply workflow. Backup Release 3 immediately
before installation; keep its backup-specific rollback command. First confirm
existing light/dimmer/fan/wall-switch behavior, then verify new MQTT topics, counters
and bounded rate. The UI package is a separate approval/install step. Observe real
traffic for a baseline before choosing alerts; do not deliberately break the bus
or send commands to sensor-only groups to generate errors.
