"""Voltage-only fallback, bounded alternative evidence and partial health semantics."""
import unittest
from unittest.mock import Mock
from cbus_bridge.cni_protocol import (build_device_voltage_request, parse_device_voltage_reply,
    parse_device_health_evidence, parse_device_health_reply, diagnose_frame)
from cbus_bridge.utils import DeviceInventory,BridgeDiagnostics
from test_release18 import health_frame
import test_release16 as previous


def voltage_frame(unit=100,dest=99,text=b'28.7V',route=0,header=None,attr=7):
    raw=bytes((0x86,unit,dest,route,0x80+1+len(text) if header is None else header,attr))+text
    return (raw+bytes((-sum(raw)&255,))).hex().upper()


class VoltageProtocolTests(unittest.TestCase):
    def test_exact_readonly_voltage_request_and_address_boundaries(self):
        self.assertEqual(build_device_voltage_request(3),'0603002107CF')
        for u in (0,254):
            raw=bytes.fromhex(build_device_voltage_request(u))
            self.assertEqual(raw[:5],bytes((6,u,0,0x21,7)));self.assertEqual(sum(raw)%256,0)
        for u in (-1,255,256,True,False,2.0,'2',None):
            with self.assertRaises(ValueError):build_device_voltage_request(u)

    def test_documented_ascii_format_with_no_inferred_diagnostic_flags(self):
        for text,value in [(b'00.0V',0),(b'28.7V',28.7),(b'39.9V',39.9),(b'99.9V',99.9)]:
            r=parse_device_voltage_reply(voltage_frame(text=text))
            self.assertEqual((r.unit,r.destination,r.attribute),(100,99,7))
            self.assertEqual(r.value['voltage_v'],value)
            self.assertEqual(r.value['voltage_text'],text.decode())
            self.assertNotIn('latched_errors',r.value);self.assertNotIn('voltage_low',r.value)
            self.assertEqual(diagnose_frame(voltage_frame(text=text)),'device_voltage')

    def test_reject_bad_format_counts_checksum_routing_and_truncated_data(self):
        for f in [voltage_frame(text=t) for t in [b'28.7v',b' 8.7V',b'28,7V',b'28.7',b'128.7V',
                       b'28.7V ',b'28.\xffV',b'28.7\x00',b'-1.0V',b'28.AV']]+[
                       voltage_frame(route=1),voltage_frame(unit=255),voltage_frame(dest=255),
                       voltage_frame(header=0x85),voltage_frame(attr=4),voltage_frame()[:-2]+'AB']:
            with self.subTest(frame=f):self.assertIsNone(parse_device_voltage_reply(f))

    def test_evidence_requires_local_addressed_bounded_checksum_valid_envelope(self):
        f=voltage_frame(text=b'UNKNOWN')
        r=parse_device_health_evidence(f);self.assertEqual(r.raw_hex,f)
        for f in [voltage_frame(text=bytes(19)),voltage_frame(header=0x85),voltage_frame(route=1),
                  voltage_frame(unit=255),voltage_frame(dest=255),voltage_frame(attr=0),
                  voltage_frame()[:-2]+'AB','not hex','']:
            self.assertIsNone(parse_device_health_evidence(f))


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.now=[0.];self.i=DeviceInventory(BridgeDiagnostics({}),clock=lambda:self.now[0],
                                           wall=lambda:1000+self.now[0])
        self.i.set_interface_unit(99);self.send=Mock()
        self.assertTrue(self.i.request({'health':[100]},True))

    def send_one(self):
        t=self.i.reserve();self.assertIsNotNone(t);self.assertTrue(self.i.transmit(t,self.send));return t

    def health(self):return self.i.snapshot()['devices'][0]['health']

    def fallback(self):
        self.assertEqual(self.send_one()['attribute'],4)
        self.now[0]=3;self.assertEqual(self.send_one()['attribute'],7)

    def test_summary_success_sends_no_fallback(self):
        self.send_one();self.i.receive(parse_device_health_reply(health_frame(unit=100)))
        self.assertIsNone(self.i.reserve());self.assertEqual(self.send.call_count,1)
        h=self.health();self.assertTrue(h['diagnostics_available']);self.assertEqual(h['voltage_source'],'identify_04')
        self.assertNotIn('voltage',h['reads'])

    def test_timeout_then_voltage_reply_yields_partial_health(self):
        self.fallback();self.assertEqual(self.health()['reads']['summary']['outcome'],'no_reply')
        self.assertTrue(self.i.receive(parse_device_voltage_reply(voltage_frame())))
        self.i.reserve();h=self.health()
        self.assertEqual(h['outcome'],'voltage_only');self.assertEqual(h['voltage_v'],28.7)
        self.assertEqual(h['voltage_source'],'identify_07');self.assertFalse(h['diagnostics_available'])
        self.assertEqual(h['sent_ts'],1000);self.assertEqual(h['observed_ts'],1003)
        self.assertNotIn('voltage_raw',h);self.assertNotIn('latched_errors',h)
        self.assertEqual(self.i.snapshot()['job']['units_finished'],1)

    def test_both_silent_reads_end_unknown_without_retry_or_zero(self):
        self.fallback();self.now[0]=6;self.assertIsNone(self.i.reserve())
        h=self.health();self.assertEqual(h['outcome'],'no_reply');self.assertNotIn('voltage_v',h)
        self.assertNotIn('observed_ts',h);self.assertEqual(self.send.call_count,2)
        self.now[0]=10000;self.assertIsNone(self.i.reserve());self.assertEqual(self.send.call_count,2)

    def test_first_alternative_only_preserved_without_extending_window_or_finishing_read(self):
        self.send_one()
        one=parse_device_health_evidence(voltage_frame(attr=4,text=b'ALT'))
        self.assertTrue(self.i.record_health_evidence(one));v=self.i.revision()
        for _ in range(1000):
            self.assertTrue(self.i.record_health_evidence(parse_device_health_evidence(voltage_frame(attr=4,text=b'OTHER'))))
        self.assertEqual(self.i.revision(),v)
        self.assertEqual(self.health()['reads']['summary']['reply_hex'],one.raw_hex)
        self.assertEqual(self.health()['outcome'],'pending');self.assertIsNone(self.i.reserve())
        self.now[0]=3;self.send_one()
        self.assertEqual(self.health()['reads']['summary']['outcome'],'unrecognized_reply')
        self.assertFalse(self.i.record_health_evidence(one))
        self.now[0]=6;self.i.reserve();self.assertEqual(self.health()['outcome'],'unrecognized_reply')
        self.assertNotIn('voltage_v',self.health())

    def test_alternative_then_valid_summary_uses_valid_data_no_fallback(self):
        self.send_one();self.i.record_health_evidence(parse_device_health_evidence(voltage_frame(attr=4)))
        self.i.receive(parse_device_health_reply(health_frame(unit=100)))
        self.i.reserve();self.assertEqual(self.health()['outcome'],'reply');self.assertEqual(self.send.call_count,1)

    def test_wrong_dest_unit_attr_unsolicited_and_late_evidence_never_stored(self):
        sample=parse_device_health_evidence(voltage_frame(attr=4))
        self.assertFalse(self.i.record_health_evidence(sample));self.send_one()
        for f in [voltage_frame(unit=101,attr=4),voltage_frame(dest=98,attr=4),voltage_frame()]:
            self.assertFalse(self.i.record_health_evidence(parse_device_health_evidence(f)))
        self.now[0]=3;self.assertFalse(self.i.record_health_evidence(sample))
        self.assertNotIn('reply_hex',self.health()['reads']['summary'])

    def test_late_summary_does_not_satisfy_voltage_and_voltage_evidence_not_measurement(self):
        self.fallback();self.assertFalse(self.i.receive(parse_device_health_reply(health_frame(unit=100))))
        self.assertTrue(self.i.record_health_evidence(parse_device_health_evidence(voltage_frame(text=b'bad'))))
        self.now[0]=6;self.i.reserve();self.assertEqual(self.health()['outcome'],'unrecognized_reply')
        self.assertNotIn('voltage_v',self.health());self.assertEqual(self.health()['reads']['voltage']['outcome'],'unrecognized_reply')

    def test_cancel_queued_fallback_and_disconnect_pending_voltage_do_not_resume(self):
        self.send_one();self.now[0]=3;t=self.i.reserve();self.i.cancel('cancelled')
        self.assertFalse(self.i.transmit(t,self.send));self.assertEqual(self.send.call_count,1)
        self.setUp();self.fallback();self.i.cancel()
        self.assertEqual(self.health()['reads']['voltage']['outcome'],'connection_lost')
        self.i.set_interface_unit(99);self.assertIsNone(self.i.reserve())
        self.assertFalse(self.i.receive(parse_device_voltage_reply(voltage_frame())))

    def test_full_255_silent_devices_remain_bounded_and_finish_within_deadline(self):
        self.i.cancel('cancelled');self.now[0]=30
        self.i.rows={u:{'unit':u,'type':'KEY4','outcome':'identified','attributes':{}} for u in range(255)}
        self.assertTrue(self.i.request('health',True))
        for u in range(255):
            t=self.send_one();self.assertEqual((t['unit'],t['attribute']),(u,4));self.now[0]+=3
            t=self.send_one();self.assertEqual((t['unit'],t['attribute']),(u,7));self.now[0]+=3
        self.i.reserve();self.assertEqual(self.i.snapshot()['job']['state'],'complete')
        self.assertEqual(self.i.snapshot()['job']['units_finished'],255);self.assertEqual(self.send.call_count,510)


class FallbackBridgeTests(unittest.TestCase):
    def test_voltage_fallback_obeys_ramp_pause_priority_and_cancel(self):
        b,w,now=previous.BridgeTests().make();b.request_inventory({'health':[100]},w)
        b.queue_inventory(w);w.run_one();now[0]=3
        b.ramps.entries[(56,13)]={};b.queue_inventory(w);self.assertEqual(w.snapshot()['depth'],0)
        b.ramps.entries.clear();w.submit(lambda lease:None,interactive=True)
        b.queue_inventory(w);self.assertEqual(w.snapshot()['depth'],1);w.run_one()
        b.queue_inventory(w);b.inventory.request('cancel',True);w.run_one()
        self.assertEqual(b.cni.send_hex_frame.call_count,1)

    def test_reader_records_alternative_and_then_accepts_voltage_without_lighting_publish(self):
        b,w,now=previous.BridgeTests().make();b.request_inventory({'health':[100]},w)
        b.queue_inventory(w);w.run_one();now[0]=3;b.queue_inventory(w);w.run_one()
        frames=iter([voltage_frame(text=b'ALT').encode(),voltage_frame().encode()])
        class End(BaseException):pass
        def recv():
            try:return next(frames)
            except StopIteration:raise End()
        b.cni.recv_line_bytes.side_effect=recv
        with self.assertRaises(End):b.run_forever(False,False)
        h=b.inventory.snapshot()['devices'][0]['health']
        self.assertEqual(h['outcome'],'voltage_only');self.assertIn('reply_hex',h['reads']['voltage'])
        b.mqtt.publish.assert_not_called();self.assertEqual(b._ack_waiters,{})


if __name__=='__main__':unittest.main()
