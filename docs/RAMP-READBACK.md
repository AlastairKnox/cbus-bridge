# Automatic dimmer ramp readback (Release 8)

Release 8 coordinates native ramps, numeric status and passive binary reports
inside the bridge. Existing HA entity IDs and MQTT topics remain unchanged. No HA
resync automation, new CNI connection, software fade loop or dependency is needed.

## Evidence and purpose

The authorized Study group 13 test on 21 October 2026 returned numeric raw levels
214, 130, 47 and 0 (84%, 51%, 18%, 0%) at roughly 10, 30, 50 and 65 seconds of a
60-second full-scale fade. Passive binary OFF arrived near the beginning and
repeatedly overrode numeric ON. A subsequent stop held at raw 60 (24%) across
separate readbacks. These are reported network group values, not independent
light-output measurements. Release 8 uses the numeric evidence to update HA and
protects it from conflicting binary state during a known ramp.

Release 11 shortens normal polling and the global automatic minimum to three
seconds. Explicit environment overrides are preserved.

## Configuration

The service defaults `RAMP_READBACK_EVERY` to 3 seconds. Set it to `0` to disable
tracking and recover Release 7's dimmer publication behavior. CLI runs default to
`--ramp-readback-every 0`; supported enabled values are 3–60 seconds. Read-only
mode disables tracking/transmissions regardless of that setting. MONITOR remains
configured as before and retains its own CNI_MONITOR opt-out.

The feature adds bus traffic only while known configured percentage dimmers have
active watches. Normal readbacks are scheduled about every three seconds by default.
Initial/final/stop confirmation can be sooner, subject to a global ceiling of one
automatic targeted block request per three seconds across all watches. A hardware
status block contains 32 groups. Active dimmers in the same block share a request;
only affected blocks are selected, never a full resync for tracking. Existing
manual/startup/periodic resyncs are unchanged and are outside this automatic budget.

With multiple active blocks, the global budget can make individual updates slower
than the configured interval. The slider moves in reported steps, not continuously.
The interval setting is not a promise of exact timing.

## Tracking and state priority

The bridge tracks successful outgoing dimmer commands (JSON transitions, ordinary
brightness sliders and ON/OFF) and observed wall-switch dimmer commands, including
TERMINATE RAMP. Relays, fans, binary sensors and unconfigured groups do not get
ramp watches. Repeated observed echoes of a recent local command do not restart it.

Protection starts atomically with a local write, before its ACK, so binary status
cannot race the start of a ramp. A negative ACK restores the prior watch when
appropriate; separately observed matching bus evidence can still justify tracking.
New commands replace the prior watch. Targets from ACKs and monitored dimmer SAL
messages are no longer published as present brightness when tracking is enabled.
Numeric status replies publish normal brightness and ON/OFF through existing topics.
A positive raw level is represented by at least 1%/scale unit so rounding cannot
turn a nonzero group OFF. Intermediate samples preserve the remembered ON level;
a reached positive target or confirmed stopped level can update that memory.
Thus ordinary dimmer controls also wait briefly for readback instead of optimistic
ACK state; fan and relay behavior is unchanged.

While a watch exists, valid binary reports remain in diagnostics but cannot publish
ON/OFF for that dimmer. After a valid numeric reply, numeric authority persists for
that dimmer for the connection session, without ongoing polling when idle. This
prevents a stale binary target from undoing a confirmed stopped/final level. Startup
level replies also establish numeric authority. Reports for other profiles continue
as in Release 7. Numeric
feedback remains usable for shared numeric state/brightness topics; no ON/OFF string
is injected into them. Existing separately mapped state topics, including the
approved Study group 13 correction, continue to work.

A watch ends after two separate requested numeric samples confirm the target. Stop
requires two equal requested samples, without guessing its stopped level. Duplicate
messages or replies outside a fresh request window cannot supply both confirmations.
No automatic command is resent. Numeric authority remains after tracking ends;
MONITOR continues supplying dimmer diagnostics rather than overriding numeric state.
Unknown ramps already in progress before connection, commands not seen by the bridge,
and ramps on unsupported scopes cannot be reconstructed by this feature.

## Bounds, failures and concurrency

All targeted requests use the existing bounded command worker and connection leases.
Readers and MQTT callbacks never send readback requests directly. A queued request
rechecks the tracked command versions and session before sending. Replaced/expired
work cannot start an old tracking query; disconnect cancels all watches, numeric-authority markers and pending
reservations. No commands or tracking queries are replayed after reconnect.

Valid numeric replies are expected within three seconds of a targeted request.
After three requests without a valid numeric level, polling for that group stops
and its phase becomes readback_failed. Absence/invalid encoding also count as no
usable level, rather than fabricating zero. Binary protection remains until the
watch's bounded deadline: the longer of 90 seconds or the full-scale rate plus
30 seconds. The minimum accommodates confirmation across several active blocks.
An unconfirmed watch eventually expires with diagnostics; subsequent commands can
start a fresh watch. On failure, HA retains its last known numeric reading, which
must not be treated as fresh measured brightness. Numeric protocol replies have
no per-request transaction ID; request windows and separate confirmations reduce
ambiguity but cannot prove physical output or completely eliminate delayed replies.

Reservations expire after six seconds if the command queue drops/expires them.
Queue rejection is counted and deferred without a tight retry loop. Missing replies,
expired watches and failed readbacks are distinct counters, not a claim of a
specific electrical fault. Existing status-send errors and worker/session recovery
handle actual transport failures.

## Diagnostics, storage and HA

Status JSON gains `ramp_tracking`: enabled, interval, global minimum interval,
active_groups, numeric_authority_groups and a bounded list of watched groups with target, rate, phase, age,
last reported raw level and missing-reply count. Existing diagnostics counters add
ramp_tracking_started/completed/expired/disconnect_cancellations,
ramp_readback_requests/samples/reply_timeouts/failed/queue_expired/queue_rejections
and ramp_binary_updates_suppressed (all names end in `_total`). A reply timeout
means no usable numeric group level in the window, not necessarily no raw packet.

The existing traffic stream shows targeted request events and bounded completion/
expiry events. This release adds no HA sensors, Recorder settings or database
schema. Existing raw traffic/group/status sensor Recorder exclusions remain in
place. Ordinary light state/brightness history and existing numeric metrics can
still be recorded according to existing HA settings. All watches are in memory.

## Deployment and acceptance

Use the exact-commit check/apply helper, preserve its printed verified backup and
rollback command, and confirm the service's RAMP_READBACK_EVERY setting. Candidate
preparation does not deploy or alter live HA. No mapping changes are part of this
release. Roll back to the saved Release 7 snapshot if ordinary controls regress.

Before the physical trial, confirm Study motion automations are paused; the bridge
does not prevent a legitimate motion automation from replacing a ramp command.
Note the initial level. Test group 13 with a 100%→0% native 60-second rate: HA
should show stepped numeric levels and remain ON while they are above zero,
then become OFF after zero is reported. Check two equal post-stop readings and
restoration of the initial level. Verify wall-switch dimming/stop, ordinary dimmer
controls, relays, fans, readonly behavior, counters and CNI connectivity. Test more
than one active dimmer/block to inspect coalescing and budget effects. No second
CNI client is needed. A controlled disconnect test requires separate agreement.
