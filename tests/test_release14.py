"""Reproductions of the reliability audit; no production network access."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import paho.mqtt.client as mqtt
import test_release12 as previous
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge.cni_connection import CniConnection
from cbus_bridge.utils import LatestPublisher, FunctionalHealth, instance_lock
from cbus_bridge.utils import BridgeDiagnostics, CommandWorker

class EndReader(BaseException): pass

class ReadinessTests(unittest.TestCase):
    def test_continuous_traffic_does_not_prevent_readiness(self):
        cni = Mock(); ready = Mock()
        n = [0]
        def recv():
            ready.assert_called_once()
            n[0] += 1
            if n[0] > 10000: raise EndReader()
            return b'g.'
        cni.recv_line_bytes.side_effect = recv
        b = CbusMqttBridge(cni, Mock(), {}, False)
        with self.assertRaises(EndReader): b.run_forever(False, False, on_ready=ready)
        self.assertEqual(n[0], 10001)

    def test_initialization_failure_never_announces_ready(self):
        cni = Mock(); cni.send_line.side_effect = OSError('failed')
        b = CbusMqttBridge(cni, Mock(), {}, False); ready = Mock()
        with self.assertRaises(OSError): b.run_forever(True, False, on_ready=ready)
        ready.assert_not_called()

    def test_setup_ack_tag_cannot_be_reused_for_lighting(self):
        b = CbusMqttBridge(Mock(), Mock(), {}, False)
        with patch('cbus_bridge.bridge.time.sleep'):
            b.send_init(56, monitor=True)
        self.assertNotIn('g', [b._next_tag() for _ in range(100)])

    def test_tcp_keepalive_is_enabled(self):
        cni = CniConnection('unused',10001); sock = Mock()
        with patch('cbus_bridge.cni_connection.socket.create_connection',return_value=sock):
            self.assertIs(cni._connect_to('unused',10001),sock)
        self.assertGreaterEqual(sock.setsockopt.call_count,1)

class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.now=[0.]; self.c=Mock(); self.d=BridgeDiagnostics({})
        self.p=LatestPublisher(self.c,self.d,capacity=4,inflight=2,clock=lambda:self.now[0])
        self.info=Mock();self.info.rc=0;self.info.is_published.return_value=False
        self.c.publish.return_value=self.info

    def test_latest_off_survives_thousands_of_updates_and_reconnect(self):
        for i in range(10000):self.p.publish('level',str(i%101),qos=1,retain=True)
        self.p.publish('level','0',qos=1,retain=True)
        self.c.publish.assert_not_called()
        self.p.set_connected(True);self.p.pump()
        self.c.publish.assert_called_once_with('level',payload='0',qos=1,retain=True)
        self.assertEqual(self.p.snapshot()['topics'],1)
        self.info.is_published.return_value=True;self.p.pump()
        self.p.set_connected(False);self.p.set_connected(True);self.p.pump()
        self.assertEqual(self.c.publish.call_count,2)
        self.assertEqual(self.c.publish.call_args.kwargs['payload'],'0')

    def test_inflight_old_value_is_followed_by_latest_not_replayed_intermediates(self):
        self.p.set_connected(True)
        self.p.publish('level','100',qos=1,retain=True);self.p.pump()
        for i in range(1000):self.p.publish('level',str(i%99),qos=1,retain=True)
        self.p.publish('level','0',qos=1,retain=True);self.p.pump()
        self.assertEqual(self.c.publish.call_count,1)
        self.info.is_published.return_value=True;self.p.pump()
        self.assertEqual(self.c.publish.call_count,2)
        self.assertEqual(self.c.publish.call_args.kwargs['payload'],'0')

    def test_topic_and_pending_limits_and_ack_age(self):
        for i in range(4):self.assertEqual(self.p.publish(str(i),'x',qos=1,retain=True).rc,0)
        self.assertNotEqual(self.p.publish('overflow','x',qos=1,retain=True).rc,0)
        self.p.set_connected(True);self.p.pump();self.now[0]=91
        self.assertEqual(self.p.snapshot()['pending'],2)
        self.assertEqual(self.p.snapshot()['oldest_pending_s'],91)

    def test_busy_topics_do_not_starve_other_topics(self):
        self.p.limit=1;self.p.set_connected(True)
        for t in ('a','b','c'):self.p.publish(t,'0',qos=1,retain=True)
        self.p.pump()
        self.info.is_published.return_value=True
        self.p.publish('a','1',qos=1,retain=True);self.p.pump();self.p.pump()
        self.assertEqual([c.args[0] for c in self.c.publish.call_args_list],['a','b','c'])

    def test_real_paho_backlog_stays_bounded_without_pubacks(self):
        c=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        c.max_queued_messages_set(32);c.max_inflight_messages_set(16)
        c._send_publish=Mock(return_value=mqtt.MQTT_ERR_SUCCESS)
        p=LatestPublisher(c,self.d);p.set_connected(True)
        for i in range(20000):
            p.publish('level',str(i%101),qos=1,retain=True);p.pump()
        self.assertEqual(len(c._out_messages),1)
        self.assertEqual(p.snapshot()['pending'],1)

    def test_local_transport_rejection_is_counted_and_latest_retained_for_retry(self):
        self.info.rc=15;self.p.set_connected(True)
        self.p.publish('level','0',qos=1,retain=True);self.p.pump()
        self.assertEqual(self.p.snapshot()['pending'],0)
        self.info.rc=0;self.p.pump()
        self.assertEqual(self.p.snapshot()['pending'],1)
        self.assertEqual(self.d.snapshot()['counters']['mqtt_publication_rejected_total'],1)

class RecoveryTests(unittest.TestCase):
    def test_singleton_excludes_second_owner_and_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as td:
            path=str(Path(td)/'lock')
            with self.assertRaises(ValueError):
                with instance_lock(path):
                    with self.assertRaises(RuntimeError):
                        with instance_lock(path): self.fail('second owner admitted')
                    raise ValueError()
            with instance_lock(path):pass

    def test_stalled_and_recovered_heartbeats(self):
        now=[0.];h=FunctionalHealth(clock=lambda:now[0],timeout=60)
        for name in ('worker','reader','metrics'):h.beat(name)
        now[0]=61;h.beat('metrics')
        self.assertEqual(h.stalled(),['worker','reader'])
        h.beat('reader');h.beat('worker');self.assertEqual(h.stalled(),[])

    def make(self):
        b,p=previous.DurationBridgeTests().make(group=72)
        w=CommandWorker(b.diagnostics,cancel=b.cancel_session)
        w.set_link('mqtt',True);w.set_link('cni',True)
        return b,p,w

    def test_silent_connection_probed_once_then_times_out(self):
        b,p,w=self.make(); now=[121.]
        b._last_valid_rx=0
        with patch('cbus_bridge.bridge.time.monotonic',side_effect=lambda:now[0]):
            b.queue_health_probe(w);w.run_one()
            b.cni.send_hex_frame.assert_called_once()
            self.assertEqual(b._health_waiting[:2],(56,64))
            b.queue_health_probe(w);self.assertEqual(w.snapshot()['depth'],0)
            now[0]=132
            with self.assertRaisesRegex(ConnectionError,'functional status probe'):
                b.run_forever(False,False)

    def test_matching_status_completes_probe_and_quiet_readonly_never_probes(self):
        b,p,w=self.make();b._health_waiting=(56,64,0)
        b._valid_rx(status=SimpleNamespace(app=56,start_group=0));self.assertIsNotNone(b._health_waiting)
        b._valid_rx(status=SimpleNamespace(app=56,start_group=64));self.assertIsNone(b._health_waiting)
        b.readonly=True;b._last_valid_rx=0;b.queue_health_probe(w)
        self.assertEqual(w.snapshot()['depth'],0)

    def test_expired_probe_reservation_does_not_disable_future_checks(self):
        b,p,w=self.make();b._last_valid_rx=0
        with patch('cbus_bridge.bridge.time.monotonic',return_value=121):b.queue_health_probe(w)
        w.items.clear()  # Model an already-expired discarded operation.
        with patch('cbus_bridge.bridge.time.monotonic',return_value=128):b.queue_health_probe(w)
        self.assertEqual(w.snapshot()['depth'],1)

    def test_interactive_command_preempts_waiting_duration_without_lighting_write(self):
        b,p,w=self.make(); entered=threading.Event();sent=[]
        def no_budget(*args):entered.set();return False
        b.ramps.send_duration_probe=Mock(side_effect=no_budget)
        w.submit(lambda lease:b.execute_queued(lambda:b._handle_transition(p,json.dumps({'level':50,'transition_seconds':20})),lease),duration=True)
        thread=threading.Thread(target=w.run_one);thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(w.submit(lambda lease:None,duration=True))
            self.assertTrue(w.submit(lambda lease:sent.append('fan'),interactive=True))
            thread.join(1);self.assertFalse(thread.is_alive())
            b.cni.send_hex_frame.assert_not_called()
            self.assertIsNone(b._duration_key)
            w.run_one();self.assertEqual(sent,['fan'])
        finally:thread.join(2)

    def test_new_command_replaces_older_same_group_and_priority_admits_when_full(self):
        w=CommandWorker(BridgeDiagnostics({}),capacity=2)
        w.set_link('mqtt',True);w.set_link('cni',True);seen=[]
        w.submit(lambda lease:seen.append('old'),key='group72')
        w.submit(lambda lease:seen.append('background'),key='resync')
        self.assertTrue(w.submit(lambda lease:seen.append('off'),interactive=True,key='group72'))
        w.run_one();w.run_one();self.assertEqual(seen,['off','background'])
        w.submit(lambda lease:None);w.submit(lambda lease:None)
        self.assertTrue(w.submit(lambda lease:None,interactive=True))
        self.assertEqual(w.snapshot()['depth'],2)

    def test_rejected_level_publish_does_not_claim_publication(self):
        b,p,w=self.make();b.mqtt.publish.return_value.rc=15
        b.handle_cbus_level_update(56,72,128,source='STATUS')
        state=b.diagnostics.group_snapshot()['groups'][0]
        self.assertIsNone(state['published_level'])
        self.assertEqual(b.diagnostics.snapshot()['counters']['state_publication_rejected_total'],1)
