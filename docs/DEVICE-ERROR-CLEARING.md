# Manual clearing of latched unit errors

Release 24 introduced opt-in, one-unit-at-a-time clearing. Release 25 adds
a bounded waiting slot so confirmed requests do not have to race background
monitoring cooldowns. Routine monitoring still
never clears errors. Clearing applies to **all latched error bits on that unit**,
not just communications TX errors. It cannot repair a bus problem or undo history.
The old flags are saved before the command; they cannot be written back into the
unit. First trial: choose one known responding unit, then observe for 24–48 hours.

## Documented command

Clipsal C-Bus Serial Interface User Guide CBUS-SIUG issue 1.17, §7.1 p38 documents
CAL RESET `$08` as clearing latched unit errors. §4.2.9.1 pp19–20 defines local
point-to-point framing: header `$06`, unit, route `$00`, CAL data, checksum.
The builder therefore produces `06 <unit> 00 08 <checksum>`, for example unit 3:
`06030008EF`. These are protocol examples, **not deployment/test instructions**.
It never sends the serial-interface `~` reset, STORE, configuration writes,
broadcasts or a routed request. IDENTIFY `$04` supplies the health readback.

[Manufacturer-authored guide, mirrored copy](https://manualzz.com/doc/4150831/clipsal-serial-interface-user-guides)

## Transaction and failure behavior

The target must be a reviewed roster entry with capability `full`. The command path must be ready and writable. A confirmed request can be
admitted during another inventory job or its 30-second cooldown. Exactly one
manual intent waits, for at most 60 seconds, before fresh preflight begins.
Background monitoring cannot start another job ahead of that waiting intent.
The existing inventory job is allowed to finish; lighting and ramps still take
priority. Once started, the clear transaction has its own 60-second deadline
(at most 120 seconds including waiting).
It uses the existing CNI connection and command worker, yields to lighting and
ramps, and shares the three-second background query budget. It does not open a
second connection, add a timer/thread, or automatically scan other units.

1. Persist admission in `waiting` and consume the request ID before waiting.
   The existing metrics/worker path starts it after the inventory job and
   cooldown finish. Expired waiting requests become `waiting_expired` without
   any clear write. There is no second request or repeated clear submission.
2. Request a fresh IDENTIFY `$04` reading, requiring the expected source,
   interface destination and attribute within three seconds. No voltage fallback.
3. Persist the old flags and a write-attempt checkpoint before sending RESET once.
4. Request another health reading after the pacing interval.
5. Only a valid zero-flags readback is `verified_clear` and atomically sets the
   baseline override to zero. Nonzero flags are `flags_remaining`; the old
   baseline is preserved even if some bits cleared.

CAL replies have no transaction ID. Correlation uses the active request's source,
destination, attribute, session and short response window, as existing surveys do.
CNI transmission alone, an ACK, disappearance from a dashboard or a socket write
returning successfully is not verification. Standard network transmission retries
are controlled by the CNI; the bridge never resubmits the clear command.

No reply, disconnect, cancellation, send exception or audit failure leaves the
old baseline intact. After a write attempt the result must be treated as uncertain
unless it is `verified_clear` or a valid `flags_remaining` readback. `sent_ts`
records that the socket may have been written, even when an exception followed.
Disconnect or an explicit inventory cancel also drops a waiting intent.
Restart marks incomplete records `interrupted_before_send` or
`interrupted_unverified`, never resumes them, and never sends anything on behalf
of an old request. Fresh monitoring is still required after reconnect/restart.
If verification fails, inspect the unit's next ordinary reading before deciding
whether to submit a new manual request.

## Persistent audit and enabling

Clearing is disabled unless `--device-error-state PATH` is explicitly configured.
The service launcher accepts the optional environment setting
`CBUS_DEVICE_ERROR_STATE`. Normal polling and control keep running if the audit
file is invalid or cannot be written; further clears are disabled.

Recommended production path:
`/opt/cbus_bridge/device_state/error_clears.json`. Provision its parent directory
as the service account (`cbusbridge`) with mode 0700, then set this path in the
existing protected service environment file. Do not expose other credentials
while editing that file. **Do this during the agreed deployment window**, not
while preparing the release. Provision the directory and add the environment
setting **before** the release check/apply snapshot; do not restart the old service
just to add this setting. The old launcher ignores the new variable. This ordering
lets the snapshot retain matching environment hashes for automatic rollback; an
environment edit after apply would cause the rollback tool to refuse restoration.
Use the usual check/apply process before installing the new HA controls.
Confirm the audit parent is writable
within any systemd restrictions. Startup creates/checks the file without C-Bus IO.

The audit file is mode 0600 and saved using a same-directory temporary file,
file fsync, atomic rename and directory fsync. It keeps one latest attempt per
configured full-capability unit (bounded to 255), plus persistent baseline overrides
and a global request-ID high-water mark. For a permanent full chronology, archive
this small file before further manual clear sessions; it is not an event database.
No secrets are written. Measurements are never restored as fresh.

The file is bound to a hash of the complete reviewed roster. A roster edit,
including refresh/baseline changes, requires an explicit review/migration of the
audit; mismatches disable clearing instead of silently discarding replay history.
Do not delete/reinitialize the file to solve a permissions or configuration error.
The selected default directory lies inside the full-app deployment backup and
rollback boundary. Custom paths need separate backup/restore arrangements.
Before rolling back, retain a copy of the latest audit: an application rollback
cannot restore the device's erased flags, and its older audit may predate a clear.
Never use automatic rolling-back/replaying of clear requests.

## MQTT contract

On `cbus/bridge/command`, non-retained, QoS 0:

```json
{"clear_device_errors":{"unit":31,"request_id":1793400000000000,"confirm":true}}
```

Use exactly these keys. `unit` must be a single integer, never 255 or a list.
`confirm` must be boolean true. `request_id` is a positive integer no greater than
9007199254740991 and **strictly greater than every previously admitted clear ID**.
The HA script generates epoch microseconds. Clock reversal or a manually supplied
future ID can cause rejection; review the saved high-water mark rather than
resetting it. Retransmissions and retained commands cannot trigger another clear.
Rejected requests use `device_error_clear_rejected` in ordinary command diagnostics.
A second clear request is rejected while one is waiting/running; it never
replaces the first. An accepted waiting request is visible immediately and can
start only within its bounded 60-second window. It is discarded on disconnect
or restart, not resumed later. Requests still reject when disabled, unready,
readonly, invalid or using a consumed ID.

Existing `cbus/bridge/devices/<unit>/state` gains `error_clear_enabled` and
`last_clear` (null or the latest persisted record). A record includes request ID,
requested timestamp, outcome, before/after raw flags where observed, sent timestamp
where attempted, and completion timestamp where finished. Existing latched/new
flag and voltage fields continue. Summary `cbus/bridge/device_monitor` includes `error_clear_enabled` and
`error_clear_request`, a bounded latest-request object with unit, request ID,
request time, state and rejection reason. It is separate from the per-unit
audit: rejecting a second request must not overwrite a first operation's audit.
These states remain non-retained with 30-second heartbeats; request and audit
progress also trigger prompt publication. The HA scalar **Last clear request**
shows waiting/progress or the actual rejection reason. A restarted bridge does
not restore this ephemeral latest-request view, but the per-unit audit remains. Once verified, a returning TX
bit is reported as a new error even across restart.

## Additive HA controls (prepare first; install separately)

```bash
python3 tools/prepare_device_error_clear.py device_monitor.json /tmp/cbus-clear-ha
```

This reads the reviewed roster and creates a package and a **single card fragment**.
It contacts neither HA nor CNI and does not rewrite the roster or dashboard.
Install `cbus_device_error_clear.yaml` through the site's existing packages setup,
merging Recorder exclusions where required by its configuration. Configuration
check/reload or restart follows the normal HA process. Append `error-clear-card.yaml`
to the current Devices view. Do not regenerate/overwrite the existing device view
from an earlier release, since its layout has since been tidied.

The card selects one full-capability unit, then requires confirmation on the
request button. It shows the latest request status/rejection and outcomes for all eligible units. The script publishes
one fresh ID with `retain: false`; it does not loop over units. Confirmation is a
UI safeguard, not authorization against other clients with MQTT write access.

New scalar outcome sensors retain useful change history. Per-attempt timestamps,
selector, support and script are excluded from Recorder; the audit JSON is not
attached as entity attributes. No lighting entities, schedules or automations are
changed. HA YAML has offline structural checks; live configuration/UI acceptance
is required after installation.

References: [HA actions/confirmation](https://www.home-assistant.io/dashboards/actions),
[HA scripts](https://www.home-assistant.io/integrations/script),
[MQTT publish](https://www.home-assistant.io/actions/mqtt.publish/).

## Acceptance after deployment

Verify ordinary physical-switch, HA on/off, dimmer and ramp behavior first. Confirm
monitor freshness and `error_clear_enabled`. Record the chosen unit and old flags,
then use the confirmed action for that unit only. Expect `verified_clear`, zero
flags and a persisted zero baseline. A `flags_remaining` result is diagnostic
evidence, not permission for repeated automatic clears. Compare ordinary monitoring
over the next 24–48 hours; a returning communications TX bit warrants investigation.

## Updating an existing Release 24 installation

Keep the existing audit path, environment, roster and flags. Do not delete or
reset the audit. Use the normal backed-up check/apply for Release 25; no new
service environment setting is needed. Archive the latest audit before rollback.
The old release cannot interpret new `waiting`/`waiting_expired` audit states:
if rolled back with such an audit, it disables clearing rather than replaying or
discarding it. Normal lighting and monitoring remain available. Preserve the audit
and review compatibility before re-enabling clears on older code.

Regenerate the additive HA artifacts. The package adds one Last clear request
sensor; keep the existing scripts, selector and outcomes. Add that row to the
existing clear controls and retain the fixed 32px button icon. Do not replace the
rest of the dashboard. HA validation and MQTT YAML reload can load the new sensor;
no new Recorder exclusion or HA restart is needed solely for this scalar sensor.
