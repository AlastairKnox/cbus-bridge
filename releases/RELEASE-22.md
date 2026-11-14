# Release 22: explicit virtual eDLT stereo control

Fixes a command-only dimmer group waiting indefinitely for physical readback.
The opt-in `254/56/250@110` profile accepts the specified eDLT source and exposes a
clearly labelled virtual command level, with monotonic ramp estimates and STOP.
Real lighting groups keep their existing readback and ACK rules. Connection loss
invalidates the virtual session; action events are never retained or replayed.

The separate HA example replaces the competing on/dimmer/off handlers with one
fresh-event coordinator and cancellable startup script. Normal volume stays
0.12..0.5, startup cannot raise a known quieter setting, and the existing 0.65 hard
cap is retained and extended to MA. Cached light state cannot start music.

This release is based on c244163 and deliberately excludes the still-uncommitted
Release 21 error-clearing work. No new runtime dependencies or module files.

Validation: existing 307 tests pass, plus focused virtual-control regressions;
a localhost full-process simulation covers the user's exact frames, ordinary
light isolation, ramp/STOP, OFF and reconnect without action replay. Eight extra
offline HA YAML/template checks validate limits, event age/order/replay rejection,
startup cancellation and preservation of unrelated automations. These are not a
substitute for HA's own configuration check and a physical eDLT acceptance test.

See [virtual controls](../docs/VIRTUAL-CONTROLS.md). Bridge deployment needs the
opt-in environment setting and normal backed-up release apply. HA needs the
additive package, disabling three old stereo handlers, extending the cap and
changing only Stereo's availability. Reload MQTT/automations/scripts/helpers as
supported by the installed HA version, or perform one checked HA restart.
