# Release 14: continuous-operation safeguards

Baseline: 682456b (Release 13). No HA schema, dependencies, service-unit or runtime
file-layout changes. Development and tests do not contact production CNI/MQTT.

## Recovery and readiness

- CNI readiness follows initialization/status-request transmission and entry to the
  reader, without waiting for a quiet receive interval. Initialization send failure
  cannot announce readiness. Online availability requires a ready command path.
- Losing either link invalidates the CNI command lease. MQTT reconnect alone cannot
  reauthorize the previous CNI session. Setup tag g is reserved after initialization
  and cannot accidentally acknowledge a lighting command.
- TCP keepalive: Linux idle 60 seconds, interval 10 seconds, three probes.
- After 120 seconds without a recognized valid CNI message, queue one status-block
  liveness probe; a matching numeric status envelope (including absent groups)
  must arrive within ten seconds after transmission, otherwise reconnect. Probe
  admission shares the three-second automatic readback send budget. It does not
  change any load. Read-only mode never sends this probe; TCP keepalive still runs.
- Independent process watchdog: a reader/command-worker/metrics heartbeat older
  than 60 seconds, a dead worker/metrics thread, or an outbound PUBACK/transport
  rejection outstanding over 90 seconds terminates the process with status 1.
  Existing cgate systemd Restart=always restarts it. Initial synchronous MQTT
  connection has a separate 60-second deadline. This is not a host-level watchdog
  and cannot recover a frozen OS or a process denied all CPU execution.
- An availability QoS1 heartbeat every 30 seconds checks broker publication progress
  even when diagnostics/traffic publishing is disabled. No extra bus query is used
  for this MQTT heartbeat.
- A process-lifetime /tmp/cbus-bridge.lock excludes another cooperating instance on
  this host. Lock files are never unlinked, and symlinks are rejected. An older
  release or another host cannot be excluded by this new local lock; continue to
  operate just one bridge against the CNI. The lock file contains no credentials.

## Bounded publication and interactive commands

- Paho queue limit 32, inflight limit 16. The bridge outbox retains one latest desired
  retained QoS1 value per topic (maximum 1024 topics), with at most 16 topics pending
  PUBACK. Intermediate unsent values coalesce. An in-flight old value must finish
  before the latest value for that topic is sent; pending packets are not duplicated
  on reconnect. All known latest retained values are replayed after reconnect.
  A brief stale state may still precede the fresh state while transport recovers.
- Round-robin topic delivery prevents frequently updated topics starving others.
  MQTT QoS0 telemetry remains best effort. Queue acceptance, broker ACK and physical
  load feedback remain distinct. Diagnostics expose mqtt_publications and
  functional_health; per-group publication_semantics describes local admission.
- Ordinary controls/STOP have priority over background queries and duration
  readback acquisition. A waiting duration acquisition yields before a lighting
  write when an interactive command arrives. Already transmitted commands are not
  replayed or interrupted mid-ACK. Older unsent commands for the same group are
  coalesced; a full queue can displace background work for an interactive command.
- Only one duration request may be active or queued. A concurrent request is rejected
  as duration_busy instead of expiring unpredictably behind another duration wait.
- Resync requests are coalesced and admitted no more than once per 30 seconds,
  including periodic requests. Full-resync frames keep their existing 0.2s spacing.
- Ordinary invalid switch/numeric commands now report last_command_failure as well
  as logging. Level publication admission failures no longer update published_level.

## Validation

205 offline regression tests pass, including continuous traffic at startup,
setup-tag exclusion, initialization failure, queue bounds, latest-OFF recovery,
round-robin fairness, real Paho with 20,000 updates and withheld PUBACKs, liveness
probe timeout/reply, read-only behavior, expired probe rescheduling, duration
preemption, singleton exclusion/release and functional heartbeat expiry.

The separate tests/integration_release14.py starts fake MQTT and CNI TCP servers
on 127.0.0.1 only, using disposable configuration. It exercises continuous status
traffic, MQTT command -> CNI ACK, withheld/restored broker PUBACKs and final OFF,
CNI reconnection, MQTT reconnection with a subsequent command, and actual process
exit status 1 on an injected watchdog stall. It is not run against live equipment.

An accelerated 30-day, 100,000-iteration component run kept publication and command
queues within their limits. Traced live allocations at 20k/50k/100k iterations were
198432 / 235685 / 199072 bytes. End outbox: 256 topics, one pending; queue depth zero;
no simulated readback-spacing violations. This is clock-accelerated component
coverage, not a physical 30-day soak or whole-process memory/FD certification.

## Deployment and acceptance

Use the existing check/apply workflow and exact release commit. Keep the printed
backup plus ~/cbus-bridge-rollback.sh. The installer file layout is unchanged.
No HA reload or new HA automation is required. Never start a second live bridge
for testing. Avoid intentional production outages to exercise fault injection.

After deliberate deployment, check availability settles online, both directions
of wall-switch/HA control work, immediate sliders/off remain immediate, Upstairs
and Study fades complete and final OFF is reported, fans respond during fades,
and diagnostic publication queue/heartbeat ages remain bounded. Check normal
scheduled/Lux handovers separately. A rejected simultaneous duration request is
now explicit; scheduler actions should not demand concurrent duration acquisitions.

Soak for 7–14 days: service restart count/reasons, RSS/FD/thread trends, publication
queue ages, CNI reconnects and command failure reasons. Retain exact bridge and
separate HA rollback backups. No cron restart is required by this design.
