# C-Bus features and the current MQTT bridge

## What is exposed today

The bridge loads 72 configured group profiles: 60 lights (20 with brightness),
8 fan presets, and 4 binary sensors. It exposes ON/OFF, brightness/levels, fixed fan
presets, bridge availability/status metrics and explicit status resync. It reads
only application 56 on the configured logical local network 254.

A group level describes a network variable, not an independently measured physical
load. Positive command ACKs confirm transmission onto C-Bus. Current optimistic
MQTT updates should not be described as measured lamp/fan feedback.

## Useful additions to discuss

| Addition | Current gap | What we need before implementing |
| --- | --- | --- |
| Bus events | Source unit and command details are parsed but not exposed as events | Define a non-retained event schema and choose useful HA triggers |
| Transition/ramp controls | Release 6 adds opt-in native rates and stop on existing dimmer brightness topics; legacy commands keep their fixed rate | See [RAMP-CONTROL.md](RAMP-CONTROL.md) for full-scale semantics and acceptance |
| Native scene triggers | No Trigger Control application support | Toolkit/project or passive traffic evidence of actual scene trigger groups/actions |
| Enable controls | No Enable Control application support | Determine which enables use that application and which are ordinary lighting groups |
| Numeric sensors | Binary profiles expose only group ON/OFF | Inventory actual measurement-capable units and their application/units/scaling |
| Blind/shutter entities | No HA cover model | Confirm installed unit types and travel/position semantics |
| Additional applications/networks | No routing or per-application dispatch | Actual topology and addressing; monitoring/init/query design |
| Labels/discovery | Topics and entity definitions come from YAML | Decide whether optional HA MQTT discovery is desirable; preserve existing entity IDs |
| Quality/diagnostics | Unknown/corrupt readings are skipped, not exposed with quality | Define stale/unknown states, counters and fault interpretation without inventing hardware failures |

These are candidate extensions, not claims that every listed device or function is
installed. Mapping names that suggest scenes, sensors or enable functions do not
prove which application or physical equipment is behind them.

For events, a bus command is not necessarily a distinct wall-button press. Some
presses may send no bus command or send repeated/dimming commands. Source unit
address alone does not identify an individual key on a multi-key unit.

Native C-Bus scenes and HA scenes differ: a scene hosted in a C-Bus controller may
be triggered rather than reconstructed as individual HA light commands. Observed
group changes alone do not establish which scene was requested.

My suggested priority is bus events and flexible dimmer transitions, followed by
native scene/enable support if the site configuration confirms it. Numeric sensors
and blinds should follow a hardware inventory. Keep these feature additions separate
from Release 3's parser/startup work and the later command-queue refactor.

## Sources and boundaries

Clipsal's lighting quick start documents ramp rates, checksums, monitored commands
and the meaning of positive ACKs:
https://ckm-content.se.com/ckmContent/sfc/servlet.shepherd/document/download/0698V00000kHfpkQAC

Clipsal's developer documentation lists Trigger Control, Enable Control,
Measurement, network discovery and DLT label capabilities in its broader driver:
https://updates.clipsal.com/ClipsalSoftwareDownload/mainsite/cis/technical/downloads/developer_tools.html

Clipsal describes broader lighting, climate, energy, A/V and security integration:
https://www.clipsal.com/training/c-bus-resources

These establish capabilities of the wider system, not what this custom bridge or
this installation supports. No live HA configuration changes are authorized by
this feature discussion; those still require explicit user approval.
