# Release 8 candidate: numeric ramp feedback transparent to HA

Baseline: Release 7, e087ea8. Not deployed by candidate preparation.

Track native dimmer ramps/stop from MQTT and physical wall-switch commands. Request
only affected 32-group level-status blocks via the existing command worker;
coalesce shared blocks. Service default RAMP_READBACK_EVERY=10, configurable 0
(disabled) or 10–60 seconds. Global automatic budget: one block per five seconds.
No additional CNI connection, dependencies or runtime-file layout change.

Binary MONITOR reports remain diagnostic and useful for other groups, but cannot
override numeric state during a tracked dimmer operation or after valid numeric
evidence establishes authority for that dimmer in the current connection session. ACK/SAL dimmer targets
are not published as present brightness while tracking is enabled. This also
changes ordinary dimmer controls to wait briefly for numeric readback; fans and
relays retain their behavior. HA topics/entities and live YAML are unchanged.

Two separate numeric samples confirm target completion or a stable stopped level.
Old tracking requests are invalidated by replacement commands or disconnects.
Missing usable replies stop polling after three attempts; protection expires at a
bounded deadline. Queue reservations, active groups, polling and retries are bounded.
No automatic lighting-command retry or physical-brightness estimation is added.
Intermediate samples preserve remembered ON brightness; confirmed stops update it.
Positive raw numeric levels cannot round into a false OFF.

Regression tests replay the Study evidence (84%, 51%, 18%, 0% while binary status
says OFF), test actual targeted wire frames, reader integration, final/stop samples,
wall-switch events, ACK failures, queue expiry/rejection, stale generations, coalescing,
global send spacing, missing/invalid data, readonly mode and unaffected fan behavior.
The full earlier regression suite remains required. See docs/RAMP-READBACK.md for
limits and physical acceptance. Offline tests do not replace hardware acceptance.

Existing live raw diagnostic Recorder exclusions remain applicable. No new HA
entities or database configuration are installed. Keep the deployment's printed
backup and rollback command. Use Study group 13 with motion automations paused for
the controlled trial, then restore its original level. Production remains on
Release 7 until deliberate deployment.
