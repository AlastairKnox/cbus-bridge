# Release 6 candidate: explicit native dimmer ramp control

Baseline runtime: Release 5, 38f107d. HA examples include the bf04489 Observatory
cleanup. This candidate has not been deployed; live HA configuration is unchanged.

Adds opt-in JSON level/ramp-rate and stop commands on existing permitted dimmer
brightness topics. All 16 native full-scale rates are supported, including zero
(instantaneous). Fade to zero uses a ramp rather than immediate OFF. TERMINATE
RAMP sends 0x09 with no level. Invalid/ambiguous requests and non-dimmer profiles
are rejected; existing simple brightness commands, fans and switch behavior remain.

Structured commands never publish their target as present brightness. Transmission
history/counters and the existing raw command events make the operation visible.
Stop suppresses delayed synthetic updates and leaves brightness for subsequent
bus/status evidence. No software fade loop, completion timer or extra polling is
introduced. Queue/retained/read-only/session policies are unchanged. An already
transmitted hardware ramp can continue through a bridge disconnect/restart.

See docs/RAMP-CONTROL.md for payloads, full-scale time semantics, readback limits,
HA examples and physical acceptance. The optional HA scripts package is a proposal
only; it needs separate approval and HA validation. A bridge deploy does not install
it, and native light.turn_on transition is not enabled by existing MQTT mappings.

Validation includes the full earlier regression suite plus golden ramp/stop wire
frames/checksums, every native rate, fade-to-zero, strict parsing, profile scaling,
legacy comma behavior, fan/relay/read-only restrictions, negative ACKs, timeout
queue invalidation, monitored stop cancellation and detached diagnostic snapshots.
No dependencies or runtime deployment-file layout change.

Deploy deliberately using the existing exact-commit check/apply helper, retaining
its verified backup and printed rollback command. First exercise existing controls,
then one agreed dimmer for ramp/stop acceptance. On failure use that deployment's
rollback command and verify normal controls again. Offline tests do not establish
hardware acceptance. No production or live HA changes are part of preparation.
