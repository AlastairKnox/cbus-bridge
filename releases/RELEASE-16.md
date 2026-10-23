# Release 16: explicit device identity inventory trial

Baseline: 8bc76c2 (Release 15). No dependency, runtime-file layout, launcher,
service-unit, credentials, HA configuration or Recorder changes.

## Included

- Explicit MQTT-triggered, local unicast IDENTIFY for device type, manufacturer
  and firmware only. Small batches of 1–16 addresses; no automatic scan/polling.
- First trial can target only the CNI's address, learned from a validated SMART
  initialization reply. Passive lighting-source addresses provide optional survey
  candidates; they are not a complete inventory of output units.
- Existing reader and command worker; one outstanding three-second response
  window. Shared three-second query budget, background priority, pause during ramp
  watches, ten-minute survey deadline, 30-second post-survey/cancel cooldown.
- Retained-command rejection, strict unit/attribute builders and response validation,
  destination matching, no retries, no reset/config writes, and cancellation on
  either connection loss. No management wait blocks lighting commands.
- Bounded, process-local result cache published as retained QoS1 JSON on
  cbus/bridge/inventory. Explicit partial/no_reply outcomes, per-attribute receipt
  timestamps and survey progress/errors. No HA sensors or discovery payloads yet.
- Identity replies are classified separately from lighting/unsupported traffic and
  can never acknowledge a lighting write or create a load state.

See [trial instructions and limitations](../docs/DEVICE-INVENTORY.md), especially
unit addresses versus lighting group addresses, incomplete passive discovery and
CAL's lack of transaction IDs. Real unit compatibility still needs the deliberate
post-deployment trial. This release does not promise voltage/fault readings yet.

## Validation

244 offline regression tests pass, including the manufacturer's exact query vector,
strict frame/count/checksum/ASCII validation, unit/destination/attribute matching,
reply windows, duplicate/late/unrelated replies, no-reply/partial identity,
read-only/retained-command rejection, caps, cancellation, deadlines, cooldown,
shared bus pacing, active-ramp pause, lighting priority and stale queued work.

A separate localhost-only integration runs the real bridge process with synthetic
MQTT/CNI peers. It verifies learned CNI address, zero inventory queries at startup,
retained request rejection, identity and missing-unit results, three-second send
spacing, ignored replies addressed elsewhere, normal lighting during the survey,
and disconnect cancellation with no replay and subsequent lighting control.
Simulated lighting command completion was about 40 ms in the initial run; this
is not a physical C-Bus latency guarantee. Existing recovery and fade integrations
are also run against loopback simulators. Development never connects to live CNI.

## Deployment

Use the existing exact-commit tools/release.sh check/apply workflow. No HA reload
or restart is required. Preserve the printed snapshot and rollback helper. Check
HA controls, physical switches, dimmer fades and final OFF before the optional
one-unit inventory trial. Capture its MQTT result for review before surveying more
units. Installing the release alone does not start a survey.
