import unittest
from unittest.mock import Mock, patch

from cbus_bridge.cni_connection import CniConnection
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge import main as app


class StopRun(BaseException):
    pass


class TransportTests(unittest.TestCase):
    def test_repeated_failures_never_try_removed_port(self):
        cni = CniConnection('example.invalid', 10001, secondary_port=10002,
                            failover_after_attempts=1, failover_after_seconds=1)
        with patch.object(cni, '_connect_to', side_effect=OSError('offline')) as connect:
            for _ in range(12):
                with self.assertRaises(OSError):
                    cni.connect()
        self.assertEqual([c.args[1] for c in connect.call_args_list], [10001] * 12)
        self.assertFalse(cni.failover_status()['cni_failed_over'])
        self.assertIsNone(cni.failover_status()['cni_secondary_port'])
        with patch.object(cni, '_connect_to', return_value=Mock()):
            cni.connect()
        self.assertEqual(cni.active_port, 10001)
        self.assertEqual(cni.failover_status()['cni_primary_fail_count'], 0)

    def test_receive_handles_fragmented_ack_and_frame(self):
        cni = CniConnection('example.invalid', 10001)
        cni.sock = Mock()
        cni.sock.recv.side_effect = [b'g', b'.05013800790148\r\n']
        self.assertEqual(cni.recv_line_bytes(), b'g.')
        self.assertEqual(cni.recv_line_bytes(), b'05013800790148')


class AckTests(unittest.TestCase):
    def setUp(self):
        self.cni = Mock()
        self.bridge = CbusMqttBridge(self.cni, Mock(), {}, False, ack_timeout_s=0.001)

    def test_send_failure_does_not_poison_next_bang(self):
        self.cni.send_hex_frame.side_effect = ConnectionError('lost connection')
        with self.assertRaises(ConnectionError):
            self.bridge.write_on(56, 1)
        self.assertEqual(self.bridge._ack_waiters, {})
        waiter = self.bridge._register_waiter('h')
        self.bridge._complete_bang()
        self.assertTrue(waiter.event.is_set())
        self.assertEqual(waiter.result, '!')

    def test_ack_success_failure_and_timeout_cleanup(self):
        for result, expected in [('.', True), ('#', False), ('$', False), ('%', False), ('!', False), (None, False)]:
            with self.subTest(result=result):
                def send(payload):
                    if result is not None:
                        self.bridge._complete_ack(payload[-1], result)
                self.cni.send_hex_frame.side_effect = send
                self.assertEqual(self.bridge.write_on(56, 1), expected)
                self.assertEqual(self.bridge._ack_waiters, {})


class AvailabilityPublisher:
    def __init__(self, client, diagnostics): self.client = client
    def publish(self, *args, **kwargs): return self.client.publish(*args, **kwargs)
    def set_connected(self, up): pass
    def snapshot(self): return {}
    def pump(self): pass


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.publisher_patch = patch.object(app, 'LatestPublisher', AvailabilityPublisher)
        self.publisher_patch.start()
        self.addCleanup(self.publisher_patch.stop)

    def test_mqtt_reconnect_waits_for_a_new_cni_ready_session(self):
        client, cni = Mock(), Mock()
        cni.failover_status.return_value = {}
        cni.active_port = 10001
        bridge = Mock()
        bridge.ramps.snapshot.return_value = {}
        def read_loop(**kwargs):
            client.on_connect(client, None, {}, 0)
            kwargs['on_ready']()
            client.on_disconnect(client, None, 1)
            client.on_connect(client, None, {}, 0)
            raise StopRun()
        bridge.run_forever.side_effect = read_loop
        with patch.object(app, 'load_ha_mqtt_profiles', return_value={}), \
             patch.object(app.mqtt, 'Client', return_value=client), \
             patch.object(app, 'CniConnection', return_value=cni), \
             patch.object(app, 'CbusMqttBridge', return_value=bridge):
            with self.assertRaises(StopRun):
                app.main(['--cni-host', 'example.invalid', '--ha-config', 'unused'])
        availability = [c.kwargs['payload'] for c in client.publish.call_args_list
                        if c.args[0] == app.AVAIL_TOPIC_DEFAULT]
        self.assertEqual(availability, ['offline', 'offline', 'online', 'offline', 'offline'])

    def test_mqtt_connect_before_cni_reports_offline(self):
        client = Mock()
        cni = Mock()
        cni.failover_status.return_value = {}
        def loop_start():
            client.on_connect(client, None, {}, 0)
            raise StopRun()
        client.loop_start.side_effect = loop_start
        with patch.object(app, 'load_ha_mqtt_profiles', return_value={}), \
             patch.object(app.mqtt, 'Client', return_value=client), \
             patch.object(app, 'CniConnection', return_value=cni):
            with self.assertRaises(StopRun):
                app.main(['--cni-host', 'example.invalid', '--ha-config', 'unused'])
        availability = [c.kwargs['payload'] for c in client.publish.call_args_list
                        if c.args[0] == app.AVAIL_TOPIC_DEFAULT]
        self.assertEqual(availability, ['offline'])


if __name__ == '__main__':
    unittest.main()
