"""On-demand read-only device voltage/latched-diagnostics trial."""
import unittest
from unittest.mock import Mock
from cbus_bridge.cni_protocol import (build_device_health_request, parse_device_health_reply,
                                     parse_identity_reply, diagnose_frame)
from cbus_bridge.utils import DeviceInventory,BridgeDiagnostics
import test_release16 as previous


def health_frame(unit=20,dest=99,common=0,errors=0,voltage=204,route=0,header=0x8D,attr=4,data=None):
    if data is None: data=bytes(9)+bytes((voltage,common,errors))
    raw=bytes((0x86,unit,dest,route,header,attr))+data
    return (raw+bytes((-sum(raw)&255,))).hex().upper()


class HealthProtocolTests(unittest.TestCase):
    def test_readonly_exact_command_and_boundaries(self):
        self.assertEqual(build_device_health_request(3),'0603002104D2')
        for u in (0,2,254):
            b=bytes.fromhex(build_device_health_request(u))
            self.assertEqual(b[:5],bytes((6,u,0,0x21,4)));self.assertEqual(sum(b)%256,0)
        for u in (True,False,-1,255,256,3.0,'3',None):
            with self.assertRaises(ValueError):build_device_health_request(u)

    def test_documented_layout_voltage_scaling_and_no_health_verdict(self):
        r=parse_device_health_reply(health_frame(data=bytes.fromhex('123456789ABCDEF011CC0300')))
        self.assertEqual((r.unit,r.destination,r.attribute),(20,99,4))
        self.assertEqual(r.value['voltage_v'],32)
        self.assertEqual(r.value['data_hex'],'123456789ABCDEF011CC0300')
        self.assertTrue(r.value['output_unit']);self.assertTrue(r.value['checksum_alarm_enabled'])
        self.assertEqual(r.value['latched_errors'],[]);self.assertNotIn('healthy',r.value)
        self.assertEqual(diagnose_frame(health_frame()),'device_health')
        self.assertIsNone(parse_identity_reply(health_frame()))

    def test_each_status_bit_and_latched_error_decoded_independently(self):
        names=['power_on_reset','internal_stack_overflow','communications_tx_error','micro_reset',
               'eeprom_data_error','eeprom_checksum_error','eeprom_write_error','installation_mmi_error']
        for bit,name in enumerate(names):
            v=parse_device_health_reply(health_frame(errors=1<<bit)).value
            self.assertEqual(v['latched_errors'],[name]);self.assertEqual(v['latched_errors_raw'],1<<bit)
        for bit,field in [(0,'output_unit'),(1,'checksum_alarm_enabled'),(5,'voltage_marginal'),
                          (6,'voltage_low'),(7,'learn_mode')]:
            v=parse_device_health_reply(health_frame(common=1<<bit)).value
            self.assertTrue(v[field]);self.assertEqual(v['common_status_raw'],1<<bit)
        for raw in (0,1,127,254,255):
            self.assertEqual(parse_device_health_reply(health_frame(voltage=raw)).value['voltage_v'],
                             round(raw/6.375,3))

    def test_strict_envelope_counts_checksum_and_no_truncated_measurements(self):
        for f in [health_frame(route=1),health_frame(unit=255),health_frame(dest=255),
                  health_frame(header=0x8C),health_frame(attr=3),health_frame(data=bytes(11)),
                  health_frame(data=bytes(13)),health_frame()[:-2]+'AB','8603','not hex','',
                  previous.reply_frame()]:
            with self.subTest(frame=f):self.assertIsNone(parse_device_health_reply(f))
        self.assertEqual(diagnose_frame(health_frame(header=0x8C)),'malformed')
        self.assertEqual(diagnose_frame(health_frame()[:-2]+'AB'),'checksum_failure')


class HealthSurveyTests(unittest.TestCase):
    def setUp(self):
        self.now=[0.];self.i=DeviceInventory(BridgeDiagnostics({}),clock=lambda:self.now[0],
                                           wall=lambda:1000+self.now[0])
        self.i.set_interface_unit(99);self.send=Mock()

    def transmit(self):
        t=self.i.reserve();self.assertIsNotNone(t)
        self.assertTrue(self.i.transmit(t,self.send));return t

    def row(self,unit=20):return next(r for r in self.i.snapshot()['devices'] if r['unit']==unit)

    def test_no_automatic_health_queries_or_empty_cached_survey(self):
        self.i.observe_unit(20);self.assertIsNone(self.i.reserve())
        self.assertFalse(self.i.request('health',True));self.send.assert_not_called()

    def test_explicit_health_one_read_per_unit_preserves_identity(self):
        self.i.rows[20]={'unit':20,'type':'DIMDN8','outcome':'identified','attributes':{}}
        self.assertTrue(self.i.request({'health':[20,21,20]},True))
        self.assertEqual(self.i.snapshot()['job']['units_finished'],0)
        self.assertEqual(self.row()['outcome'],'identified')
        self.assertEqual(self.transmit()['kind'],'health')
        self.assertTrue(self.i.receive(parse_device_health_reply(health_frame())))
        self.assertEqual(self.row()['health']['voltage_v'],32)
        self.now[0]=3;self.transmit();self.now[0]=6;self.assertIsNone(self.i.reserve())
        s=self.i.snapshot();self.assertEqual(s['job']['state'],'complete')
        self.assertEqual(s['job']['units_finished'],2);self.assertEqual(self.send.call_count,2)
        self.assertEqual(self.row(21)['health']['outcome'],'no_reply')
        self.assertNotIn('voltage_v',self.row(21)['health'])
        self.assertNotIn('observed_ts',self.row(21)['health'])
        self.assertEqual(self.row()['type'],'DIMDN8')

    def test_cached_inventory_health_all_identified_units_not_observed_guess(self):
        self.i.rows={u:{'unit':u,'type':'KEY4','outcome':'identified','attributes':{}} for u in range(43)}
        self.i.observe_unit(110)
        self.assertTrue(self.i.request('health',True));self.assertEqual(len(self.i.tasks),43)
        self.assertEqual(self.i.snapshot()['job']['requested_units'],list(range(43)))
        for u in range(43):
            self.transmit();self.assertTrue(self.i.receive(parse_device_health_reply(health_frame(unit=u))))
            self.now[0]+=3
        self.i.reserve();self.assertEqual(self.i.snapshot()['job']['units_finished'],43)
        self.assertEqual(self.send.call_count,43)

    def test_bad_inputs_readonly_not_ready_unknown_interface_and_busy_rejected(self):
        for spec in [{'health':[]},{'health':list(range(17))},{'health':[255]},
                     {'health':[True]},{'health':[20.0]},{'health':'all'},
                     {'health':[20],'reset':True}]:
            self.assertFalse(self.i.request(spec,True))
        self.assertFalse(self.i.request({'health':[20]},True,True))
        self.assertFalse(self.i.request({'health':[20]},False))
        self.i.interface_unit=None;self.assertFalse(self.i.request({'health':[20]},True))
        self.i.set_interface_unit(99);self.assertTrue(self.i.request({'health':[20]},True))
        self.assertFalse(self.i.request({'units':[20]},True));self.send.assert_not_called()

    def test_wrong_unit_destination_attribute_and_late_reply_do_not_match(self):
        self.i.request({'health':[20]},True);self.transmit()
        for r in [parse_device_health_reply(health_frame(unit=21)),
                  parse_device_health_reply(health_frame(dest=98)),
                  parse_identity_reply(previous.reply_frame(unit=20))]:
            self.assertFalse(self.i.receive(r))
        self.now[0]=3;self.assertFalse(self.i.receive(parse_device_health_reply(health_frame())))
        self.i.reserve();self.assertEqual(self.row()['health']['outcome'],'no_reply')

    def test_cancel_disconnect_send_failure_and_deadline_invalidate_remaining_reads(self):
        for reason in ('cancelled','connection_lost','send_failed','survey_deadline'):
            self.setUp();self.i.request({'health':[20,21]},True)
            t=self.transmit()
            if reason=='send_failed':
                self.i.receive(parse_device_health_reply(health_frame()))
                self.now[0]=3;t=self.i.reserve()
                with self.assertRaises(OSError):self.i.transmit(t,Mock(side_effect=OSError('offline')))
                self.assertEqual(self.row(21)['health']['outcome'],'send_failed')
            elif reason=='survey_deadline':
                self.now[0]=600;self.i.reserve()
            else:self.i.cancel(reason)
            self.assertEqual(self.i.snapshot()['job']['state'],reason)
            self.assertIsNone(self.i.reserve());self.assertFalse(self.i.receive(parse_device_health_reply(health_frame())))
            self.assertFalse(self.i.transmit(t,self.send))

    def test_new_health_attempt_removes_stale_values_and_identity_rescan_retains_timestamp(self):
        self.i.request({'health':[20]},True);self.transmit()
        self.i.receive(parse_device_health_reply(health_frame()));self.i.reserve();self.now[0]=30
        self.assertTrue(self.i.request({'units':[20]},True))
        self.assertEqual(self.row()['health']['observed_ts'],1000)
        self.i.cancel('cancelled');self.now[0]=60
        self.assertTrue(self.i.request({'health':[20]},True))
        self.assertEqual(self.row()['health']['outcome'],'pending')
        self.assertNotIn('voltage_v',self.row()['health'])
        self.transmit();self.now[0]=63;self.i.reserve()
        self.assertNotIn('voltage_v',self.row()['health'])

    def test_restart_cache_empty_and_snapshots_are_independent(self):
        self.i.request({'health':[20]},True);self.transmit()
        self.i.receive(parse_device_health_reply(health_frame(errors=4)))
        s=self.i.snapshot();s['devices'][0]['health']['latched_errors'].clear()
        self.assertEqual(self.row()['health']['latched_errors'],['communications_tx_error'])
        fresh=DeviceInventory(BridgeDiagnostics({}));self.assertEqual(fresh.snapshot()['devices'],[])
        self.assertIsNone(fresh.reserve())


class HealthBridgeTests(unittest.TestCase):
    def test_reader_health_reply_does_not_publish_lighting_state_or_consume_ack(self):
        b,w,now=previous.BridgeTests().make()
        b.request_inventory({'health':[20]},w);b.queue_inventory(w);w.run_one()
        frames=iter([health_frame().encode()])
        class End(BaseException):pass
        def recv():
            try:return next(frames)
            except StopIteration:raise End()
        b.cni.recv_line_bytes.side_effect=recv
        with self.assertRaises(End):b.run_forever(False,False)
        self.assertEqual(b.inventory.snapshot()['devices'][0]['health']['outcome'],'reply')
        b.mqtt.publish.assert_not_called();self.assertEqual(b._ack_waiters,{})

    def test_active_ramp_and_interactive_queue_pause_health_shared_budget(self):
        b,w,now=previous.BridgeTests().make();b.request_inventory({'health':[20]},w)
        b.ramps.entries[(56,13)]={};b.queue_inventory(w)
        self.assertEqual(w.snapshot()['depth'],0)
        b.ramps.entries.clear();w.submit(lambda lease:None,interactive=True)
        b.queue_inventory(w);self.assertEqual(w.snapshot()['depth'],1);w.run_one()
        b.ramps.last_sent=0;now[0]=2;b.queue_inventory(w);self.assertEqual(w.snapshot()['depth'],0)
        now[0]=3;b.queue_inventory(w);w.run_one()
        b.cni.send_hex_frame.assert_called_once_with(build_device_health_request(20))
        self.assertFalse(b.ramps.send_duration_probe(56,0,lambda *args:None))

    def test_cancel_prevents_already_queued_health_send(self):
        b,w,now=previous.BridgeTests().make();b.request_inventory({'health':[20]},w)
        b.queue_inventory(w);b.inventory.request('cancel',True);w.run_one()
        b.cni.send_hex_frame.assert_not_called()


if __name__=='__main__':unittest.main()
