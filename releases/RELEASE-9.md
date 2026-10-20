# Release 9: immediate ordinary dimmer controls

Baseline: Release 8 (54de48d).

Ordinary HA brightness values and remembered-brightness ON commands now send the
native immediate rate (0x02) for every configured percentage dimmer. Previously
these used a four-second full-scale rate (0x0A), causing the first numeric readback
to return an intermediate value and move the HA slider backwards.

Explicit JSON ramp durations and incoming physical wall-switch ramps remain
unchanged. Fans retain their existing rate and ON behavior; relays and binary
sensors are unchanged. Numeric readback remains authoritative, so feedback can
still be delayed by the existing polling budget. No HA YAML changes required.

Validation covers ordinary brightness and ON at all 256 group addresses, explicit
60-second ramps, fan behavior, OFF, checksums, and the existing regression suite.
Deploy with the existing check/apply helper and retain its printed rollback command.
Accept with Study group 13 slider up/down, remembered ON, explicit fade-to-off,
physical switches and a fan. Restore starting levels after testing.
