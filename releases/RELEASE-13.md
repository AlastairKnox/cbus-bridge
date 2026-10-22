# Release 13: configured dimmer durations and actionable command failures

Baseline: 966c34c (Release 12).

Duration commands now work for configured writable percentage dimmers, including
Upstairs group 72. Fresh readback uses the target group's 32-group block and
accepts only that exact network/application/group's numeric report. Existing
readback budget, queue/session guards, tracking requirement and nearest native
rate selection remain. Hardware durations are approximate, especially over short
brightness changes. Ordinary UI numeric/ON/OFF commands remain unchanged.

Diagnostics add last_command_failure (reason, app, group, timestamp) and a
command_failures_total counter. Reasons cover invalid/unsupported transitions,
read-only or disabled tracking, missing fresh numeric data, send errors, ACK
failures/timeouts and queue rejection/discards. Queue/read-only callback failures
may lack group context. Counts represent observations and can count more than one
failure in a rejected/discarded command lifecycle. History is process-local and
last failure persists across later successes; it is not an active-fault flag.
No raw payloads or credentials are stored in these fields.

An optional separate HA package adds two diagnostic sensors:
examples/homeassistant/cbus_command_failures_package.yaml. Deploy the bridge
first, then install the package with HA validation. Existing dashboard/Recorder
configuration is not changed by the bridge installer.

The installer saves /home/ack/cbus-bridge-rollback.sh atomically after a verified
backup and before service mutation. It is owned by ack and mode 0700. Run it as
ack with `bash ~/cbus-bridge-rollback.sh`; it invokes sudo and the independent
backup's deploy.py. Failure to save the helper aborts installation before service
stop. A failure during installation still invokes the existing automatic restore.
The saved helper targets the snapshot for the latest attempted installation, even
if the installation fails. It does not promise recovery from disk/hardware loss.

Validation: 187 offline tests pass. No live CNI/MQTT commands or deployments.

## Acceptance on cgate

Use tools/release.sh check/apply with the exact pushed release commit. Keep the
printed backup path as well as the saved helper. Confirm bidirectional controls
and immediate slider/off behavior. Run Upstairs 100 -> 50 and 50 -> 20 duration
scripts, observe physical fades and reported progress/final levels. Test an off
fade and an ordinary manual override. Check Study still works and normal fan and
wall-switch operations stay responsive. Confirm HA diagnostics update and helper
points at this installation's backup; do not run rollback merely to inspect it.

On rollback to Release 12, Upstairs transition_seconds scripts are unsupported;
coordinate HA schedule rollback using its separate saved configuration. The bridge
rollback helper restores the bridge, not HA schedules or scripts.
