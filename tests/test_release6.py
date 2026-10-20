import json
import unittest
from unittest.mock import Mock, patch
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge.ha_profiles import GroupProfile
from cbus_bridge.utils import CommandWorker, parse_transition_payload
from cbus_bridge.cni_protocol import RAMP_CODES, RAMP_COMMANDS


class TransitionTests(unittest.TestCase):
    topic = 'cbus/write/254/56/1/ramp'

    def make_bridge(self, mode='pct', readonly=False, ack='.'):
        p = GroupProfile(254, 56, 1, state_topic='state', command_topic='cbus/write/254/56/1/switch',
                         level_state_topic='level', level_command_topic=self.topic, level_mode=mode)
        cni, mqtt = Mock(), Mock()
        bridge = CbusMqttBridge(cni, mqtt, {(254, 56, 1): p}, readonly, ack_timeout_s=.001)
        if ack:
            cni.send_hex_frame.side_effect = lambda frame: bridge._complete_ack(frame[-1], ack)
        return bridge, p

    def send(self, bridge, payload):
        bridge.handle_mqtt_command(self.topic, json.dumps(payload).encode())

    def test_all_vendor_rates_are_encodable_and_readable(self):
        seconds = [0, 4, 8, 12, 20, 30, 40, 60, 90, 120, 180, 300, 420, 600, 900, 1020]
        codes = [2, 10, 18, 26, 34, 42, 50, 58, 66, 74, 82, 90, 98, 106, 114, 122]
        self.assertEqual(RAMP_CODES, dict(zip(seconds, codes)))
        self.assertEqual(set(codes), set(RAMP_COMMANDS))
        for rate, code in zip(seconds, codes):
            with self.subTest(rate=rate):
                b, p = self.make_bridge()
                self.send(b, {'level': 50, 'ramp_seconds': rate})
                raw = bytes.fromhex(b.cni.send_hex_frame.call_args.args[0][:-1])
                self.assertEqual(raw[3], code)
                self.assertEqual(sum(raw) % 256, 0)

    def test_native_fade_and_zero_use_ramp_not_immediate_off(self):
        for level, expected in [(100, '0538001A01FFA9'), (0, '0538001A0100A8')]:
            with self.subTest(level=level):
                b, p = self.make_bridge()
                self.send(b, {'level': level, 'ramp_seconds': 12})
                self.assertEqual(b.cni.send_hex_frame.call_args.args[0][:-1], expected)
                b.mqtt.publish.assert_not_called()
                last = b.diagnostics.group_snapshot()['groups'][0]['last_transition']
                self.assertEqual(last['target_level'], 255 if level else 0)
                self.assertEqual(last['result'], 'target_transmitted')
                self.assertEqual(last['full_scale_seconds'], 12)

    def test_instantaneous_is_explicit_native_rate(self):
        b, p = self.make_bridge()
        self.send(b, {'level': 100, 'ramp_seconds': 0})
        self.assertEqual(b.cni.send_hex_frame.call_args.args[0][:-1], '0538000201FFC1')
        b.mqtt.publish.assert_not_called()

    def test_stop_has_no_level_and_cancels_synthetic_timer(self):
        b, p = self.make_bridge()
        timer = Mock(); b._pending_synthetic_timers[(56, 1)] = timer
        self.send(b, {'stop': True})
        self.assertEqual(b.cni.send_hex_frame.call_args.args[0][:-1], '0538000901B9')
        timer.cancel.assert_called_once()
        b.mqtt.publish.assert_not_called()
        last = b.diagnostics.group_snapshot()['groups'][0]['last_transition']
        self.assertEqual(last['result'], 'stop_transmitted')
        self.assertIsNone(last['target_level'])
        self.assertIsNone(b.diagnostics.group_snapshot()['groups'][0]['published_level'])

    def test_invalid_or_ambiguous_json_never_sends(self):
        invalid = ['"50"', '\ufeff{"level":50,"ramp_seconds":4}', '[]', '[50,12]', '{}', '{"level":50}', '{"stop":false}', '{"stop":1}',
                   '{"stop":true,"level":0}', '{"level":50,"ramp_seconds":5}',
                   '{"level":50,"ramp_seconds":-4}', '{"level":50,"ramp_seconds":1021}',
                   '{"level":true,"ramp_seconds":4}', '{"level":50.5,"ramp_seconds":4}',
                   '{"level":50,"ramp_seconds":4.0}', '{"level":50,"ramp_seconds":true}',
                   '{"level":101,"ramp_seconds":4}', '{"level":-1,"ramp_seconds":4}',
                   '{"level":"50","ramp_seconds":4}', '{"level":null,"ramp_seconds":4}',
                   '{"level":NaN,"ramp_seconds":4}', '{"level":50,"ramp_seconds":Infinity}',
                   '{"level":50,"ramp_seconds":4,"unknown":1}',
                   '{"level":10,"level":50,"ramp_seconds":4}', '{bad json 50}']
        for payload in invalid:
            with self.subTest(payload=payload):
                b, p = self.make_bridge()
                b.handle_mqtt_command(self.topic, payload.encode())
                b.cni.send_hex_frame.assert_not_called()
                b.mqtt.publish.assert_not_called()
                self.assertEqual(b.diagnostics.snapshot()['counters']['transition_rejected_total'], 1)

    def test_invalid_utf8_and_oversized_json_are_rejected(self):
        for payload in [b'{"level":5\xff0,"ramp_seconds":4}', b'{' + b' '*257 + b'"stop":true}']:
            b, p = self.make_bridge()
            b.handle_mqtt_command(self.topic, payload)
            b.cni.send_hex_frame.assert_not_called()

    def test_explicit_transition_does_not_change_fan_or_relay(self):
        for mode in ['fan', 'none']:
            for payload in [{'level': 50, 'ramp_seconds': 4}, {'stop': True}]:
                b, p = self.make_bridge(mode)
                self.send(b, payload)
                b.cni.send_hex_frame.assert_not_called()

    def test_permissions_and_readonly_still_apply(self):
        b, p = self.make_bridge(readonly=True)
        self.send(b, {'stop': True}); b.cni.send_hex_frame.assert_not_called()
        b, p = self.make_bridge(); p.level_command_topic = None
        self.send(b, {'level': 50, 'ramp_seconds': 4}); b.cni.send_hex_frame.assert_not_called()
        b, p = self.make_bridge()
        b.handle_mqtt_command('cbus/write/254/56/2/ramp', b'{"stop":true}')
        b.cni.send_hex_frame.assert_not_called()

    def test_failed_ack_does_not_record_transmitted_transition(self):
        for ack in ['#', '$', '%', '!']:
            b, p = self.make_bridge(ack=ack)
            self.send(b, {'level': 50, 'ramp_seconds': 12})
            b.mqtt.publish.assert_not_called()
            self.assertNotIn('last_transition', b.diagnostics.group_snapshot()['groups'][0])

    def test_queue_timeout_discards_following_transition_without_replay(self):
        b, p = self.make_bridge(ack=None)
        worker = CommandWorker(b.diagnostics, cancel=b.cancel_session)
        worker.set_link('mqtt', True); worker.set_link('cni', True)
        for _ in range(2):
            worker.submit(lambda lease: b.execute_queued(
                lambda: self.send(b, {'level': 50, 'ramp_seconds': 12}), lease))
        worker.run_one()
        self.assertFalse(worker.run_one())
        b.cni.send_hex_frame.assert_called_once()
        self.assertNotIn('last_transition', b.diagnostics.group_snapshot()['groups'][0])

    def test_profile_scale_and_legacy_comma_behavior_preserved(self):
        b, p = self.make_bridge(); p.pct_scale = 255
        self.send(b, {'level': 127, 'ramp_seconds': 4})
        self.assertEqual(bytes.fromhex(b.cni.send_hex_frame.call_args.args[0][:-1])[5], 127)
        b, p = self.make_bridge()
        b.handle_mqtt_command(self.topic, b'50,34')
        self.assertEqual(bytes.fromhex(b.cni.send_hex_frame.call_args.args[0][:-1])[3], 0x0a)
        self.assertTrue(b.mqtt.publish.called)

    def test_monitored_stop_cancels_timer_without_level_update(self):
        b, p = self.make_bridge()
        timer = Mock(); b._pending_synthetic_timers[(56, 1)] = timer
        class Done(BaseException): pass
        # Monitored source unit 3, application 56, TERMINATE RAMP group 1.
        b.cni.recv_line_bytes.side_effect = [b'050338000901B6', Done()]
        with self.assertRaises(Done): b.run_forever(False, False)
        timer.cancel.assert_called_once()
        b.mqtt.publish.assert_not_called()

    def test_transition_snapshot_is_detached(self):
        b, p = self.make_bridge()
        self.send(b, {'stop': True})
        b.diagnostics.group_snapshot()['groups'][0]['last_transition']['result'] = 'changed'
        self.assertEqual(b.diagnostics.group_snapshot()['groups'][0]['last_transition']['result'], 'stop_transmitted')
