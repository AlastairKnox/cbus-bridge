# Release 19: voltage-only fallback and bounded alternative health replies

Baseline: 26efc8a (Release 18). Preparation leaves production and HA untouched.
Runtime file layout, dependencies, interface options and endpoints are unchanged.

Health commands first read the existing diagnostic summary (IDENTIFY 04). If it
gets no usable reply in three seconds, one separate IDENTIFY 07 read requests the
documented voltage-only ASCII value. Summary responders receive no extra query.
Both stages use the existing shared three-second background budget, priority for
lighting and pause during ramp watches. A unit completes only after its final read.

Results distinguish full reply, voltage_only, no_reply and unrecognized_reply.
Voltage-only results have no invented diagnostic flags. Per-read timestamps and
source identify what was actually measured; selected units' stale data is cleared
on each new health attempt. Existing identity data stays separate.

For a matching checksum-valid local long CAL reply with unfamiliar data, retain
only the first bounded envelope per read. It cannot satisfy the read or extend its
timeout; a later valid reply can still succeed. Unrelated, corrupt, late, routed or
unsolicited frames are not attributed to the current read. At most two reads per
unit, no automatic retries, scans, polling, arbitrary reads, writes or error resets.
No HA entities, dashboard edits or new Recorder activity are introduced.

See [compatibility guide](../docs/DEVICE-HEALTH-COMPATIBILITY.md) for commands,
precise outcomes, interpretation and the CNI/keypad/light-sensor acceptance trial.

## Validation

291 offline tests pass, including existing lighting, fades, discovery, deployment
and rollback/recovery checks. New coverage includes exact voltage request/reply,
strict ASCII/count/checksum/local addressing, partial vs unknown results, one-shot
fallback, alternative-then-valid replies, bounded duplicate evidence, late/unrelated
reply rejection, cancellation during fallback, priority/ramp pause and all 255
silent units completing within the survey deadline using at most 510 reads.

The full-process localhost integration exercises the real bridge with simulated
MQTT and CNI peers: full summary, voltage-only, unknown formats, silent units,
fragmented TCP, duplicate evidence, wrong destination, retained-command rejection,
three-second send spacing, lighting during a survey, disconnect cancellation/no
replay and normal control after reconnect. The previous presence discovery and
partial-map full-process test is also run. These are simulations, not proof of
installed hardware support for IDENTIFY 07.

## Deployment

Push from the desktop, then use tools/release.sh check/apply with the exact commit
on cgate. Existing verified backup/rollback handling applies. No HA restart/reload.
After HA, physical switches and fades pass, follow the three-unit trial in the
guide and share the final complete result. Normal rollback remains:
`bash /home/ack/cbus-bridge-rollback.sh`.
