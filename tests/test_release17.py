"""Master presence discovery, complete-map gating and explicit fallback sweep."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from cbus_bridge.cni_protocol import (build_presence_request,parse_binary_status_reply,
                                     diagnose_frame,parse_identity_reply)
from cbus_bridge.utils import BridgeDiagnostics,DeviceInventory
from cbus_bridge.bridge import CbusMqttBridge
import test_release16 as previous


def presence_frame(start=0,count=88,present=None):
    values=present or {};data=[]
    assert count%4==0
    for group in range(start,start+count,4):
        data.append(sum(values.get(group+i,0) << (2*i) for i in range(4)))
    b=bytes([0xC0+2+len(data),255,start]+data)
    return (b+bytes([-sum(b)&255])).hex().upper()


def fragments(present=None):
    return [parse_binary_status_reply(presence_frame(start,count,present))
            for start,count in [(0,88),(88,88),(176,80)]]


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.now=[0.];self.d=BridgeDiagnostics({})
        self.i=DeviceInventory(self.d,clock=lambda:self.now[0],wall=lambda:1000+self.now[0])
        self.i.set_interface_unit(2);self.send=Mock()

    def start(self):
        self.assertTrue(self.i.request('discover',True))
        t=self.i.reserve();self.assertEqual(t['kind'],'presence')
        self.assertTrue(self.i.transmit(t,self.send));return t

    def complete(self,values=None):
        for r in fragments(values):self.assertTrue(self.i.receive_presence(r))

    def test_exact_master_request_and_binary_classification(self):
        self.assertEqual(build_presence_request(),'05FF007AFF0083')
        self.assertEqual(diagnose_frame(presence_frame()),'presence')
        raw=bytes.fromhex(build_presence_request())
        self.assertEqual(sum(raw)%256,0);self.assertEqual(raw[3:6],bytes([0x7a,255,0]))

    def test_no_passive_or_unsolicited_report_can_start_discovery(self):
        self.assertFalse(self.i.receive_presence(fragments({12:1})[0]))
        self.assertIsNone(self.i.reserve());self.send.assert_not_called()

    def test_reordered_duplicate_fragments_wait_for_all_256_slots(self):
        self.start();r=fragments({2:1,12:2,114:3})
        for fragment in [r[2],r[0],r[0]]:self.assertTrue(self.i.receive_presence(fragment))
        self.assertIsNone(self.i.reserve())
        self.assertEqual(self.i.snapshot()['job']['requested_units'],[])
        self.assertTrue(self.i.receive_presence(r[1]))
        s=self.i.snapshot()
        self.assertEqual(s['presence']['covered_slots'],256)
        self.assertEqual(s['presence']['reported_units'],[2,12,114])
        self.assertEqual(s['presence']['ambiguous_units'],[114])
        self.assertEqual(s['job']['requested_units'],[2,12,114])
        self.assertEqual(s['job']['units_total'],3)
        self.assertEqual(s['job']['phase'],'identifying')
        self.assertEqual(len(self.i.tasks),3)

    def test_presence_timeout_is_ten_seconds_with_no_automatic_sweep(self):
        self.start();self.now[0]=9.9;self.assertIsNone(self.i.reserve())
        self.now[0]=10;self.assertIsNone(self.i.reserve())
        s=self.i.snapshot();self.assertEqual(s['job']['state'],'discovery_no_reply')
        self.assertEqual(s['presence']['state'],'no_reply');self.assertEqual(self.send.call_count,1)
        self.now[0]=100;self.assertIsNone(self.i.reserve())

    def test_partial_report_is_not_mistaken_for_absence(self):
        self.start();self.i.receive_presence(fragments({12:1})[0])
        self.now[0]=10;self.i.reserve();s=self.i.snapshot()
        self.assertEqual(s['job']['state'],'discovery_incomplete')
        self.assertEqual(s['presence']['covered_slots'],88)
        self.assertEqual(s['job']['requested_units'],[]);self.assertEqual(s['devices'],[])

    def test_conflicting_overlap_stops_without_identifying_from_partial_map(self):
        self.start();self.i.receive_presence(fragments({12:1})[0])
        self.assertFalse(self.i.receive_presence(fragments({12:2})[0]))
        self.assertEqual(self.i.snapshot()['job']['state'],'discovery_conflict')
        self.assertIsNone(self.i.reserve());self.assertEqual(self.send.call_count,1)

    def test_malformed_fragments_do_not_add_coverage_or_extend_window(self):
        self.start()
        for states in [{}, {-1:'on'}, {256:'on'}, {1:'bad'}, {True:'on'}]:
            self.assertFalse(self.i.receive_presence(SimpleNamespace(app=255,states=states)))
        self.assertFalse(self.i.receive_presence(SimpleNamespace(app=56,states={1:'on'})))
        self.assertEqual(self.i.snapshot()['presence']['covered_slots'],0)
        self.now[0]=10;self.i.reserve()
        self.assertEqual(self.i.snapshot()['job']['state'],'discovery_no_reply')

    def test_late_fragments_and_identity_during_presence_do_not_match(self):
        self.start()
        self.assertFalse(self.i.receive(parse_identity_reply(previous.reply_frame(unit=2,dest=2))))
        self.now[0]=10
        for r in fragments({2:1}):self.assertFalse(self.i.receive_presence(r))
        self.i.reserve();self.assertEqual(self.i.snapshot()['job']['state'],'discovery_no_reply')

    def test_address_255_is_reported_but_never_probed(self):
        self.start();self.complete({2:1,255:1})
        s=self.i.snapshot();self.assertTrue(s['presence']['address_255_reported'])
        self.assertEqual(s['job']['requested_units'],[2])
        self.assertEqual(list(self.i.tasks),[(2,1)])

    def test_empty_complete_map_warns_about_known_units_without_guessing_targets(self):
        self.i.observe_unit(12);self.start();self.complete();self.i.reserve()
        s=self.i.snapshot();self.assertEqual(s['job']['state'],'complete')
        self.assertEqual(s['presence']['known_units_not_reported'],[2,12])
        self.assertEqual(s['job']['units_total'],0);self.assertEqual(self.send.call_count,1)

    def test_previously_identified_unreported_unit_remains_historical_and_flagged(self):
        self.i.rows[77]={'unit':77,'type':'RELAY','outcome':'identified','scan_id':0,'attributes':{}}
        self.start();self.complete({2:1})
        s=self.i.snapshot()
        self.assertEqual(s['presence']['known_units_not_reported'],[77])
        self.assertEqual(s['job']['requested_units'],[2])
        self.assertEqual(next(r for r in s['devices'] if r['unit']==77)['scan_id'],0)

    def test_cancel_pending_presence_and_stale_unsent_reservation(self):
        self.assertTrue(self.i.request('discover',True));token=self.i.reserve()
        self.i.request('cancel',True)
        self.assertFalse(self.i.transmit(token,self.send));self.send.assert_not_called()
        self.now[0]=31;self.start();self.i.cancel()
        for r in fragments({2:1}):self.assertFalse(self.i.receive_presence(r))
        self.i.set_interface_unit(2);self.assertIsNone(self.i.reserve())
        self.assertEqual(self.i.snapshot()['job']['state'],'connection_lost')

    def test_discovery_respects_readonly_readiness_interface_and_cooldown(self):
        self.assertFalse(self.i.request('discover',True,readonly=True))
        self.assertFalse(self.i.request('discover',False))
        self.i.interface_unit=None;self.assertFalse(self.i.request('discover',True))
        self.i.set_interface_unit(2);self.start()
        self.assertFalse(self.i.request('sweep',True))
        self.i.cancel('cancelled');self.assertFalse(self.i.request('discover',True))

    def test_explicit_sweep_covers_only_0_to_254_and_has_long_deadline(self):
        self.assertTrue(self.i.request('sweep',True))
        self.assertEqual(self.i.snapshot()['job']['requested_units'],list(range(255)))
        self.assertEqual(len(self.i.tasks),255)
        self.assertEqual(self.i.job['deadline'],3600)
        self.assertEqual(self.i.snapshot()['presence']['state'],'not_requested')
        t=self.i.reserve();self.i.transmit(t,self.send)
        self.assertEqual(bytes.fromhex(self.send.call_args.args[0])[1],0)

    def test_complete_presence_then_identity_can_exceed_old_sixteen_unit_cap(self):
        self.start();self.complete({u:1 for u in range(20)})
        while self.i.job['state']=='running':
            self.now[0]+=3;t=self.i.reserve()
            if t:
                self.i.transmit(t,self.send)
                r=parse_identity_reply(previous.reply_frame(t['unit'],2,t['attribute']))
                self.assertTrue(self.i.receive(r))
        s=self.i.snapshot();self.assertEqual(s['job']['units_finished'],20)
        self.assertEqual(s['job']['units_total'],20)
        self.assertTrue(all(row['outcome']=='identified' for row in s['devices']))
        self.assertEqual(self.send.call_count,61)

    def test_full_sweep_all_responsive_stays_bounded_and_finishes_within_deadline(self):
        self.assertTrue(self.i.request('sweep',True))
        while self.i.job['state']=='running':
            t=self.i.reserve()
            if t:
                self.i.transmit(t,self.send)
                self.i.receive(parse_identity_reply(previous.reply_frame(t['unit'],2,t['attribute'])))
            self.now[0]+=3
        s=self.i.snapshot();self.assertEqual(s['job']['units_finished'],255)
        self.assertEqual(s['job']['state'],'complete');self.assertEqual(self.send.call_count,765)
        self.assertLess(len(json.dumps(s)),200000)
        self.assertEqual(len(self.i.tasks),0);self.assertIsNone(self.i.pending)

    def test_presence_never_changes_light_state_in_reader(self):
        b=CbusMqttBridge(Mock(),Mock(),{},False)
        class End(BaseException):pass
        b.cni.recv_line_bytes.side_effect=[presence_frame(present={12:1}).encode(),End()]
        with self.assertRaises(End):b.run_forever(False,False)
        b.mqtt.publish.assert_not_called()
        self.assertEqual(b.inventory.snapshot()['devices'],[])
        self.assertEqual(b.diagnostics.snapshot()['counters']['rx_presence_total'],1)
