# HA transition durations — Study group 13 trial (Release 12)

The existing percentage dimmer profile and read/write topics are unchanged. An
additional strict JSON format is accepted only for Study (local 254/56/13):

    {"level":50,"transition_seconds":20}
    {"restore":true,"transition_seconds":20}

Level is an integer 0–100 for this installation. Duration is a finite number
0–3600 seconds. restore=true selects the existing remembered nonzero raw level,
falling back to full brightness when none is known. OFF is level=0. Unknown fields,
duplicates, booleans used as numbers, NaN, infinity and out-of-range values reject.
Existing {level,ramp_seconds} and {stop:true} payloads retain their exact meaning
and remain available for other configured dimmers. No other group gains duration
support in this trial. Fans, relays, binary sensors and ordinary numeric commands
are unchanged. Tracking must be enabled; readonly blocks the new path too.

## Duration selection

For each nonzero requested duration, obtain a fresh numeric status for block 0–31
through the existing command worker/CNI connection before sending a lighting command.
Never use HA's displayed state or remembered ON brightness as the starting level.
Queries share the global three-second automatic block-send limit. An unsent
background reservation may be superseded; its queued stale callback cannot send.
No new periodic polling is added. The independent reader handles the reply.

The budget/response wait is bounded to four seconds in total, with a maximum
three-second response wait and existing queue/session lease checks before sends.
A command can therefore be rejected when the bus budget/queue is busy or no usable
numeric level arrives. It is not retried/replayed. Absence/invalid data cannot
supply the starting level. Already running hardware ramps are not cancelled by
rejection. As with existing readback, the protocol has no transaction ID: a
coincident valid numeric report is usable fresh evidence, not proof of attribution.

Given start and target raw values, each native rate's nominal travel time is:

    native_full_scale_seconds × abs(target - start) / 255

Choose the rate with the smallest absolute error from the requested duration;
ties choose the faster rate. Same-level or zero-duration commands use instantaneous
RAMP. For 255→128 over 20 seconds, select the 40-second full-scale rate (nominal
19.922 seconds). Requests outside the hardware's achievable partial-travel durations
select the closest endpoint; a small brightness change cannot necessarily last the
requested time. This is an approximation, not a software-generated exact fade.
Zero-duration commands do not query a starting level.

The initial query adds a small preflight delay before the fade starts. A busy
worker can delay other queued actions while waiting (bounded above); it does not
block MQTT callbacks or the CNI reader. Three-second progress sampling remains.

On transmission success, existing group last_transition metadata includes
requested_duration_s, start_reported_level and nominal_duration_s alongside the
selected full_scale_seconds. The activity event duration_transition_selected and
INFO log expose the selected rate. New duration counters distinguish fresh-level
requests, unavailable levels and successful transmissions. These do not assert
physical lamp brightness. No target-as-current feedback is added.

## Proposed HA change — requires separate approval

Replace only Study's existing default-schema MQTT entry with
examples/homeassistant/study_transition_light.yaml's entry. Preserve unique_id
study_lights_001 and entity light.study_lights; do not create a second entity.
The template schema forwards HA transition seconds and converts HA brightness
0–255 into the bridge's existing 0–100 scale. Without transition it sends zero.
Bare ON restores the remembered ON level. State and brightness come from the
existing numeric level topic; no optimistic target feedback. No new entity or
Recorder buffer is introduced.

Remove the old on_command_type, brightness_* topic/scale and state_topic entries
for that ONE HA light. Do not inherit cbus_defaults because its payload_on/off
fields belong to the old schema; the proposal lists availability explicitly.
All other entities, mappings and automations remain untouched.

The bridge's /opt/cbus_bridge/ha_mqtt.yaml remains its existing validated default-
schema runtime profile file. This HA-only template example is not a replacement
bridge profile: the profile loader does not support template-schema commands on
command_topic. Do not copy the full live configuration or this example into that
runtime file. The existing /13/ramp topic is already authorized by its brightness
command profile, so bridge subscription/permission changes are unnecessary.

After bridge deployment and approval: back up/hash current configuration.yaml,
replace only the Study block, check HA configuration, reload manually configured
MQTT entities, and verify entity identity/state and ordinary controls first.

Example HA action:

    action: light.turn_on
    target:
      entity_id: light.study_lights
    data:
      brightness_pct: 50
      transition: 20

Use light.turn_off with transition:20 to fade off. To stop, the existing bridge
{stop:true} MQTT command remains available; HA light actions have no native STOP.

## Scheduler proposal — not activated

The installed scheduler-card source has no built-in transition action field.
Merge examples/homeassistant/study_transition_scheduler.yaml into the existing
card's customize section. It adds brightness_pct and transition variables for
Study turn_on and transition for turn_off. Existing schedules are not rewritten:
choose the new action/fields explicitly for the desired schedule. Validate the
installed card's editor after activation; the proposal has not been tested live.

## Acceptance / rollback

Pause conflicting Study motion/scheduled actions deliberately for the test, recording
their prior states; don't leave them disabled afterward. Note initial brightness.
Check immediate UI controls and physical switches, 100→50 over ~20 seconds, 50→0
over ~20 seconds (native40 rate), bare ON with transition, and no-transition ON/OFF.
Observe actual native behavior and HA convergence; restore the initial brightness.
Check fan responsiveness, query budget, queues, ACK outcomes and missing replies.

Keep the bridge installer's backup/rollback command and exact before-copy/hash of
Study's HA configuration. Restore the old HA Study entry and reload MQTT before
rolling back to Release 11, which rejects the new duration JSON. If HA validation
fails, restore the original file immediately. Never overwrite intervening user edits.

References:
- https://www.home-assistant.io/integrations/light.mqtt/ (template schema)
- https://github.com/home-assistant/core/blob/dev/homeassistant/components/mqtt/light/schema_template.py
- https://github.com/nielsfaber/scheduler-card (custom actions and numeric variables)
- docs/RAMP-CONTROL.md (existing native rate semantics)
