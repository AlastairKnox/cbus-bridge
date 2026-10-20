# Release 7 candidate: passive binary status monitoring

Baseline runtime: Release 6, 8b4be84. Not deployed by candidate preparation.

The service defaults to the agreed MONITOR trial (CNI_MONITOR=1); CNI_MONITOR=0
restores the prior initialization. EXSTAT and other interface settings remain
unchanged. Numeric level replies and all existing commands keep their behavior.
No extra CNI connection, status polling or dependency change is introduced.

Decode standard binary status reports with strict checksum, length, application
and group-bound validation. Valid ON/OFF can update separate state topics without
inventing brightness or fan speeds. Error/absence do not fabricate OFF. Shared
numeric state/brightness topics are skipped and counted. Repeated binary state
updates are deduplicated without hiding later command/readback changes; failed
MQTT publishes can be retried on the next report.

Expose binary observations, error/absence counts and timestamps independently of
numeric-level coverage and confidence. Binary reports do not satisfy a pending
level request or make historical brightness look fresh. Binary errors do not
imply an electrical/noise fault. Valid binary evidence cancels obsolete synthetic
level timers. Historical observations retain age across reconnect.

The HA examples propose six sensors, a passive-status card and binary state/age
columns. They remain uninstalled and require separate approval. The existing live
traffic view works without those changes. No native fade progress or completion
notification is promised; the controlled Study group 13 trial determines what the
installation actually reports. Full instructions: docs/PASSIVE-STATUS.md.

Validation covers manufacturer three-line report fixtures, all 256 packed byte
values, corruption/truncation/overflow, other scopes, binary/level separation,
shared numeric topics, dedupe/reconnect/publish failures, read-only behavior,
initialization option bytes, CLI intent reporting and launcher default/opt-out.
The complete prior regression suite remains required. No hardware commands are
sent by offline tests. Exact-commit deployment and verified backup/rollback
remain the rollout mechanism; physical HA/CNI acceptance follows deployment.
