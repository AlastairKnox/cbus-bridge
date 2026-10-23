"""Explicit local IDENTIFY survey; no real network or production configuration."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from cbus_bridge.cni_protocol import build_identity_request,parse_identity_reply,diagnose_frame
from cbus_bridge.utils import BridgeDiagnostics,DeviceInventory,CommandWorker
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge import main as app


def reply_frame(unit=3,dest=99,attr=1,text=b'DIMMER  ',route=0,header=0x89):
    b=bytes([0x86,unit,dest,route,header,attr])+text
    return (b+bytes([-sum(b)&255])).hex().upper()


class ProtocolTests(unittest.TestCase):
    def test_identify_matches_manufacturer_document_example(self):
        self.assertEqual(build_identity_request(3,2),'0603002102D4')
        for unit in (0,254):
            for attr in (0,1,2):
                b=bytes.fromhex(build_identity_request(unit,attr))
                self.assertEqual(b[:5],bytes([6,unit,0,0x21,attr]));self.assertEqual(sum(b)%256,0)

    def test_only_readonly_identity_opcodes_and_local_unicast_are_constructible(self):
        for unit in (-1,255,256,True,'3',3.0):
            with self.assertRaises(ValueError):build_identity_request(unit,0)
        for attr in (-1,3,4,8,0x21,True,'1'):
            with self.assertRaises(ValueError):build_identity_request(3,attr)

    def test_strict_long_reply_and_classifier(self):
        r=parse_identity_reply(reply_frame())
        self.assertEqual((r.unit,r.destination,r.attribute,r.value),(3,99,1,'DIMMER'))
        self.assertEqual(diagnose_frame(reply_frame()),'identity')
        self.assertEqual(parse_identity_reply(reply_frame(text=b'CLIPSAL\x00',attr=0)).value,'CLIPSAL')

    def test_reject_routing_broadcast_wrong_counts_controls_and_truncation(self):
        for frame in [reply_frame(route=1),reply_frame(unit=255),reply_frame(dest=255),
                      reply_frame(header=0x88),reply_frame(attr=3),reply_frame(text=b'1234567'),
                      reply_frame(text=b'123456789'),reply_frame(text=b'  \x01abc  '),reply_frame(text=b'        '),
                      reply_frame()[:-2]+'00','not hex','8603','']:
            with self.subTest(frame=frame):self.assertIsNone(parse_identity_reply(frame))
        self.assertIsNone(parse_identity_reply('8604990082300328'))  # Documented RECALL, not IDENTIFY.


class SurveyTests(unittest.TestCase):
    def setUp(self):
        self.now=[0.];self.d=BridgeDiagnostics({})
        self.i=DeviceInventory(self.d,clock=lambda:self.now[0],wall=lambda:1000+self.now[0])
        self.i.set_interface_unit(99)
        self.send=Mock()

    def start(self,units=(3,)):
        self.assertTrue(self.i.request({'units':list(units)},True))

    def transmit(self):
        token=self.i.reserve();self.assertIsNotNone(token)
        self.assertTrue(self.i.transmit(token,self.send));return token

    def receive(self,token,text=b'DIMMER  '):
        return self.i.receive(parse_identity_reply(reply_frame(token['unit'],99,token['attribute'],text)))

    def test_no_queries_at_startup_or_passive_observation(self):
        self.i.observe_unit(3);self.i.observe_unit(255)
        self.assertIsNone(self.i.reserve());self.send.assert_not_called()
        self.assertEqual(self.i.snapshot()['observed_units'],[3])
        self.assertEqual(self.i.snapshot()['devices'],[])

    def test_interface_trial_targets_learned_interface_address_only(self):
        self.assertTrue(self.i.request('interface',True))
        self.assertEqual(self.i.snapshot()['job']['requested_units'],[99])
        self.transmit()
        self.send.assert_called_once_with(build_identity_request(99,1))

    def test_complete_identity_three_reads_only_and_latest_inventory(self):
        self.start();fields=[]
        for text in (b'DIMMER  ',b'CLIPSAL ',b'1.2.43  '):
            t=self.transmit();fields.append(t['attribute']);self.assertTrue(self.receive(t,text))
            self.now[0]+=3
        self.assertIsNone(self.i.reserve())
        s=self.i.snapshot();row=s['devices'][0]
        self.assertEqual(fields,[1,0,2]);self.assertEqual(s['job']['state'],'complete')
        self.assertEqual((row['type'],row['manufacturer'],row['firmware'],row['outcome']),('DIMMER','CLIPSAL','1.2.43','identified'))
        self.assertEqual(self.send.call_count,3)

    def test_missing_unit_gets_only_one_probe_not_three_or_automatic_retry(self):
        self.start();self.transmit();self.now[0]=3
        self.assertIsNone(self.i.reserve())
        s=self.i.snapshot();self.assertEqual(s['job']['state'],'complete')
        self.assertEqual(s['devices'][0]['outcome'],'no_reply');self.assertEqual(self.send.call_count,1)
        self.now[0]=100;self.assertIsNone(self.i.reserve())

    def test_response_matching_rejects_wrong_unit_destination_attribute_and_late(self):
        self.start();t=self.transmit()
        for args in [(4,99,1),(3,98,1),(3,99,0)]:
            self.assertFalse(self.i.receive(parse_identity_reply(reply_frame(*args))))
        self.now[0]=3;self.assertFalse(self.receive(t))
        self.i.reserve();self.assertEqual(self.i.snapshot()['devices'][0]['outcome'],'no_reply')

    def test_partial_identity_is_not_reported_as_complete(self):
        self.start();self.receive(self.transmit());self.now[0]=3
        self.transmit();self.now[0]=6
        t=self.transmit();self.assertTrue(self.receive(t,b'1.2.43  '));self.i.reserve()
        self.assertEqual(self.i.snapshot()['devices'][0]['outcome'],'partial')

    def test_disconnect_cancels_pending_and_never_replays_after_reconnection(self):
        self.start((3,4));t=self.transmit();self.i.cancel()
        self.i.set_interface_unit(99);self.now[0]=20
        self.assertFalse(self.receive(t));self.assertIsNone(self.i.reserve())
        self.assertEqual(self.i.snapshot()['job']['state'],'connection_lost')
        self.assertTrue(all(r['outcome']=='connection_lost' for r in self.i.snapshot()['devices']))

    def test_admission_rejections_are_explicit_and_do_not_start_queries(self):
        for spec in [None,{}, {'units':[]},{'units':[255]}, {'units':[True]}, {'units':list(range(17))},
                     {'units':[3],'attribute':8},'all','arbitrary command']:
            self.assertFalse(self.i.request(spec,True))
        self.assertFalse(self.i.request({'units':[3]},True,readonly=True))
        self.assertFalse(self.i.request({'units':[3]},False))
        self.i.interface_unit=None;self.assertFalse(self.i.request({'units':[3]},True))
        self.assertEqual(self.i.snapshot()['last_request_error'],'interface_address_unknown')
        self.send.assert_not_called()

    def test_busy_cancel_cooldown_and_cancelled_reservation(self):
        self.start();t=self.i.reserve()
        self.assertFalse(self.i.request({'units':[4]},True))
        self.assertTrue(self.i.request('cancel',True))
        self.assertFalse(self.i.transmit(t,self.send))
        self.assertFalse(self.i.request({'units':[4]},True))
        self.now[0]=31;self.start((4,))

    def test_complete_and_cancelled_jobs_have_thirty_second_reuse_guard(self):
        self.start();self.transmit();self.now[0]=3;self.i.reserve()
        self.now[0]=32;self.assertFalse(self.i.request({'units':[3]},True))
        self.now[0]=33;self.start()

    def test_expired_queue_reservation_releases_without_sending_stale_job(self):
        self.start();t=self.i.reserve();self.now[0]=6
        new=self.i.reserve();self.assertNotEqual(t['token'],new['token'])
        self.assertFalse(self.i.transmit(t,self.send));self.assertTrue(self.i.transmit(new,self.send))

    def test_job_deadline_cancels_starved_survey(self):
        self.start();self.now[0]=600
        self.assertIsNone(self.i.reserve());self.assertEqual(self.i.snapshot()['job']['state'],'survey_deadline')

    def test_send_failure_aborts_survey_without_retry(self):
        self.start();self.send.side_effect=OSError('closed')
        with self.assertRaises(OSError):self.transmit()
        self.assertIsNone(self.i.reserve());self.assertEqual(self.i.snapshot()['job']['state'],'send_failed')

    def test_observed_selection_and_snapshot_copies_are_bounded(self):
        for unit in range(255):self.i.observe_unit(unit)
        self.assertTrue(self.i.request('observed',True))
        self.assertEqual(self.i.snapshot()['job']['requested_units'],list(range(16)))
        s=self.i.snapshot();s['job']['requested_units'].clear();s['devices'][0]['attributes']['bad']=True
        self.assertEqual(len(self.i.snapshot()['job']['requested_units']),16)
        self.assertNotIn('bad',self.i.snapshot()['devices'][0]['attributes'])


class BridgeTests(unittest.TestCase):
    def make(self):
        b=CbusMqttBridge(Mock(),Mock(),{},False)
        w=CommandWorker(b.diagnostics,cancel=b.cancel_session)
        w.set_link('mqtt',True);w.set_link('cni',True)
        now=[0.];b.inventory.clock=lambda:now[0];b.ramps.clock=lambda:now[0]
        b.inventory.set_interface_unit(99)
        return b,w,now

    def test_idle_default_never_sends_and_inventory_only_sends_via_worker(self):
        b,w,now=self.make();b.queue_inventory(w);b.cni.send_hex_frame.assert_not_called()
        self.assertTrue(b.request_inventory({'units':[3]},w));b.queue_inventory(w)
        b.cni.send_hex_frame.assert_not_called();w.run_one()
        b.cni.send_hex_frame.assert_called_once_with(build_identity_request(3,1))
        self.assertEqual(b._ack_waiters,{})

    def test_shared_three_second_budget_and_active_fades_pause_inventory(self):
        b,w,now=self.make();b.request_inventory({'units':[3]},w)
        b.ramps.last_sent=0;b.queue_inventory(w);self.assertEqual(w.snapshot()['depth'],0)
        now[0]=3;b.ramps.entries[(56,13)]={};b.queue_inventory(w)
        self.assertEqual(w.snapshot()['depth'],0)
        b.ramps.entries.clear();b.queue_inventory(w);w.run_one()
        self.assertEqual(b.ramps.last_sent,3)
        self.assertFalse(b.ramps.send_duration_probe(56,0,lambda *args:None))

    def test_waiting_lighting_work_blocks_inventory_admission_and_has_priority(self):
        b,w,now=self.make();b.request_inventory({'units':[3]},w)
        w.submit(lambda lease:None,interactive=True);b.queue_inventory(w)
        self.assertEqual(w.snapshot()['depth'],1);w.run_one()
        b.queue_inventory(w);seen=[];w.submit(lambda lease:seen.append('light'),interactive=True)
        w.run_one();self.assertEqual(seen,['light']);b.cni.send_hex_frame.assert_not_called()
        w.run_one();b.cni.send_hex_frame.assert_called_once()

    def test_cancelled_unsent_job_cannot_send_from_old_worker_item(self):
        b,w,now=self.make();b.request_inventory({'units':[3]},w);b.queue_inventory(w)
        b.inventory.request('cancel',True);w.run_one();b.cni.send_hex_frame.assert_not_called()

    def test_reader_learns_interface_address_and_does_not_publish_light_from_identity(self):
        b,w,now=self.make();b.inventory.interface_unit=None
        def checksum(raw):return (raw+bytes([-sum(raw)&255])).hex().upper().encode()
        setup=checksum(bytes([0x86,99,99,0,0x32,0x30,0]))
        frames=iter([setup,reply_frame().encode()])
        class End(BaseException):pass
        def recv():
            try:return next(frames)
            except StopIteration:raise End()
        b.cni.recv_line_bytes.side_effect=recv
        with self.assertRaises(End):b.run_forever(False,False)
        self.assertEqual(b.inventory.interface_unit,99);b.mqtt.publish.assert_not_called()
        self.assertEqual(b.diagnostics.snapshot()['counters']['rx_identity_total'],1)


class RequestRoutingTests(unittest.TestCase):
    def check(self, readonly=False):
        client,cni,bridge,worker=Mock(),Mock(),Mock(),Mock()
        cni.failover_status.return_value={};bridge.ramps.snapshot.return_value={}
        class End(BaseException):pass
        def loop():
            for retain,payload in [(True,b'{"inventory":"interface"}'),
                                   (False,b'{"inventory":"interface"}'),
                                   (False,b'{"inventory":{"units":[3,4]}}'),
                                   (False,b'{"inventory":"cancel"}')]:
                client.on_message(client,None,SimpleNamespace(topic='cbus/bridge/command',payload=payload,retain=retain))
            raise End()
        client.loop_start.side_effect=loop
        with patch.object(app,'load_ha_mqtt_profiles',return_value={}), \
             patch.object(app.mqtt,'Client',return_value=client), \
             patch.object(app,'CniConnection',return_value=cni), \
             patch.object(app,'CbusMqttBridge',return_value=bridge), \
             patch.object(app,'CommandWorker',return_value=worker), \
             patch.object(app.threading,'Timer'):
            with self.assertRaises(End):app.main(['--cni-host','unused','--ha-config','unused']+(['--readonly'] if readonly else []))
        return bridge,worker

    def test_fresh_explicit_requests_route_but_retained_request_never_runs(self):
        bridge,worker=self.check()
        self.assertEqual([call.args[0] for call in bridge.request_inventory.call_args_list],['interface',{'units':[3,4]},'cancel'])
        worker.submit.assert_not_called();bridge.handle_mqtt_command.assert_not_called()

    def test_readonly_mode_cannot_trigger_management_queries(self):
        bridge,worker=self.check(readonly=True)
        bridge.request_inventory.assert_not_called();worker.submit.assert_not_called()


class InventoryBoundTests(unittest.TestCase):
    def test_full_address_space_across_explicit_batches_remains_bounded(self):
        now=[0.];i=DeviceInventory(BridgeDiagnostics({}),clock=lambda:now[0]);i.set_interface_unit(99)
        sent=[]
        for start in range(0,255,16):
            self.assertTrue(i.request({'units':list(range(start,min(start+16,255)))},True))
            while i.snapshot()['job']['state']=='running':
                token=i.reserve()
                if token:i.transmit(token,sent.append)
                now[0]+=3
            now[0]+=31
        self.assertEqual(len(i.snapshot()['devices']),255)
        self.assertEqual(len(sent),255)
        self.assertEqual(len(i.tasks),0);self.assertIsNone(i.pending);self.assertIsNone(i.reserved)
        self.assertLess(len(json.dumps(i.snapshot())),100000)
