# C-Bus MQTT Bridge

**Bring C-Bus lighting into Home Assistant—with native fades, responsive command queues, and visibility into the devices behind the wall switches.**

A Python bridge that talks directly to a C-Bus Network Interface (CNI) and exposes controls, state and diagnostics over MQTT. No C-Gate server is required.

```text
C-Bus lights, dimmers, switches and sensors
                    ↕
              Ethernet CNI
                    ↕
             C-Bus MQTT Bridge
                    ↕
                MQTT broker
                    ↕
       Home Assistant / MQTT clients
```

## What makes it useful

### Native fades, with feedback that follows the ramp

Let C-Bus do the dimming. The bridge sends a native ramp command rather than generating a stream of software brightness steps. Request a hardware ramp rate, ask for a transition duration, or stop a fade at its current level.

Optional targeted readback samples the affected **32-group status block**, so Home Assistant can follow a fade and confirm its final level—including OFF. Dimmers in the same block share a query. The service defaults to a global limit of one automatic block request every three seconds; several active blocks share that budget.

Ordinary slider changes use the immediate native rate. Explicit fades retain their requested behavior. Numeric readback takes priority over passive binary reports that could otherwise make a fading light appear OFF too early.

### A command queue built for a busy home

Start fades on several groups while continuing to use other lights and fans. Commands are transmitted serially, but the worker **does not wait for a physical fade to finish** before preparing the next one.

- **Overlapping fades:** up to eight duration requests, including the one acquiring its starting level, within a bounded 32-entry command queue.
- **Responsive ordinary controls:** interactive commands take priority over background reads and can interrupt a fade's preparation; the pending fade can then resume.
- **Latest request wins:** newer unsent commands for the same group supersede older ones.
- **No stale-command surprise:** queued commands expire, retained MQTT commands are ignored, and queued control actions are discarded on link loss rather than replayed after reconnect.

Duration-based fades need a fresh starting-level query and may start a few seconds apart. This supports overlapping fades, not synchronized scene activation. See [queue behavior and limits](releases/RELEASE-15.md).

### See C-Bus voltage at individual devices

Opt-in device monitoring exposes **C-Bus supply voltage reported by each responding unit**, alongside device identity, firmware and supported diagnostic flags.

A generated Home Assistant package provides one device entry per unit, voltage sensors suitable for history, reading status, low/marginal-voltage flags, learn mode and latched error flags. A saved commissioning baseline distinguishes newly observed error bits from flags already present when monitoring was configured.

Reads are spread across a nominal 15-minute cycle and yield to lighting activity; monitoring pauses during ramp tracking. Units without usable health replies stay in the inventory without being repeatedly polled or shown as zero volts. Missing replies and stale readings are represented explicitly.

Voltage and diagnostic support depend on the unit and firmware. Latched flags describe recorded events; they are not an error rate or proof of a current hardware fault. [Device monitoring setup →](docs/DEVICE-MONITORING.md)

### A window onto the wire

The optional **C-Bus Observatory** Home Assistant dashboard makes bus activity visible:

- A live RX/TX view with decoded commands, group/unit details, ACK outcomes and bounded raw-frame samples.
- Connection state, reconnect counts, command failures, timeouts and parser/checksum diagnostics.
- Group-level observations and synchronization status, including the distinction between a transmitted target and a reported level.
- Ramp-watch activity, feedback requests and device-health views.

The live traffic feed is a bounded, sampled view—not a packet archive or an automation-grade event stream. The supplied diagnostic examples exclude the large live-traffic/group attributes from HA Recorder; voltage and other useful numeric history can be recorded separately. [Observatory examples →](examples/homeassistant/README.md)

### Recovery and deployment safeguards

The bridge includes reconnect handling, command acknowledgements and timeouts, TCP keepalive, application-level CNI liveness checks, and a process watchdog intended to work with a restarting service supervisor.

MQTT state publication uses a bounded outbox that coalesces unsent updates to the latest value per topic. Readiness and availability reflect the command path, and a local instance lock helps prevent accidentally starting a second cooperating bridge on the same host.

For an existing supported systemd installation, the deployment tools check an exact Git commit, run offline tests, validate active configuration, verify a backup and rehearse its restoration before replacing runtime files. Failed installation/start checks attempt rollback. Physical Home Assistant/C-Bus acceptance checks remain part of deployment. [Deployment and rollback →](DEPLOYMENT.md)

## Controls and integration

The bridge derives its group profiles from a local YAML file using Home Assistant's default MQTT entity format. It supports:

| Entity/control | Behavior |
| --- | --- |
| Lights and relays | ON/OFF and remembered nonzero dimmer level for ON |
| Percentage dimmers | Brightness, immediate changes, native ramp rates, requested-duration fades and stop-ramp |
| Fans | Configured preset/level mappings |
| Binary sensors | Configured lighting-group ON/OFF observations |
| Status synchronization | Startup, on-demand and optional periodic reads |
| Device inventory | On-demand local presence discovery, identity queries and an explicit paced address-sweep fallback |
| Device health | On-demand diagnostic/voltage reads and opt-in monitoring from a reviewed roster |

The current lighting scope is **logical local network 254, application 56**. Discovery and device-health queries also target the local network. Routed networks, native Trigger Control scenes, blinds/covers and general Measurement-application sensors are not implemented.

Home Assistant MQTT discovery is not published automatically. Existing topics and entity IDs come from the reviewed YAML profiles. Other MQTT clients can use the same command/state topics.

## Getting started

You need a compatible Ethernet CNI, an MQTT broker, and a Linux host with Python 3. Use one active bridge connection to the CNI. The default CNI TCP port is **10001**; the bridge uses one configured endpoint, with no secondary-port failover.

```bash
git clone https://github.com/AlastairKnox/cbus-bridge.git
cd cbus-bridge
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python run_bridge.py --help
```

Create a local `ha_mqtt.yaml` containing your reviewed group profiles. This minimal example defines a percentage dimmer; replace group **13** and the entity metadata with values appropriate to your installation:

```yaml
mqtt:
  light:
    - name: Example C-Bus dimmer
      unique_id: example_cbus_dimmer_13
      command_topic: cbus/write/254/56/13/switch
      state_topic: cbus/read/254/56/13/state
      brightness_command_topic: cbus/write/254/56/13/ramp
      brightness_state_topic: cbus/read/254/56/13/level
      brightness_scale: 100
      availability_topic: cbus/bridge/availability
```

The file is a standalone profile file: supply the relevant MQTT definitions, not a whole HA configuration that depends on `!secret` or `!include` resolution. If you use template-schema lights in HA to expose transitions, keep the bridge's default-schema runtime profiles separate.

For a deliberately configured installation, a foreground invocation with monitoring and ramp feedback enabled is:

```bash
python run_bridge.py \
  --cni-host 192.0.2.10 --cni-port 10001 \
  --mqtt-host 127.0.0.1 \
  --ha-config ./ha_mqtt.yaml \
  --init --monitor --status \
  --diagnostics-every 5 --traffic-every 1 \
  --ramp-readback-every 3
```

`192.0.2.10` is a placeholder; replace it with your CNI address and configure broker authentication for your installation. This invocation initializes the interface, requests status and accepts controls for configured groups. Use `--readonly` for a passive connection that sends no CNI initialization, status queries or load commands; do not run it alongside a live bridge.

Diagnostics, traffic publication, ramp readback and device monitoring are configurable. Several features are disabled by default on the bare CLI; the command above enables the first three. Device monitoring additionally needs a reviewed roster via `--device-monitor-config`. The supplied service launcher has its own defaults and expects an existing `/opt/cbus_bridge` installation; it is not a fresh-host installer.

Site mappings, monitoring rosters and credentials stay outside Git. `ha_mqtt.yaml`, `config.json`, `device_monitor.json`, environment files and virtual environments are ignored.

## Try a fade over MQTT

Publish **non-retained** messages to the ramp topic of a configured writable percentage dimmer. These examples use group 13 and a brightness scale of 0–100.

**Ask for 20% brightness over approximately 20 seconds:**

```text
Topic:   cbus/write/254/56/13/ramp
Payload: {"level":20,"transition_seconds":20}
```

The bridge reads the current numeric level and chooses the closest achievable native rate for the distance to the target. C-Bus offers discrete rates, so requested duration is an approximation. Missing starting-level feedback causes a reported failure rather than a guessed fade.

**Select a native full-scale rate directly:**

```json
{"level":0,"ramp_seconds":60}
```

Here, 60 seconds is the nominal time for a complete 100%→0% change. A 50%→0% change takes approximately 30 seconds at the same rate. Zero selects the immediate rate.

**Stop at the current hardware level:**

```json
{"stop":true}
```

Stop is queued and acknowledged like other commands; it is not an emergency stop. A fade already delivered to C-Bus runs in the hardware and may continue if the bridge disconnects.

To pass HA's `light.turn_on` / `light.turn_off` transition setting through to the bridge, use an appropriate template-schema light definition. Standard MQTT light definitions do not gain this automatically. See [HA transition integration](docs/HA-TRANSITIONS.md), [native ramp commands](docs/RAMP-CONTROL.md) and [numeric ramp feedback](docs/RAMP-READBACK.md).

## Feedback you can interpret

An ACK confirms command transmission, not that a lamp reached a particular physical brightness. Numeric readback reports the **C-Bus group level**; it is not an independent measurement of the connected load. Ramp feedback updates HA in sampled steps, and multiple active blocks can reduce the per-group update rate.

Passive MONITOR reports complement this feedback, but do not provide a continuous stream of intermediate numeric fade levels. The bridge coordinates targeted reads and state priority to keep those different observations from fighting each other. [Passive status behavior →](docs/PASSIVE-STATUS.md)

## Development and documentation

The regression suite runs offline. Separate integration harnesses use fake MQTT/CNI servers on localhost; they do not require a second connection to real equipment.

```bash
python -B -m unittest discover -s tests -v
```

- [Deployment, checks and rollback](DEPLOYMENT.md)
- [Device discovery](docs/DEVICE-DISCOVERY.md) and [identity inventory](docs/DEVICE-INVENTORY.md)
- [Health-query compatibility](docs/DEVICE-HEALTH-COMPATIBILITY.md) and [ongoing device monitoring](docs/DEVICE-MONITORING.md)
- [Home Assistant examples](examples/homeassistant/README.md)
- [Release-by-release implementation and validation notes](releases/)

Detailed release notes and deployment instructions include the original installation's examples and paths. Adapt them to your own installation rather than copying site-specific settings unchanged.
