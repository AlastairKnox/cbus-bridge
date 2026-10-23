# On-demand device health trial (Release 18, extended by Release 19)

Release 19 adds one voltage-only fallback and per-read evidence/outcomes. See
[health compatibility](DEVICE-HEALTH-COMPATIBILITY.md) for current behavior.
The single-read/no-retry description below documents the original Release 18 trial.

This release reads IDENTIFY attribute 04 (extended diagnostic summary) from local
unit addresses. It reuses the existing single CNI connection and inventory worker.
There is no periodic/startup health polling, RESET, parameter write or error clear.
The first release is a protocol trial: validate actual hardware replies before
adding HA device sensors, automatic health polling or alerts.

## First trial after deployment

Check ordinary HA controls, physical switches and fades first. Listen to
`cbus/bridge/inventory` using HA's MQTT integration. Once interface_unit is known,
run this in Tools → Actions (unit 2 is the site's CNI):

```yaml
action: mqtt.publish
data:
  topic: cbus/bridge/command
  payload: '{"inventory":{"health":[2]}}'
  qos: 0
  retain: false
```

Expect job.mode and phase `health`, then job.state `complete`. The device row's
nested `health.outcome` is `reply` or `no_reply`. Identity is separate: on a fresh
process, a health-only row has identity outcome `not_requested`, not `identified`.
Send the final JSON for review, including `health.data_hex`. It is diagnostic
payload, not a credential. Do not assume a missing reply means an absent or faulty
unit: the installed firmware's actual summary support remains to be established.

After a successful first trial, wait at least 30 seconds from completion, then
sample different hardware: `{"inventory":{"health":[20,21,1,12,100,130]}}`.
These are physical unit addresses from the accepted discovery, not light groups.
Explicit lists accept 1–16 addresses 0–254; duplicates are deduplicated, extra keys
and broadcast 255 are rejected. A small batch has a ten-minute overall deadline.

## Survey the discovered inventory

Inventory is process-local and starts empty after deployment. Run the existing
`{"inventory":"discover"}` first if you want to restore the full identified
inventory. Once it completes, wait 30 seconds and request:

```yaml
action: mqtt.publish
data:
  topic: cbus/bridge/command
  payload: '{"inventory":"health"}'
  qos: 0
  retain: false
```

This queries all cached rows with an identified type, including historical rows
from earlier surveys in the same process. It does not run another presence request,
query passively observed addresses by guess, or sweep missing addresses. An empty
cache is rejected with `expected_known_inventory_or_1_to_16_explicit_health_units`.
The full health job has a one-hour deadline. At most 255 rows are possible; 43 units
need 43 queries, roughly two to three minutes without higher-priority activity.
No replies are retried automatically. Cancellation uses `{"inventory":"cancel"}`.

## Reading the result

Identity metadata and its scan ID remain at device-row level. Health has its own:

- `scan_id`, `attempt_ts` (queued), `sent_ts` (send attempt), `completed_ts` (reply or
  timeout), and `observed_ts` (valid reply only), in Unix seconds. Use these with
  bridge_start_ts; a retained value is a historical observation, not live health.
- `outcome`: pending, reply, no_reply, or the job's cancellation/failure reason.
- `data_hex`: all 12 diagnostic data bytes, retained for protocol verification.
- `voltage_raw` and `voltage_v`: byte 9 ADC reading divided by 6.375, rounded to
  three decimals. Display precision does not imply measurement accuracy.
- `common_status_raw`, `output_unit`, `checksum_alarm_enabled`, `voltage_marginal`,
  `voltage_low`, `learn_mode`: documented bits from byte 10. Reserved bits are kept
  in the raw byte but not assigned meanings.
- `latched_errors_raw` and `latched_errors`: byte 11, decoded as power_on_reset,
  internal_stack_overflow, communications_tx_error, micro_reset, eeprom_data_error,
  eeprom_checksum_error, eeprom_write_error, installation_mmi_error.

Latched errors may describe past events. A power-on-reset flag alone is not a
current fault; no flags is not proof of an error-free bus. There are no inferred
voltage thresholds, general healthy/unhealthy verdicts or automatic clear commands.
Zero ADC is preserved if actually returned; missing replies never become zero.

A new health attempt replaces that unit's previous health observation with pending,
then a reply or explicit failure. Old measurements are not retained as current on
failure. Other units keep their own historical timestamps. An identity rescan
preserves health with its original timestamp and scan ID. Restart clears the entire
cache; retained MQTT data from an older process/version must not be read as current.

## Safeguards and limits

One outstanding read, three-second reply window from transmission, at least three
seconds between all inventory/ramp feedback queries. Queries yield to queued or
active work and pause while any ramp watch exists. No worker waits for a reply.
Retained requests are ignored; read-only mode and an unready command path reject
requests. A second inventory/health job is rejected; the 30-second cooldown applies
to all modes. Link loss cancels queued/pending reads and never resumes them.

Accept only a checksum-valid local long CAL reply with unit, CNI destination,
attribute 04, exact 12-byte data length, count 0D and current response window.
Malformed, unrelated, late, short-form and routed replies cannot satisfy a request.
As with identity, CAL has no transaction ID; a very late same-key reply in a later
job cannot be distinguished absolutely. Cooldown reduces this ambiguity.

This release adds no HA entities or dashboard changes, so it introduces no new HA
Recorder history. The retained inventory topic continues to update only on changes.
Existing deployment and rollback tools apply; no HA restart/reload is needed.

## Protocol basis

Clipsal CBUS-SIUG issue 1.17, sections 4.2.9.1, 7.1 and 7.2 (page 42). IDENTIFY
04 returns 12 data bytes: first nine contain configuration/identification summary,
then voltage ADC, common status, error flags. Local unit-3 request is
`0603002104D2`; accepted reply envelope is
`86 <unit> <CNI> 00 8D 04 <12 data bytes> <checksum>`.

Manufacturer-authored guide (mirror):
https://manualzz.com/doc/4150831/clipsal-serial-interface-user-guides
