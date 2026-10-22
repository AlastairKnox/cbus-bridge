import json
import unittest
from unittest.mock import patch
import test_release12 as previous
from cbus_bridge.utils import BridgeDiagnostics

class ConfiguredDurationTests(unittest.TestCase):
    def test_upstairs_uses_block_64_and_only_its_own_report(self):
        helper = previous.DurationBridgeTests()
        b, p = helper.make(group=72)
        original = b.cni.send_hex_frame.side_effect
        queried = []
        def send(frame):
            if len(frame) == 16:
                queried.append(frame)
                b.handle_cbus_level_update(56, 13, 1, source='STATUS')
                self.assertIsNone(b._duration_level)
                b.handle_cbus_level_update(56, 72, 255, source='STATUS')
            else:
                original(frame)
        b.cni.send_hex_frame.side_effect = send
        helper.send(b, p, {'level': 50, 'transition_seconds': 20})
        raw = bytes.fromhex(queried[0])
        self.assertEqual((raw[5], raw[6]), (56, 64))
        self.assertIsNone(b._duration_key)
        sent = bytes.fromhex(b.cni.send_hex_frame.call_args.args[0][:-1])
        self.assertEqual((sent[3], sent[4], sent[5]), (0x32, 72, 128))

    def test_missing_readback_has_actionable_failure_without_write(self):
        helper = previous.DurationBridgeTests()
        b, p = helper.make(group=72)
        with patch.object(b, '_duration_start_level', return_value=None):
            helper.send(b, p, {'level': 0, 'transition_seconds': 20})
        b.cni.send_hex_frame.assert_not_called()
        failure = b.diagnostics.snapshot()['last_command_failure']
        self.assertEqual((failure['reason'], failure['group']), ('fresh_level_unavailable', 72))

    def test_binary_profile_rejected(self):
        helper = previous.DurationBridgeTests()
        b, p = helper.make(group=72)
        p.level_mode = 'none'
        helper.send(b, p, {'level': 50, 'transition_seconds': 20})
        b.cni.send_hex_frame.assert_not_called()
        self.assertEqual(b.diagnostics.snapshot()['last_command_failure']['reason'], 'unsupported_profile')

    def test_failure_snapshot_is_independent_and_queue_rejection_visible(self):
        d = BridgeDiagnostics({}, wall=lambda: 123)
        d.command_failure('timeout', app=56, group=72)
        snap = d.snapshot()
        snap['last_command_failure']['group'] = 13
        self.assertEqual(d.snapshot()['last_command_failure']['group'], 72)
        d.bump('queue_full_rejected_total')
        self.assertEqual(d.snapshot()['last_command_failure']['reason'], 'queue_full_rejected')
        self.assertEqual(d.snapshot()['counters']['command_failures_total'], 2)
