import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from cbus_bridge.cni_protocol import parse_binary_status_reply, diagnose_frame, parse_status_reply
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge.ha_profiles import GroupProfile
from cbus_bridge.utils import BridgeDiagnostics
from cbus_bridge import main as app


def packet(data):
    return (bytes(data) + bytes([(-sum(data)) & 255])).hex().upper()


VENDOR_REPORTS = [
    'D8380068AA0140550550001000000014000000000000000000CF',
    'D838580000000000000000000000000000000000000000000098',
    'D638B000000000FF00000000000000000000000000000043',
]


class BinaryParserTests(unittest.TestCase):
    def test_manufacturer_three_line_status_fixture_covers_all_groups(self):
        replies = [parse_binary_status_reply(s) for s in VENDOR_REPORTS]
        self.assertTrue(all(replies))
        self.assertEqual([r.start_group for r in replies], [0, 88, 176])
        self.assertEqual([len(r.states) for r in replies], [88, 88, 80])
        self.assertEqual(set().union(*(r.states.keys() for r in replies)), set(range(256)))
        self.assertEqual([replies[0].states[g] for g in range(4)], ['absent', 'off', 'off', 'on'])
        self.assertTrue(all(diagnose_frame(s) == 'binary_status' for s in VENDOR_REPORTS))

    def test_all_packed_values_decode_least_significant_pairs_first(self):
        meanings = ('absent', 'on', 'off', 'error')
        for value in range(256):
            r = parse_binary_status_reply(packet([0xc3, 56, 12, value]))
            self.assertEqual([r.states[12+i] for i in range(4)],
                             [meanings[(value >> (i*2)) & 3] for i in range(4)])

    def test_checksum_length_empty_payload_and_bounds_are_strict(self):
        good = packet([0xc3, 56, 252, 0x99])
        self.assertEqual(set(parse_binary_status_reply(good).states), {252, 253, 254, 255})
        bad = [good[:-2]+'00', good[:-2], good+'00', 'not hex', '',
               packet([0xc2, 56, 0]), packet([0xc3, 56, 253, 0]),
               packet([0xc4, 56, 0, 0x55]), packet([0xe3, 56, 0, 0x55])]
        for s in bad:
            with self.subTest(frame=s): self.assertIsNone(parse_binary_status_reply(s))

    def test_other_application_is_not_accepted_as_lighting(self):
        self.assertEqual(diagnose_frame(packet([0xc3, 57, 0, 0x55])), 'unsupported')
        self.assertEqual(diagnose_frame(packet([0xc3, 56, 0, 0x55])[:-2]+'00'), 'checksum_failure')

    def test_binary_and_level_envelopes_do_not_cross_decode(self):
        level = packet([0xe5, 7, 56, 1, 0x55, 0x55])
        self.assertIsNone(parse_binary_status_reply(level))
        self.assertIsNone(parse_status_reply(VENDOR_REPORTS[0]))
        self.assertEqual(diagnose_frame(level), 'status')
        # EXSTAT binary coding is not a numeric level encoding.
        self.assertIsNone(parse_status_reply(packet([0xe5, 0x40, 56, 0, 0x55, 0x55])))


class BinaryBridgeTests(unittest.TestCase):
    def make_bridge(self, shared=False, readonly=False):
        p = GroupProfile(254, 56, 13, state_topic='level' if shared else 'state',
                         level_state_topic='level', level_mode='pct')
        cni, mqtt = Mock(), Mock()
        mqtt.publish.return_value.rc = 0
        return CbusMqttBridge(cni, mqtt, {(254, 56, 13):p}, readonly), p

    def reply(self, value):
        # Bits for group 13 are pair 1 in a report starting at group 12.
        return parse_binary_status_reply(packet([0xc3, 56, 12, value << 2]))

    def test_on_off_only_publish_boolean_state_not_brightness(self):
        b, p = self.make_bridge()
        for value, state in [(1, 'ON'), (2, 'OFF')]:
            b.handle_binary_status(self.reply(value))
            self.assertEqual(b.mqtt.publish.call_args.args, ('state',))
            self.assertEqual(b.mqtt.publish.call_args.kwargs['payload'], state)
        self.assertEqual(b.mqtt.publish.call_count, 2)
        self.assertEqual(p.last_nonzero_255, 255)
        row = b.diagnostics.group_snapshot()['groups'][0]
        self.assertIsNone(row['published_level'])
        self.assertEqual(row['confidence'], 'unknown')
        self.assertEqual(row['binary_status'], 'off')
        self.assertEqual(b.diagnostics.snapshot()['groups_with_reported_level'], 0)
        b.cni.send_hex_frame.assert_not_called()

    def test_error_and_absence_do_not_fabricate_off_or_a_noise_fault(self):
        b, p = self.make_bridge()
        for value in (0, 3): b.handle_binary_status(self.reply(value))
        b.mqtt.publish.assert_not_called()
        snap = b.diagnostics.snapshot()
        self.assertEqual(snap['errors_observed_total'], 0)
        self.assertEqual(snap['binary_error_configured_groups'], 1)

    def test_shared_numeric_topic_is_never_given_boolean_payload(self):
        b, p = self.make_bridge(shared=True)
        b.handle_binary_status(self.reply(1)); b.handle_binary_status(self.reply(2))
        b.mqtt.publish.assert_not_called()
        self.assertEqual(b.diagnostics.snapshot()['counters']['binary_shared_topic_skips_total'], 2)
        self.assertEqual(b.diagnostics.group_snapshot()['groups'][0]['binary_status'], 'off')

    def test_binary_does_not_complete_level_request_or_refresh_numeric_age(self):
        b, p = self.make_bridge()
        now = [100.]
        b.diagnostics = BridgeDiagnostics({(254, 56, 13):p}, clock=lambda:now[0], wall=lambda:now[0])
        b.diagnostics.status(parse_status_reply(packet([0xe5, 7, 56, 13, 0xaa, 0xaa])))
        b.diagnostics.published(p, 0, 'STATUS')
        b.diagnostics.request(56, 0)
        now[0] += 20
        b.handle_binary_status(self.reply(1))
        snap = b.diagnostics.snapshot(); row = b.diagnostics.group_snapshot()['groups'][0]
        self.assertEqual(snap['status_pending_groups'], 1)
        self.assertEqual(snap['status_overdue_groups'], 1)
        self.assertEqual(row['observation_age_s'], 20.)
        self.assertEqual(row['binary_observation_age_s'], 0.)
        self.assertEqual(row['published_level'], 0)
        self.assertEqual(row['status_quality'], 'awaiting_reply')

    def test_dedupe_respects_later_command_state_and_disconnect(self):
        b, p = self.make_bridge()
        b.handle_binary_status(self.reply(2)); b.handle_binary_status(self.reply(2))
        self.assertEqual(b.mqtt.publish.call_count, 1)
        b.publish_level(p, 128)  # sends numeric level and ON
        b.handle_binary_status(self.reply(2))
        self.assertEqual(b.mqtt.publish.call_args.kwargs['payload'], 'OFF')
        count = b.mqtt.publish.call_count
        b.cancel_session(); b.handle_binary_status(self.reply(2))
        self.assertEqual(b.mqtt.publish.call_count, count+1)

    def test_failed_mqtt_publish_is_retried_on_next_report(self):
        b, p = self.make_bridge(); b.mqtt.publish.return_value.rc = 4
        b.handle_binary_status(self.reply(1))
        b.mqtt.publish.return_value.rc = 0
        b.handle_binary_status(self.reply(1))
        self.assertEqual(b.mqtt.publish.call_count, 2)
        self.assertEqual(b.diagnostics.snapshot()['counters']['binary_state_publish_failures_total'], 1)

    def test_binary_report_cancels_obsolete_synthetic_timer(self):
        b, p = self.make_bridge(); timer = Mock()
        b._pending_synthetic_timers[(56,13)] = timer
        b.handle_binary_status(self.reply(1))
        timer.cancel.assert_called_once()

    def test_reader_accepts_standard_reports_and_filters_foreign_scope(self):
        b, p = self.make_bridge(readonly=True)
        class End(BaseException): pass
        b.cni.recv_line_bytes.side_effect = [packet([0xc3,57,12,4]).encode(),
            packet([0xc3,56,12,4]).encode(), End()]
        with self.assertRaises(End): b.run_forever(False, False)
        b.mqtt.publish.assert_called_once()
        b.cni.send_hex_frame.assert_not_called()
        self.assertEqual(b.diagnostics.snapshot()['counters']['binary_status_reports_total'], 1)

    def test_reports_for_unconfigured_groups_remain_visible_without_publishing(self):
        b, p = self.make_bridge()
        b.handle_binary_status(parse_binary_status_reply(packet([0xc3,56,100,0x55])))
        b.mqtt.publish.assert_not_called()
        self.assertIsNotNone(b.diagnostics.snapshot()['last_binary_report_ts'])
        self.assertEqual(b.diagnostics.snapshot()['counters']['binary_status_on_groups_total'], 4)


class MonitorConfigurationTests(unittest.TestCase):
    def setUp(self):
        timer = patch.object(app.threading, 'Timer')
        timer.start()
        self.addCleanup(timer.stop)

    def test_only_monitor_bit_changes_and_exstat_remains_off(self):
        for enabled, command in [(False,'A3300059g'), (True,'A3300079g')]:
            b = CbusMqttBridge(Mock(),Mock(),{},False)
            with patch('cbus_bridge.bridge.time.sleep'): b.send_init(56, monitor=enabled)
            self.assertEqual([c.args[0] for c in b.cni.send_line.call_args_list],
                             ['~~~','A3210038g','A3420002g',command])

    def test_readonly_initialization_does_not_set_monitor(self):
        b = CbusMqttBridge(Mock(),Mock(),{},True)
        b.send_init(56, monitor=True)
        b.cni.send_line.assert_not_called()

    def test_monitor_without_init_fails_before_mqtt_or_cni(self):
        with patch.object(app.mqtt,'Client') as client, self.assertRaises(SystemExit):
            app.main(['--cni-host','unused','--ha-config','unused','--monitor'])
        client.assert_not_called()

    def test_monitor_intent_is_reported_and_passed_to_reader(self):
        client,cni,bridge = Mock(),Mock(),Mock()
        bridge.ramps.snapshot.return_value = {}
        bridge.inventory.snapshot.return_value = {"version": 0}
        cni.failover_status.return_value = {}
        client.loop_start.side_effect = lambda:client.on_connect(client,None,{},0)
        class End(BaseException): pass
        bridge.run_forever.side_effect = End()
        with patch.object(app,'load_ha_mqtt_profiles',return_value={}), \
             patch.object(app.mqtt,'Client',return_value=client), \
             patch.object(app,'CniConnection',return_value=cni), \
             patch.object(app,'CbusMqttBridge',return_value=bridge), \
             patch.object(app.threading,'Thread'):
            with self.assertRaises(End):
                app.main(['--cni-host','unused','--ha-config','unused','--init','--monitor'])
        self.assertTrue(bridge.run_forever.call_args.kwargs['monitor'])
        statuses = [json.loads(c.kwargs['payload']) for c in client.publish.call_args_list
                    if c.args[0]=='cbus/bridge/status']
        self.assertTrue(all(s['cni_monitor_requested'] for s in statuses))
        self.assertTrue(all(s['cni_initialization_enabled'] for s in statuses))

    def test_launcher_default_optout_and_invalid_values_without_live_connections(self):
        source = (Path(__file__).resolve().parents[1]/'run_bridge_service.sh').read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'activate').write_text('')
            fake = root/'python'
            fake.write_text('#!/usr/bin/env python3\nimport sys,json,os\nopen(os.environ["LAUNCH_OUTPUT"],"w").write(json.dumps(sys.argv[1:]))\n')
            fake.chmod(0o700)
            source = source.replace('cd /opt/cbus_bridge','cd "$LAUNCH_TEST_ROOT"').replace(
                'source /opt/cbus_bridge/.venv/bin/activate','source "$LAUNCH_TEST_ROOT/activate"').replace(
                'exec python ', 'exec "$LAUNCH_TEST_ROOT/python" ')
            script = root/'launch.sh'; script.write_text(source)
            env = os.environ.copy()
            for key in ['CNI_HOST','CNI_PRIMARY_PORT','MQTT_HOST','MQTT_PORT','MQTT_USER','MQTT_PASS',
                        'HA_CONFIG','LOG_LEVEL','STATUS','STATUS_96','RESYNC_EVERY','RESYNC_MODE',
                        'AVAIL_TOPIC','STATUS_TOPIC','METRICS_EVERY']: env[key] = 'fixture'
            env.update(INIT='--init',LAUNCH_TEST_ROOT=str(root),LAUNCH_OUTPUT=str(root/'output'))
            for value,flag in [(None,'--monitor'),('0','--no-monitor'),('1','--monitor')]:
                env.pop('CNI_MONITOR',None)
                if value is not None: env['CNI_MONITOR'] = value
                subprocess.run(['bash',str(script)],env=env,check=True,capture_output=True)
                self.assertIn(flag,json.loads((root/'output').read_text()))
            (root/'output').unlink(); env['CNI_MONITOR']='true'
            result = subprocess.run(['bash',str(script)],env=env,capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse((root/'output').exists())
