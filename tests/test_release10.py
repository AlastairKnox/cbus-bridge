import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from cbus_bridge.cni_protocol import parse_interface_reply, parse_status_reply, parse_binary_status_reply
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge.utils import BridgeDiagnostics
from cbus_bridge.ha_profiles import GroupProfile


def packet(data):
    return (bytes(data)+bytes([-sum(data) & 255])).hex().upper()


class SetupReplyTests(unittest.TestCase):
    def test_real_startup_samples(self):
        self.assertEqual(parse_interface_reply('3242008C')['parameter'],0x42)
        self.assertEqual(parse_interface_reply('8602020032300014')['unit'],2)
        self.assertIsNone(parse_interface_reply('A3210038322100AD'))
        parsed=parse_interface_reply('A3210038322100AD',['A3210038'])
        self.assertEqual(parsed['echoed_setup'],'A3210038')
        self.assertEqual(parsed['parameter'],0x21)
        self.assertIsNotNone(parse_interface_reply('A3210038g322100AD',['A3210038']))

    def test_invalid_and_unrelated_replies_never_match(self):
        for text in ['3242008D','324200','3242008CFF','zz3242008C','3242018B',
                     packet([0x86,2,3,0,0x32,0x30,0]),
                     packet([0x86,2,2,0,0x89,2,0]),'A32100383242008C']:
            with self.subTest(text=text):
                self.assertIsNone(parse_interface_reply(text,['A3210038']))

    def run_reader(self,frames,pending=None):
        b=CbusMqttBridge(Mock(),Mock(),{},False)
        b._pending_setup=pending or {}
        class End(BaseException):pass
        b.cni.recv_line_bytes.side_effect=[*frames,End()]
        with self.assertRaises(End):b.run_forever(False,False)
        b.cni.send_line.assert_not_called();b.cni.send_hex_frame.assert_not_called()
        b.mqtt.publish.assert_not_called()
        return b

    def test_reader_consumes_startup_without_state_or_lighting_ack(self):
        with patch('cbus_bridge.bridge.time.monotonic',return_value=1):
            b=self.run_reader([b'A3210038322100AD',b'3242008C',b'8602020032300014'],{'A3210038':10})
        self.assertEqual(b.diagnostics.snapshot()['counters']['interface_setup_replies_total'],3)
        self.assertEqual(b.diagnostics.snapshot()['counters'].get('rx_unsupported_total',0),0)
        self.assertFalse(b._pending_setup)

    def test_expired_echo_and_corrupt_reply_still_visible(self):
        with patch('cbus_bridge.bridge.time.monotonic',return_value=20):
            b=self.run_reader([b'A3210038322100AD',b'3242008D'],{'A3210038':10})
        self.assertEqual(b.diagnostics.snapshot()['counters']['rx_unsupported_total'],2)


class ActivityTests(unittest.TestCase):
    def test_binary_flood_does_not_displace_activity_or_hide_errors(self):
        d=BridgeDiagnostics({})
        d.event('lighting','RX',group=13)
        for _ in range(200):
            d.event('binary_status_frame','RX');d.event('binary_status_report','RX')
        d.event('checksum_failure_frame','RX')
        view=d.traffic_snapshot()
        self.assertEqual([e['kind'] for e in view['activity_events']],['lighting','checksum_failure_frame'])
        self.assertEqual(len(view['events']),20)
        self.assertEqual(d.snapshot()['counters']['traffic_routine_events_total'],400)
        self.assertEqual(d.snapshot()['counters']['traffic_events_sampled_out_total'],382)
        d.traffic_snapshot(False)
        self.assertFalse(d.traffic_snapshot()['activity_events'])

    def test_negative_acks_and_binary_error_reports_remain_in_activity(self):
        d=BridgeDiagnostics({})
        d.event('ack','RX',code='.')
        d.event('ack','RX',code='#')
        d.event('ack','RX',code='!')
        d.event('binary_status_report','RX',error=1)
        self.assertEqual(len(d.traffic_snapshot()['activity_events']),3)

    def test_activity_itself_stays_bounded_and_setup_is_detailed_only(self):
        d=BridgeDiagnostics({})
        for _ in range(30):d.event('lighting','RX')
        d.event('interface_setup_reply','RX')
        view=d.traffic_snapshot()
        self.assertEqual(len(view['activity_events']),20)
        self.assertEqual(view['events'][-1]['kind'],'interface_setup_reply')


class CoverageTests(unittest.TestCase):
    def test_session_coverage_and_numeric_age_are_independent_of_commands(self):
        now=[0.]
        p=GroupProfile(254,56,13,level_mode='pct')
        d=BridgeDiagnostics({(254,56,13):p},clock=lambda:now[0],wall=lambda:100+now[0])
        d.request(56,0)
        now[0]=2
        d.status(parse_status_reply(packet([0xe5,7,56,13,0xaa,0xaa])))
        self.assertEqual(d.snapshot()['session_level_reported_groups'],1)
        now[0]=12;d.observed(56,13)
        d.binary_status(parse_binary_status_reply(packet([0xc3,56,12,0x04])))
        row=d.group_snapshot()['groups'][0]
        self.assertEqual(row['observation_age_s'],0)
        self.assertEqual(row['level_report_age_s'],10)
        d.reset_connection()
        row=d.group_snapshot()['groups'][0]
        self.assertFalse(row['session_level_seen'])
        self.assertEqual(row['level_report_age_s'],10)
        self.assertEqual(d.snapshot()['session_level_unconfirmed_groups'],1)
        self.assertEqual(row['status_quality'],'not_requested')

    def test_absent_reply_is_not_unanswered_or_automatic_bus_fault(self):
        p=GroupProfile(254,56,13)
        d=BridgeDiagnostics({(254,56,13):p})
        d.request(56,0)
        d.status(parse_status_reply(packet([0xe5,7,56,13,0,0])))
        s=d.snapshot()
        self.assertEqual(s['status_absent_configured_groups'],1)
        self.assertEqual(s['status_pending_groups'],0)
        self.assertEqual(s['session_level_reported_groups'],0)
        self.assertEqual(s['errors_observed_total'],0)


class HaProposalTests(unittest.TestCase):
    def test_new_metrics_and_both_views_keep_raw_buffers_excluded(self):
        import yaml
        root=Path(__file__).resolve().parents[1]/'examples/homeassistant'
        pkg=yaml.safe_load((root/'cbus_diagnostics_package.yaml').read_text())
        sensors=pkg['mqtt']['sensor']; ids={s['default_entity_id'] for s in sensors}
        self.assertEqual(len(sensors),57)
        dashboard=yaml.safe_load((root/'cbus_dashboard.yaml').read_text())
        self.assertEqual(len(dashboard['views']),2)
        for view in dashboard['views']:
            for card in view['cards']:
                for entity in card.get('entities',card.get('entity_id',[])):
                    self.assertIn(entity['entity'] if isinstance(entity,dict) else entity,ids)
        wire=next(c for c in dashboard['views'][0]['cards'] if c.get('title','').startswith('Live wire'))
        self.assertIn('activity_events',wire['content'])
        self.assertNotIn('activity_events',dashboard['views'][1]['cards'][0]['content'])
        self.assertEqual(set(pkg['recorder']['exclude']['entities']),{'sensor.cbus_diag_traffic','sensor.cbus_diag_groups'})
