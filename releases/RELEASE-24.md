# Release 24 — audited manual clearing of latched device errors

Finalizes the previously uncommitted Release 21 candidate on top of the accepted
stereo and dynamic-label releases, plus the captured HA examples at `1dbec97`.

Clearing remains disabled until a writable audit path is explicitly configured.
Each request targets one reviewed full-diagnostics responder, saves its fresh
before-flags, sends one local CAL RESET, then requires fresh health readback.
Only a verified zero-flags reply changes that unit's persistent baseline to zero.
A returning flag is then new, including after a bridge restart. This clears all
latched flags on that unit; it does not repair faults or restore erased history.

Lighting/ramp commands keep priority. The operation uses the existing single CNI
connection, background pacing, bounded inventory job and a 60-second deadline.
Retained/repeated requests are rejected; disconnects, timeouts and restarts never
resume an old clear. Missing/invalid audit storage disables clearing alone.

[Manual clearing and deployment guide](../docs/DEVICE-ERROR-CLEARING.md).
The additive HA generator produces a unit selector, a confirmed manual request
button and scalar results. It does not replace the existing Observatory layout.

## Verification on 15 November 2026

- All 370 unit tests pass on the combined current source.
- Full-process localhost clearing simulation passes: durable before/after flags,
  exactly one paced clear, wrong-destination/fragmented readback handling,
  restart with no replay, and returning TX flags recognized as new.
- Independent localhost DLT-label and virtual-stereo integration regressions pass.
- Launcher and release wrapper pass Bash syntax validation.
- The live roster was read without changing production: 43 units, 21 full-health
  responders. Generated HA package/card matches that roster.
- A timestamped local snapshot preserves the 21 units' existing HA flag readings
  and each reading's observation timestamp. It is not a simultaneous bus survey.

No live clear, HA configuration change or production deployment was performed
while preparing this release. First trial: one responding unit, verify its zero
readback, archive the audit, then observe for 24–48 hours before clearing others.
Unit 12 (SENPIRIB) currently reports power-on-reset and communications-TX flags
and is a suitable trial candidate; use fresh preflight rather than trusting a
saved snapshot. Units without full-health replies cannot get a verified baseline.

## Deployment ordering and rollback

Provision the protected audit directory and add `CBUS_DEVICE_ERROR_STATE` to the
existing environment **before** release check/apply, leaving the old service
running until apply. The old launcher ignores the new variable. This lets the
usual deployment snapshot include the new setting: the rollback tool refuses
configuration changed after its snapshot. Preserve the stereo and label opt-ins.

The ordinary release script creates a full application backup and writes
`/home/ack/cbus-bridge-rollback.sh`. Before any later rollback, archive the newest
error-clear audit separately: a code rollback cannot restore flags on a device,
and an older app backup may not contain the latest audit. Do not delete audit
state to bypass a request-ID or roster mismatch.
