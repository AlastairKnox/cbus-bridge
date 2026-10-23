# C-Bus MQTT bridge

Python bridge between a C-Bus CNI and MQTT without using cgate or any other components, using Home Assistant MQTT YAML to define group profiles. Imported from the running cgate server on 20 October 2026.

## Working copy

The editable source lives on rackpi at `/srv/storage/files/projects/cbus-bridge`, mounted on the desktop at `/home/ack/mnt/cbus-bridge`. Run Git from the desktop mount so GitHub authentication stays on the desktop.

## Runtime

The existing service is `cbus-bridge.service`, running as `cbusbridge` from `/opt/cbus_bridge`. Its environment file is `/etc/cbus-bridge.env`. Keep credentials and live site mappings outside Git: `config.json`, `ha_mqtt.yaml`, and environment files are ignored. The existing `run_bridge_service.sh` uses that environment.

Create a local virtual environment and install `requirements.txt` when preparing a new deployment. Check CLI options with `python run_bridge.py --help`; do not start a second bridge against the live CNI/MQTT system during development.

## Deployment

Commit and push changes from the desktop. Fetch/pull into the separate cgate checkout, review the change, and deploy deliberately. A Git pull alone does not replace `/opt/cbus_bridge`, install dependencies, change settings, or restart the service. Preserve the live environment and site mappings when deploying.

The original runtime source and settings remain on cgate. Virtual environments, generated files, and legacy scratch versions are not tracked.

For repeatable checks, installation and rollback, see [DEPLOYMENT.md](DEPLOYMENT.md).

For explicit dimmer fade rates and stop-ramp commands (Release 6), see
[docs/RAMP-CONTROL.md](docs/RAMP-CONTROL.md). Release 9 makes ordinary dimmer
brightness and ON commands immediate; explicit fades and fan rates are preserved.

Release 7 adds configurable passive status monitoring; see
[docs/PASSIVE-STATUS.md](docs/PASSIVE-STATUS.md) for the trial and its limits.

Release 8 adds targeted numeric ramp readback; see
[docs/RAMP-READBACK.md](docs/RAMP-READBACK.md) for rate limits and acceptance.

Release 10 improves interface reply classification, live traffic readability and
current-session numeric coverage; see [release notes](releases/RELEASE-10.md) and
[optional HA updates](docs/RELEASE-10-HA.md).

Release 11 changes service ramp readback to three seconds, globally limited to one
automatic block request per three seconds. See [release notes](releases/RELEASE-11.md).

Release 12 prepares a Study-only HA transition-duration trial; see
[HA transitions](docs/HA-TRANSITIONS.md) for the optional HA definition, scheduling
and rollback. The bridge installer does not activate the HA proposals.

Release 14 adds continuous-operation safeguards: bounded/coalesced MQTT state
publication, explicit readiness, CNI liveness checks, process watchdog recovery
and priority for ordinary controls. See [release notes](releases/RELEASE-14.md).

Release 15 queues overlapping fade requests across groups, preserves immediate UI
priority and resumes fade acquisition after ordinary controls. See
[release notes](releases/RELEASE-15.md) for queue limits and acceptance checks.

Release 16 adds an explicit device identity survey trial, with no automatic scan.
See [device inventory](docs/DEVICE-INVENTORY.md) for the one-unit CNI trial and
[release notes](releases/RELEASE-16.md) for pacing, validation and deployment.

Release 17 adds on-demand flat-network presence discovery and an explicit paced
address-sweep fallback. See [device discovery](docs/DEVICE-DISCOVERY.md) for commands,
progress, coverage limits and acceptance; existing identity-trial commands remain.

Release 18 adds on-demand device voltage and latched-diagnostics reads. See
[device health](docs/DEVICE-HEALTH.md) for the CNI-only trial, batch/full-inventory
commands, historical-reading semantics and limits. No periodic polling is enabled.

Release 19 adds one voltage-only fallback read for units without a usable health
summary, with explicit partial/unknown results and bounded alternative replies.
See [health compatibility](docs/DEVICE-HEALTH-COMPATIBILITY.md) for the trial.

Release 20 adds opt-in device monitoring with a reviewed roster and an offline HA
package/dashboard generator. See [device monitoring](docs/DEVICE-MONITORING.md) for
15-minute paced reads, freshness/baseline semantics, Recorder and installation.
