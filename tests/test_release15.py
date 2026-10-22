"""Simultaneous fade admission, deadlines and safe interactive interruption."""
import json
import unittest
from unittest.mock import patch
from cbus_bridge.utils import BridgeDiagnostics, CommandWorker, command_requires_duration_wait
import test_release12 as previous


class FadeQueueTests(unittest.TestCase):
    def make(self, **kwargs):
        self.now = [0.]
        self.d = BridgeDiagnostics({})
        w = CommandWorker(self.d, clock=lambda:self.now[0], **kwargs)
        w.set_link('mqtt', True); w.set_link('cni', True)
        return w

    def test_eight_simultaneous_fades_execute_even_beyond_old_five_second_ttl(self):
        w = self.make(); seen = []
        for group in range(8):
            def operation(lease, group=group):
                lease.atomic(lambda:seen.append(group)); self.now[0] += 4
            self.assertTrue(w.submit(operation, key=f'cbus/write/254/56/{group}', duration=True))
        self.assertFalse(w.submit(lambda lease:None, key='cbus/write/254/56/99', duration=True))
        self.assertEqual(self.d.last_command_failure['reason'], 'duration_queue_full')
        self.assertEqual((self.d.last_command_failure['app'],self.d.last_command_failure['group']),(56,99))
        while w.run_one(): pass
        self.assertEqual(seen,list(range(8)))
        self.assertEqual(w.snapshot()['duration_pending'],0)

    def test_newest_same_group_fade_replaces_unsent_request_even_at_limit(self):
        w = self.make(duration_capacity=2); seen=[]
        w.submit(lambda lease:seen.append('old'),key='a',duration=True)
        w.submit(lambda lease:seen.append('other'),key='b',duration=True)
        self.assertTrue(w.submit(lambda lease:seen.append('new'),key='a',duration=True))
        while w.run_one():pass
        self.assertEqual(seen,['other','new'])

    def test_active_fade_accepts_another_group_and_counts_towards_bound(self):
        w=self.make(duration_capacity=2); seen=[]
        def first(lease):
            self.assertTrue(w.submit(lambda lease:seen.append('second'),duration=True,key='b'))
            self.assertFalse(w.submit(lambda lease:None,duration=True,key='c'))
            seen.append('first')
        w.submit(first,duration=True,key='a');w.run_one();w.run_one()
        self.assertEqual(seen,['first','second'])

    def test_interactive_control_defers_then_resumes_duration_before_background(self):
        w=self.make();seen=[];attempt=[0]
        def fade(lease):
            attempt[0]+=1
            if attempt[0]==1:w.submit(lambda lease:seen.append('fan'),interactive=True)
            lease.yield_to_interactive()
            lease.atomic(lambda:seen.append('fade'))
        w.submit(lambda lease:seen.append('background'))
        w.submit(fade,key='fade',duration=True)
        while w.run_one():pass
        self.assertEqual(seen,['fan','fade','background'])
        self.assertIsNone(self.d.last_command_failure)

    def test_off_supersedes_active_preflight_instead_of_resuming_old_fade(self):
        w=self.make();seen=[]
        def fade(lease):
            w.submit(lambda lease:seen.append('off'),interactive=True,key='same')
            lease.yield_to_interactive();seen.append('wrong')
        w.submit(fade,duration=True,key='same')
        while w.run_one():pass
        self.assertEqual(seen,['off'])

    def test_new_fade_supersedes_same_group_still_acquiring_level(self):
        w=self.make();seen=[]
        def old(lease):
            w.submit(lambda lease:seen.append('new'),duration=True,key='same')
            lease.yield_to_interactive();seen.append('old')
        w.submit(old,duration=True,key='same')
        while w.run_one():pass
        self.assertEqual(seen,['new'])

    def test_saturated_interactive_queue_reports_fade_resume_failure(self):
        w=self.make(capacity=1)
        def fade(lease):
            w.submit(lambda lease:None,interactive=True,key='other')
            lease.yield_to_interactive()
        w.submit(fade,duration=True,key='cbus/write/254/56/72');w.run_one()
        self.assertEqual(self.d.last_command_failure['reason'],'duration_queue_full')
        self.assertEqual(self.d.last_command_failure['group'],72)
        self.assertEqual(w.snapshot()['depth'],1)

    def test_deferred_fade_keeps_original_deadline(self):
        w=self.make();seen=[]
        def fade(lease):
            self.now[0]=59
            w.submit(lambda lease:seen.append('fan'),interactive=True)
            lease.yield_to_interactive()
        w.submit(fade,duration=True,key='cbus/write/254/56/13');w.run_one();w.run_one()
        self.now[0]=61;w.run_one()
        self.assertEqual(seen,['fan'])
        self.assertEqual(self.d.last_command_failure['reason'],'command_expired')
        self.assertEqual(self.d.last_command_failure['group'],13)

    def test_disconnect_discards_all_queued_fades_without_replay(self):
        w=self.make();seen=[]
        for i in range(8):w.submit(lambda lease:seen.append('wrong'),duration=True,key=str(i))
        w.set_link('mqtt',False);w.set_link('mqtt',True);w.set_link('cni',True)
        self.assertFalse(w.run_one());self.assertEqual(seen,[])

    def test_ordinary_commands_keep_short_deadline_and_priority(self):
        w=self.make();seen=[]
        w.submit(lambda lease:seen.append('fade'),duration=True)
        w.submit(lambda lease:seen.append('off'),interactive=True)
        self.now[0]=6
        while w.run_one():pass
        self.assertEqual(seen,['fade'])

    def test_full_background_queue_yields_slot_but_does_not_evict_fades(self):
        w=self.make(capacity=2)
        w.submit(lambda lease:None,duration=True,key='fade')
        w.submit(lambda lease:None,key='background')
        self.assertTrue(w.submit(lambda lease:None,interactive=True,key='off'))
        self.assertEqual([item[4] for item in w.items],['off','fade'])

    def test_zero_time_and_restore_commands_are_interactive_but_invalid_values_still_parse(self):
        for field in ('transition_seconds','ramp_seconds'):
            self.assertFalse(command_requires_duration_wait({field:0,'level':50}))
            self.assertTrue(command_requires_duration_wait({field:20,'level':50}))
            for invalid in (False,None,'0'):
                self.assertTrue(command_requires_duration_wait({field:invalid,'level':50}))
        self.assertFalse(command_requires_duration_wait({'restore':True,'transition_seconds':0}))
        self.assertFalse(command_requires_duration_wait(None))

    def test_real_bridge_fades_both_groups_with_acknowledgements(self):
        w=self.make();bridges=[]
        for group in (13,72):
            b,p=previous.DurationBridgeTests().make(group=group);bridges.append((b,p))
            payload=json.dumps({'level':20,'transition_seconds':20}).encode()
            w.submit(lambda lease,b=b,p=p,payload=payload:b.execute_queued(
                lambda:b.handle_mqtt_command(p.level_command_topic,payload),lease),
                duration=True,key=p.level_command_topic.rsplit('/',1)[0])
        with patch('cbus_bridge.bridge.CbusMqttBridge._duration_start_level',return_value=255):
            while w.run_one():pass
        for b,p in bridges:
            frames=b.cni.send_hex_frame.call_args_list
            self.assertEqual(len(frames),1)
            raw=bytes.fromhex(frames[0].args[0][:-1])
            self.assertEqual((raw[4],raw[5]),(p.group,51))
            self.assertEqual(b.diagnostics.snapshot()['counters']['duration_transitions_transmitted_total'],1)
