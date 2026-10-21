import unittest
import threading
from unittest.mock import Mock, patch
from cbus_bridge.utils import RampTracker, BridgeDiagnostics, CommandWorker
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge.ha_profiles import GroupProfile
from cbus_bridge.cni_protocol import parse_binary_status_reply
from cbus_bridge import main as app


def packet(data):
    return (bytes(data)+bytes([(-sum(data)) & 255])).hex().upper()


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.now=0.
        self.d=BridgeDiagnostics({})
        self.t=RampTracker(self.d,{(56,g) for g in (13,14,40)},interval=10,clock=lambda:self.now)

    def begin(self, group=13, target=0, seconds=60):
        token=self.t.begin(56,group,target,seconds)
        self.t.ack(token,True)
        return token

    def query(self, at, action=None):
        self.now=at
        token=self.t.reserve()
        self.assertIsNotNone(token)
        send=action or Mock()
        self.assertTrue(self.t.send(token,send))
        return send

    def test_no_queries_without_a_configured_acknowledged_watch(self):
        self.assertIsNone(self.t.reserve())
        self.assertIsNone(self.t.begin(56,99,0,60))
        self.t.begin(56,13,0,60)
        self.now=2
        self.assertIsNone(self.t.reserve())

    def test_same_block_is_combined_and_other_block_obeys_global_budget(self):
        self.begin(13);self.begin(14);self.begin(40)
        self.query(1).assert_called_once_with(56,0)
        self.t.observe(56,13,200);self.t.observe(56,14,200)
        self.now=2;self.assertIsNone(self.t.reserve())
        self.query(6).assert_called_once_with(56,32)

    def test_real_study_trace_cannot_end_on_binary_off_or_first_target_sample(self):
        self.begin()
        for at,raw in [(10,214),(30,130),(50,47),(65,0)]:
            self.query(at);self.t.observe(56,13,raw)
            self.assertTrue(self.t.suppress_binary(56,13))
        self.query(70);self.t.observe(56,13,0)
        self.assertEqual(self.t.snapshot()['active_groups'],0)
        self.assertTrue(self.t.suppress_binary(56,13))
        self.assertEqual(self.d.snapshot()['counters']['ramp_tracking_completed_total'],1)

    def test_stop_requires_two_fresh_stable_replies(self):
        self.begin(target=None,seconds=None)
        self.query(1);self.t.observe(56,13,60)
        self.t.observe(56,13,60)  # duplicate/unsolicited message is not a new confirmation
        self.assertTrue(self.t.suppress_binary(56,13))
        self.query(6);self.t.observe(56,13,60)
        self.assertEqual(self.t.snapshot()['active_groups'],0)
        self.assertTrue(self.t.suppress_binary(56,13))

    def test_stale_reserved_query_cannot_cross_replacement_or_session_reset(self):
        self.begin();self.now=1;token=self.t.reserve()
        self.begin(target=255)
        action=Mock();self.assertFalse(self.t.send(token,action));action.assert_not_called()
        self.now=6;token=self.t.reserve();self.assertIsNotNone(token)
        self.t.reset();self.assertFalse(self.t.send(token,action));action.assert_not_called()
        self.assertEqual(self.t.snapshot()['active_groups'],0)

    def test_negative_ack_restores_previous_watch_without_overwriting_newer_command(self):
        old=self.begin(target=0)
        token=self.t.begin(56,13,255,4);self.t.ack(token,False)
        self.assertEqual(self.t.snapshot()['groups'][0]['target_level'],0)
        token=self.t.begin(56,13,255,4)
        self.begin(target=128)
        self.t.ack(token,False)
        self.assertEqual(self.t.snapshot()['groups'][0]['target_level'],128)

    def test_own_observed_echo_does_not_restart_local_ack_transaction(self):
        token=self.t.begin(56,13,0,60)
        self.assertIsNone(self.t.begin(56,13,0,60,observed=True))
        self.t.ack(token,True);self.query(1)

    def test_three_missing_replies_stop_polling_but_keep_bounded_protection(self):
        self.begin()
        for at in (1,11,21):self.query(at)
        self.now=25;self.assertIsNone(self.t.reserve())
        self.assertEqual(self.t.snapshot()['groups'][0]['phase'],'readback_failed')
        self.assertTrue(self.t.suppress_binary(56,13))
        self.now=91;self.assertIsNone(self.t.reserve())
        self.assertFalse(self.t.suppress_binary(56,13))
        self.assertEqual(self.d.snapshot()['counters']['ramp_readback_reply_timeouts_total'],3)

    def test_delayed_queue_dispatch_cannot_exceed_actual_bus_rate(self):
        self.begin(13);self.begin(40)
        self.now=1;first=self.t.reserve()
        self.now=5.5;self.assertTrue(self.t.send(first,Mock()))
        self.now=6;second=self.t.reserve();self.assertIsNotNone(second)
        action=Mock();self.assertFalse(self.t.send(second,action));action.assert_not_called()
        self.now=8.99;self.assertIsNone(self.t.reserve())
        self.now=9;self.assertIsNotNone(self.t.reserve())

    def test_abandoned_reservation_expires_without_sending_stale_work(self):
        self.begin();self.now=1;token=self.t.reserve()
        self.now=7;new=self.t.reserve();self.assertIsNotNone(new)
        action=Mock();self.assertFalse(self.t.send(token,action));action.assert_not_called()

    def test_expired_readback_reply_does_not_confirm_completion(self):
        self.begin();self.query(1)
        self.now=5;self.t.observe(56,13,0)
        self.assertIsNone(self.t.snapshot()['groups'][0]['last_reported_level'])

    def test_snapshot_is_detached_and_storage_is_bounded_by_configured_groups(self):
        for group in range(256):self.t.begin(56,group,0,60,observed=True)
        self.assertEqual(self.t.snapshot()['active_groups'],3)
        snap=self.t.snapshot();snap['groups'][0]['phase']='modified'
        self.assertNotEqual(self.t.snapshot()['groups'][0]['phase'],'modified')


class RampBridgeTests(unittest.TestCase):
    def make(self, interval=10, readonly=False, mode='pct', ack='.'):
        p=GroupProfile(254,56,13,state_topic='state',level_state_topic='level',
                       command_topic='cbus/write/254/56/13/switch',
                       level_command_topic='cbus/write/254/56/13/ramp',level_mode=mode)
        cni,mc=Mock(),Mock();mc.publish.return_value.rc=0
        b=CbusMqttBridge(cni,mc,{(254,56,13):p},readonly,ack_timeout_s=.001,ramp_readback_every=interval)
        now=[0.];b.ramps.clock=lambda:now[0]
        def send(frame):
            if frame[-1] in 'ghijklmnopqrstuvwxyz' and ack:b._complete_ack(frame[-1],ack)
        cni.send_hex_frame.side_effect=send
        w=CommandWorker(b.diagnostics,cancel=b.cancel_session)
        w.set_link('mqtt',True);w.set_link('cni',True)
        return b,p,w,now

    def begin(self,b,payload=b'{"level":0,"ramp_seconds":60}'):
        b.handle_mqtt_command('cbus/write/254/56/13/ramp',payload)

    def binary_off(self,b):
        b.handle_binary_status(parse_binary_status_reply(packet([0xc3,56,12,8])))

    def test_actual_target_block_frame_and_numeric_feedback_beat_binary_off(self):
        b,p,w,now=self.make()
        self.begin(b)
        self.assertTrue(b.ramps.suppress_binary(56,13))
        b.mqtt.publish.assert_not_called()
        now[0]=1;b.queue_ramp_readback(w);w.run_one()
        self.assertEqual(b.cni.send_hex_frame.call_args.args[0],'05FF00730738004A')
        b.handle_cbus_level_update(56,13,214,source='STATUS')
        calls=b.mqtt.publish.call_count
        self.binary_off(b)
        self.assertEqual(b.mqtt.publish.call_count,calls)
        self.assertEqual(b.mqtt.publish.call_args.kwargs['payload'],'ON')
        self.assertEqual(b.diagnostics.snapshot()['counters']['ramp_binary_updates_suppressed_total'],1)

    def test_binary_is_suppressed_even_before_local_ack(self):
        b,p,w,now=self.make()
        def send(frame):
            self.binary_off(b)
            b._complete_ack(frame[-1],'.')
        b.cni.send_hex_frame.side_effect=send
        self.begin(b);b.mqtt.publish.assert_not_called()

    def test_plain_slider_and_switch_commands_also_wait_for_numeric_readback(self):
        for topic,payload in [('ramp',b'50,34'),('switch',b'OFF'),('switch',b'ON')]:
            b,p,w,now=self.make()
            b.handle_mqtt_command('cbus/write/254/56/13/'+topic,payload)
            self.assertEqual(b.ramps.snapshot()['active_groups'],1)
            b.mqtt.publish.assert_not_called()

    def test_wall_switch_ramp_and_stop_are_tracked_without_publishing_targets(self):
        b,p,w,now=self.make()
        class End(BaseException):pass
        b.cni.recv_line_bytes.side_effect=[packet([5,3,56,0,0x3a,13,0]).encode(),End()]
        with self.assertRaises(End):b.run_forever(False,False)
        self.assertEqual(b.ramps.snapshot()['groups'][0]['full_scale_seconds'],60)
        b.mqtt.publish.assert_not_called()
        now[0]=3
        b.cni.recv_line_bytes.side_effect=[packet([5,3,56,0,9,13]).encode(),End()]
        with self.assertRaises(End):b.run_forever(False,False)
        self.assertEqual(b.ramps.snapshot()['groups'][0]['phase'],'stop')

    def test_queued_readback_rechecks_command_generation_before_sending(self):
        b,p,w,now=self.make();self.begin(b)
        now[0]=1;b.queue_ramp_readback(w)
        self.begin(b,b'{"level":100,"ramp_seconds":60}')
        count=b.cni.send_hex_frame.call_count
        w.run_one()
        self.assertEqual(b.cni.send_hex_frame.call_count,count)

    def test_disconnect_discards_tracking_and_pending_readbacks(self):
        b,p,w,now=self.make();self.begin(b)
        now[0]=1;b.queue_ramp_readback(w)
        w.set_link('mqtt',False)
        self.assertEqual(b.ramps.snapshot()['active_groups'],0)
        self.assertFalse(w.run_one())

    def test_failed_ack_does_not_schedule_polling(self):
        b,p,w,now=self.make(ack='#');self.begin(b)
        now[0]=2;b.queue_ramp_readback(w)
        self.assertFalse(w.run_one())
        self.assertEqual(b.ramps.snapshot()['active_groups'],0)

    def test_tracking_disabled_or_readonly_retains_previous_safety(self):
        b,p,w,now=self.make(interval=0)
        b.handle_mqtt_command('cbus/write/254/56/13/ramp',b'50')
        self.assertTrue(b.mqtt.publish.called)
        self.assertEqual(b.ramps.snapshot()['active_groups'],0)
        b,p,w,now=self.make(readonly=True);self.begin(b)
        now[0]=2;b.queue_ramp_readback(w)
        b.cni.send_hex_frame.assert_not_called()

    def test_fans_keep_preset_behavior_and_never_get_tracking(self):
        b,p,w,now=self.make(mode='fan')
        b.handle_mqtt_command('cbus/write/254/56/13/ramp',b'128')
        self.assertTrue(b.mqtt.publish.called)
        self.assertEqual(b.ramps.snapshot()['active_groups'],0)

    def test_failed_or_full_queue_does_not_leave_a_permanent_reservation(self):
        b,p,w,now=self.make();self.begin(b)
        now[0]=1
        with patch.object(w,'submit',return_value=False):b.queue_ramp_readback(w)
        self.assertIsNone(b.ramps.pending)
        now[0]=6;b.queue_ramp_readback(w)
        self.assertTrue(w.run_one())

    def test_replacement_cancels_obsolete_synthetic_timer(self):
        b,p,w,now=self.make();timer=Mock();b._pending_synthetic_timers[(56,13)]=timer
        self.begin(b);timer.cancel.assert_called_once()

    def test_cli_interval_rejects_unsafe_values_before_opening_connections(self):
        for value in ('1','2','61','nan','inf','-1'):
            with patch.object(app.mqtt,'Client') as client,self.assertRaises(SystemExit):
                app.main(['--cni-host','unused','--ha-config','unused','--ramp-readback-every',value])
            client.assert_not_called()

    def test_reader_routes_real_numeric_status_and_suppresses_following_binary(self):
        b,p,w,now=self.make()
        class End(BaseException):pass
        stage=[0]
        symbols='A965'
        packed=''.join(symbols[(214 >> shift)&3] for shift in (2,0,6,4))
        def receive():
            stage[0]+=1
            if stage[0]==1:return packet([5,3,56,0,0x3a,13,0]).encode()
            if stage[0]==2:
                now[0]=1;b.queue_ramp_readback(w);w.run_one()
                return packet([0xe5,7,56,13,*bytes.fromhex(packed)]).encode()
            if stage[0]==3:return packet([0xc3,56,12,8]).encode()
            raise End()
        b.cni.recv_line_bytes.side_effect=receive
        with self.assertRaises(End):b.run_forever(False,False)
        published=[(c.args[0],c.kwargs['payload']) for c in b.mqtt.publish.call_args_list]
        self.assertEqual(published,[('level','84'),('state','ON')])
        self.assertEqual(b.diagnostics.snapshot()['status_pending_groups'],0)
        self.assertEqual(b.diagnostics.group_snapshot()['groups'][0]['binary_status'],'off')

    def test_tracking_scheduler_runs_without_diagnostics_publishing(self):
        client,cni,bridge=Mock(),Mock(),Mock()
        cni.failover_status.return_value={};bridge.ramps.snapshot.return_value={}
        targets={}
        mqtt_up,cni_up=threading.Event(),threading.Event()
        stop=Mock();stop.wait.side_effect=[False,True]
        def thread(**kw):targets[kw['name']]=kw['target'];return Mock()
        class End(BaseException):pass
        bridge.run_forever.side_effect=End()
        with patch.object(app,'load_ha_mqtt_profiles',return_value={}), \
             patch.object(app.mqtt,'Client',return_value=client), \
             patch.object(app,'CniConnection',return_value=cni), \
             patch.object(app,'CbusMqttBridge',return_value=bridge), \
             patch.object(app.threading,'Thread',side_effect=thread), \
             patch.object(app.threading,'Event',side_effect=[mqtt_up,cni_up,stop]):
            with self.assertRaises(End):
                app.main(['--cni-host','unused','--ha-config','unused','--ramp-readback-every','3'])
        self.assertIn('cbus-metrics',targets)
        # The same scheduler drives readback even with every publish interval off.
        targets['cbus-metrics']()
        bridge.queue_ramp_readback.assert_called_once()
        cni.send_hex_frame.assert_not_called()

    def test_observed_bus_evidence_survives_a_failed_matching_local_command(self):
        b,p,w,now=self.make(ack='#')
        def send(frame):
            b._begin_ramp(56,13,0x3a,0,observed=True)
            b._complete_ack(frame[-1],'#')
        b.cni.send_hex_frame.side_effect=send
        self.begin(b)
        self.assertTrue(b.ramps.suppress_binary(56,13))
        now[0]=1;b.queue_ramp_readback(w)
        # Queued readback exists because the matching command was observed on bus,
        # even though the local transaction received a negative ACK.
        self.assertEqual(w.snapshot()['depth'],1)

    def test_completed_stop_numeric_level_cannot_be_overridden_by_old_binary_target(self):
        b,p,w,now=self.make()
        self.begin(b,b'{"stop":true}')
        for time in (1,6):
            now[0]=time;b.queue_ramp_readback(w);w.run_one()
            b.handle_cbus_level_update(56,13,60,source='STATUS')
        self.assertEqual(b.ramps.snapshot()['active_groups'],0)
        count=b.mqtt.publish.call_count
        self.binary_off(b)
        self.assertEqual(b.mqtt.publish.call_count,count)
        now[0]=100;b.queue_ramp_readback(w)
        self.assertFalse(w.run_one())  # authority persists without ongoing polling

    def test_positive_raw_level_never_rounds_to_off_with_tracking_enabled(self):
        b,p,w,now=self.make()
        b.handle_cbus_level_update(56,13,1,source='STATUS')
        self.assertEqual([(c.args[0],c.kwargs['payload']) for c in b.mqtt.publish.call_args_list],
                         [('level','1'),('state','ON')])
        self.binary_off(b)
        self.assertEqual(b.mqtt.publish.call_args.kwargs['payload'],'ON')

    def test_fade_to_zero_does_not_replace_remembered_on_level_with_intermediate_values(self):
        b,p,w,now=self.make();p.last_nonzero_255=255
        self.begin(b)
        for when,raw in [(10,214),(30,130),(50,47),(65,0),(70,0)]:
            now[0]=when;b.queue_ramp_readback(w);w.run_one()
            b.handle_cbus_level_update(56,13,raw,source='STATUS')
            self.assertEqual(p.last_nonzero_255,255)
        b.handle_mqtt_command(p.command_topic,b'ON')
        self.assertEqual(bytes.fromhex(b.cni.send_hex_frame.call_args.args[0][:-1])[5],255)

    def test_confirmed_stop_updates_remembered_level_without_guessing(self):
        b,p,w,now=self.make();self.begin(b,b'{"stop":true}')
        for when in (1,6):
            now[0]=when;b.queue_ramp_readback(w);w.run_one()
            b.handle_cbus_level_update(56,13,60,source='STATUS')
        self.assertEqual(p.last_nonzero_255,60)
