"""Protocol fixtures: manufacturer examples plus synthetic packets, not site captures."""
import unittest
from unittest.mock import Mock, patch
from cbus_bridge.cni_protocol import parse_monitored_sal, parse_monitored_sals, parse_status_reply, decode_status_packed4
from cbus_bridge.cni_connection import CniConnection
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge.ha_profiles import validate_mqtt_config, GroupProfile


def packet(data):
    b = bytes(data)
    return (b + bytes([(-sum(b)) & 255])).hex().upper()


class ProtocolTests(unittest.TestCase):
    def test_manufacturer_monitored_on_example(self):
        # CBUS-SIUG 1.17 section 4.3.3.2.
        sal = parse_monitored_sal('0503380079083F')
        self.assertEqual((sal.dev, sal.app, sal.cmd, sal.group, sal.level), (3, 56, 0x79, 8, None))

    def test_both_local_headers_and_all_documented_ramp_rates(self):
        for prefix in ([5, 3, 56, 0], [5, 3, 56, 1, 0]):
            for cmd in range(2, 0x7b, 8):
                with self.subTest(prefix=prefix, cmd=cmd):
                    sal = parse_monitored_sal(packet(prefix + [cmd, 10, 128]))
                    self.assertEqual((sal.group, sal.level, sal.cmd), (10, 128, cmd))
            for cmd in (1, 0x79, 9):
                sal = parse_monitored_sal(packet(prefix + [cmd, 10]))
                self.assertIsNone(sal.level)

    def test_compound_on_is_not_mistaken_for_a_level(self):
        frame = packet([5, 3, 56, 0, 0x79, 8, 1, 9, 0x0a, 10, 128, 9, 11])
        sals = parse_monitored_sals(frame)
        self.assertEqual([(s.cmd, s.group, s.level) for s in sals],
                         [(0x79, 8, None), (1, 9, None), (0x0a, 10, 128), (9, 11, None)])
        self.assertIsNone(parse_monitored_sal(frame))

    def test_invalid_packets_rejected_atomically(self):
        samples = [packet([5, 3, 56, 0, 0x79, 8, 0x0a, 9]),
                   packet([5, 3, 56, 0, 0x79, 8, 0xff, 9]),
                   packet([5, 3, 56, 2, 0x79, 8]),
                   '05033800790800', '0503380079083', '0503380079083F00',
                   '05 03 38 00 79 08 3F', '050338007908GG', '']
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(parse_monitored_sals(sample), [])

    def test_manufacturer_extended_status_examples(self):
        # CBUS-SIUG 1.17 section 7.4, three consecutive short-form replies.
        samples = ['F9073800AAAA000095990000000055550000000000005555555548',
                   'F907380B0000000000005555000000000000000000000000000013',
                   'F70738160000000000000000000000000000000000000000B4']
        replies = [parse_status_reply(s) for s in samples]
        self.assertEqual([r.start_group for r in replies], [0, 11, 22])
        self.assertEqual([len(r.levels) for r in replies], [11, 11, 10])
        self.assertEqual(replies[0].levels[0], 0)
        self.assertIsNone(replies[0].levels[1])
        self.assertEqual(replies[0].levels[2], 0x57)
        self.assertEqual(replies[0].levels[5], 255)

    def test_every_level_encoding_roundtrips(self):
        symbols = 'A965'
        for value in range(256):
            packed = ''.join(symbols[(value >> shift) & 3] for shift in [2, 0, 6, 4])
            self.assertEqual(decode_status_packed4(packed), value)

    def test_status_checksum_lengths_coding_and_group_bounds(self):
        valid = packet([0xe5, 7, 56, 250, 0x55, 0x55])
        self.assertEqual(parse_status_reply(valid).levels, {250: 255})
        elsewhere = packet([0xe5, 0x47, 56, 250, 0x55, 0x55])
        self.assertEqual(parse_status_reply(elsewhere).levels, {250: 255})
        bad = [valid[:-2] + '00', valid[:-2], valid + '00',
               packet([0xe3, 7, 56, 0]), packet([0xe4, 7, 56, 0, 0x55]),
               packet([0xe5, 0x40, 56, 0, 0x55, 0x55]),
               packet([0xe7, 7, 56, 255, 0x55, 0x55, 0x55, 0x55])]
        for sample in bad:
            with self.subTest(sample=sample):
                self.assertIsNone(parse_status_reply(sample))

    def test_corrupted_or_absent_group_never_becomes_off(self):
        for packed in (0, 0x1234, 0xffff, 0xaaa0):
            reply = parse_status_reply(packet([0xe5, 7, 56, 1, packed >> 8, packed & 255]))
            self.assertEqual(reply.levels, {1: None})


class EndRead(BaseException): pass


class BridgeTests(unittest.TestCase):
    def test_startup_queries_every_configured_block_only_once(self):
        cni = Mock()
        bridge = CbusMqttBridge(cni, Mock(), {}, False)
        with patch('cbus_bridge.bridge.time.sleep'):
            bridge.request_status_blocks(56, {1, 2, 95, 96, 128, 191, 192, 250})
        frames = [bytes.fromhex(c.args[0]) for c in cni.send_hex_frame.call_args_list]
        self.assertEqual([b[-2] for b in frames], [0, 64, 96, 128, 160, 192, 224])
        self.assertTrue(all(sum(b) % 256 == 0 for b in frames))

    def test_empty_and_readonly_queries_do_not_transmit(self):
        for readonly, groups in [(False, set()), (True, {250})]:
            cni = Mock()
            CbusMqttBridge(cni, Mock(), {}, readonly).request_status_blocks(56, groups)
            cni.send_hex_frame.assert_not_called()

    def test_compound_updates_are_all_published_but_terminate_is_not_fabricated(self):
        profiles = {(254, 56, g): GroupProfile(254, 56, g, state_topic=f'test/{g}') for g in [8, 9, 10]}
        cni, mc = Mock(), Mock()
        cni.recv_line_bytes.side_effect = [packet([5, 3, 56, 0, 0x79, 8, 1, 9, 9, 10]).encode(), EndRead()]
        bridge = CbusMqttBridge(cni, mc, profiles, False)
        with self.assertRaises(EndRead): bridge.run_forever(False, False)
        self.assertEqual([(c.args[0], c.kwargs['payload']) for c in mc.publish.call_args_list],
                         [('test/8', 'ON'), ('test/9', 'OFF')])

    def test_corrupted_frame_cannot_publish_state(self):
        cni, mc = Mock(), Mock()
        cni.recv_line_bytes.side_effect = [b'05033800790800', EndRead()]
        bridge = CbusMqttBridge(cni, mc, {(254, 56, 8): GroupProfile(254, 56, 8, state_topic='test')}, False)
        with self.assertRaises(EndRead): bridge.run_forever(False, False)
        mc.publish.assert_not_called()

    def test_unsupported_address_scopes_fail_before_operation(self):
        for net, app in [(0, 56), (254, 57), (1, 202)]:
            with self.subTest(net=net, app=app):
                with self.assertRaisesRegex(ValueError, 'supported local network'):
                    validate_mqtt_config({'light': [{'state_topic': f'cbus/read/{net}/{app}/1/state',
                                                      'command_topic': f'cbus/write/{net}/{app}/1/switch'}]})

    def test_cr_lf_and_crlf_traffic_is_framed(self):
        for ending in (b'\r', b'\n', b'\r\n'):
            cni = CniConnection('unused', 10001)
            cni.sock = Mock()
            cni.sock.recv.return_value = b'FIRST' + ending + b'SECOND' + ending
            self.assertEqual(cni.recv_line_bytes(), b'FIRST')
            self.assertEqual(cni.recv_line_bytes(), b'SECOND')

    def test_split_crlf_does_not_merge_packets(self):
        cni = CniConnection('unused', 10001)
        cni.sock = Mock()
        cni.sock.recv.side_effect = [b'FIRST\r', b'\nSECOND\r\n']
        self.assertEqual(cni.recv_line_bytes(), b'FIRST')
        self.assertEqual(cni.recv_line_bytes(), b'')
        self.assertEqual(cni.recv_line_bytes(), b'SECOND')
