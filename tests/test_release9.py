import unittest
from unittest.mock import Mock
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge.ha_profiles import GroupProfile


class ImmediateDimmerTests(unittest.TestCase):
    def make(self, group, mode='pct'):
        root = f'cbus/write/254/56/{group}'
        p = GroupProfile(254, 56, group, state_topic='state',
                         level_state_topic='level', command_topic=root+'/switch',
                         level_command_topic=root+'/ramp', level_mode=mode)
        cni, mqtt = Mock(), Mock()
        b = CbusMqttBridge(cni, mqtt, {(254,56,group):p}, False,
                           ramp_readback_every=10)
        cni.send_hex_frame.side_effect = lambda frame: b._complete_ack(frame[-1], '.')
        return b, p

    def command(self, group, suffix, payload, mode='pct'):
        b,p = self.make(group, mode)
        p.last_nonzero_255 = 128
        b.handle_mqtt_command(f'cbus/write/254/56/{group}/{suffix}', payload)
        frame = bytes.fromhex(b.cni.send_hex_frame.call_args.args[0][:-1])
        self.assertEqual(sum(frame) % 256, 0)
        self.assertEqual(frame[4], group)
        return b, frame

    def test_all_group_addresses_use_immediate_slider_and_remembered_on(self):
        for group in range(256):
            for suffix, payload in [('ramp', b'50'), ('ramp', b'50,34'), ('switch', b'ON')]:
                with self.subTest(group=group, suffix=suffix, payload=payload):
                    b,frame = self.command(group, suffix, payload)
                    self.assertEqual(frame[3], 0x02)
                    self.assertEqual(frame[5], 128)
                    self.assertEqual(b.ramps.snapshot()['groups'][0]['full_scale_seconds'], 0)
                    b.mqtt.publish.assert_not_called()

    def test_explicit_ramps_keep_requested_rate(self):
        for group in (0,13,72,255):
            b,frame = self.command(group, 'ramp', b'{"level":50,"ramp_seconds":60}')
            self.assertEqual(frame[3], 0x3a)
            self.assertEqual(b.ramps.snapshot()['groups'][0]['full_scale_seconds'], 60)

    def test_fans_keep_existing_rate_and_on_level(self):
        for suffix,payload,level in [('ramp', b'128',128), ('switch',b'ON',10)]:
            b,frame = self.command(13,suffix,payload,mode='fan')
            self.assertEqual(frame[3], 0x0a)
            self.assertEqual(frame[5], level)

    def test_zero_and_off_still_send_immediate_off(self):
        for suffix,payload in [('ramp',b'0'),('switch',b'OFF')]:
            b,frame = self.command(13,suffix,payload)
            self.assertEqual(frame[3], 0x01)
