# Device health compatibility (Release 19)

Some units answer identity reads but do not answer the extended health summary.
This release distinguishes missing data from usable voltage-only data without
assuming that a simple device is faulty or that every firmware supports diagnostics.
It adds no automatic polling, HA sensors, configuration writes or error clearing.

## Changes to the existing health command

The command is unchanged. For every selected unit:

1. Send one IDENTIFY 04 extended diagnostic-summary read.
2. Accept a valid twelve-byte summary as before; finish that unit without another read.
3. If no usable summary arrives within three seconds, send one different read:
   IDENTIFY 07 network voltage. This fallback uses the same worker, three-second
   global read budget and lighting/ramp priority. It may wait if higher-priority
   work is active.
4. Accept only the documented five ASCII bytes `NN.NV`, exact count, checksum,
   local source/destination and current attribute/window. Otherwise finish unknown
   after the second three-second window. Neither read is retried automatically.

The voltage read does not provide error flags, learn mode or voltage warning flags.
Those fields are absent unless the full summary actually replied. No flags are
synthesized from a voltage value or from an unanswered query.

## First trial after normal deployment acceptance

Listen to `cbus/bridge/inventory`, then use HA Tools → Actions:

```yaml
action: mqtt.publish
data:
  topic: cbus/bridge/command
  payload: '{"inventory":{"health":[2,100,130]}}'
  qos: 0
  retain: false
```

These are known physical unit addresses: the site's CNI, a keypad, and light sensor.
The trial should take roughly 10–20 seconds if both simple units need fallback,
plus any waits for normal lighting work. This is not proof the simple units can
answer the voltage query. Share the final JSON, including any reads.*.reply_hex.
Check normal HA/physical controls and fades before and during the trial.
No HA restart, entity changes or Recorder changes are needed.

## Results

The retained inventory keeps its existing schema and topic. Each health entry adds:

- `reads.summary` and (if sent) `reads.voltage`: outcome, sent_ts, completed_ts, and
  observed_ts only on a decoded reply. A queued fallback leaves the unit pending.
- `voltage_source`: identify_04 or identify_07, only when voltage was decoded.
- `diagnostics_available`: true for an accepted summary; false for a completed
  voltage-only/unknown result. This reports availability in this attempt, not a
  permanent hardware-capability classification. On an interrupted/pending job it
  can be absent; treat it as unknown.

Top-level health.outcome becomes:

| Outcome | Meaning |
|---|---|
| reply | Full summary decoded; voltage and diagnostic flags available. |
| voltage_only | Fallback voltage decoded; diagnostic flags remain unknown. |
| no_reply | Neither read produced an accepted reply or matching alternative. This does not prove no bytes arrived or the unit is offline. |
| unrecognized_reply | No usable reading, but at least one matching, validated CAL envelope carried an unfamiliar data format. |
| pending / cancellation reason | Job progress or interruption, as before. |

Existing identity outcome `not_requested` remains normal for health-only rows.
Top-level sent_ts records the first read attempt; observed_ts and completed_ts refer
to the accepted reading/final completion. Per-read timestamps show both stages.
A timeout never creates zero voltage, an empty error list or an observation time.
A new survey still removes prior selected-unit readings, so old flags cannot be
mistaken for fresh data when only voltage is available. Other rows retain their
own historical scan IDs/timestamps. Cache is process-local and resets on deployment.

## Alternative reply evidence

Only a checksum-valid local long CAL envelope addressed to this CNI, matching the
pending unit, attribute (04 or 07), exact CAL byte count and active three-second
window can be captured. Keep only the first alternative per read, at most 18 data
bytes, as reads.*.reply_hex plus alternative_observed_ts. Duplicates cannot extend
the timeout, consume the read, grow the cache or force inventory republishing.
A later valid reply can still complete the same read successfully.

Wrong addressing, routing, checksum/length errors, unrelated attributes and late or
unsolicited replies are not saved as evidence for the unit. Existing general frame
diagnostics still handle them. Captured bytes never become voltage/health values
or light state. No arbitrary parameter reads, writes, RESET, clears or new sockets
are introduced. The 255-row limit, <=16 explicit batches, admission/cooldown,
overall deadlines, ramp pause and disconnect/no-resume rules remain unchanged.
A full 255-unit health survey needs at most 510 sends if every summary is silent;
its existing one-hour deadline still bounds starvation.

## Protocol basis and limits

Clipsal CBUS-SIUG issue 1.17 section 7.2, page 43 describes IDENTIFY 07 network
voltage as two whole-volt ASCII digits, a decimal point, one decimal digit and V.
Unit-3 request is `0603002107CF`; the local long reply is
`86 <unit> <CNI> 00 86 07 <5 ASCII bytes> <checksum>`.

Manufacturer-authored guide (mirror):
https://manualzz.com/doc/4150831/clipsal-serial-interface-user-guides

This is a compatibility trial until installed hardware replies are reviewed.
Missing replies do not prove lack of support; captured alternatives do not assign
new meanings to undocumented formats. Latched flags remain historical and cannot
establish an error rate. The bridge deliberately does not clear unit 12's TX flag.
