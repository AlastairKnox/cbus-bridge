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
