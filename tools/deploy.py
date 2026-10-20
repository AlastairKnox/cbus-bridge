#!/usr/bin/env python3
"""Local cgate release installer. No network access; no dependency/config changes."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

FILES = tuple('cbus_bridge/' + n + '.py' for n in
              ('__init__', 'bridge', 'cni_connection', 'cni_protocol', 'ha_profiles', 'main', 'utils')) + ('run_bridge.py', 'run_bridge_service.sh')
SERVICE = 'cbus-bridge.service'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def inventory(root):
    """Compare contents, links, modes and ownership without displaying secrets."""
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        st = path.lstat()
        value = os.readlink(path) if path.is_symlink() else digest(path) if path.is_file() else None
        result[str(path.relative_to(root))] = [st.st_mode, st.st_uid, st.st_gid, value]
    return result


def checked_copy(source, target):
    shutil.copytree(source, target, symlinks=True, copy_function=copy_owned)
    for item in [source, *source.rglob('*')]:
        st = item.lstat()
        copied = target / item.relative_to(source)
        os.chown(copied, st.st_uid, st.st_gid, follow_symlinks=False)
        if not item.is_symlink():
            os.chmod(copied, st.st_mode & 0o7777)
    if inventory(source) != inventory(target):
        raise RuntimeError('Backup/restore verification failed; production has not been changed')


def copy_owned(source, target):
    shutil.copy2(source, target, follow_symlinks=False)
    st = os.stat(source, follow_symlinks=False)
    os.chown(target, st.st_uid, st.st_gid, follow_symlinks=False)
    if not Path(source).is_symlink():
        os.chmod(target, st.st_mode & 0o7777)
    return target


class Deployment:
    def __init__(self, root=Path('/')):
        # Alternate roots are used only by the offline test suite.
        self.root = root
        self.live = root / 'opt/cbus_bridge'
        self.backups = root / 'var/backups/cbus-bridge'
        self.env = root / 'etc/cbus-bridge.env'
        self.unit = root / 'etc/systemd/system/cbus-bridge.service'

    def ctl(self, *args):
        return subprocess.check_output(['systemctl', *args], text=True).strip()

    def health(self):
        pid = self.ctl('show', SERVICE, '--property=MainPID', '--value')
        if not pid.isdigit() or int(pid) == 0:
            raise RuntimeError('Service has no running main process')
        for _ in range(10):
            self.ctl('is-active', '--quiet', SERVICE)
            if self.ctl('show', SERVICE, '--property=MainPID', '--value') != pid:
                raise RuntimeError('Service restarted during health check')
            time.sleep(1)

    def config_path(self):
        pid = self.ctl('show', SERVICE, '--property=MainPID', '--value')
        if not pid.isdigit() or int(pid) == 0:
            raise RuntimeError('Service must be running before deployment')
        args = (self.root / f'proc/{pid}/cmdline').read_bytes().split(b'\0')
        try:
            path = Path(os.fsdecode(args[args.index(b'--ha-config') + 1]))
        except (ValueError, IndexError):
            raise RuntimeError('Cannot locate active HA configuration') from None
        if not path.is_absolute():
            path = (self.root / f'proc/{pid}/cwd').resolve() / path
        return path.resolve(strict=True)

    def preflight(self, stage):
        self.ctl('is-active', '--quiet', SERVICE)
        if self.ctl('show', SERVICE, '--property=DropInPaths', '--value'):
            raise RuntimeError('Service drop-ins require a separately reviewed deployment')
        config = self.config_path()
        if self.env.is_symlink() or self.unit.is_symlink():
            raise RuntimeError('Symlinked environment/unit requires a separately reviewed backup')
        if not config.is_file() or not self.env.is_file() or not self.unit.is_file():
            raise RuntimeError('Required production configuration is missing')
        module_files = {str(p.relative_to(stage)) for p in (stage / 'cbus_bridge').rglob('*') if p.is_file()}
        if module_files != {n for n in FILES if n.startswith('cbus_bridge/')} or (stage / 'cbus_bridge').is_symlink():
            raise RuntimeError('Runtime module layout changed; review installer file list first')
        for name in FILES:
            candidate, live = stage / name, self.live / name
            if not candidate.is_file() or candidate.is_symlink() or not live.is_file() or live.is_symlink():
                raise RuntimeError(f'Expected regular runtime file: {name}')
            if candidate.suffix == '.py':
                compile(candidate.read_text(), name, 'exec')
        subprocess.run(['bash', '-n', str(stage / 'run_bridge_service.sh')], check=True)
        # Dependency changes need their own venv/rollback plan. Check installed versions.
        requirements = (stage / 'requirements.txt').read_text().strip().splitlines()
        for line in requirements:
            if not re.fullmatch(r'[A-Za-z0-9_-]+==[A-Za-z0-9.]+', line):
                raise RuntimeError('Only reviewed exact dependency pins are supported')
        probe = 'import importlib.metadata,json,sys; print(json.dumps({n:importlib.metadata.version(n) for n in sys.argv[1:]}))'
        names = [line.split('==')[0] for line in requirements]
        versions = json.loads(subprocess.check_output(
            [str(self.live / '.venv/bin/python'), '-B', '-c', probe, *names], text=True))
        if any(versions[n] != v for n, v in (line.split('==') for line in requirements)):
            raise RuntimeError('Installed dependencies differ; refusing to change the live venv')
        self.validate_profiles(stage, config)
        return config

    def validate_profiles(self, stage, config):
        # Load candidate code against the active mapping without opening MQTT/CNI.
        # Suppress parser output: YAML diagnostics may contain sensitive source lines.
        probe = "from cbus_bridge.ha_profiles import load_ha_mqtt_profiles; import sys; load_ha_mqtt_profiles(sys.argv[1])"
        result = subprocess.run(
            [str(self.live / '.venv/bin/python'), '-B', '-c', probe, str(config)],
            cwd=stage, capture_output=True, text=True,
        )
        if result.returncode:
            raise RuntimeError('Candidate rejected the active HA mapping; production was not changed. Review configuration compatibility privately.')

    def snapshot(self, stage, commit, config):
        self.backups.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.backups, 0o700)
        backup = Path(tempfile.mkdtemp(prefix=commit[:12] + '-', dir=self.backups))
        print(f'Backup: {backup}', flush=True)
        checked_copy(self.live, backup / 'app')
        for source, name in [(self.env, 'environment'), (self.unit, 'unit'), (config, 'ha-config')]:
            copy_owned(source, backup / name)
            if digest(source) != digest(backup / name):
                raise RuntimeError('Configuration changed during backup; aborting')
        # Keep the root-owned candidate and recovery tool independently of the checkout.
        checked_copy(stage, backup / 'candidate')
        copy_owned(Path(__file__).resolve(), backup / 'deploy.py')
        checked_copy(backup / 'app', backup / 'restore-check')
        shutil.rmtree(backup / 'restore-check')
        metadata = {'commit': commit, 'config_path': str(config),
                    'app_inventory': inventory(backup / 'app'),
                    'config_hashes': {n: digest(backup / n) for n in ('environment', 'unit', 'ha-config')}}
        (backup / 'manifest.json').write_text(json.dumps(metadata, indent=2) + '\n')
        return backup

    def restore(self, backup):
        backup = backup.resolve(strict=True)
        if backup.parent != self.backups.resolve() or backup.stat().st_uid != os.geteuid():
            raise RuntimeError('Expected a protected backup in the deployment backup directory')
        metadata = json.loads((backup / 'manifest.json').read_text())
        if inventory(backup / 'app') != metadata['app_inventory']:
            raise RuntimeError('Backup integrity verification failed')
        for name, expected in metadata['config_hashes'].items():
            if digest(backup / name) != expected:
                raise RuntimeError('Configuration backup integrity verification failed')
        # These settings are deliberately never modified by this installer. Refuse an
        # ambiguous rollback rather than overwrite subsequent configuration edits.
        for path, name in [(self.env, 'environment'), (self.unit, 'unit'),
                           (Path(metadata['config_path']), 'ha-config')]:
            if digest(path) != metadata['config_hashes'][name]:
                raise RuntimeError('Configuration changed since backup; manual recovery required')
        restored = self.live.with_name('.cbus-restore-' + backup.name)
        if restored.exists():
            shutil.rmtree(restored)
        checked_copy(backup / 'app', restored)
        self.ctl('stop', SERVICE)
        # Preserve the failed release for diagnosis; restore at the venv's original path.
        failed = self.live.with_name('cbus_bridge.failed-' + backup.name + '-' + str(time.time_ns()))
        self.live.rename(failed)
        try:
            shutil.move(str(restored), str(self.live))
        except BaseException:
            failed.rename(self.live)
            raise
        self.ctl('start', SERVICE)
        self.health()
        print('Previous application restored; service is stable. Confirm HA operation.')

    def apply(self, stage, commit):
        config = self.preflight(stage)
        backup = self.snapshot(stage, commit, config)
        print(f'Rollback: sudo python3 {backup}/deploy.py rollback {backup}', flush=True)
        # Abort before stopping if production/config changed while making the snapshot.
        metadata = json.loads((backup / 'manifest.json').read_text())
        if inventory(self.live) != metadata['app_inventory']:
            raise RuntimeError('Production files changed during staging; aborting')
        for path, name in [(self.env, 'environment'), (self.unit, 'unit'), (config, 'ha-config')]:
            if digest(path) != metadata['config_hashes'][name]:
                raise RuntimeError('Configuration changed during staging; aborting')
        try:
            self.ctl('stop', SERVICE)
            for name in FILES:
                target = self.live / name
                st = target.stat()
                # Replace via a same-directory temporary file; retain runtime permissions.
                fd, temp = tempfile.mkstemp(prefix='.release-', dir=target.parent)
                os.close(fd)
                try:
                    shutil.copyfile(backup / 'candidate' / name, temp)
                    os.chown(temp, st.st_uid, st.st_gid)
                    os.chmod(temp, st.st_mode & 0o7777)
                    os.replace(temp, target)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
                if digest(target) != digest(backup / 'candidate' / name):
                    raise RuntimeError('Installed file verification failed')
            self.ctl('start', SERVICE)
            self.health()
        except BaseException:
            print('Deployment failed/interrupted; attempting rollback.', flush=True)
            self.restore(backup)
            raise
        print(f'Installed {commit}. Service is stable; HA/CNI acceptance is still required.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'apply', 'rollback'])
    parser.add_argument('path', type=Path)
    parser.add_argument('--commit')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo locally on cgate')
    if args.action != 'rollback' and not re.fullmatch(r'[0-9a-f]{40}', args.commit or ''):
        parser.error('A full Git commit is required')
    os.umask(0o077)
    # Serialize checks, deployment and rollback across terminals.
    with open('/run/lock/cbus-bridge-deploy.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deployment = Deployment()
        if args.action == 'rollback':
            deployment.restore(args.path)
        elif args.action == 'check':
            deployment.preflight(args.path)
            print('Preflight passed. No production files changed or services restarted.')
        else:
            deployment.apply(args.path, args.commit)


if __name__ == '__main__':
    main()
