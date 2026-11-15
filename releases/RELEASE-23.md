# Release 23 — opt-in original DLT text labels over MQTT

Home Assistant can submit short text labels to explicitly enabled local Lighting
groups, including the new playlist selector at 254/56/91. The bridge encodes
CBUS-APP/02 LABEL frames and sends them through its existing serialized worker
and single CNI connection. No new runtime dependencies or modules.

Disabled by default. Set `CBUS_DLT_LABEL_TARGET=254/56/91` to accept JSON on
`cbus/write/254/56/91/label`. English and Australian English, Toolkit variants 1..4,
and 1..16 printable ASCII characters are supported. Blank labels and icons are
not exposed. See [DLT label setup](../docs/DLT-LABELS.md).

Lighting and fade commands have queue priority. Label requests coalesce, expire,
are limited to one transmission per second, and are never replayed after a
reconnect. Duplicate ACKed text is skipped for 30 seconds. Results distinguish CNI
transmission ACK from physical display confirmation. Received label bytes cannot
be interpreted as lighting or stereo events.

The bridge release itself does not implement playlist cycling or modify HA/Toolkit
configuration. The subsequently installed [HA example](../docs/STEREO-PLAYLISTS.md)
adds playlist selection and display-name updates separately.
It excludes the still-pending Release 21 manual device-error-clearing changes.

Deployment uses the normal `tools/release.sh check|apply COMMIT` backup/rollback
workflow. Enabling the setting requires a bridge restart. Physical label updates
must be restored separately; rolling back bridge files does not undo displayed text.
For another installation, confirm the original text and DLT variant before
publishing a first short label.

Offline verification: 345 tests pass for the isolated release; 370 pass with the
pending error-clearing work also present. Real-process localhost tests pass for
label delivery, command isolation, retained-message rejection, variants, rate
limits, reconnect and missing ACKs, plus the existing stereo regression scenario.
These counts describe pre-deployment verification.

## Live acceptance recorded — 15 November 2026

The user deployed `ae85be20450e6a3fad243028fbd67f573f038afa` and confirmed that
labels published through HA appeared on the physical original Saturn DLT.
The later HA playlist selector was also accepted by the user. Its current package,
separate volume-cap automation and remaining limitations are captured in
[the stereo/playlist guide](../docs/STEREO-PLAYLISTS.md).
No runtime bridge changes are introduced by this documentation/example update.
