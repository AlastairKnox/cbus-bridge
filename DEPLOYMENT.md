# Deploying to cgate

Use the desktop checkout to develop, test, commit and push to GitHub. On cgate,
use `/home/ack/cbus-bridge` for Git, not `/opt/cbus_bridge`. Production runs from
`/opt/cbus_bridge`; Git operations alone never replace the running application.

## One-time setup for these tools

In a normal ack terminal on cgate:

```bash
cd /home/ack/cbus-bridge
git pull --ff-only origin main
```

The repository's existing read-only deploy key handles GitHub access. Enter its
passphrase locally when requested. Sudo also prompts locally; no credentials
belong in chat, Git, or command arguments for deployment.

## Each release

After the reviewed commit has been pushed, replace COMMIT with its commit ID:

```bash
cd /home/ack/cbus-bridge
bash tools/release.sh check COMMIT
```

This fetches GitHub, resolves an exact commit reachable from origin/main, stages
only that commit in a temporary directory, runs its offline tests with the live
venv interpreter, and checks production configuration, service and dependency
compatibility. It also loads the active HA mapping using the candidate profile
loader, without opening MQTT/CNI connections or displaying YAML contents. It does
not install files, back up production or restart the
service. `check` is not a backup/rollback rehearsal; `apply` performs that next.

When ready for a brief outage:

```bash
bash tools/release.sh apply COMMIT
```

The same checks run again. The installer then:

1. Creates a unique protected backup under `/var/backups/cbus-bridge/`.
2. Copies the entire application including .venv, plus the active HA config,
   environment file and systemd unit. Verifies contents, permissions and ownership.
3. Rehearses a full application restore into a temporary directory, without
   starting a second bridge. Prints an independent rollback command.
4. Checks nothing changed during backup, stops the service, installs the fixed
   list of runtime files and verifies their hashes. Dependencies/settings stay intact.
5. Starts the service and checks that the same main process stays active for ten
   seconds. Installation/start/check failures attempt an automatic full-app rollback.

The backup manifest records the target commit, original app inventory and
configuration hashes. Keep the printed backup path in the maintenance record.
Backups are retained; the installer does not delete older backups. Allow roughly
three times the app's disk footprint for snapshot, rehearsal/restore and diagnostics,
plus retained older releases. If backup/rehearsal fails, the service is not stopped.
No tools are installed globally and no passwordless sudo is required.

## Acceptance and rollback

After every deployment, check http://cbuscni.lan for a fresh connection from
192.168.1.15. Check HA availability and physical command/readback for a light,
dimmer and fan where available; observe normal operation for at least 15 minutes.
A stable service process alone does not prove MQTT/CNI/physical operation.

If acceptance fails, run the exact rollback command printed during deployment:

```text
sudo python3 /var/backups/cbus-bridge/BACKUP/deploy.py rollback /var/backups/cbus-bridge/BACKUP
```

The saved helper verifies backup integrity, prepares a full restore beside the
live directory, stops the service, preserves the failed app for diagnosis, restores
the previous app at its original path, starts it and checks process stability.
Confirm HA/CNI operation again. The old venv must return to its original path.

Config is saved for disaster recovery but this installer never modifies it. If
config/unit/environment changed after backup, automatic rollback refuses to
silently overwrite those edits; a deliberate manual recovery is required.

The original manually verified release-1 fallback remains separate:

```bash
sudo bash /var/backups/cbus-bridge-before-686720f.qvCsQf/rollback.sh
```

Use that only to restore the initial import runtime, not as the default fallback
for later deployments.

## Supported scope and limitations

This first installer supports Python/launcher updates for the current module
layout. Dependency changes, new runtime files, config migrations, systemd drop-ins
or service layout changes require a separately reviewed deployment update. It
refuses unsupported layouts and mismatched dependencies. It does not alter HA
configuration, MQTT credentials, the unit, or the venv. All tests must stay offline.

Do not edit production/configuration or run another deployment concurrently.
The helper serializes its own check/apply/rollback operations with a lock.
Backups must remain root-protected and unmodified. Symlink targets outside the
application, such as the system Python interpreter, are not copied; this release
process does not modify them.

Automatic rollback covers errors caught by the installer, not power loss, forced
process termination, hardware/disk failure, physical device actions or MQTT
retained messages. The independently saved rollback helper and off-host backups
are the recovery options for interrupted operations. Never promise guaranteed
recovery. If recovery itself fails, retain the backup and diagnose locally; do not
start another deployment.

Tooling tests use isolated temporary files and fake service controls. Production
activation of the installer still requires local sudo and manual HA acceptance.

## Saved rollback helper (Release 13)

Each verified deployment snapshot also updates `/home/ack/cbus-bridge-rollback.sh`
atomically before production is stopped. Run `bash ~/cbus-bridge-rollback.sh` as
ack to restore that snapshot. Keep off-host backups too. The helper covers bridge
application files; separately edited HA schedules/scripts need their own rollback.
