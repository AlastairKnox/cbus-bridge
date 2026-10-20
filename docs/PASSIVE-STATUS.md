# Passive status monitoring (Release 7)

This release prepares a reversible MONITOR trial on the existing single CNI
connection. It adds no connection, periodic status request or fade-progress poll.
Commands, ACK policy, numeric level handling and ramp control remain in place.

## Configuration and initialization

The service launcher now defaults `CNI_MONITOR` to `1`. It supplies `--monitor`,
which requires `--init`. Initialization changes only Interface Options 1 from
0x59 to 0x79, enabling MONITOR alongside the existing options. The application
remains 56 and Interface Options 3 remains 0x02 (LOCAL_SAL on, EXSTAT off).
Read-only mode continues to suppress all initialization transmissions.

Set `CNI_MONITOR=0` in the existing production environment file before restarting
the bridge to restore the prior initialization without changing code. Only literal
0 or 1 is accepted. Do not expose credentials while editing that file. No new
service drop-in is required. A plain CLI run defaults to `--no-monitor`; opt in
explicitly with `--init --monitor`. `--monitor` without `--init` is rejected before
opening any connections.

Status JSON includes `cni_monitor_requested` and `cni_initialization_enabled`.
These report intent, not confirmation that the interface accepted its parameters.
Successful receipt of binary reports is the useful evidence of the trial's effect.

## What is decoded

With EXSTAT off, standard short-form binary status has a C0-based length header,
application, block start, packed two-bit states and checksum. Each byte describes
four groups, least-significant pair first: absent, on, off or error. The full report
can span several independently validated lines. Header length, checksum and group
bounds are checked before any state is used. Only lighting application 56 is
accepted for publishing; other scopes remain unsupported. Extended-format binary
reports are not decoded by this release because EXSTAT remains off.

The binary error state can indicate disagreeing output units or initialization
requests. It does not establish a wiring/noise fault. Binary report state counts
are deliberately separate from the existing aggregate protocol/error counters.

MONITOR forwards existing network status activity. It does not instruct dimmers
to broadcast fade steps, guarantee completion events or provide numeric brightness
through ordinary binary reports. Numeric level replies already supported by the
bridge remain supported. No assumption is made about what this installation will
send until acceptance is observed.

## State publishing and safety

Valid ON/OFF updates only the profile's separate boolean state topic, including
binary sensor profiles. ON never becomes 100% brightness or a fan speed; OFF never
writes a numeric zero to the brightness/preset topic. The last nonzero dimmer/fan
level is preserved. Error and absence publish no assumed state.

Some live profiles use one numeric topic for both state and brightness/preset.
Those are recorded in diagnostics but skipped for binary MQTT publication. They
continue to need numeric level replies; changing their HA mappings is outside this
release. Dedupe compares the last successfully enqueued state publication, including
updates from normal commands/readbacks. A failed publish is retried on the next
report. The cache is bounded by configured state topics and cleared on disconnect,
so a fresh report can republish after reconnection.

Valid binary reports cancel delayed synthetic level updates for covered groups.
They cannot complete outstanding numeric LEVEL requests or refresh timestamps or
confidence associated with a historical numeric reading. A binary timestamp is
kept separately, avoiding the appearance of a fresh brightness measurement.

## Diagnostics and HA proposal

Existing diagnostics add `binary_status_reports_total`, per-state group-observation
counts, `binary_state_publishes_total`, `binary_state_publish_failures_total` and
`binary_shared_topic_skips_total`. Group observations count every report, including
unconfigured groups and repeats; they are not distinct-device totals. Snapshot
fields include `last_binary_report_ts`, `last_binary_report_age_s`,
`binary_error_configured_groups` and `binary_absent_configured_groups`.

Configured group rows gain `binary_status`, `last_binary_status_ts`,
`binary_observation_age_s` and `last_binary_publish_ts`. History survives reconnect
with its original age and resets on process restart. Current binary counts are
last-observed states, not a declaration of current hardware health. The existing
bounded traffic view includes raw binary frames and per-report summaries.

The example HA package proposes six new sensors and a Passive status monitoring
card, plus separate binary status/age in the group table. No existing entity IDs
or control mappings change. These examples require separate approval and HA
validation/reload; a bridge deployment never installs them. Without HA changes,
the existing live traffic card still displays binary frame/report events.

## Trial and recovery

Use the existing exact-commit `tools/release.sh check` then `apply` procedure. Keep
the installer's verified backup and rollback command. Production stays on Release 6
until the user performs deployment; candidate preparation performs no live writes.

After deployment, confirm ordinary HA controls, fan speeds, physical-switch
feedback and CNI connectivity. Allow time for passive reports; record their rate,
checksum/format failures and binary state observations. Choose Study group 13:
run the agreed fade-to-OFF and compare the physical light with the HA on/off state
and traffic. Do not expect the brightness slider to track binary reports. Record
whether OFF arrives naturally and how long it takes. Use an explicit resync only
as a separate comparison, so it is not mistaken for passive feedback.

If no passive reports appear, record that result without enabling more options or
adding polling. MONITOR availability does not guarantee the installation generates
reports. If queue drops, parsing errors or control regressions appear, restore the
previous release using this deployment's printed rollback command, or deliberately
set CNI_MONITOR=0 and restart for an isolated monitoring comparison. Verify normal
controls again. Do not use a second CNI client for the trial.

## Protocol references

- Clipsal CBUS-QS 2.0, section 5.2.2: A3300079g enables MMI reporting:
  https://ckm-content.se.com/ckmContent/sfc/servlet.shepherd/document/download/0698V00000kHfpkQAC
- Clipsal CBUS-SIUG 1.17 (manufacturer text mirrored), sections 4.3, 7.3 and
  10.3: status framing, two-bit encoding and interface options:
  https://manualzz.com/doc/4150831/clipsal-serial-interface-user-guides
