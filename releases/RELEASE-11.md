# Release 11: three-second ramp readback

Baseline: Release 10 (621c5f0).

The service default RAMP_READBACK_EVERY changes from 10 to 3 seconds. CLI enabled
range changes from 10–60 to 3–60 seconds (0 still disables; standalone CLI default
remains 0). Explicit environment overrides are preserved.

The automatic ramp-readback ceiling changes from one 32-group block request per
five seconds to one per three seconds globally. Both reservation scheduling and
actual send time enforce the limit, including delayed command-queue dispatch.
Groups in the same block coalesce; separate blocks share the budget, so each block
may update less frequently when several ramps are active. First readback remains
due after one second; later requests remain subject to the global limit.

No lighting command rates, MONITOR settings, HA entities, raw Recorder exclusions,
startup/manual resyncs or periodic full resync settings change. The three-second
usable-reply timeout, three-miss stop, queue/session guards, two-sample completion
and idle no-polling behavior remain. This is a bounded ramp-only change, not a
manufacturer throughput claim or a new general network polling service.

## Validation and deployment

170 offline regression tests pass, including actual-send spacing, block sharing,
missing replies and final completion at the faster cadence; launcher syntax checked.
Deploy using tools/release.sh check/apply and retain the printed rollback command.
No HA configuration reload/restart is required.

After deployment, use the existing Study group 13 60-second ramp. Observe smoother
measured HA steps and final OFF; check request counts (roughly 20 requests during a
full minute with one active block, plus completion confirmation), missing replies,
command ACK latency/negative ACKs, queue failures and physical switch responsiveness.
Stop/restore the starting level afterward. Multiple blocks and command queue load
can reduce observed per-group update frequency. Do not increase the polling rate
further based only on absence of bridge errors.

If RAMP_READBACK_EVERY is explicitly configured in /etc/cbus-bridge.env, the launcher
keeps that value. Set that one setting to 3 if needed; do not expose the file's
credentials. Runtime ramp_tracking reports interval_s=3 and global_min_interval_s=3
for this release's intended default setup. Rollback restores prior bridge files
and launcher; preserve any separately edited environment value appropriately.
