# Device monitoring and Home Assistant (Release 20)

This is opt-in read-only monitoring. Without a reviewed device_monitor.json file,
lighting behavior remains as before and no device-health polls are started.
Missing/invalid roster files disable monitoring; invalid-file errors are logged
without exposing file contents or preventing bridge startup.

## Reviewed roster, baseline and persistence

Use tools/prepare_device_monitor.py with a completed full health-survey JSON:

```bash
python3 tools/prepare_device_monitor.py survey.json output-directory \
  --dashboard /path/to/current/cbus_observatory.yaml
```

The output directory must be new. This tool does not access CNI, MQTT, HA APIs or
credentials. It produces:

- device_monitor.json: all unit addresses, type/manufacturer/firmware, observed
  diagnostic capability, and saved baseline error masks. No past measurements are
  restored as fresh. For this site's accepted survey: 43 devices, 21 full responders,
  22 without health replies. No permanent hardware-capability claim is implied.
- cbus_device_health.yaml: additive HA package with sensors/devices and a refresh
  button; no changes to lights, schedules, automations or existing diagnostics.
- devices-view.yaml: new Devices view for the existing Observatory.
- cbus_observatory.yaml and cbus_observatory.before.yaml when --dashboard is given:
  exact original text plus the new view; no rewrite of existing views or cards.

Install the reviewed config as /opt/cbus_bridge/device_monitor.json, readable by
cbusbridge. The service launcher accepts CBUS_DEVICE_MONITOR_CONFIG to override
this path, but the default path keeps the roster inside the existing full-app
backup/rollback coverage. It is ordinary device metadata, not credentials. The
bridge reads it once on startup. Changes need review and a service restart.
The example roster is NOT installed automatically from Git.

## Refresh scheduling

The existing inventory manager, command worker and sole CNI connection perform all
reads. There are no new polling sockets or threads, RESET, writes or error clears.
Configured full/voltage responders alone are eligible for automatic health reads;
configured nonresponders remain catalogued and are not routinely queried.

For 21 responders, one unit starts about every 43 seconds, spread across a nominal
15-minute cycle. The first read is delayed at least 10 seconds after process start
and waits for both links, the initialized command path and learned CNI address.
Each read still yields to lighting work, shares the global three-second query
budget, and pauses while any ramp watch exists. Missing summary replies can use
the Release 19 voltage fallback. Actual intervals can lengthen during lighting
activity, manual surveys, link loss or starvation. There is no catch-up burst.
Larger rosters use at least 31 seconds between individual jobs; the heartbeat
reports cycle_seconds_minimum, and config validation requires a stale limit long
enough for at least two full cycles.

The HA Refresh device health button sends non-retained
`{"device_monitor":"refresh"}` to cbus/bridge/command. It coalesces requests and
rate-limits them to one per 30 seconds. It waits for an existing inventory job and
its cooldown, then requests one paced batch of eligible responders only. It never
cancels a running survey, includes known nonresponders or bypasses worker/ramp
priority. Public explicit batches keep the 16-unit cap; only the internal reviewed
monitor roster can create the larger health batch. The inventory cancel command
stops a current read batch, not the configured ongoing monitoring schedule.

## Freshness and reported flags

Each device publishes non-retained QoS0 JSON to cbus/bridge/devices/UNIT/state every
30 seconds; monitor status goes to cbus/bridge/device_monitor. Both use the existing
MQTT client; missed updates recover on the next heartbeat without replaying a
backlog. Nothing is sent while MQTT is disconnected. No raw-frame attributes are attached to HA sensors.
The roster and manager maps are bounded by the 255 possible local unit addresses.

States distinguish fresh, awaiting_reading, stale, no_reply, unrecognized_reply,
cancellation reasons and diagnostics_unavailable. Failure is a missing observation,
not an assertion that the physical unit is offline or faulty. Last successful
values and their original observed_ts remain available for reference, but HA
numeric/flag sensors are unavailable unless the latest read is fresh; flags also
require diagnostics_available. Voltage-only readings cannot invent healthy flags.

Default stale time is 45 minutes. Age/staleness use monotonic time so clock
corrections cannot extend freshness. observed_ts is the original wall-clock time
of the accepted reply. On link loss, freshness is invalidated; reconnection alone
cannot make an old reading fresh. Monitoring does not replay cancelled jobs.
Restart starts without measurements; baseline/identity metadata survive only via
the reviewed roster. HA entities also require bridge availability and expire after
90 seconds without monitor heartbeats. Monitor payloads are deliberately not retained, so reconnecting HA cannot
replay an old heartbeat as fresh. HA waits at most 30 seconds for the next live
heartbeat. These observations are not a network-health certificate.

Latched error flags are not error rates and do not identify when events occurred.
The saved survey's bit mask is the commissioning baseline. New flags since baseline
shows bits currently present that were absent from that baseline; existing
power-on/reset/TX flags are historical findings. Reappearance of an already-set
baseline bit is not detectable as a new event. No bit is cleared automatically;
changing a baseline requires a reviewed new survey and roster regeneration.

## Home Assistant and Recorder

The package uses native MQTT entities grouped into one device per C-Bus address,
with model and firmware, short entity names, and one monitor refresh button. The
Devices view shows all 43 catalogued units, voltage, observation times, voltage
history and per-device latched/new flags, low/marginal voltage and learn mode.
No physical circuit/channel mappings are inferred from device addresses.

Numeric voltage is a measurement sensor and is retained in normal HA Recorder
history (subject to the site's existing Recorder settings). Changes to meaningful
flags and reading status are also retained. Observation timestamp entities, refresh
button events and the routine fresh-reading count are excluded by the additive
package. No continuously changing age counter or JSON diagnostic attributes are
recorded. Existing Recorder exclusions and physical light history remain intact.
Raw MQTT publication alone does not create Recorder history.

Native MQTT sensor/button and Recorder references:
https://www.home-assistant.io/integrations/sensor.mqtt/
https://www.home-assistant.io/integrations/button.mqtt/
https://www.home-assistant.io/integrations/recorder/

## Installation order and acceptance

1. Push and deploy the exact Release 20 commit using tools/release.sh check/apply.
   With no roster file installed yet, normal bridge operation is unchanged.
2. Verify HA controls, physical switches and fades. Install the reviewed JSON
   roster into /opt/cbus_bridge/device_monitor.json (root:root 0644 is sufficient)
   and restart cbus-bridge.service once to enable monitoring. Keep a copy locally.
   An alternative is to install the reviewed roster before apply, so apply's
   normal restart enables it and its backup includes the roster immediately.
3. Confirm cbus/bridge/device_monitor reports 43 devices/21 polled, and device
   states progress from awaiting_reading to fresh over the initial 15-minute cycle.
4. Back up HA's existing dashboard, add packages/cbus_device_health.yaml, and
   install the additive Observatory candidate only if the current dashboard still
   matches its saved original. If it changed, regenerate against the current file.
5. Run HA's configuration check, then restart HA to load the new MQTT entities and
   Recorder exclusions. Opening/reloading a dashboard alone does not apply these.
   Do not restart HA while the light-display script or other stateful scripts run.
6. Confirm the Devices tab, 43 device status sensors, 21 numeric voltages (once
   observed), historical/new flags and manual refresh. Nonresponders should show
   diagnostics unavailable rather than false zero voltage or a device fault.
   Verify normal physical/UI control and fades during a refresh. Voltage history
   starts accumulating after installation; it is not backfilled from the survey.

The generated HA YAML has offline structural/template checks, but needs HA's live
configuration validation and acceptance after installation. No live HA files are
changed by generating candidates. The bridge installer does not deploy HA files.

## Rollback

Use bash /home/ack/cbus-bridge-rollback.sh for the saved full application backup.
If the roster was added after that backup, a full app restore removes it as part
of returning to the saved directory. Older releases ignore this monitor roster.
Restore HA's backed-up dashboard and remove the new package, check configuration
and restart HA to undo the HA portion. Bridge rollback does not restore HA files.
To disable monitoring alone, move the roster out of its configured path and restart
the bridge; lighting continues, monitor status becomes disabled and device entities
expire. The external roster is not overwritten by normal Git pulls.
