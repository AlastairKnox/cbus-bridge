# Release 5 candidate: clearer coverage and bounded command handling

Baseline runtime: Release 4, dc60f743. Preparation does not deploy this candidate.

## Diagnostics

An explicit absent or invalid-encoded status reply now clears the unanswered
request count. Separate current configured-group counts report absent and invalid
status. Neither absence nor a lack of observed traffic fabricates an OFF state.
The group quality is `awaiting_reply` while a request is outstanding. Historical
readings retain their timestamps and confidence; reconnect resets current coverage.

The nine previously reported absent groups (65, 73, 90, 99–103 and 250) require
mapping/output-unit confirmation. Eight are configured as lights and one as a
binary sensor. Profile type alone does not establish that absence is expected;
this release does not change or suppress any mapping.

Unsupported traffic remains informational. Diagnostics now include up to 16
unique unsupported-frame samples with counts, first/last timestamps, byte length,
header and application where recognizable. Only 80 bytes per sample are exposed;
full-frame hashes distinguish frames with identical prefixes. Oldest distinct
samples are evicted. Samples are in memory, reset on process restart and contain
bus traffic rather than MQTT credentials or command payloads. They are carried in
`diagnostics.unsupported_samples` on the existing status/diagnostics topic.
No extra bus polling or connection is introduced.

## Command handling

MQTT callbacks validate retained/topic/size/read-only policy and enqueue immutable
commands. One worker serializes device commands and subsequent manual/periodic
resyncs. The reader continues processing ACKs and bus events independently.
Initialization and initial status requests finish before worker readiness; the
reader drains initial replies to its first receive timeout before accepting work.

The FIFO holds at most 32 waiting operations, plus one active operation. Work has
five seconds from receipt to each send; expired work is discarded. A full or busy
queue rejects new work without blocking the MQTT callback or replacing older work.
An accepted enqueue is not proof that a command was sent or acknowledged.

Either link disconnect discards waiting work, invalidates active work, cancels ACK
waiters and delayed synthetic updates, and closes the old CNI socket. Reconnection
starts a fresh session; old commands are never replayed. A missing command ACK also
closes the session and discards waiting work rather than retrying an uncertain
operation. A command already sent may have executed even if its ACK was lost.
Positive ACKs still gate optimistic state; they do not prove physical feedback.

Status adds `command_queue` (depth, capacity, high-water mark, active, ready, TTL).
Diagnostics count accepted/completed operations, readiness/full/busy rejections,
expiry/session discards, disconnect discards and operation failures. Completed
means the handler returned, not necessarily a positive command ACK. Counters reset
on process restart. Queue changes are visible at the next normal metrics publish.

## HA proposal and alerts

The example package renames the pending sensor and adds six sensors for explicit
absence/invalid coverage, queue depth, queue-full, expiry and disconnect discards.
Existing entity IDs are preserved. These are proposals; the bridge deployment
helper does not install HA files. Live HA configuration needs separate approval.

Collect a baseline before enabling notifications. Prioritize sustained bridge
unavailability, repeated command timeouts/negative ACKs, newly observed checksum or
encoding errors and queue drops during ordinary use. Unsupported frames and quiet
or absent groups alone are informational. Do not interpret these observations as
a complete electrical health measurement of the C-Bus network.

## Validation and rollout

Offline tests cover bounded FIFO order, expiry before each send, callbacks during
ACK waits, disconnect/reconnect invalidation, cancellation wakeup, no replay after
ACK timeout, startup readiness, optimistic-publish suppression, status coverage,
and bounded unsupported samples. Existing parser, transport, read-only, retained
command and deployment/rollback tests remain required.

Use the existing exact-commit `tools/release.sh check COMMIT` then `apply COMMIT`
workflow on cgate. The installer makes and verifies a restore copy before changing
the service. Preserve its printed backup path and rollback command. Recovery has
been tested offline but cannot be guaranteed against every storage/host failure.
After deployment, test HA lights/dimming/fans and physical switches; check queue
rejections/expiry, ACK outcomes, CNI connection and diagnostics. A controlled
reconnect test should be separately agreed because it briefly interrupts service.
Do not deploy this candidate merely to collect unsupported samples while Release 4
is soaking without deciding to end that soak period.
