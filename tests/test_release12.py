import json
import math
import unittest
from unittest.mock import Mock, patch
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge.ha_profiles import GroupProfile
from cbus_bridge.utils import parse_transition_payload,nearest_duration_rate,RampTracker,BridgeDiagnostics,CommandWorker


class DurationParsingTests(unittest.TestCase):
    def test_duration_and_explicit_native_rates_are_distinct(self):
        self.assertEqual(parse_transition_payload('{"level":50,"transition_seconds":20.5}',100),('duration',50,20.5))
        self.assertEqual(parse_transition_payload('{"restore":true,"transition_seconds":0}',100),('duration_restore',None,0))
        self.assertEqual(parse_transition_payload('{"level":50,"ramp_seconds":20}',100),('ramp',50,20))

    def test_strict_payload_rejection(self):
        for value in ['true','-1','3601','NaN','Infinity','"20"','null']:
            with self.subTest(value=value),self.assertRaises(ValueError):
                parse_transition_payload('{"level":50,"transition_seconds":'+value+'}',100)
        for obj in [{'level':50.5,'transition_seconds':20},{'level':True,'transition_seconds':20},
                    {'restore':False,'transition_seconds':20},{'level':50,'transition_seconds':20,'ramp_seconds':20},
                    {'restore':True,'level':50,'transition_seconds':20}]:
            with self.assertRaises(ValueError):parse_transition_payload(json.dumps(obj),100)
        with self.assertRaises(ValueError):parse_transition_payload('{"level":1,"level":2,"transition_seconds":20}',100)

    def test_rate_selection_uses_distance_not_duration_alone(self):
        for start,target,want,rate in [(255,0,20,20),(255,128,20,40),(0,128,20,40),
                                       (0,255,10,8),(100,100,20,0),(0,255,0,0),(0,255,3600,1020)]:
            with self.subTest(start=start,target=target,want=want):
                actual,nominal=nearest_duration_rate(start,target,want)
                self.assertEqual(actual,rate)
                self.assertAlmostEqual(nominal,rate*abs(target-start)/255)


class DurationBridgeTests(unittest.TestCase):
    def make(self, group=13, readonly=False, interval=3):
        root=f'cbus/write/254/56/{group}'
        p=GroupProfile(254,56,group,state_topic='state',level_state_topic='level',
                       command_topic=root+'/switch',level_command_topic=root+'/ramp',level_mode='pct')
        b=CbusMqttBridge(Mock(),Mock(),{(254,56,group):p},readonly,ramp_readback_every=interval)
        b.ack_timeout_s=.001
        def send(frame):
            if frame[-1] in 'ghijklmnopqrstuvwxyz':b._complete_ack(frame[-1],'.')
        b.cni.send_hex_frame.side_effect=send
        return b,p

    def send(self,b,p,obj):b.handle_mqtt_command(p.level_command_topic,json.dumps(obj).encode())

    def test_fresh_numeric_readback_then_half_travel_twenty_second_transition(self):
        b,p=self.make();p.last_nonzero_255=1
        original=b.cni.send_hex_frame.side_effect
        def send(frame):
            if frame=='05FF00730738004A':b.handle_cbus_level_update(56,13,255,source='STATUS')
            else:original(frame)
        b.cni.send_hex_frame.side_effect=send
        self.send(b,p,{'level':50,'transition_seconds':20})
        frames=[c.args[0] for c in b.cni.send_hex_frame.call_args_list]
        self.assertEqual(frames[0],'05FF00730738004A')
        raw=bytes.fromhex(frames[1][:-1]);self.assertEqual(raw[3],0x32)
        self.assertEqual(raw[5],128)
        self.assertEqual(b.ramps.snapshot()['groups'][0]['full_scale_seconds'],40)
        record=b.diagnostics.group_snapshot()['groups'][0]['last_transition']
        self.assertEqual(record['requested_duration_s'],20)
        self.assertEqual(record['start_reported_level'],255)
        self.assertAlmostEqual(record['nominal_duration_s'],19.922,places=3)
        # The only published brightness is the fresh 100%, never target 50%.
        self.assertEqual(b.mqtt.publish.call_args_list[0].kwargs['payload'],'100')
        self.assertFalse(any(c.kwargs.get('payload')=='50' for c in b.mqtt.publish.call_args_list))

    def test_zero_duration_remains_immediate_without_probe_and_on_restores_raw_level(self):
        b,p=self.make();p.last_nonzero_255=128
        self.send(b,p,{'restore':True,'transition_seconds':0})
        raw=bytes.fromhex(b.cni.send_hex_frame.call_args.args[0][:-1])
        self.assertEqual((raw[3],raw[5]),(2,128));b.cni.send_hex_frame.assert_called_once()
        b.mqtt.publish.assert_not_called()

    def test_missing_level_cannot_send_lighting_or_fall_back_to_remembered_value(self):
        b,p=self.make()
        with patch.object(b,'_duration_start_level',return_value=None):self.send(b,p,{'level':0,'transition_seconds':20})
        b.cni.send_hex_frame.assert_not_called()
        self.assertEqual(b.diagnostics.snapshot()['counters']['duration_level_unavailable_total'],1)

    def test_probe_rejects_binary_only_and_disconnect(self):
        for disconnect in (False,True):
            b,p=self.make()
            def send(frame):
                if disconnect:b.cancel_session()
            b.cni.send_hex_frame.side_effect=send
            with patch.object(b._duration_level_event,'wait',return_value=False):
                self.assertIsNone(b._duration_start_level(p))
            b.cni.send_hex_frame.assert_called_once_with('05FF00730738004A')

    def test_readonly_and_disabled_tracker_cannot_use_duration_path(self):
        for group,readonly,interval in [(13,True,3),(13,False,0)]:
            b,p=self.make(group,readonly,interval)
            self.send(b,p,{'level':50,'transition_seconds':20})
            b.cni.send_hex_frame.assert_not_called()

    def test_stale_queued_duration_is_not_executed(self):
        b,p=self.make();now=[0.]
        w=CommandWorker(b.diagnostics,clock=lambda:now[0],cancel=b.cancel_session)
        w.set_link('mqtt',True);w.set_link('cni',True)
        w.submit(lambda lease:b.execute_queued(lambda:self.send(b,p,{'level':50,'transition_seconds':20}),lease))
        now[0]=6;w.run_one();b.cni.send_hex_frame.assert_not_called()

    def test_probe_and_background_poll_share_actual_send_budget(self):
        now=[0.];t=RampTracker(BridgeDiagnostics({}),{(56,13)},interval=3,clock=lambda:now[0])
        token=t.begin(56,13,0,60);t.ack(token,True)
        now[0]=1;old=t.reserve();action=Mock()
        self.assertTrue(t.send_duration_probe(56,0,action))
        self.assertFalse(t.send(old,action))
        now[0]=3.99;self.assertFalse(t.send_duration_probe(56,0,action))
        self.assertIsNone(t.reserve())
        now[0]=4;pending=t.reserve();self.assertIsNotNone(pending)
        self.assertTrue(t.send_duration_probe(56,0,action))
        self.assertFalse(t.send(pending,action))
        self.assertEqual(action.call_count,2)

    def test_waiting_for_probe_budget_is_bounded_and_never_sends(self):
        b,p=self.make();clock=[0.]
        def wait(*args):clock[0]+=1
        with patch('cbus_bridge.bridge.time.monotonic',side_effect=lambda:clock[0]), \
             patch.object(b.ramps,'send_duration_probe',return_value=False), \
             patch.object(b._duration_level_event,'wait',side_effect=wait):
            self.assertIsNone(b._duration_start_level(p))
        b.cni.send_hex_frame.assert_not_called()
        self.assertLessEqual(clock[0],4)

    def test_duration_query_and_ack_flow_through_independent_reader_and_worker(self):
        import queue
        import threading
        b,p=self.make();incoming=queue.Queue();stop=threading.Event();errors=[]
        class End(BaseException):pass
        def receive():
            if stop.is_set():raise End()
            try:return incoming.get(timeout=.05)
            except queue.Empty:return None
        def frame(data):return (bytes(data)+bytes([-sum(data)&255])).hex().upper().encode()
        def send(value):
            if value=='05FF00730738004A':incoming.put(frame([0xe5,7,56,13,0x55,0x55]))
            else:incoming.put((value[-1]+'.').encode())
        def reader():
            try:b.run_forever(False,False)
            except End:pass
            except Exception as exc:errors.append(exc)
        b.ack_timeout_s=1;b.cni.recv_line_bytes.side_effect=receive
        b.cni.send_hex_frame.side_effect=send
        thread=threading.Thread(target=reader,daemon=True);thread.start()
        worker=CommandWorker(b.diagnostics,cancel=b.cancel_session)
        worker.set_link('mqtt',True);worker.set_link('cni',True)
        try:
            worker.submit(lambda lease:b.execute_queued(lambda:self.send(b,p,{'level':50,'transition_seconds':20}),lease))
            self.assertTrue(worker.run_one())
            self.assertEqual(b.diagnostics.snapshot()['counters']['duration_transitions_transmitted_total'],1)
            self.assertEqual(bytes.fromhex(b.cni.send_hex_frame.call_args.args[0][:-1])[3],0x32)
        finally:
            stop.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertFalse(errors)
