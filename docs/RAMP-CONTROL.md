Ordinary numeric brightness commands and dimmer ON now use the immediate native
rate across all configured dimmer groups (Release 9). Comma suffixes remain
ignored. Explicit JSON ramp durations, physical wall-switch ramps and fan
commands retain their existing behavior. Numeric readback still confirms dimmer
state; an immediate command does not promise an immediate HA update.

# Explicit native dimmer ramps (Release 6)

Release 8 optionally adds automatic numeric readback and state priority; see
[RAMP-READBACK.md](RAMP-READBACK.md). The original Release 6 behavior below
applies when that feature is disabled.

Existing light ON/OFF, brightness sliders, numeric/comma payloads and fan presets
keep their previous behavior. This addition is opt-in JSON on an existing,
configured dimmer brightness command topic:

```
cbus/write/254/56/<configured dimmer group>/ramp
```

Send non-retained JSON, for example:

```json
{"level": 50, "ramp_seconds": 12}
```

`level` is an integer in the profile's brightness scale (0–100 for this site's
20 configured dimmers). `ramp_seconds` must be one of:

```
0, 4, 8, 12, 20, 30, 40, 60, 90, 120, 180, 300, 420, 600, 900, 1020
```

These are hardware rates: the nominal time for a complete 0–100% change, rather
than an exact fade duration from the present level. A 12-second rate starting at
25% and targeting 75% takes about six seconds. Zero selects instantaneous RAMP.
A target level of zero with a nonzero rate fades down rather than sending OFF.
The hardware runs the fade; the bridge sends one command and no timed steps.

To terminate a ramp at its present hardware level, send:

```json
{"stop": true}
```

Stop uses the same bounded FIFO as other commands. It does not jump ahead of
queued work, is not a guaranteed immediate stop and must not be used as an
emergency control. The queue's five-second expiry applies to sending a command,
not to completion of a hardware fade lasting several minutes. A bridge/MQTT
restart or disconnect cannot undo a command already delivered to the hardware;
a ramp already running there may continue. The bridge never replays it.

## State and diagnostics

A successful ACK proves transmission, not ramp completion or physical brightness.
Structured ramps, including instantaneous ones, and stop do not publish invented
brightness readbacks. Subsequent monitored SAL targets remain `observed_command`;
status replies remain `reported_level`. A monitored target is not proof that a
fade has finished. HA may keep its previous state until new bus/status data arrives.

Each configured group's existing snapshot can include `last_transition`, recording
transmission timestamp, target raw level, full-scale seconds and
`target_transmitted` or `stop_transmitted`. This is history, not an active-ramp
tracker. Stop records no target. It survives reconnect with its original timestamp
and resets on process restart. Successful local stop and observed stop also cancel
pending synthetic dimmer updates; neither fabricates a stopped brightness.

Counters distinguish `transition_requests_total`, `transition_rejected_total`,
`transition_transmitted_total`, `transition_stop_transmitted_total` and
`transition_stops_observed_total`. Existing command ACK/timeouts/errors remain in
force; the existing live traffic view shows command opcode and target. A normal
resync can obtain a later status sample, but this release adds no automatic polling.

Malformed JSON, duplicate/unknown fields, bool/string/fractional numbers,
unsupported rates and out-of-range levels are rejected. Structured commands are
restricted to configured percentage dimmers with an enabled brightness command
topic. They do not enable fan, relay or binary-sensor writes. Retained commands,
read-only mode, size limits, queue expiry and connection-generation checks still
apply. Legacy comma suffixes remain ignored, so `50,34` keeps its existing fixed
rate and is not a new rate selector. Use JSON for explicit control.

## Home Assistant use

No live HA changes are required to use `mqtt.publish` from an automation. Example
action (replace group 1 with a verified configured dimmer before using it):

```yaml
action: mqtt.publish
data:
  topic: cbus/write/254/56/1/ramp
  payload: '{"level":50,"ramp_seconds":12}'
  qos: 0
  retain: false
```

The optional `examples/homeassistant/cbus_ramp_scripts_package.yaml` proposes two
reusable HA script actions with selectors and input checks: `script.cbus_ramp_light`
(group, level, ramp_seconds) and `script.cbus_stop_ramp` (group). The example uses
this site's 0–100 brightness scale. It does not install itself or run automatically.
Installing it requires separate approval and HA validation/reload. Existing MQTT
lights remain unchanged; `light.turn_on` with `transition` is not automatically
translated by these existing MQTT light definitions. No new entities or discovery
messages are sent by the bridge.

## Acceptance after deliberate bridge deployment

First confirm ordinary light, dimmer and fan controls and physical switches. Then
choose one noncritical configured dimmer: try a known rate toward a moderate level,
fade down to zero, instantaneous ramp, and stop during a longer ramp. Check physical
behavior, ACK outcomes, no fabricated stopped level, normal HA convergence and no
queue expiry/drops. Test one light at a time; record its initial level and restore
it afterward. No real hardware commands were sent during candidate development.

## References

- Clipsal CBUS-QS 2.0, section 6.3 (ramp-rate table and framing):
  https://ckm-content.se.com/ckmContent/sfc/servlet.shepherd/document/download/0698V00000kHfpkQAC
- Clipsal CBUS-SIUG 1.17, section 6 (TERMINATE RAMP 0x09; manufacturer text mirrored):
  https://manualzz.com/doc/4150831/clipsal-serial-interface-user-guides
- Home Assistant MQTT publish and script actions:
  https://www.home-assistant.io/integrations/mqtt/
  https://www.home-assistant.io/integrations/script/
