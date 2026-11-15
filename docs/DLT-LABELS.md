# Dynamic text labels for the original Saturn DLT

This feature sends text from MQTT through the existing bridge/CNI connection.
The bridge does not choose playlists. The accompanying
[HA example](STEREO-PLAYLISTS.md) now handles group 91's 50%/90% button commands
separately and publishes the selected display name. A label never changes a lighting level.

## Opt in one label address

Default: disabled. Add to `/etc/cbus-bridge.env` on cgate, preserving every other setting:

```text
CBUS_DLT_LABEL_TARGET=254/56/91
```

The launcher enables one group. Direct CLI users can repeat
`--dlt-label-target 254/56/91` for up to 16 distinct local Lighting groups.
An HA lighting profile is not required for a label-only target. Only local network
254, application 56 and groups 0..254 are supported in this first release.

Check the intended DLT key's group, dynamic-label setting, language and variant
in Toolkit. This installation uses group 91 and English. The updated project
report confirms key 8 has Idle / Recall 1 / Recall 2 / Idle, at 50%/90%.
The live HA controls use language 1 and variant 1; the user confirmed that
dynamic labels display correctly. When adapting this setup, verify the intended
variant and record the original text before changing another key.

Labels are broadcast by group/language/variant, not addressed to unit 110 or to
screen row 8. Every matching dynamically labelled key can update. Keep a project
backup and record the original text before a live test.

## Publish the actual text

Home Assistant action, after the bridge release has been deployed and enabled:

```yaml
action: mqtt.publish
data:
  topic: cbus/write/254/56/91/label
  payload: '{"text":"Jazz","language":1,"variant":1}'
  qos: 0
  retain: false
```

- `text`: required, 1..16 printable ASCII characters. Blank-only text, empty text,
  newlines, non-ASCII, control bytes and oversized text are rejected without a write.
  Spaces are preserved. No silent truncation or automatic scrolling.
- `language`: optional; 1=English(default),2=English(Australia). Other languages
  are not exposed in this release. This never changes the network's preferred language.
- `variant`: optional; Toolkit 1..4(default 1), translated to protocol flavour 0..3.
  For example Toolkit Variant 2 sends flavour 1.
- Only those JSON fields are accepted. Duplicate fields and implicit numeric/bool
  conversions are rejected. Plain unquoted strings are not accepted.

The protocol limit is not a guarantee of visible text width. The user has verified
that “Night time” fits this original DLT. Use short display aliases such as
“Jazz”, “Favourites” or “Night time”, keeping full playlist names in HA.
Bitmaps, icons, Unicode and label erasure are not implemented.

## Result and delivery semantics

Listen before sending on `cbus/read/254/56/91/label_result`. This is a non-retained
QoS 0 JSON result; it contains text, address, language, variant, timestamp, result
and `display_confirmed:false`. It is not an MQTT light state or music action.

Results include `ack_success`, `duplicate_skipped`, `rate_limited`, `queue_rejected`,
`ack_timeout`, `retries_exhausted`, `corruption`, `clock_loss`, `bang` and `ack_failure`.
`ack_success` means a successful CNI transmission ACK, not confirmation that a
particular DLT accepted or displayed the text. Confirm the actual display visually.
Malformed/unconfigured/retained/read-only requests are counted in diagnostics rather
than publishing a result for an unvalidated address. Queue expiry or session
cancellation is reported through the existing queue diagnostics; results are best effort.

## Queue, replay and traffic bounds

- Uses the existing single command worker, write lock, ACK tags and CNI on 10001.
- Lighting controls and duration commands have priority over pending labels.
  An already-sent label waits for its ACK or existing 2.5s timeout; it cannot be preempted.
- Pending requests coalesce by group/language/variant. Label keys cannot displace
  or supersede a lighting command for the same group.
- Maximum one label transmission per second across all configured targets.
  Requests executed earlier return `rate_limited` without a write. Debounce changing
  selections in HA; on this result, send the latest text after at least one second.
  This release does not automatically defer or retry rate-limited requests.
- An identical ACKed request within 30s is skipped. A changed observed label for that
  same address clears its cached transmission evidence. The cache is bounded by
 16 targets *2 languages *4 variants.
- The existing bounded queue and 5s expiry apply. MQTT/CNI loss discards pending
  work and duplicate evidence. No label is automatically restored at startup,
  reconnect or process restart; send a fresh desired label deliberately.
- Missing ACK invalidates the connection/session before a tag can be reused.
  A failed/ambiguous write is not retried automatically.
- Retained commands are refused. Do not use retained label command topics.

Valid received text-label packets are classified as labels and cannot produce
brightness, switch or virtual stereo-control actions. Mixed packets preserve actual
lighting commands while skipping label bytes. Unsupported label formats still
reject the whole packet rather than interpreting their contents as commands.

## Acceptance recorded — 15 November 2026

Release `ae85be20450e6a3fad243028fbd67f573f038afa` was deployed by the user.
The user successfully sent labels through HA and confirmed the text on the
physical original Saturn DLT. The later playlist selector also passed the user's
physical-switch/playback checks. These observations confirm this installation,
not every DLT language/variant or unsupported display format.

See [the HA configuration and exact acceptance scope](STEREO-PLAYLISTS.md).

## Acceptance checklist for a new installation

1. Record the current text and confirm the label variant; inspect other keys
   using the same group/variant. Back up Toolkit's project.
2. Deploy with the target disabled first; check normal lighting/stereo operation.
3. Back up the environment file, add the single target setting, and restart the
   bridge through the normal release/service workflow. The DLT requires no restart.
4. Subscribe to the result topic. Send a short “Jazz” label using the verified
   variant and English. Check the text on the physical DLT.
5. Wait at least one second. Publish the exact original text using the same
   language/variant. Verify it is restored. There is no label readback or automatic
   physical-label rollback provided by this feature.
6. Verify fan control, stereo ON/reset-to 50%, volume/OFF and a physical light switch.

Removing the environment setting disables further MQTT label writes but does not
restore labels already displayed. Restoring the old bridge release likewise does
not undo a label broadcast; use a fresh label write or Toolkit to restore the text.
No HA reload is required for manually publishing labels. Playlist automations
are captured in the accompanying example; validate the single-label path before
enabling them on a new installation.

## Protocol references

Clipsal CBUS-APP/02 issue 1.5, 13 February 2007:
https://aca.im/driver_docs/Clipsal/CBUS-Lighting-Application.pdf
Section 2.4.3 (pages 6–7) gives language codes; section 2.6.5 (pages 11–12)
defines LABEL and options; section 2.9.6 (page 18) describes matching and deletion.
Class 4 local SAL is `05 38 00`, command `A0 | argument_count`, group,
options `(variant-1)<<5`, language, ASCII text, two's-complement checksum, ACK tag.
There are 3 fixed arguments plus the text byte count.
