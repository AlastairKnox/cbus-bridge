# Release 12 candidate: Study duration-aware HA transitions

Baseline: Release 11 (759830c). Development does not deploy bridge or live HA.

Add strict {level,transition_seconds} and {restore:true,transition_seconds} commands
for Study group 13 only. A nonzero duration obtains a fresh block-level numeric
report, then selects the hardware ramp rate with the closest nominal duration for
the actual brightness change. Failed/invalid/absent readback rejects without a
lighting write. Status queries share the existing three-second automatic global
budget and use the existing bounded worker and single CNI connection.

No-transition HA controls remain immediate; legacy numeric writes, explicit native
ramp_seconds/STOP, fans and other groups retain existing behavior. No dependencies
or deploy runtime-file layout changes. Existing error-encoding investigation is
separate; this release does not reclassify or suppress those counters.

Proposed optional Study MQTT-template light preserves the existing entity/unique ID
and reads the existing numeric feedback. A scheduler-card customization exposes
brightness and duration. Neither HA proposal is applied by the bridge installer.

See docs/HA-TRANSITIONS.md for constraints, activation, acceptance and coordinated
HA/bridge rollback. A requested duration is approximate and may have a substantial
error at unsupported partial-travel extremes. Current brightness is freshly read,
not independently measured light output; query latency precedes the fade.

Validation: 182 offline regression tests, including the independent reader/worker
query-and-ACK flow, native-rate nearest-duration selection,
strict input, fresh-readback-before-command, missing data/readonly/other-group
rejection, disconnect and queue guards, global-budget sharing, bounded waits,
rendered HA templates and YAML proposal syntax. No physical C-Bus commands or live
HA changes were made during development. Live HA configuration/card validation and
physical acceptance are required after deliberate deployment/activation.
