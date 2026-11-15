# Release 21: manual per-unit clearing of latched errors

Adds documented local CAL RESET with fresh before/after health readings, durable
before-flags audit and verified-zero baseline overrides. No automatic clearing,
replay, broadcast, extra connection or change to routine polling. Existing worker
priority, three-second background budget, inventory cooldown and a 60-second
transaction deadline apply. Clearing is opt-in through a writable audit path;
invalid storage disables clearing while ordinary bridge operation continues.

The additive offline HA generator produces a unit selector, confirmed manual
action and last-outcome/time sensors without rewriting the existing Observatory
or collecting diagnostic JSON in Recorder. No live HA changes occur in preparation.

See [manual clearing guide](../docs/DEVICE-ERROR-CLEARING.md) for the protocol source,
MQTT contract, enabling, backup/rollback implications and first-unit acceptance.
The existing release check/apply deployment process is unchanged. The recommended
audit directory is included in the existing full-app backup. Application rollback
cannot undo cleared unit flags; retain the newest audit before rolling back.

## Validation

All 332 offline tests pass (307 existing and 25 new). New tests cover frame
boundaries/checksum, strict target and confirmation validation, opt-in/default-off,
retained/readonly/oversized request rejection, fresh preflight/readback matching,
remaining flags, timeout, disconnect, send failure, deadline, token reuse, replay
IDs, disk failures at each checkpoint, atomic permissions, roster binding, bounded
persistence, restart without replay, returning TX errors and lighting/ramp priority.
HA artifacts are YAML round-trip checked and contain no raw JSON attributes.
Live bus/HA acceptance remains required; no live flag was cleared in preparation.

A full-process localhost integration test also passes against disposable MQTT/CNI
peers. It verifies retained-command rejection, exactly one paced clear submission,
fragmented/wrong-destination readback handling, durable before/after flags, restart
without replay, a returning TX bit reported as new, and ordinary lighting after
restart. Lighting during the clear responded in 0.020 seconds in this local fixture;
this is not a hardware performance guarantee. Run integration checks separately
from the unit suite because both intentionally use the single-instance guard.

## Finalization

This candidate was retained while Releases 22/23 and the HA playlist controls were
completed. It is finalized as [Release 24](RELEASE-24.md), with 370 current unit
tests and clearing, DLT-label and virtual-stereo localhost integrations passing.
The earlier 332-test count above records the original candidate verification.
