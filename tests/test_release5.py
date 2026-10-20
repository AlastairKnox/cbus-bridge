import threading
import unittest
from unittest.mock import Mock
from cbus_bridge.utils import BridgeDiagnostics, CommandWorker, CommandRejected


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.diag = BridgeDiagnostics({})
        self.cancel = Mock()
        self.worker = CommandWorker(self.diag, capacity=2, clock=lambda: self.now, cancel=self.cancel)
        self.worker.set_link('mqtt', True)
        self.worker.set_link('cni', True)

    def test_fifo_and_reject_newest_when_full(self):
        seen = []
        for n in range(2):
            self.assertTrue(self.worker.submit(lambda lease, n=n: seen.append(n)))
        self.assertFalse(self.worker.submit(lambda lease: seen.append(3)))
        self.worker.run_one(); self.worker.run_one()
        self.assertEqual(seen, [0, 1])
        self.assertEqual(self.diag.snapshot()['counters']['queue_full_rejected_total'], 1)

    def test_expiry_never_executes(self):
        action = Mock()
        self.worker.submit(action)
        self.now = 5.
        self.worker.run_one()
        action.assert_not_called()
        self.assertEqual(self.diag.snapshot()['counters']['queue_expired_discarded_total'], 1)

    def test_disconnect_discards_and_rejects_until_both_links_return(self):
        action = Mock()
        self.worker.submit(action)
        self.worker.set_link('mqtt', False)
        self.assertFalse(self.worker.submit(action))
        self.worker.set_link('mqtt', True)
        self.assertFalse(self.worker.run_one())
        action.assert_not_called()
        self.cancel.assert_called_once()

    def test_active_lease_cannot_send_or_publish_after_disconnect(self):
        seen = []
        def operation(lease):
            self.worker.set_link('cni', False)
            self.worker.set_link('cni', True)
            lease.atomic(lambda: seen.append('sent'), expire=False)
        self.worker.submit(operation)
        self.worker.run_one()
        self.assertEqual(seen, [])

    def test_waiting_operation_does_not_block_callback_submission(self):
        entered, release = threading.Event(), threading.Event()
        self.worker.submit(lambda lease: (entered.set(), release.wait(2)))
        thread = threading.Thread(target=self.worker.run_one)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.worker.submit(lambda lease: None))
            self.worker.set_link('mqtt', False)
            self.assertEqual(self.worker.snapshot()['depth'], 0)
        finally:
            release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_failure_does_not_retry_and_discards_following_work(self):
        action = Mock(side_effect=OSError('transport'))
        following = Mock()
        self.worker.submit(action); self.worker.submit(following)
        self.worker.run_one()
        self.assertFalse(self.worker.run_one())
        action.assert_called_once(); following.assert_not_called()
        self.assertFalse(self.worker.snapshot()['ready'])

    def test_expiry_is_checked_again_before_each_send(self):
        def operation(lease):
            self.now = 6
            lease.atomic(lambda: self.fail('expired command sent'))
        self.worker.submit(operation); self.worker.run_one()
        self.assertEqual(self.diag.snapshot()['counters']['queue_expired_discarded_total'], 1)

    def test_stop_discards_and_wakes_worker(self):
        self.worker.stop()
        thread = threading.Thread(target=self.worker.run)
        thread.start(); thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.worker.submit(Mock()))


class SampleTests(unittest.TestCase):
    def test_bounded_samples_repeat_counts_and_invalid_input(self):
        d = BridgeDiagnostics({})
        for n in range(20):
            d.sample_unsupported('05%02X00' % n)
        d.sample_unsupported('051300')
        d.sample_unsupported('not hex')
        samples = d.snapshot()['unsupported_samples']
        self.assertEqual(len(samples), 16)
        self.assertEqual(samples[-1]['count'], 2)

    def test_raw_sample_is_truncated_but_full_frame_identity_is_used(self):
        d = BridgeDiagnostics({})
        d.sample_unsupported('05' * 100 + '00')
        d.sample_unsupported('05' * 100 + '01')
        samples = d.snapshot()['unsupported_samples']
        self.assertEqual(len(samples), 2)
        self.assertEqual(len(samples[0]['hex']), 160)
        self.assertTrue(samples[0]['truncated'])

class BridgeSessionTests(unittest.TestCase):
    def make_bridge(self):
        from cbus_bridge.bridge import CbusMqttBridge
        from cbus_bridge.ha_profiles import GroupProfile
        p = GroupProfile(254, 56, 1, state_topic='state', command_topic='cbus/write/254/56/1/switch')
        return CbusMqttBridge(Mock(), Mock(), {(254, 56, 1): p}, False, ack_timeout_s=.001), p

    def test_cancellation_wakes_ack_waiter_and_closes_socket(self):
        bridge, p = self.make_bridge()
        waiter = bridge._register_waiter('g')
        bridge.cancel_session()
        self.assertTrue(waiter.event.is_set())
        self.assertEqual(waiter.result, 'cancelled')
        bridge.cni.close.assert_called_once()

    def test_ack_timeout_closes_session_and_never_runs_following_command(self):
        bridge, p = self.make_bridge()
        worker = CommandWorker(bridge.diagnostics, cancel=bridge.cancel_session)
        worker.set_link('mqtt', True); worker.set_link('cni', True)
        for _ in range(2):
            worker.submit(lambda lease: bridge.execute_queued(
                lambda: bridge.handle_mqtt_command(p.command_topic, b'ON'), lease))
        worker.run_one()
        self.assertFalse(worker.run_one())
        bridge.cni.send_hex_frame.assert_called_once()
        bridge.mqtt.publish.assert_not_called()
        self.assertFalse(worker.snapshot()['ready'])

    def test_stale_startup_cannot_reenable_queue(self):
        worker = CommandWorker(BridgeDiagnostics({}))
        token = worker.session_token()
        worker.set_link('mqtt', False)
        with self.assertRaises(CommandRejected):
            worker.ready_cni(token)
        self.assertFalse(worker.snapshot()['ready'])

    def test_startup_drains_ack_before_enabling_worker(self):
        bridge, p = self.make_bridge()
        class Done(BaseException): pass
        bridge.cni.recv_line_bytes.side_effect = [b'g.', None, Done()]
        ready = Mock()
        with self.assertRaises(Done):
            bridge.run_forever(False, False, on_ready=ready)
        ready.assert_called_once()
        self.assertEqual(bridge.diagnostics.snapshot()['counters']['ack_unmatched_total'], 1)

    def test_disconnected_lease_cannot_publish_optimistic_state(self):
        bridge, p = self.make_bridge()
        worker = CommandWorker(bridge.diagnostics, cancel=bridge.cancel_session)
        worker.set_link('mqtt', True); worker.set_link('cni', True)
        def operation(lease):
            worker.set_link('mqtt', False)
            worker.set_link('mqtt', True)
            bridge.execute_queued(lambda: bridge.publish_level(p, 255), lease)
        worker.submit(operation); worker.run_one()
        bridge.mqtt.publish.assert_not_called()

    def test_old_synthetic_timer_cannot_remove_replacement(self):
        from unittest.mock import patch
        bridge, p = self.make_bridge()
        callbacks = []
        def timer(delay, callback):
            callbacks.append(callback)
            return Mock()
        with patch('cbus_bridge.bridge.threading.Timer', side_effect=timer):
            bridge._delay_synthetic_level_update(56, 1, 0, source='SAL')
            bridge._delay_synthetic_level_update(56, 1, 255, source='SAL')
            callbacks[0]()
            self.assertIn((56, 1), bridge._pending_synthetic_timers)
            bridge.cancel_session()
            callbacks[1]()
        bridge.mqtt.publish.assert_not_called()

    def test_expired_resync_does_not_create_pending_or_transport_error(self):
        bridge, p = self.make_bridge()
        now = [0.]
        worker = CommandWorker(bridge.diagnostics, clock=lambda: now[0])
        worker.set_link('mqtt', True); worker.set_link('cni', True)
        def operation(lease):
            now[0] = 6
            bridge.execute_queued(lambda: bridge.request_resync(), lease)
        worker.submit(operation); worker.run_one()
        bridge.cni.send_hex_frame.assert_not_called()
        snap = bridge.diagnostics.snapshot()
        self.assertEqual(snap['status_pending_groups'], 0)
        self.assertEqual(snap['errors_observed_total'], 0)
