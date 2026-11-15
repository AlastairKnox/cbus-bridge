"""Manual clears wait once for the shared inventory slot, with durable admission."""
import json
from pathlib import Path
from unittest.mock import Mock, patch
import unittest
import test_release21 as previous
from cbus_bridge.utils import DeviceInventory, DeviceMonitor, DeviceErrorStore, BridgeDiagnostics
from test_release20 import config


class WaitingClearTests(unittest.TestCase):
    setUp = previous.ClearTests.setUp
    advance = previous.ClearTests.advance
    request = previous.ClearTests.request
    transmit = previous.ClearTests.transmit
    reply = previous.ClearTests.reply

    def waiting(self):
        self.i.last_start = self.now[0]
        self.assertTrue(self.request())
        self.assertEqual(self.m.error_store.record(0)['outcome'], 'waiting')

    def test_cooldown_admits_once_and_blocks_background_then_verifies_fresh_zero(self):
        self.waiting()
        for seconds in (10, 10, 9):
            self.advance(seconds)
            self.m.tick(True)
            self.assertIsNone(self.i.reserve())
        self.assertEqual(self.i.job['state'], 'idle')
        self.send.assert_not_called()
        self.advance(1)
        self.transmit()
        self.assertEqual(self.i.job['mode'], 'clear_errors')
        self.assertEqual(self.m.error_store.record(0)['outcome'], 'preflight')
        self.assertTrue(self.reply(5))
        self.advance(); self.transmit()
        self.advance(); self.transmit(); self.reply(0)
        self.i.reserve(); self.m.tick(True)
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.m.snapshot()[0]['last_clear']['outcome'], 'verified_clear')
        self.assertEqual(self.m.error_store.baseline(0, 5), 0)

    def test_running_health_finishes_then_waiting_clear_precedes_next_monitor(self):
        self.assertTrue(self.i.request({'health':[0]}, True))
        self.transmit()
        job = self.i.job['id']
        self.assertTrue(self.request())
        self.assertEqual(self.i.job['id'], job)
        self.assertEqual(self.i.job['mode'], 'health')
        self.assertFalse(self.i.request({'health':[0]}, True))
        self.reply(5)
        self.assertIsNone(self.i.reserve())  # Finish the existing read job.
        self.assertEqual(self.m.error_store.record(0)['outcome'], 'waiting')
        self.advance(29); self.m.tick(True)
        self.assertIsNone(self.i.reserve())
        self.advance(1); self.transmit()
        self.assertEqual(self.i.job['mode'], 'clear_errors')
        self.assertNotIn('before_flags', self.m.error_store.record(0))
        self.reply(4)  # Only this new preflight is accepted as the saved before-value.
        self.assertEqual(self.m.error_store.record(0)['before_flags'], 4)

    def test_second_request_cannot_replace_or_duplicate_waiting_intent(self):
        self.waiting()
        self.assertFalse(self.request())
        self.assertFalse(self.request(request_id=2))
        self.assertEqual(self.i.clear_waiting['request_id'], 1)
        self.assertEqual(self.m.error_store.record(0)['request_id'], 1)
        self.assertEqual(self.m.clear_request_snapshot()['reason'], 'clear_already_pending')
        self.advance(30); self.transmit()
        self.assertEqual(self.send.call_count, 1)

    def test_waiting_expires_without_any_bus_write_or_baseline_change(self):
        self.i.last_start = 100
        self.assertTrue(self.request())
        self.advance(60)
        self.assertIsNone(self.i.reserve())
        self.assertIsNone(self.i.clear_waiting)
        self.assertEqual(self.m.error_store.record(0)['outcome'], 'waiting_expired')
        self.assertEqual(self.m.error_store.baseline(0, 5), 5)
        self.send.assert_not_called()
        self.advance(100); self.assertIsNone(self.i.reserve())

    def test_disconnect_and_explicit_cancel_drop_waiting_intents(self):
        for reason in ('connection_lost','cancelled'):
            with self.subTest(reason=reason):
                self.setUp(); self.waiting(); self.i.cancel(reason)
                self.assertIsNone(self.i.clear_waiting)
                self.i.set_interface_unit(99); self.advance(100)
                self.assertIsNone(self.i.reserve())
                self.assertEqual(self.m.error_store.record(0)['outcome'], reason)
                with self.assertRaises(ValueError):
                    DeviceErrorStore(self.path, config(1)).begin(0, 1)

    def test_restart_marks_waiting_interrupted_and_consumed_id_cannot_replay(self):
        self.waiting()
        i = DeviceInventory(BridgeDiagnostics({}), clock=lambda:self.now[0], wall=lambda:self.wall[0])
        i.set_interface_unit(99)
        m = DeviceMonitor(i, config(1), clock=lambda:self.now[0], wall=lambda:self.wall[0])
        m.enable_error_clearing(self.path)
        self.assertEqual(m.error_store.record(0)['outcome'], 'interrupted_before_send')
        self.assertIsNone(i.clear_waiting); self.assertIsNone(i.reserve())
        self.assertFalse(m.clear_errors({'unit':0,'request_id':1,'confirm':True}, True))
        self.assertEqual(m.clear_request_snapshot()['state'], 'rejected')

    def test_admission_disk_failure_does_not_reserve_a_wait_or_disturb_existing_job(self):
        self.i.last_start = 0
        with patch('os.replace', side_effect=OSError('disk')):
            self.assertFalse(self.request())
        self.assertIsNone(self.i.clear_waiting)
        self.assertEqual(self.i.job['state'], 'idle')
        self.assertFalse(self.m.error_store.enabled)
        self.send.assert_not_called()

    def test_activation_disk_failure_never_sends_preflight_or_reset(self):
        self.waiting(); self.advance(30)
        with patch('os.replace', side_effect=OSError('disk')):
            self.assertIsNone(self.i.reserve())
        self.assertIsNone(self.i.clear_waiting)
        self.assertNotEqual(self.i.job['state'], 'running')
        self.assertFalse(self.m.error_store.enabled)
        self.send.assert_not_called()
        self.assertEqual(DeviceErrorStore(self.path, config(1)).record(0)['outcome'], 'interrupted_before_send')

    def test_feedback_publishes_waiting_and_actual_rejection_without_rewriting_audit(self):
        self.waiting(); p = Mock(); self.m.publish(p)
        summary = json.loads(next(c.args[1] for c in p.publish.call_args_list
                                  if c.args[0] == 'cbus/bridge/device_monitor'))
        self.assertEqual(summary['error_clear_request']['state'], 'waiting')
        self.assertFalse(self.request(request_id=2))
        p.reset_mock(); self.m.publish(p)
        summary = json.loads(next(c.args[1] for c in p.publish.call_args_list
                                  if c.args[0] == 'cbus/bridge/device_monitor'))
        self.assertEqual(summary['error_clear_request']['state'], 'rejected')
        self.assertEqual(summary['error_clear_request']['reason'], 'clear_already_pending')
        self.assertEqual(self.m.error_store.record(0)['outcome'], 'waiting')

    def test_state_progress_forces_publication_before_normal_heartbeat(self):
        self.waiting(); self.m.tick(True); self.m.publish(Mock())
        self.assertGreater(self.m.next_publish, self.now[0])
        self.advance(30); self.transmit(); self.m.tick(True)
        self.assertEqual(self.m.next_publish, 0)
        self.assertEqual(self.m.clear_request_snapshot()['state'], 'preflight')

    def test_unready_readonly_disabled_and_invalid_requests_are_visible_rejections(self):
        for ready, readonly, expected in [(False,False,'command_path_not_ready'),(True,True,'readonly')]:
            self.assertFalse(self.m.clear_errors({'unit':0,'request_id':1,'confirm':True},ready,readonly))
            self.assertEqual(self.m.clear_request_snapshot()['reason'], expected)
        self.assertFalse(self.m.clear_errors({'unit':0,'request_id':1,'confirm':False},True))
        self.assertEqual(self.m.clear_request_snapshot()['reason'], 'invalid_clear_request')
        self.assertIsNone(self.i.clear_waiting); self.send.assert_not_called()
