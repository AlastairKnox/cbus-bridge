# Release 3 candidate: protocol validation and startup coverage

Baseline: Release 2 cb866a8. Prepared while Release 2 soaks; NOT deployed.

## Changes

- Validate incoming monitored-lighting and short-form extended-level-status
  checksums, hex framing and lengths before publishing state.
- Decode concatenated ON/OFF/ramp commands. A following command is no longer
  mistaken for an ON/OFF level. Unknown/truncated commands reject the full packet.
- Consume TERMINATE RAMP without inventing an intermediate level. Reporting its
  actual stopped level requires a later readback; no new stop command is exposed.
- Keep all documented lighting ramp rates readable. Outgoing ramp behavior stays
  unchanged. Preserve both existing local monitored packet header forms.
- Validate extended status payload sizes and group boundaries; corrupt/absent
  packed group values remain unknown rather than being turned into OFF states.
- Query each configured 32-group block at startup when --status is enabled,
  including high-numbered groups. The shared mapping needs starts 0,32,64,96,224;
  the old startup logic omitted 224 (and 96 without the legacy flag).
- Accept CR, LF and CRLF line endings, including fragmented CRLF.
- Reject profiles outside the implemented logical local network 254/application 56.
  Previous code accepted them but did not route/read them correctly. This is an
  implementation limit, not a claim that other C-Bus applications are invalid.

No changes to HA YAML, credentials, dependencies, initialization settings, outgoing
commands, MQTT topics, fan conventions or dimmer scaling. --status-include-96 is
accepted for compatibility; --status now covers all configured blocks. Existing
status/resync CLI opt-ins remain; the release does not enable them implicitly.

## Evidence and limits

52 offline tests pass locally and on cgate using its production Python environment.
The active production mapping passes validation with 72 profiles; --status is
already enabled and its configured blocks are 0,32,64,96,224. Tests include the original
37 regressions, manufacturer-published monitored/status examples, synthetic
malformed/compound frames, all 256 packed levels, startup coverage and transport
framing. These are explicitly NOT captures from this installation.

Primary protocol references:
- Clipsal CBUS-QS issue 2.0, checksum and monitored-lighting sections:
  https://ckm-content.se.com/ckmContent/sfc/servlet.shepherd/document/download/0698V00000kHfpkQAC
- Clipsal CBUS-SIUG issue 1.17, sections 6 and 7.4 (manufacturer document mirrored):
  https://manualzz.com/doc/4150831/clipsal-serial-interface-user-guides

No raw frames were found in the existing recent production journal. Logging was
not changed and no extra CNI connection was opened. Strict acceptance of real
traffic therefore still requires installation-specific confirmation. In particular,
short-form extended level replies are supported; standard binary MMI and routed
long-form CAL replies are not newly implemented. Existing input normalization is
unchanged. This release does not claim full protocol/application support.

## Activation later

Let Release 2 complete its soak first. Run check for this commit, then explicitly
choose a maintenance window before apply. The immediate fallback must be the
currently working Release 2 snapshot made by apply.

Verify relays, dimmers, fan speeds, wall-switch readbacks and groups above 127 after
restart. Confirm the existing production --status/resync settings before expecting
startup refresh; a normal running service alone is not proof that status replies
are accepted. Replay recorded frames offline when available. If state updates
stop or startup refresh regresses, roll back promptly using the printed command.
