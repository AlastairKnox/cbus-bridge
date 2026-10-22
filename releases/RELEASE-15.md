# Release 15: overlapping fades without duration_busy

Baseline: 7a4aa1e (Release 14). No HA, dependency, service-unit, credentials or
installer runtime-layout changes.

## Behavior

- Replace the single-duration admission gate with a bounded queue: at most eight
  duration requests including the active acquisition, within the existing total
  queue capacity of 32. Requests for different groups can be submitted together.
- One worker still prepares and transmits commands serially. It does not wait for
  physical fades to finish. Fresh starting-level queries retain the global minimum
  three-second query spacing, so several duration fades can start a few seconds
  apart. This is overlapping fade support, not synchronized scene activation.
- Duration requests have a 60-second admission-to-send deadline; ordinary controls
  retain the five-second deadline. Neither deadline is renewed on deferral.
  Excess load reports duration_queue_full; expired commands report command_expired.
  These failures include application/group where available and a warning log.
- Unsent same-group commands coalesce to the latest request. A newer same-group
  command also cancels an older fade still acquiring its starting level. Commands
  already transmitted are never replayed or interrupted mid-acknowledgement.
- Immediate UI requests containing transition_seconds: 0 or ramp_seconds: 0 use
  ordinary-control priority and do not occupy the duration queue. Payload validation
  remains strict; malformed values cannot bypass bridge validation.
- An acquiring fade yields to ordinary controls. Unless superseded, disconnected,
  expired or the total queue has filled, it resumes afterward with a fresh level
  query. Deferral increments duration_deferred_total rather than command failures.
- Fades have priority over background queries. Interactive admission can displace
  queued background work, but does not silently evict a queued fade. Link loss
  discards queued work; nothing is replayed automatically after recovery.
- The status command_queue object adds duration_pending, duration_capacity and
  duration_ttl_s. Existing dashboard sensors remain compatible.

## Validation

218 offline regression tests pass, including eight simultaneous requests beyond
five seconds of aggregate preparation time, admission limits with an active fade,
same-group replacement, preflight cancellation, deferral/resumption, bounded queue
saturation, original-deadline expiry and disconnect discard. Mock CNI ACKs verify
both Study (13) and Upstairs (72) receive their requested fade commands.

The separate tests/integration_release15.py uses a real bridge subprocess and
fake MQTT/CNI servers bound only to localhost. Two different-group duration requests
arrive together; a zero-time UI request interrupts the second acquisition, executes
promptly, and the fade resumes. Both fade commands are acknowledged without waiting
for their physical durations, with zero reported command failures. This checks
transport/command handling, not real physical fades or HA automations.

## Deployment and acceptance

Use tools/release.sh check/apply with the exact release commit and retain the
printed backup plus /home/ack/cbus-bridge-rollback.sh. No HA restart/reload is needed.
After deployment check normal HA/wall-switch control, then submit Study and Upstairs
fades together at a suitable time. Confirm both run, feedback follows, and final
levels/OFF are correct. Try an ordinary control for another load during acquisition.
Do not intentionally run competing controls or Lux automation against a test fade.
Check the command failure count does not increase. A historical last failure can
remain visible until replaced; use its timestamp/count to distinguish new failures.
