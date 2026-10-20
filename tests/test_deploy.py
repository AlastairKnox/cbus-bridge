"""Offline release/rollback tests. No systemd, MQTT or CNI connections."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('deployment', Path(__file__).resolve().parents[1] / 'tools/deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class FakeDeployment(deploy.Deployment):
    def __init__(self, root, config):
        super().__init__(root)
        self.config = config
        self.calls = []
        self.fail_health_once = False

    def ctl(self, *args):
        self.calls.append(args)
        return ''

    def preflight(self, stage):
        return self.config

    def validate_profiles(self, stage, config):
        pass

    def health(self):
        if self.fail_health_once:
            self.fail_health_once = False
            raise RuntimeError('simulated service crash')


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'external-ha.yaml'
        self.config.write_text('private configuration')
        self.d = FakeDeployment(self.root, self.config)
        self.stage = self.root / 'stage'
        for base, prefix in [(self.d.live, 'old'), (self.stage, 'new')]:
            for name in deploy.FILES:
                p = base / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f'# {prefix} {name}\n')
                p.chmod(0o750 if name.endswith('.sh') else 0o640)
        (self.stage / 'requirements.txt').write_text('paho-mqtt==2.1.0\nPyYAML==6.0.3\n')
        venv = self.d.live / '.venv'
        venv.mkdir()
        (venv / 'dependency').write_text('unchanged dependency')
        (venv / 'link').symlink_to('dependency')
        for p in [self.d.env, self.d.unit]:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('private settings')
            p.chmod(0o600)
        self.original = deploy.inventory(self.d.live)
        self.commit = 'a' * 40

    def backup(self):
        return next(self.d.backups.iterdir())

    def test_success_and_explicit_rollback_restore_entire_app(self):
        self.d.apply(self.stage, self.commit)
        for name in deploy.FILES:
            self.assertEqual(deploy.digest(self.d.live / name), deploy.digest(self.stage / name))
            self.assertEqual((self.d.live / name).stat().st_mode & 0o777,
                             0o750 if name.endswith('.sh') else 0o640)
        self.assertTrue((self.backup() / 'deploy.py').is_file())
        self.assertEqual(self.d.env.read_text(), 'private settings')
        (self.d.live / 'extra-file').write_text('created by new release')
        self.d.restore(self.backup())
        self.assertEqual(deploy.inventory(self.d.live), self.original)
        self.assertFalse((self.d.live / 'extra-file').exists())
        self.assertEqual(self.config.read_text(), 'private configuration')

    def test_service_failure_automatically_restores_last_version(self):
        self.d.fail_health_once = True
        with self.assertRaisesRegex(RuntimeError, 'simulated service crash'):
            self.d.apply(self.stage, self.commit)
        self.assertEqual(deploy.inventory(self.d.live), self.original)
        self.assertEqual(self.d.calls.count(('start', deploy.SERVICE)), 2)

    def test_install_failure_automatically_restores_last_version(self):
        with patch.object(deploy.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaisesRegex(OSError, 'disk error'):
                self.d.apply(self.stage, self.commit)
        self.assertEqual(deploy.inventory(self.d.live), self.original)

    def test_preflight_failure_never_stops_service(self):
        with patch.object(self.d, 'preflight', side_effect=RuntimeError('wrong dependency')):
            with self.assertRaises(RuntimeError):
                self.d.apply(self.stage, self.commit)
        self.assertEqual(self.d.calls, [])
        self.assertEqual(deploy.inventory(self.d.live), self.original)

    def test_corrupt_backup_is_rejected_before_stop(self):
        self.d.apply(self.stage, self.commit)
        self.d.calls.clear()
        (self.backup() / 'app/run_bridge.py').write_text('corrupted')
        with self.assertRaisesRegex(RuntimeError, 'integrity'):
            self.d.restore(self.backup())
        self.assertEqual(self.d.calls, [])

    def test_new_configuration_is_not_overwritten_by_rollback(self):
        self.d.apply(self.stage, self.commit)
        self.d.calls.clear()
        self.config.write_text('subsequent configuration change')
        with self.assertRaisesRegex(RuntimeError, 'Configuration changed'):
            self.d.restore(self.backup())
        self.assertEqual(self.d.calls, [])
        self.assertEqual(self.config.read_text(), 'subsequent configuration change')

    def test_backup_failure_never_stops_service(self):
        with patch.object(deploy, 'checked_copy', side_effect=OSError('backup disk full')):
            with self.assertRaises(OSError):
                self.d.apply(self.stage, self.commit)
        self.assertEqual(self.d.calls, [])
        self.assertEqual(deploy.inventory(self.d.live), self.original)

    def test_real_preflight_rejects_missing_runtime_files(self):
        # Exercise the actual preflight method, with only systemctl/config mocked.
        (self.stage / 'run_bridge.py').unlink()
        with patch.object(self.d, 'config_path', return_value=self.config):
            with self.assertRaisesRegex(RuntimeError, 'regular runtime file'):
                deploy.Deployment.preflight(self.d, self.stage)
        self.assertNotIn(('stop', deploy.SERVICE), self.d.calls)

    def test_real_preflight_accepts_matching_dependencies(self):
        with patch.object(self.d, 'config_path', return_value=self.config), \
             patch.object(deploy.subprocess, 'check_output', return_value='{"paho-mqtt":"2.1.0","PyYAML":"6.0.3"}'):
            self.assertEqual(deploy.Deployment.preflight(self.d, self.stage), self.config)
        self.assertNotIn(('stop', deploy.SERVICE), self.d.calls)

    def test_real_preflight_rejects_dependency_changes(self):
        with patch.object(self.d, 'config_path', return_value=self.config), \
             patch.object(deploy.subprocess, 'check_output', return_value='{"paho-mqtt":"1.0","PyYAML":"6.0.3"}'):
            with self.assertRaisesRegex(RuntimeError, 'dependencies differ'):
                deploy.Deployment.preflight(self.d, self.stage)
        self.assertNotIn(('stop', deploy.SERVICE), self.d.calls)

    def test_real_preflight_rejects_service_dropins(self):
        with patch.object(self.d, 'ctl', side_effect=['', '/etc/systemd/system/extra.conf']):
            with self.assertRaisesRegex(RuntimeError, 'drop-ins'):
                deploy.Deployment.preflight(self.d, self.stage)

    def test_health_rejects_a_restarting_service(self):
        with patch.object(self.d, 'ctl', side_effect=['123', '', '456']), \
             patch.object(deploy.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'restarted'):
                deploy.Deployment.health(self.d)

    def test_health_accepts_stable_service(self):
        with patch.object(self.d, 'ctl', side_effect=lambda *a: '123' if a[0] == 'show' else ''), \
             patch.object(deploy.time, 'sleep'):
            deploy.Deployment.health(self.d)

    def test_candidate_mapping_validation_failure_is_private(self):
        result = type('Result', (), {'returncode': 1, 'stderr': 'private YAML content'})()
        with patch.object(deploy.subprocess, 'run', return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, 'active HA mapping') as error:
                deploy.Deployment.validate_profiles(self.d, self.stage, self.config)
        self.assertNotIn('private YAML content', str(error.exception))
        self.assertEqual(run.call_args.kwargs['cwd'], self.stage)
        self.assertTrue(run.call_args.kwargs['capture_output'])
        self.assertNotIn(('stop', deploy.SERVICE), self.d.calls)

    def test_config_change_during_snapshot_aborts_before_stop(self):
        original_snapshot = self.d.snapshot
        def snapshot(*args):
            backup = original_snapshot(*args)
            self.config.write_text('changed concurrently')
            return backup
        with patch.object(self.d, 'snapshot', side_effect=snapshot):
            with self.assertRaisesRegex(RuntimeError, 'Configuration changed during staging'):
                self.d.apply(self.stage, self.commit)
        self.assertNotIn(('stop', deploy.SERVICE), self.d.calls)
        self.assertEqual(deploy.inventory(self.d.live), self.original)


if __name__ == '__main__':
    unittest.main()
