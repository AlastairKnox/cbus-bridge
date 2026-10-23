# On-demand device identity trial (Release 16; retained in Release 17)

Release 17 also adds presence discovery and an explicit full-address sweep; see
[Device discovery](DEVICE-DISCOVERY.md). The commands below still select small
identity trials; their 16-address limit remains unchanged.

This release adds local unicast IDENTIFY reads for manufacturer (attribute 0),
unit type (1), and firmware (2). It does not query voltage/errors yet, write device
parameters, clear errors, or perform an automatic startup/background scan. Existing
lighting, MONITOR, numeric feedback, endpoints and credentials are unchanged.

## First trial: the CNI itself

Deploy and accept normal lighting control first. In Home Assistant, open the MQTT
integration's **Configure → Listen to a topic** and listen to:

```
cbus/bridge/inventory
```

The inventory is retained QoS1 JSON. The bridge learns its local CNI unit address
from a validated long-form initialization acknowledgement. Wait for interface_unit
to become a number. If it stays null, do not guess an address or change interface
options: save the inventory output and report interface_address_unknown.

In HA **Tools → Actions**, use YAML:

```yaml
action: mqtt.publish
data:
  topic: cbus/bridge/command
  payload: '{"inventory":"interface"}'
  qos: 0
  retain: false
```

This queries only the learned CNI unit address. Expect three identity reads taking
roughly 6–12 seconds when lighting/readback work is quiet. Check job.state becomes
complete and the device row contains manufacturer/type/firmware. Preserve the full
JSON output for protocol review; these are device identifiers, not credentials.
A valid response is proof of this query/reply path, not proof all devices support it.

## Other units and discovery

The passive observed_units list contains unit addresses seen originating local
lighting commands. These are **physical C-Bus unit addresses**, not lighting group
numbers (Study group 13 and Upstairs group 72 are not device-address instructions).
Output units which only act on commands might not appear in this list. Toolkit's
unit inventory is a better source for known output-unit addresses.

To survey the first 16 passively observed addresses:

```yaml
action: mqtt.publish
data:
  topic: cbus/bridge/command
  payload: '{"inventory":"observed"}'
  qos: 0
  retain: false
```

Or specify 1–16 known addresses explicitly. The following numbers are illustrative;
replace them with physical unit addresses from your installation:

```yaml
action: mqtt.publish
data:
  topic: cbus/bridge/command
  payload: '{"inventory":{"units":[3,4,5]}}'
  qos: 0
  retain: false
```

A deliberately chosen small address range can discover responders: supply its
individual addresses as a list. There is no implicit whole-network scan. Review
one small trial before surveying additional batches. Address 255, arbitrary CAL
commands, extra options, noninteger addresses and batches over 16 are rejected.
Repeated addresses in a valid batch are deduplicated. An observed trial with no
observed addresses is rejected. Check last_request_error in the inventory JSON.

To stop a survey without changing lighting:

```yaml
action: mqtt.publish
data:
  topic: cbus/bridge/command
  payload: '{"inventory":"cancel"}'
  qos: 0
  retain: false
```

## Results and limits

- job.state: idle, running, complete, cancelled, connection_lost, send_failed or
  survey_deadline. complete means the requested survey finished, not every read
  succeeded. last_request_error describes a rejected request; it is cleared when
  a new survey starts successfully.
- Each device row has unit, scan_id, attempt_ts, outcome, supported identity fields
  and per-attribute reply/no_reply outcomes with local observed_ts timestamps.
- identified = all three identity replies; partial = type replied but one of the
  remaining identity reads did not; no_reply = the initial type query timed out.
  No reply does not establish absence, an offline unit, or a hardware fault.
- Identity is queried first; silent units are not given the other two reads.
  A request has a three-second reply window and is not retried automatically.
- At most one query is outstanding. Sends share the existing automatic readback
  budget: at least three seconds apart. Inventory yields to queued/active work,
  pauses while any ramp watch exists, and does not wait for replies on the command
  worker. The entire survey expires after ten minutes if starved.
- A new survey is blocked while one is running, and for 30 seconds after completion
  or cancellation. Queued work and pending replies are invalidated on either link
  loss; reconnect never restarts the survey. Send failures abort it.
- Matching requires unit, CNI destination, attribute, checksum, exact length, ASCII
  content and an outstanding response window. Routed and short-form identity replies
  remain unsupported. CAL has no transaction ID: the cooldown reduces ambiguity,
  but a very delayed same-key reply cannot be distinguished absolutely from a new
  one. This trial does not assume fresh physical measurements from identity text.
- Results are bounded by the 255 possible local unit addresses and are held only
  for the bridge process lifetime. A new survey replaces the selected rows; other
  rows remain historical with their own scan IDs/timestamps. bridge_start_ts scopes
  the cache to a process. Restart publishes an empty fresh cache; rollback to an
  older bridge may leave this retained MQTT topic with historical results.

No HA entities are created automatically, so the inventory topic itself adds no
Recorder sensor history. Per-device HA sensors and voltage/health polling are later
releases. No additional CNI client, direct socket tool or broker credential change
is needed for the trial. The existing rollback helper restores the prior bridge.

## Protocol basis

Clipsal CBUS-SIUG issue 1.17: §§4.2.9.1, 7.1, 7.2 and 9.2. The documented local
firmware IDENTIFY example is `0603002102D4`; the request builder is tested against
that exact vector. Long replies retain source and CNI destination; the standard
REPLY byte count includes the attribute and its data. Only the three documented
eight-byte ASCII identity attributes are enabled here.

Manufacturer-authored guide (mirror):
https://manualzz.com/doc/4150831/clipsal-serial-interface-user-guides
