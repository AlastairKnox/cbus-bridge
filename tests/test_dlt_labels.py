"""Text labels use existing serialization without changing lighting authority."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from cbus_bridge.cni_protocol import (DltLabelRequest, MonitoredLabel, build_dlt_label_frame,
    parse_dlt_label_request, parse_dlt_label_targets, parse_monitored_labels,
    parse_monitored_sals, diagnose_frame)
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge.utils import CommandWorker, CommandRejected


def rx(payload, unit=110):
    raw=bytes((5,unit,56,0))+bytes(payload)
    return (raw+bytes((-sum(raw)&255,))).hex().upper()


class LabelProtocolTests(unittest.TestCase):
    def test_exact_vector_and_language_variant_byte_mapping(self):
        # Independently composed CBUS-APP/02 section2.6.5 vector for group91.
        r=DltLabelRequest(91,'Night time')
        expected=bytes.fromhex('053800AD5B00014E696768742074696D65')
        self.assertEqual(bytes.fromhex(build_dlt_label_frame(r))[:-1],expected)
        for language in (1,2):
            for variant in range(1,5):
                raw=bytes.fromhex(build_dlt_label_frame(DltLabelRequest(91,'Jazz',language,variant)))
                self.assertEqual(raw[5:7],bytes(((variant-1)<<5,language)))
                self.assertEqual(sum(raw)&255,0)
                self.assertEqual(raw[3],0xA7)

    def test_printable_text_and_length_boundaries_preserve_literal_spaces(self):
        for text in ('X','Night time','A'*16,'A B',' test '):
            raw=bytes.fromhex(build_dlt_label_frame(DltLabelRequest(91,text)))
            self.assertEqual(raw[7:-1],text.encode('ascii'))
            self.assertEqual(raw[3]&31,len(text)+3)
        for text in ('',' ','A'*17,'Jazz\n','A\x00B','café',None,50):
            with self.subTest(text=text),self.assertRaises(ValueError):
                build_dlt_label_frame(DltLabelRequest(91,text))

    def test_no_clamping_bool_coercion_or_unsupported_payload_fields(self):
        for request in (DltLabelRequest(True,'x'),DltLabelRequest(255,'x'),DltLabelRequest(-1,'x'),
                        DltLabelRequest(91,'x',True),DltLabelRequest(91,'x',3),
                        DltLabelRequest(91,'x',1,True),DltLabelRequest(91,'x',1,0),
                        DltLabelRequest(91,'x',1,5)):
            with self.assertRaises(ValueError):build_dlt_label_frame(request)
        for payload in (b'Jazz',b'[]',b'{"text":"x","force":true}',b'{"text":"a","text":"b"}',
                        b'{"text":"a","variant":1.0}',b'{"text":"x","language":"1"}',b'\xff'):
            with self.assertRaises(ValueError):parse_dlt_label_request(91,payload)
        self.assertEqual(parse_dlt_label_request(91,b'{"text":"Jazz"}'),DltLabelRequest(91,'Jazz'))

    def test_targets_are_explicit_canonical_unique_and_bounded(self):
        self.assertEqual(parse_dlt_label_targets(['254/56/91']),{91})
        for targets in (['254/56/091'],['254/56/255'],['0/56/91'],['254/202/91'],
                        ['254/56/91 '],['254/56/91']*2,[f'254/56/{g}' for g in range(17)]):
            with self.assertRaises(ValueError):parse_dlt_label_targets(targets)
        self.assertEqual(parse_dlt_label_targets([]),set())

    def test_label_text_bytes_that_look_like_commands_never_become_actions(self):
        frame=rx(bytes((0xA7,91,0,1))+b'y\x38AZ')  # all printable; y is ON opcode
        self.assertEqual(diagnose_frame(frame),'label')
        self.assertEqual(parse_monitored_sals(frame),[])
        self.assertEqual(parse_monitored_labels(frame),[MonitoredLabel(110,56,91,'y8AZ',1,1)])

    def test_mixed_packet_preserves_real_command_and_skips_whole_label(self):
        frame=rx(bytes((0x79,13,0xA7,91,0x60,2))+b'Jazz')
        self.assertEqual(diagnose_frame(frame),'lighting')
        self.assertEqual([(s.cmd,s.group) for s in parse_monitored_sals(frame)],[(0x79,13)])
        self.assertEqual(parse_monitored_labels(frame)[0].variant,4)

    def test_received_deletion_is_observed_but_cannot_be_requested(self):
        self.assertEqual(parse_monitored_labels(rx(bytes((0xA3,91,0,1))))[0].text,'')
        with self.assertRaises(ValueError):build_dlt_label_frame(DltLabelRequest(91,''))

    def test_bad_label_rejects_whole_packet_without_partial_lighting_actions(self):
        for payload in (bytes((0x79,13,0xA7,91,0,1))+b'J',
                        bytes((0x79,13,0xA4,91,2,1,65)), # icon: unsupported
                        bytes((0x79,13,0xA4,91,0,1,0)),
                        bytes((0x79,13,0xA2,91,0))):
            frame=rx(payload)
            self.assertEqual(parse_monitored_sals(frame),[])
            self.assertEqual(parse_monitored_labels(frame),[])
        frame=rx(bytes((0xA4,91,0,1,65)))[:-2]+'FF'
        self.assertEqual(parse_monitored_labels(frame),[])
        self.assertEqual(diagnose_frame(frame),'checksum_failure')


class LabelBridgeTests(unittest.TestCase):
    def setUp(self):
        self.mqtt=Mock();self.mqtt.publish.return_value=SimpleNamespace(rc=0)
        self.b=CbusMqttBridge(Mock(),self.mqtt,{},False,ack_timeout_s=.001,dlt_label_targets={91})
        self.labels=self.b.dlt_labels;self.now=[0.];self.labels.clock=lambda:self.now[0]
        self.b.cni.send_hex_frame.side_effect=lambda frame:self.b._complete_ack(frame[-1],'.')
        self.worker=CommandWorker(self.b.diagnostics,cancel=self.b.cancel_session,clock=lambda:self.now[0])
        self.worker.set_link('mqtt',True);self.worker.set_link('cni',True)
    def enqueue(self,operation,**options):
        return self.worker.submit(lambda lease:self.b.execute_queued(operation,lease),**options)
    def admit(self,text='Jazz',**fields):
        return self.labels.admit('cbus/write/254/56/91/label',json.dumps({'text':text,**fields}).encode(),self.enqueue)
    def results(self):return [json.loads(c.kwargs['payload']) for c in self.mqtt.publish.call_args_list]

    def test_success_nonretained_result_and_no_light_state_or_event(self):
        self.assertTrue(self.admit('Night time'));self.worker.run_one()
        self.assertEqual(self.results()[-1]['result'],'ack_success')
        self.assertFalse(self.results()[-1]['display_confirmed'])
        self.assertEqual(self.b._ack_waiters,{})
        for c in self.mqtt.publish.call_args_list:
            self.assertEqual(c.args[0],'cbus/read/254/56/91/label_result')
            self.assertFalse(c.kwargs['retain']);self.assertEqual(c.kwargs['qos'],0)
        self.assertEqual(self.b.ramps.entries,{})
        self.assertEqual(self.b._last_published_state,{})

    def test_unconfigured_invalid_and_readonly_never_send(self):
        for topic in ('cbus/write/254/56/13/label','cbus/write/0/56/91/label'):
            self.assertFalse(self.labels.admit(topic,b'{"text":"Jazz"}',self.enqueue))
        self.assertFalse(self.admit(''))
        self.b.readonly=True;self.assertFalse(self.admit())
        self.worker.run_one();self.b.cni.send_hex_frame.assert_not_called()
        self.assertEqual(self.labels.cache,{})

    def test_latest_label_coalesces_and_lighting_has_priority(self):
        self.admit('First');self.admit('Second');self.admit('Last')
        order=[]
        self.worker.submit(lambda lease:order.append('light'),interactive=True,key='light')
        self.worker.run_one();self.assertEqual(order,['light'])
        self.assertEqual(self.b.cni.send_hex_frame.call_count,0)
        self.worker.run_one()
        raw=bytes.fromhex(self.b.cni.send_hex_frame.call_args.args[0][:-1])
        self.assertEqual(raw[7:-1],b'Last')
        self.assertFalse(self.worker.run_one())

    def test_same_group_brightness_and_different_variants_do_not_coalesce_each_other(self):
        self.admit('One');self.admit('Two',variant=2)
        self.worker.submit(lambda lease:None,interactive=True,key='cbus/write/254/56/91')
        self.assertEqual(self.worker.snapshot()['depth'],3)

    def test_duplicate_suppression_expires_and_known_external_change_invalidates_it(self):
        self.admit();self.worker.run_one()
        self.now[0]=2;self.admit();self.worker.run_one()
        self.assertEqual(self.results()[-1]['result'],'duplicate_skipped')
        self.assertEqual(self.b.cni.send_hex_frame.call_count,1)
        self.labels.observe(MonitoredLabel(4,56,91,'Other',1,1))
        self.admit();self.worker.run_one();self.assertEqual(self.b.cni.send_hex_frame.call_count,2)
        self.now[0]=33;self.admit();self.worker.run_one();self.assertEqual(self.b.cni.send_hex_frame.call_count,3)

    def test_global_rate_limit_and_queue_ttl_bound_label_traffic(self):
        self.admit();self.worker.run_one();self.admit('Other');self.worker.run_one()
        self.assertEqual(self.results()[-1]['result'],'rate_limited')
        self.assertEqual(self.b.cni.send_hex_frame.call_count,1)
        self.now[0]=1;self.admit('Other');self.worker.run_one()
        self.assertEqual(self.b.cni.send_hex_frame.call_count,2)
        self.admit('Expired');self.now[0]=10;self.worker.run_one()
        self.assertEqual(self.b.cni.send_hex_frame.call_count,2)

    def test_disconnect_drops_pending_and_duplicate_cache_without_replaying(self):
        self.admit();self.worker.run_one();self.admit('Pending')
        self.worker.set_link('cni',False);self.worker.set_link('cni',True)
        self.assertFalse(self.worker.run_one());self.assertEqual(self.labels.cache,{})
        self.now[0]=2;self.admit();self.worker.run_one()
        self.assertEqual(self.b.cni.send_hex_frame.call_count,2)

    def test_nacks_never_cache_and_timeout_invalidates_session(self):
        for i,result in enumerate(('#','$','%','!')):
            self.now[0]=i*2
            self.b.cni.send_hex_frame.side_effect=lambda frame,r=result:self.b._complete_ack(frame[-1],r)
            self.admit();self.worker.run_one();self.assertEqual(self.labels.cache,{})
            self.assertEqual(self.b._ack_waiters,{})
        self.now[0]=10;self.b.cni.send_hex_frame.side_effect=None
        self.admit();self.admit('Other',variant=2);self.worker.run_one()
        self.assertEqual(self.results()[-1]['result'],'ack_timeout')
        self.assertFalse(self.worker.snapshot()['ready']);self.assertFalse(self.worker.run_one())
        self.assertEqual(self.b._ack_waiters,{})

    def test_send_failure_does_not_leave_an_ack_waiter_or_queue_work(self):
        self.b.cni.send_hex_frame.side_effect=OSError('send failed')
        self.admit();self.worker.run_one()
        self.assertFalse(self.worker.snapshot()['ready']);self.assertEqual(self.b._ack_waiters,{})
        self.assertEqual(self.labels.cache,{})

    def test_cancellation_during_ack_cannot_publish_success_from_old_session(self):
        def send(frame):self.worker.set_link('cni',False)
        self.b.cni.send_hex_frame.side_effect=send
        self.admit();self.worker.run_one()
        self.assertEqual(self.results(),[]);self.assertEqual(self.labels.cache,{})
        self.assertEqual(self.b._ack_waiters,{})

    def test_reader_label_does_not_touch_physical_or_virtual_state(self):
        class End(BaseException):pass
        self.b.cni.recv_line_bytes.side_effect=[rx(bytes((0xA7,91,0,1))+b'Jazz').encode(),End()]
        with self.assertRaises(End):self.b.run_forever(False,False)
        self.assertEqual(self.results(),[])
        self.assertEqual(self.b._last_published_state,{})
        self.assertEqual(self.b.diagnostics.snapshot()['counters']['rx_label_total'],1)


if __name__=='__main__':unittest.main()
