# Release 10: clearer traffic and synchronization diagnostics

Baseline: Release 9 (8c4ef58). Candidate preparation does not deploy production or HA.

- Strictly recognize short and addressed CNI parameter-setting acknowledgements.
  A joined echo is accepted only for a matching setup command pending in this
  connection's ten-second initialization window. Invalid and unknown traffic stays
  visible. No lighting state or transmit ACK is inferred from a setup reply.
- Preserve detailed traffic (20-event bounded sample) and add a separate bounded
  activity buffer. Routine binary reports, successful ACKs, echoes and setup replies
  no longer displace commands in the main dashboard. Negative ACKs, error-bearing
  binary report summaries and parser failures remain in activity. Both views are
  sampled, not archives; counters still include all received tokens/reports.
- Add 13 optional HA sensors for ramp watches/readbacks/missing usable replies,
  queue rejections/expiry, suppressed binary updates, setup replies, routine events
  and numeric report coverage in the current CNI session. Include the six passive
  status sensors previously prepared but not installed: proposed package has 57
  sensors, compared with 38 currently installed.
- Per-group numeric report age is independent of command and binary activity.
  Session confirmation resets on CNI disconnect/reconnect while historical values
  remain timestamped. MQTT reconnect alone preserves the CNI session evidence.
  No new polling, retries, commands, dependencies or CNI connections are introduced.

## Site coverage review

Read-only baseline: 72 configured groups; 63 valid numeric reports and nine absent
in both numeric and binary status: 65, 73, 90, 99–103, 250. Requests received replies;
this is different from unanswered requests or a diagnosed bus wiring fault. Keep
these groups visible and confirm their installed purpose before changing mappings.
No automatic suppression or profile removal is included.

## Deployment and acceptance

Use tools/release.sh check/apply with the exact release SHA; save the printed backup
and rollback command. Bridge deployment is independent from the optional HA YAML.
No live HA mappings/automations change. See docs/RELEASE-10-HA.md for the proposed
package/dashboard changes, activation and rollback.

After bridge deployment, check normal UI brightness, physical switches, fans and
explicit Study group 13 fade-to-off/stop. At startup setup acknowledgements should
increase their own counter, rather than unsupported frames. New unknown traffic
must remain visible. A deliberate future CNI reconnect should clear current-session
coverage until fresh numeric reports arrive; do not interrupt a healthy live CNI
solely to exercise this. Offline tests cover disconnect/queue/session recovery.

HA acceptance after approval: main activity stays readable during MONITOR reports;
Detailed traffic remains active; negative ACKs/errors remain visible; new sensors
resolve and numeric coverage matches group rows. Recorder exclusions for raw traffic
and groups are preserved. Ordinary light history and numeric metrics remain recorded.
