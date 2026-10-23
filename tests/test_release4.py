import concurrent.futures
import json
import unittest
from unittest.mock import Mock, patch
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge.cni_protocol import diagnose_frame, parse_status_reply
from cbus_bridge.ha_profiles import GroupProfile
from cbus_bridge.utils import BridgeDiagnostics
from cbus_bridge import main as app


def packet(data):
    return (bytes(data) + bytes([(-sum(data)) & 255])).hex().upper()


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.p = GroupProfile(254, 56, 1, state_topic='test')
        self.d = BridgeDiagnostics({(254, 56, 1): self.p}, clock=lambda: self.now, wall=lambda: 1000 + self.now)

    def test_unknown_is_not_fabricated_as_healthy(self):
        snap = self.d.snapshot()
        self.assertIsNone(snap['last_cni_rx_ts'])
        self.assertEqual(snap['groups_never_observed'], 1)
        self.assertIsNone(self.d.group_snapshot()['groups'][0]['published_level'])
        self.assertEqual(self.d.group_snapshot()['groups'][0]['confidence'], 'unknown')

    def test_unsupported_is_not_a_network_error(self):
        self.d.bump('rx_unsupported_total')
        self.d.bump('ack_unmatched_total')
        self.assertEqual(self.d.snapshot()['errors_observed_total'], 0)
        self.d.bump('rx_checksum_failure_total')
        self.assertEqual(self.d.snapshot()['errors_observed_5min'], 1)
        self.now += 301
        self.assertEqual(self.d.snapshot()['errors_observed_5min'], 0)
        self.assertEqual(self.d.snapshot()['errors_observed_total'], 1)

    def test_rx_rate_and_age_decay_during_quiet_period(self):
        self.d.rx(); self.d.rx()
        self.d.tx('lighting_command')
        self.now += 2
        self.assertEqual(self.d.snapshot()['rx_tokens_per_second_60s'], 1)
        self.assertEqual(self.d.snapshot()['last_cni_rx_age_s'], 2)
        self.assertEqual(self.d.snapshot()['tx_frames_per_second_60s'], .5)
        self.now += 61
        self.assertEqual(self.d.snapshot()['rx_tokens_per_second_60s'], 0)

    def test_live_view_bounded_and_sampling_is_counted(self):
        for n in range(1000): self.d.event('lighting', 'RX', group=n % 256)
        view = self.d.traffic_snapshot()
        self.assertEqual(len(view['events']), 20)
        self.assertEqual(view['events'][0]['seq'], 981)
        self.assertEqual(self.d.snapshot()['counters']['traffic_events_sampled_out_total'], 980)
        self.assertEqual(self.d.traffic_snapshot()['events_since_publish'], 0)
        self.assertEqual(self.d.snapshot()['counters']['traffic_events_sampled_out_total'], 980)

    def test_no_backlog_replayed_after_mqtt_disconnect(self):
        self.d.event('ack', 'RX')
        self.assertIsNone(self.d.traffic_snapshot(connected=False))
        self.assertEqual(self.d.traffic_snapshot()['events'], [])
        self.assertEqual(self.d.snapshot()['counters']['traffic_events_not_streamed_total'], 1)

    def test_pending_coverage_tracks_unanswered_replies_not_absence(self):
        self.d.request(56, 0)
        self.d.observed(56, 1)
        self.d.published(self.p, 255, 'ACK')
        self.assertEqual(self.d.snapshot()['status_pending_groups'], 1)
        self.now += 16
        self.assertEqual(self.d.snapshot()['status_overdue_groups'], 1)
        absent = parse_status_reply(packet([0xe5, 7, 56, 1, 0, 0]))
        self.d.status(absent)
        self.assertEqual(self.d.snapshot()['status_pending_groups'], 0)
        self.assertEqual(self.d.snapshot()['status_absent_configured_groups'], 1)
        self.assertEqual(self.d.snapshot()['counters']['status_absent_groups_total'], 1)
        valid = parse_status_reply(packet([0xe5, 7, 56, 1, 0x55, 0x55]))
        self.d.status(valid)
        self.d.published(self.p, 255, 'STATUS')
        self.assertEqual(self.d.snapshot()['status_pending_groups'], 0)
        self.assertEqual(self.d.group_snapshot()['groups'][0]['confidence'], 'reported_level')

    def test_invalid_encoding_and_absence_are_separate(self):
        self.d.status(parse_status_reply(packet([0xe5, 7, 56, 1, 0xab, 0xcd])))
        self.assertEqual(self.d.snapshot()['counters']['status_invalid_encoding_groups_total'], 1)
        self.assertEqual(self.d.group_snapshot()['groups'][0]['status_quality'], 'invalid_encoding')

    def test_reconnect_clears_pending_but_preserves_historical_reading(self):
        self.d.published(self.p, 128, 'STATUS')
        self.d.request(56, 0)
        self.d.reset_connection()
        self.assertEqual(self.d.snapshot()['status_pending_groups'], 0)
        self.assertEqual(self.d.group_snapshot()['groups'][0]['published_level'], 128)
        self.assertEqual(self.d.group_snapshot()['groups'][0]['status_quality'], 'not_requested')

    def test_snapshot_is_json_serializable_and_detached(self):
        snap = self.d.group_snapshot()
        snap['groups'][0]['confidence'] = 'tampered'
        self.assertEqual(self.d.group_snapshot()['groups'][0]['confidence'], 'unknown')
        json.dumps(self.d.snapshot()); json.dumps(snap); json.dumps(self.d.traffic_snapshot())

    def test_traffic_is_nonretained_qos_zero_and_not_queued_offline(self):
        client = Mock(); client.publish.return_value.rc = 0
        self.d.event('ack', 'RX')
        self.d.publish_traffic(client, 'traffic', True)
        call = client.publish.call_args
        self.assertEqual(call.kwargs, {'qos': 0, 'retain': False})
        self.assertEqual(json.loads(call.args[1])['events'][0]['kind'], 'ack')
        client.reset_mock()
        self.d.publish_traffic(client, 'traffic', False)
        client.publish.assert_not_called()
        client.publish.return_value.rc = 4
        self.d.publish_traffic(client, 'traffic', True)
        self.assertEqual(self.d.snapshot()['counters']['traffic_publish_failures_total'], 1)

    def test_threaded_counters_do_not_lose_updates(self):
        def worker(_):
            for i in range(300): self.d.bump('count'); self.d.event('ack', 'RX')
        with concurrent.futures.ThreadPoolExecutor(4) as executor:
            list(executor.map(worker, range(4)))
        self.assertEqual(self.d.snapshot()['counters']['count'], 1200)
        self.assertEqual(len(self.d.traffic_snapshot()['events']), 20)


class ClassifierTests(unittest.TestCase):
    def test_known_damage_distinguished_from_unsupported(self):
        self.assertEqual(diagnose_frame('05033800790800'), 'checksum_failure')
        self.assertEqual(diagnose_frame(packet([5, 3, 56, 0, 0x0a, 8])), 'malformed')
        self.assertEqual(diagnose_frame(packet([5, 3, 56, 0, 0xff, 8])), 'unsupported')
        self.assertEqual(diagnose_frame(packet([0xc2, 56, 0])), 'malformed')
        self.assertEqual(diagnose_frame('hello'), 'nonhex')
        self.assertEqual(diagnose_frame(packet([5, 3, 202, 0, 1, 8])), 'unsupported')


class StopRead(BaseException): pass


class InstrumentationTests(unittest.TestCase):
    def test_command_outcomes_are_separate(self):
        for code, counter in [('.', 'success'), ('#', 'retries_exhausted'), ('$', 'corruption'), ('%', 'clock_loss'), ('!', 'bang'), (None, 'timeout')]:
            with self.subTest(code=code):
                cni = Mock(); bridge = CbusMqttBridge(cni, Mock(), {}, False, ack_timeout_s=.001)
                def send(frame):
                    if code is not None: bridge._complete_ack(frame[-1], code)
                cni.send_hex_frame.side_effect = send
                bridge.write_on(56, 1)
                counts = bridge.diagnostics.snapshot()['counters']
                self.assertEqual(counts['command_' + counter + '_total'], 1)
                self.assertEqual(counts['cni_tx_frames_total'], 1)

    def test_unmatched_negative_ack_is_visible_without_double_counting(self):
        bridge = CbusMqttBridge(Mock(), Mock(), {}, False)
        bridge._complete_ack('g', '%')
        snap = bridge.diagnostics.snapshot()
        self.assertEqual(snap['errors_observed_total'], 1)
        self.assertEqual(snap['counters']['ack_unmatched_total'], 1)
        self.assertEqual(snap['counters']['ack_clock_loss_tokens_total'], 1)
        bridge.diagnostics.bump('command_clock_loss_total')
        self.assertEqual(bridge.diagnostics.snapshot()['errors_observed_total'], 1)

    def test_send_failure_does_not_count_a_successful_transmission(self):
        cni = Mock(); cni.send_hex_frame.side_effect = ConnectionError('offline')
        bridge = CbusMqttBridge(cni, Mock(), {}, False)
        with self.assertRaises(ConnectionError): bridge.write_on(56, 1)
        counts = bridge.diagnostics.snapshot()['counters']
        self.assertEqual(counts['command_send_failures_total'], 1)
        self.assertNotIn('cni_tx_frames_total', counts)
        self.assertEqual(bridge._ack_waiters, {})

    def test_stream_records_bus_frames_and_decoded_commands(self):
        cni = Mock()
        cni.recv_line_bytes.side_effect = [b'g.', b'0503380079083F', b'05033800790800', b'NOT A FRAME', StopRead()]
        bridge = CbusMqttBridge(cni, Mock(), {}, False)
        with self.assertRaises(StopRead): bridge.run_forever(False, False)
        snap = bridge.diagnostics.snapshot()
        self.assertEqual(snap['counters']['cni_rx_tokens_total'], 4)
        self.assertEqual(snap['counters']['rx_checksum_failure_total'], 1)
        kinds = [e['kind'] for e in bridge.diagnostics.traffic_snapshot()['events']]
        self.assertIn('lighting', kinds); self.assertIn('ack', kinds)
        self.assertIn('checksum_failure_frame', kinds)
        self.assertNotIn('NOT A FRAME', json.dumps(bridge.diagnostics.traffic_snapshot()))

    def test_actual_vs_optimistic_state_source_is_explicit(self):
        p = GroupProfile(254, 56, 1, state_topic='test')
        client = Mock()
        client.publish.return_value.rc = 0
        bridge = CbusMqttBridge(Mock(), client, {(254, 56, 1): p}, False)
        bridge.publish_level(p, 128)
        self.assertEqual(bridge.diagnostics.group_snapshot()['groups'][0]['confidence'], 'transmitted')
        bridge.handle_cbus_level_update(56, 1, 255, source='SAL cmd=0x79')
        self.assertEqual(bridge.diagnostics.group_snapshot()['groups'][0]['confidence'], 'observed_command')
        bridge.handle_cbus_level_update(56, 1, 128, source='STATUS')
        self.assertEqual(bridge.diagnostics.group_snapshot()['groups'][0]['confidence'], 'reported_level')


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        timer = patch.object(app.threading, 'Timer')
        timer.start()
        self.addCleanup(timer.stop)

    def test_periodic_diagnostics_do_not_poll_cni_and_use_fresh_topics(self):
        import threading
        mqtt_up, cni_up = threading.Event(), threading.Event()
        stop = Mock(); stop.wait.side_effect = [False, True]
        client, cni, bridge = Mock(), Mock(), Mock()
        bridge.ramps.snapshot.return_value = {}
        bridge.inventory.snapshot.return_value = {"version": 0}
        client.publish.return_value.rc = 0
        cni.failover_status.return_value = {}
        client.loop_start.side_effect = lambda: client.on_connect(client, None, {}, 0)
        bridge.run_forever.side_effect = StopRead()
        targets = {}
        def thread(**kwargs):
            targets[kwargs['name']] = kwargs['target']
            return Mock()
        with patch.object(app.threading, 'Event', side_effect=[mqtt_up, cni_up, stop]), \
             patch.object(app.threading, 'Thread', side_effect=thread), \
             patch.object(app, 'load_ha_mqtt_profiles', return_value={}), \
             patch.object(app.mqtt, 'Client', return_value=client), \
             patch.object(app, 'CniConnection', return_value=cni), \
             patch.object(app, 'CbusMqttBridge', return_value=bridge):
            with self.assertRaises(StopRead):
                app.main(['--cni-host','unused','--ha-config','unused',
                          '--diagnostics-every','5','--traffic-every','1'])
            targets['cbus-metrics']()
        traffic = [c for c in client.publish.call_args_list if c.args[0] == 'cbus/bridge/traffic']
        self.assertEqual(len(traffic), 1)
        self.assertEqual(traffic[0].kwargs, {'qos': 0, 'retain': False})
        groups = [c for c in client.publish.call_args_list if c.args[0] == 'cbus/bridge/status/groups']
        self.assertEqual(groups[0].kwargs, {'qos': 0, 'retain': False})
        fresh = [c for c in client.publish.call_args_list if c.args[0] == 'cbus/bridge/status/diagnostics']
        self.assertTrue(fresh)
        self.assertTrue(all(c.kwargs['retain'] is False for c in fresh))
        cni.send_line.assert_not_called(); cni.send_hex_frame.assert_not_called()
        bridge.request_resync.assert_not_called()


class ProposalTests(unittest.TestCase):
    def test_proposed_ha_entities_are_unique_and_live_buffers_excluded(self):
        from pathlib import Path
        import yaml
        root = Path(__file__).resolve().parents[1] / 'examples/homeassistant'
        package = yaml.safe_load((root / 'cbus_diagnostics_package.yaml').read_text())
        sensors = package['mqtt']['sensor']
        self.assertEqual(len({s['unique_id'] for s in sensors}), len(sensors))
        self.assertEqual(len({s['default_entity_id'] for s in sensors}), len(sensors))
        excluded = package['recorder']['exclude']['entities']
        for identity in ['sensor.cbus_diag_traffic', 'sensor.cbus_diag_groups']:
            self.assertIn(identity, excluded)
            sensor = next(s for s in sensors if s['default_entity_id'] == identity)
            self.assertIn('json_attributes_topic', sensor)
            self.assertGreater(sensor['expire_after'], 0)
        dashboard = yaml.safe_load((root / 'cbus_dashboard.yaml').read_text())
        ids = {s['default_entity_id'] for s in sensors}
        for card in dashboard['views'][0]['cards']:
            self.assertFalse(card['type'].startswith('custom:'))
            for entity in card.get('entities', card.get('entity_id', [])):
                if isinstance(entity, dict):
                    self.assertTrue(entity.get('name'))
                    self.assertNotIn('C-Bus', entity['name'])
                    self.assertIn(entity['entity'], ids)
                else:
                    self.assertIn(entity, ids)

    def test_cli_prevents_feedback_and_unbounded_publish_intervals(self):
        for flags in [['--traffic-every','0.01'], ['--diagnostics-every','nan'],
                      ['--traffic-topic','cbus/write/254/56/1/switch'],
                      ['--traffic-topic','cbus/bridge/status']]:
            with self.subTest(flags=flags), patch.object(app.mqtt, 'Client') as client:
                with self.assertRaises(SystemExit):
                    app.main(['--cni-host','unused','--ha-config','unused',*flags])
                client.assert_not_called()
