# Saturn DLT stereo, playlists and dynamic labels

Working configuration captured on **15 November 2026**, after the stereo recovery
and dynamic-label bridge release `ae85be20450e6a3fad243028fbd67f573f038afa`.
This is an HA example/documentation update, not a new bridge runtime release.
It does not include or enable the pending Release 21 error-clearing feature.

## Captured files and dependencies

| File | Purpose |
|---|---|
| [Stereo package](../examples/homeassistant/cbus_stereo_controls_package.yaml) | Helpers, startup script, stereo events, playlist selection, labels and track skip |
| [Independent volume cap](../examples/homeassistant/cbus_stereo_volume_cap_automation.yaml) | One existing `automations.yaml` entry, separate from the package |
| [Stereo MQTT light](../examples/homeassistant/cbus_stereo_light.yaml) | One light to merge into an existing `mqtt.light` list |

These examples describe the original installation. They are not installed by a
bridge deployment. Existing working installations need **no HA reload, restart or
bridge deployment** just because these files have been captured in Git.

The service environment already contains both opt-ins:

```text
CBUS_VIRTUAL_CONTROL=254/56/250@110
CBUS_DLT_LABEL_TARGET=254/56/91
```

The bridge needs the configured writable percentage profile for stereo group 250;
label-only group 91 does not need a light entity. It uses the existing single CNI
connection on port 10001. HA uses Music Assistant player `media_player.big_room`,
underlying Yamaha `media_player.media_room_zone2`, and wall-control `light.stereo`.
The controller is an original Saturn DLT (5085DL-PW), source unit 110.

The existing diagnostic sensor `sensor.cbus_diag_traffic` is useful for inspection
but is not required by the playlist handler: that handler subscribes directly to
`cbus/bridge/traffic`, which must publish every second. Stereo on/off/volume uses
its own `cbus/bridge/controls/254/56/250/event` stream instead.

## Behaviour

- Stereo ON resets the logical wall level from 100% to 50%, waits for a fresh
  accepted control event, powers the players and starts the selected playlist.
- Initial MA volume is capped at 50%. Later wall dimming maps directly to volume,
  with a 12% floor and 65% ceiling. The independent automation caps both players
  at 65%. It is needed in addition to the package's own request limits.
- Stereo OFF cancels startup and pending playlist application, stops MA playback
  and powers down MA and the Yamaha zone.
- Playlist short press selects the next option, wrapping at the end. While off,
  it updates selection and label silently; during an active wall-control session
  with players ready, it replaces playback without changing volume.
- Playlist long press skips the next track **only while Big Room is playing**.
  It does not select a different playlist, alter the label, resume paused music
  or turn on a player.
- Selection settles for 1.2 seconds before the label/playback action. The newest
  selection wins; a change during stereo startup is reconciled afterward.
- Selected playlists restore across HA restart. Startup/reconnect label
  synchronization never starts music. The stereo active helper starts false;
  use OFF then ON to re-establish wall control after an HA restart if necessary.

The script entity `script.stereo_start_favourites` retains its historical ID so
existing OFF cancellation and startup-volume guards keep working. Its displayed
name is now **Stereo - Start selected playlist safely**.

## Playlist mapping

| Selector / DLT label | Music Assistant library URI |
|---|---|
| 1970s | `library://playlist/53` |
| 1980s | `library://playlist/16` |
| 1990s | `library://playlist/18` |
| 2000s | `library://playlist/17` |
| 2010s | `library://playlist/55` |
| Australian | `library://playlist/54` |

The first five refer to the corresponding decade's Happy Songs playlist. These
IDs belong to this MA database; replace them with your own exact playlist URIs.
The package uses the mapping in both startup and playlist-application actions,
including a 1970s fallback. Update both mappings, that fallback and the selector
options together. Adding a playlist to the MA library does not make its streaming
provider unnecessary. Do not assume library IDs survive a database rebuild.

## Toolkit and press events

Group 91 uses Idle / Recall 1 / Recall 2 / Idle for short press / short release /
long press / long release. Recall 1 is 50%, Recall 2 is 90%. The actual captured
RX immediate-level commands were command 2, level **127** and **229** respectively,
from unit 110, application 56, group 91. Every repeated press had a fresh sequence.
Those levels are button codes; they do not set stereo volume.

Dynamic labels use group 91, English (language 1), variant 1. Their broadcasts can
update every matching key; they are not private to a physical button number.
The DLT's existing level bar still jumps to 50%/90%. This configuration does not
hide it. Capture new commands before changing Toolkit recall levels to endpoints
such as 0/100: they may use different ON/OFF opcodes.

The playlist input currently consumes the bounded diagnostic activity window,
not a dedicated automation-grade button stream. It filters RX lighting commands
by application, group, unit and the two exact levels, checks event age, deduplicates
sequences and advances its cursor before side effects. The first packet, a sequence
reset or a gap over four seconds establishes a baseline without acting on history.
The cursor is excluded from Recorder. Ordinary duplicate history does not repeat
actions, but extreme traffic/outages can lose presses. A dedicated button-event
topic remains future work; this documentation does not claim it exists.

## Reusing the examples

1. Back up the existing HA configuration. Adapt all entity IDs, source/group
   addresses, bridge opt-ins and playlist IDs to the intended installation.
2. Keep just one copy of the package, one Stereo light and one independent cap.
   Merge the light into existing MQTT configuration; do not add a duplicate
   top-level `mqtt:` block or inherit a conflicting `availability_topic`.
3. Disable the old on/dimmer/off stereo automations. Retain or adapt the existing
   cap rather than enabling another copy of it. The captured cap's automation ID
   belongs to this site; choose an appropriate ID for a new installation.
4. Use the existing HA packages setup for the package. Merge Recorder exclusions;
   the per-second playlist cursor and raw traffic arrays should not be recorded.
5. Run HA's configuration check before reload/restart. Helper/script/automation
   reloads can activate changes; new Recorder exclusions require an HA restart.
   Verify entity IDs rather than assuming HA will not add suffixes.
6. Test OFF-state selection/labels first, then startup, volume, OFF and playing
   playlist changes. Test long-press skip separately. Check ordinary C-Bus lights
   and fans remain unaffected.

Never overwrite an evolved live Observatory/dashboard from an older example.
No dashboard replacement is needed for these stereo controls. Rolling back a
package cannot restore a text label already displayed; use a fresh label write or
Toolkit if the old text is needed. Preserve a backup before changing either system.

## Acceptance record and limits

- The user confirmed the original stereo recovery worked on the live system.
- On 15 November the user deployed the dynamic-label bridge release, published
  labels from HA and confirmed the physical DLT displayed them correctly.
- Six captured press events established repeated short/long levels 127/229.
- The six-playlist package passed HA configuration checks and template checks for
  fresh, duplicate, old, foreign-unit/group and TX/label events. Off-state wrap
  around and label transmission were checked with both players off.
- After an HA restart, selection restored, handlers resumed, and neither player
  started. The user then confirmed the physical playlist controls worked well.
- Long press was subsequently changed from previous-playlist to next-track. HA
  configuration validation, automation reload and the player's supported next-track
  feature were checked. The user subsequently confirmed that the physical
  long-press next-track action worked well on the deployed configuration.

This snapshot makes the working configuration reviewable; it is not evidence that
every failure mode has been exercised on hardware. No live state or credentials
are bundled, and current measurements are not restored as fresh observations.
