# Flat-network device discovery (Release 17)

Release 17 extends the explicit Release 16 inventory trial. There is no startup,
periodic or automatic fallback scan. It uses the existing single CNI connection,
existing interface options and existing MQTT inventory result topic. No HA files,
Recorder settings, dependencies or service options need changing.

## Preferred operation: presence then identity

After deployment and normal HA/physical control checks, listen in HA's MQTT
integration to `cbus/bridge/inventory`. Run in **Tools → Actions → YAML**:

```yaml
action: mqtt.publish
data:
  topic: cbus/bridge/command
  payload: '{"inventory":"discover"}'
  qos: 0
  retain: false
```

One switched-status request reads the Master Application (FF) from slot zero:
`05FF007AFF0083`. This is a read request, not a lighting command or interface
configuration change. It requests the local network only, suitable for this flat
installation; no network bridges/routes are traversed.

The bridge waits up to ten seconds after transmission for a complete 256-slot
report. Frame length, checksum and range are checked. Fragments can be split across
TCP reads, arrive in any order, or repeat consistently. Conflicting overlaps abort.
Slots with any nonzero state become candidate unit addresses; identity replies
establish which candidates respond and what they report. The raw ERROR encoding
is flagged as ambiguous, not diagnosed as noise or an output fault.

Only after complete coverage does the bridge query type/manufacturer/firmware for
candidate addresses 0–254. Quiet relays, dimmers and fan controllers do not have to
originate a lighting event to be candidates. Slot 255 is reported separately when
nonzero; it is never individually queried or automatically readdressed. Presence
is not a serial-number inventory and cannot resolve duplicate unit addresses.

A complete bitmap does not prove every physical unit responded. Known addresses
omitted from the report are flagged separately, not silently labelled offline.
The first real installation report still needs reviewing for hardware compatibility.
Don't run Toolkit/Commission or another scan concurrently; standard short-form
status reports have no origin/request ID, so a coincident master report cannot be
unambiguously attributed. We do not change EXSTAT or application filtering.

## Progress and interpretation

The retained JSON keeps the existing `devices` list and adds:

- `job.mode`: `discover`, `sweep` or `explicit`.
- `job.phase`: `presence`, `identifying`, `complete` or `stopped`.
- `job.units_total` and `job.units_finished`: identity survey progress. They are
  initially zero while presence is being collected.
- `presence.state`, `covered_slots`, `reported_units`, `ambiguous_units`,
  `address_255_reported`, and `known_units_not_reported` (once coverage is complete).
  Presence metadata has its own scan ID and timestamps.
- `pending.kind`: `presence` or `identity` while a reply is outstanding.

The scope string becomes `local_explicit_device_survey`. Existing explicit unit,
interface-only, observed and cancel commands remain supported. The JSON is a live
process-local observation cache, not persistent HA discovery or a physical wiring
map. Device rows from earlier jobs may remain: compare each row's `scan_id` with
`job.id` and use `job.requested_units` to identify the latest survey's targets.

Wait for `job.state: complete`. Each target still has its own `identified`, `partial`
or `no_reply` outcome. A finished survey is not a guarantee every query succeeded.
Usually only the final message is needed for review, plus the presence metadata.

Missing/partial/conflicting presence stops as `discovery_no_reply`,
`discovery_incomplete` or `discovery_conflict`. No identity requests are sent from
an incomplete map and no address sweep is started automatically. Preserve that
result for review. A complete but empty map reports zero targets and flags known
addresses that were not reported.

## Explicit fallback: paced address sweep

If presence is unsupported or incomplete, an independently requested fallback is
available. Wait at least 30 seconds after the previous operation ends, then run:

```yaml
action: mqtt.publish
data:
  topic: cbus/bridge/command
  payload: '{"inventory":"sweep"}'
  qos: 0
  retain: false
```

This tries IDENTIFY type at each address **0–254** and reads manufacturer/firmware
only from responders. It never queries address 255, sends arbitrary CAL commands,
resets units or writes configuration. It is much slower than presence discovery.
At the existing three-second budget, a silent address costs about three seconds
and a responsive unit about nine seconds across its three identity queries.
A full sweep takes at least roughly 13 minutes, plus six seconds per responsive
unit and any time yielding to normal work. All 255 responding would take about
38 minutes. No reply does not prove a missing or faulty unit.

## Priority, cancellation and recovery

Both new full-network modes have a one-hour overall deadline, allowing a large
inventory without the previous ten-minute small-batch deadline. Explicit batches
of up to 16 retain their ten-minute deadline. All modes share the existing minimum
three-second automatic readback budget and pause while ramp watches are active.
One outstanding read, no automatic retry, and no reply wait on the command worker.
Normal controls and readbacks retain priority. Large surveys can therefore take
longer or reach the deadline in a busy installation.

Cancellation is unchanged:

```yaml
action: mqtt.publish
data:
  topic: cbus/bridge/command
  payload: '{"inventory":"cancel"}'
  qos: 0
  retain: false
```

Either link loss cancels the active survey and discards pending/queued work;
reconnect never replays it. Retained requests are ignored. A new job is blocked
while one runs, and for 30 seconds after completion/cancellation. The cache and
coverage are bounded by the local address space. Historical retained MQTT results
may outlive the running bridge; use availability, job timestamps and bridge_start_ts.
The existing snapshot/rollback helper restores the previous bridge. No HA restart
or file reload is needed, and installing the release alone sends no discovery query.

## Protocol basis

Clipsal CBUS-SIUG issue 1.17, §4.2.9.2 documents switched-status on Master Application
FF as reporting unit presence. Standard two-bit status framing/packing is described
in §7.3. IDENTIFY fields remain the three verified Release 16 attributes from §7.2.
The complete-map requirement and conservative nonzero-candidate interpretation are
bridge safeguards; they still require a real-device acceptance trial.

Manufacturer-authored guide (mirror):
https://manualzz.com/doc/4150831/clipsal-serial-interface-user-guides

After a successful discovery, Release 18 can separately survey voltage and latched
diagnostic flags for cached typed units. See [device health](DEVICE-HEALTH.md);
wait for job completion and the 30-second cooldown before starting another survey.
