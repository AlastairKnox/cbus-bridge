# Release 17: flat-network presence discovery and explicit address sweep

Baseline: 7ac13c6 (Release 16). No production or HA changes during development.
Existing runtime file layout, dependencies, interface options and endpoints remain.

## Behavior

- `{"inventory":"discover"}` sends one local Master Application FF switched-status
  request. A complete validated 256-slot map gates automatic identification of its
  nonzero candidate addresses 0–254. Quiet outputs need not originate lighting.
- Accept consistent duplicate/reordered fragments; abort conflicts. A ten-second
  presence window stops on missing or incomplete reports without automatic retries
  or a full address sweep. Master reports never create lighting state or ACKs.
- `{"inventory":"sweep"}` separately requests a paced IDENTIFY walk of 0–254.
  Silent units get one type read; responders get manufacturer/firmware too.
  Address 255 is never queried or readdressed. Full modes have a one-hour deadline;
  explicit <=16-unit batches retain ten minutes.
- Existing queue priority, three-second query budget, ramp-watch pause, connection
  generation cancellation and 30-second post-job cooldown are retained. No new
  sockets or waiting worker threads. No periodic/startup discovery.
- Inventory JSON adds mode/phase/progress, presence coverage/candidates/ambiguity,
  slot-255 and known-unreported flags. Old cache rows remain historical by scan ID.
  Presence completion is not proof all physical units responded; IDENTIFY outcomes
  remain explicit. No device category or circuit names are guessed from type text.

See [operation and limitations](../docs/DEVICE-DISCOVERY.md), including standard
status replies' missing request IDs, duplicate-address limitations and local scope.
No voltage/health polling or HA device entities are added in this release.

## Validation

261 offline tests pass, covering exact request bytes, framing, out-of-order and
consistent duplicate assembly, missing/late/conflicting reports, complete-map gating,
slot 255, historical rows, queue cancellation, read-only/admission rules, full
255-address sweep bounds and all 765 identity replies within the one-hour deadline.
The existing lighting, ramp, deployment and recovery regressions remain included.

The separate localhost integration runs the real bridge against synthetic MQTT/CNI
peers. It discovers and identifies a quiet output plus the CNI, handles split TCP
frames/reordering/duplicates, enforces three-second spacing, rejects retained
requests and accepts ordinary lighting control during discovery. A second fresh
process receives only a partial map and stops without identity queries or a sweep;
lighting control continues. These are simulations, not installed-hardware proof.
The Release 16 full-process integration is also checked for backward compatibility.

## Deployment and trial

Use tools/release.sh check/apply with the exact commit. Keep the printed backup
and /home/ack/cbus-bridge-rollback.sh. No HA restart, reload or new file is required.
Check HA and wall switches both ways plus ordinary fades before starting discovery.
Listen to cbus/bridge/inventory, run the discover action from the guide, and send
its final result for review. If discovery fails, keep the result rather than
assuming absent units; the explicit sweep is a separately selected fallback.
