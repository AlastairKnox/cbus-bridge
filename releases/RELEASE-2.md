# Release 2: command safety

Baseline application: 686720f. Deployment tooling baseline: 752f3a4.

## Behavior changes

- Ignore broker-delivered MQTT commands marked retained, including resync commands.
  This prevents replay on subscription/reconnect. Existing retained broker messages
  are not deleted. MQTT 3 does not mark ordinary live deliveries with the publisher's
  retain flag; this guard specifically protects retained-message replay.
- Accept switch/ramp commands only on the exact command topics enabled by that
  profile. Binary sensors cannot grant write access; relays without a ramp command
  topic no longer implicitly accept ramp commands.
- `--readonly` blocks device writes, initialization and status/resync transmissions.
  Rejected writes return failure and do not publish an invented commanded state.
  Real monitored C-Bus traffic is still published. Combining --readonly and the
  legacy --enable-writes flag is an error before any connections are opened.
- Validate C-Bus MQTT profiles before connecting: mapping/list structure, required
  topics, matching group addresses, address range 0..255, supported topic roles,
  paired brightness/preset topics, duplicate groups/topic conflicts and positive
  integer brightness scales. Unrelated MQTT entities remain ignored. Validation
  messages identify fields rather than reproducing site configuration values.
- Deployment preflight uses candidate code to validate the active production HA
  mapping before stopping the bridge. Parser output is captured to avoid disclosing
  configuration lines. A validation failure leaves production unchanged.

## Compatibility

No HA YAML, credentials, environment, dependencies, CNI initialization strings,
fan speeds, fan off conventions, dimmer ramp codes or protocol parsing change.
The current ON/OFF and comma-separated brightness payload conventions remain.
State and brightness may intentionally use the same level topic.

The provided live HA config, shared bridge mapping, and ACTUAL active production
mapping each pass validation with 72 profiles. Existing three state-topic
variations between HA and the shared mapping do not require a configuration edit:
read topics stay as configured in whichever file the bridge loads. Both fan-off
conventions (0 and 5) remain supported.

There are 37 offline tests, including 17 new cases (with parameterized malformed
configuration and command cases), passing locally and in the cgate production
Python environment. Tests use mocked transports and temporary files; no test
starts a live bridge or writes to the supplied live YAML files. Hardware acceptance
still follows deployment.

## Deploy and acceptance

On cgate, fetch this release and first run `bash tools/release.sh check COMMIT`.
After review, use `bash tools/release.sh apply COMMIT`. Keep the printed backup
path and independent rollback command. The immediate fallback is the running
application captured by apply (currently release 1), not the old initial import.

Verify CNI client 192.168.1.15, HA availability, relay ON/OFF, dimmer ON/OFF and
brightness, fan off/low/medium/high, and normal binary-sensor readbacks. Do not
publish test commands to real sensors. Observe operation for at least 15 minutes.
Readonly and retained-replay failure cases are covered offline; do not run a
second bridge against production or disrupt the broker to repeat those tests.

Rollback on acceptance failure using the backup-specific command printed by apply.
See DEPLOYMENT.md for supported scope, recovery and limitations.
