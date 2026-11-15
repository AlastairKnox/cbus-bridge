# Release 25 — accept manual error clears during diagnostic cooldown

Release 24 rejected a manual clear unless the shared inventory manager was idle
and beyond its 30-second cooldown. With automatic health reads every roughly 43
seconds on this installation, the dashboard left users racing a small window.
A rejected request also left the last admitted audit as `never_requested`.

A confirmed request now durably consumes its ID and enters one bounded **waiting**
slot. The current inventory job can finish, then the request takes the next slot
after cooldown, ahead of new automatic monitoring. It waits at most 60 seconds;
the existing fresh-preflight/single-clear/fresh-readback transaction then has its
own 60-second deadline. Lighting and ramp priority and three-second management
pacing are unchanged. No retry of an uncertain clear is introduced.

Only one clear may wait/run. Further requests reject without replacing it.
Timeout, disconnect, explicit inventory cancel and restart discard a waiting
intent. Persisted consumed IDs prevent replay after restart. A restart records an
interrupted-before-send outcome; it never resumes an old intent.

The monitor summary gains an explicit latest-request state/rejection reason.
Accepted progress triggers prompt publication instead of waiting only for the
30-second heartbeat. The HA generator adds a **Last clear request** scalar sensor
and card row, preserves confirmation, and retains the fixed 32px icon size.
Historical per-unit results stay separate from a newly rejected request.

## Verification

- 381 unit tests pass, including 11 new waiting/admission, timeout, cancellation,
  restart/replay, storage-failure, publication and monitoring-priority tests.
- Full-process localhost simulation recreates the actual failure: complete a
  health read, immediately submit a clear during cooldown, observe durable waiting,
  then exactly one paced clear with zero-flags readback after the cooldown.
  Ordinary lighting remains responsive while the clear waits. Returning TX flags
  are new after restart, and no clear is replayed.
- DLT-label and virtual-stereo localhost regression results are recorded with
  the release preparation artifacts; no live device is cleared during preparation.

Existing audit path/environment and reviewed roster are retained. Use the normal
release check/apply backup process. Add the one HA status sensor/row separately;
no fresh clear request is needed until after deployment and acceptance. Preserve
the newest audit before rollback. Old Release 24 safely disables clearing if it
encounters a new waiting-state audit it cannot interpret; do not delete the audit.

See [manual clearing](../docs/DEVICE-ERROR-CLEARING.md).
