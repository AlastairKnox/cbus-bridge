# Explicit virtual control groups

Default profiles describe real C-Bus loads. Release 22 adds a separate opt-in for
command-only groups that have no physical output to answer level requests.
Nothing is inferred from absence or a timeout. For this site's eDLT stereo group:

```
--virtual-control 254/56/250@110
```

The service launcher accepts the same single specification through
`CBUS_VIRTUAL_CONTROL=254/56/250@110`. The bare CLI permits repeated specifications.
The loader validates the configured writable percentage/dimmer profile and the
local 254/56 address before connecting. Source unit 110 is the only accepted wall
controller for this opt-in. ACK-successful MQTT commands for the configured group
also work; unsolicited echoes from other units cannot impersonate the eDLT.

## Root cause

With ramp tracking enabled, observed percentage-profile SAL commands enter
`_begin_ramp` and deliberately skip publishing an endpoint as a measured level.
`_publish_level` also suppresses ACK estimates for physical dimmers. Numeric
status reports supply current brightness instead. Group 250 reports absent, so
there is no physical level to release to MQTT. Existing writable/read-only/fan
profiles do not represent this control-only use case.

The virtual handler runs before that physical path and is excluded from the
physical ramp tracker. Physical STATUS and binary reports cannot overwrite its
logical control level. Other groups retain their existing reporting behavior.
Diagnostics identify `profile_kind=virtual_control`,
`state_semantics=virtual_command_model`, `confidence=virtual_control` and the
allowed source unit. A virtual level never sets `session_level_seen` by itself.

## Level and event semantics

- ON and OFF establish logical levels 255 and 0. Immediate ramps establish their
  explicit level. The HA percentage topic uses the profile's configured scale.
- A timed ramp with a known starting level is estimated using the documented
  nominal **full-scale** rate (255 levels per ramp period), not a guaranteed
  endpoint duration. The existing quarter-second metrics loop advances it; no new
  timer, thread, socket, poll or bus write is created.
- STOP samples/fixes the current model estimate. An unknown starting level stays
  unknown during a timed ramp and after STOP; it is never guessed as 100% or OFF.
- Adjacent identical commands within one second are suppressed. Identical ON,
  OFF and immediate levels do not retrigger unchanged actions. Repeated identical
  active ramps do not restart the ramp clock. A terminal command supersedes its
  ramp immediately.

The existing `cbus/read/254/56/250/level` carries **retained display state** at QoS
0. It is not an automation trigger in the accompanying MA example. The per-group
`.../control_availability` is retained and goes offline on startup, disconnect or
reset until a fresh known control level is published. Combine it with the bridge
availability in this light's definition; expand its YAML defaults explicitly so
an inherited `availability_topic` does not conflict with the availability list.

Fresh actions use `cbus/bridge/controls/254/56/250/event`, **non-retained, QoS 0**.
There is no offline replay queue. Fields include `schema`, `session`, `sequence`,
`ts`, `action`, `net`, `app`, `group`, `unit`, `command`, `level`, `percent`,
`origin`, `semantics`, `estimated`, `ramping` and `physical_confirmed=false`.
`action` is on/off/level/stop or unknown_ramp. A positive ramp first becoming known
is an on action; a STOP can establish that first positive value before a tick.
`origin` distinguishes `observed_command` from `transmitted_command`.

Every connection session starts unknown and invalidates old ramp state. No
remembered logical level is restored or used to start music. Retained display
state can repopulate the UI after an HA restart without becoming an action.

C-Bus lighting SAL has no timestamp or unique transaction ID. A delayed replay
indistinguishable from a fresh wall-switch command cannot be perfectly detected.
The bridge consumes live CNI packets only, never retained diagnostic event lists.
Source filtering, bounded duplicate suppression, session reset and non-retained
MQTT events address ordinary retry/reconnect duplication without claiming an
impossible guarantee against arbitrary bus replays.

Fixed-rate JSON ramp commands and STOP work for virtual groups. Nonzero requested
`transition_seconds` durations are explicitly rejected, since the physical fresh-
level lookup is inappropriate here; no background physical query is attempted.

## Music Assistant example

See `examples/homeassistant/cbus_stereo_controls_package.yaml` for the site-specific
Big Room/Yamaha Zone 2 example. Adjust entity IDs before reuse elsewhere. It uses
fresh events, not `light.stereo` state/brightness or diagnostic history, to avoid
startup replay. A persisted session/sequence/timestamp key rejects duplicate,
out-of-order and old MQTT events; events older than ten seconds are ignored.

A fresh ON first sends a non-retained numeric `50` to the group's ramp topic.
The bridge's ACK-success feedback changes the virtual level from 100% to 50% and
emits a **level** event, not another ON. The eDLT can receive that group-level
command; its actual display/indicator behavior requires physical acceptance.
Startup waits for a newer accepted control event, then powers the players and
sets MA volume to 0.5 before playing Favourites, enabling shuffle and repeat.
A more recent deliberate volume request during startup wins instead. OFF cancels
startup, stops/powers off MA, then explicitly powers off the physical Yamaha zone.

Wall percentage maps directly to player percentage, with a 0.12 floor and the
existing 0.5 normal-control ceiling: 50% on the eDLT means 50% player volume.
Positions above 50% do not raise volume beyond that ceiling. This differs from
the earlier draft's half-scale mapping (which made wall 50% equal player 25%).
There is exactly one startup level write; its ACK/observed echo does not replay
ON, restart the playlist or write another level back to C-Bus.
An independent hard cap remains 0.65 on both MA and its physical output.

The three old on/dimmer/off automations must be disabled when enabling this
replacement. Their original definitions/IDs may be retained with
`initial_state: false`. The old hard-cap automation is extended rather than
removed. The included helper states/script are excluded from Recorder. The only reverse C-Bus write is the explicit one-time 50% startup command;
continuous player-state-to-C-Bus synchronization is not added.

After HA restart, control-active starts false: no music restarts automatically.
If the bridge's logical control was already ON, use OFF then ON to explicitly
resume wall control. Volume/STOP alone cannot unexpectedly start a player.
Full live HA configuration validation remains necessary before reload.

References: [HA Music Assistant](https://www.home-assistant.io/integrations/music_assistant),
[HA media-player actions](https://www.home-assistant.io/integrations/media_player).
