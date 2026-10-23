"""Device monitoring pacing, session freshness, baseline and offline HA generation."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from cbus_bridge.utils import DeviceInventory,DeviceMonitor,BridgeDiagnostics
from cbus_bridge.cni_protocol import parse_device_health_reply
from test_release18 import health_frame
import test_release16 as previous


def config(count=21):
    return {'schema':1,'refresh_seconds':900,'stale_seconds':2700,'devices':[
        {'unit':u,'type':'DIMDN8','manufacturer':'CLIPSAL','firmware':'1.00',
         'capability':'full' if u<count else 'unavailable','baseline_errors':1}
        for u in range(count+1)]}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.now=[0.];self.wall=[1000.]
        self.i=DeviceInventory(BridgeDiagnostics({}),clock=lambda:self.now[0],wall=lambda:self.wall[0])
        self.i.set_interface_unit(99)
        self.m=DeviceMonitor(self.i,config(),clock=lambda:self.now[0],wall=lambda:self.wall[0])
        self.send=Mock()

    def advance(self,seconds):self.now[0]+=seconds;self.wall[0]+=seconds

    def result(self,errors=1,reply=True):
        t=self.i.reserve();self.assertIsNotNone(t);self.i.transmit(t,self.send)
        if reply:
            self.i.receive(parse_device_health_reply(health_frame(unit=t['unit'],errors=errors)))
        else:
            self.advance(3);t=self.i.reserve();self.i.transmit(t,self.send);self.advance(3)
        self.i.reserve();self.m.tick(True)

    def test_schedule_spreads_21_devices_over_15_minutes_and_never_polls_unavailable(self):
        seen=[];times=[]
        for _ in range(1801):
            self.m.tick(True)
            if self.i.job['state']=='running':
                seen.append(self.i.job['requested_units'][0]);times.append(self.now[0]);self.result()
            self.advance(1)
        self.assertEqual(seen[:21],list(range(21)))
        self.assertNotIn(21,seen)
        self.assertTrue(all(b-a>=42 for a,b in zip(times,times[1:])))
        by_unit={}
        for u,t in zip(seen,times):
            if u in by_unit:self.assertGreaterEqual(t-by_unit[u],900)
            by_unit[u]=t

    def test_missing_config_and_readonly_unready_unknown_interface_never_query(self):
        off=DeviceMonitor(self.i)
        self.advance(10000);off.tick(True);self.assertEqual(self.i.job['state'],'idle')
        self.m.tick(False);self.m.tick(True,True);self.assertEqual(self.i.job['state'],'idle')
        self.i.interface_unit=None;self.m.tick(True);self.assertEqual(self.i.job['state'],'idle')

    def test_manual_refresh_coalesces_rate_limits_and_queues_only_responders(self):
        self.assertTrue(self.m.refresh());self.assertFalse(self.m.refresh())
        self.advance(10);self.m.tick(True)
        self.assertEqual(self.i.job['requested_units'],list(range(21)))
        self.assertFalse(self.m.refresh_pending)
        self.assertFalse(self.i.request({'health':list(range(21))},True))

    def test_manual_survey_blocks_auto_and_manual_refresh_waits_without_cancelling_it(self):
        self.assertTrue(self.i.request({'units':[55]},True))
        self.advance(10);self.m.refresh();self.m.tick(True)
        self.assertEqual(self.i.job['requested_units'],[55]);self.assertTrue(self.m.refresh_pending)
        self.i.cancel('cancelled');self.advance(29);self.m.tick(True)
        self.assertEqual(self.i.job['state'],'cancelled')
        self.advance(1);self.m.tick(True);self.assertEqual(self.i.job['requested_units'],list(range(21)))

    def test_startup_has_no_saved_measurement_and_baseline_flags_not_new(self):
        self.assertEqual(self.m.snapshot()[0]['status'],'awaiting_reading')
        self.assertNotIn('voltage_v',self.m.snapshot()[0])
        self.advance(10);self.m.tick(True);self.result(errors=5)
        h=self.m.snapshot()[0]
        self.assertEqual(h['status'],'fresh');self.assertEqual(h['new_error_bits'],4)
        self.assertEqual(h['new_errors'],['communications_tx_error'])
        self.assertEqual(h['latched_errors'],['power_on_reset','communications_tx_error'])
        self.assertEqual(h['voltage_v'],32)
        self.assertEqual(self.m.snapshot()[21]['status'],'diagnostics_unavailable')

    def test_stale_after_threshold_monotonic_even_when_wall_clock_goes_backwards(self):
        self.advance(10);self.m.tick(True);self.result();self.wall[0]=-10000
        self.advance(2700)
        self.assertEqual(self.m.snapshot()[0]['status'],'stale')
        self.assertEqual(self.m.snapshot()[0]['age_s'],2700)

    def test_failure_keeps_last_observation_but_is_not_fresh_and_reconnect_requires_new_read(self):
        self.advance(10);self.m.tick(True);self.result();old=self.m.snapshot()[0]['observed_ts']
        self.advance(31);self.i.request({'health':[0]},True);self.result(reply=False)
        self.assertEqual(self.m.snapshot()[0]['status'],'no_reply')
        self.assertEqual(self.m.snapshot()[0]['observed_ts'],old)
        self.m.tick(False);self.i.cancel();self.i.set_interface_unit(99);self.advance(31)
        self.m.tick(True)
        self.assertNotEqual(self.m.snapshot()[0]['status'],'fresh')

    def test_reconnect_invalidates_even_completed_good_read_without_replaying(self):
        self.advance(10);self.m.tick(True);self.result()
        self.m.tick(False);self.i.cancel();self.i.set_interface_unit(99)
        self.m.tick(True);self.assertEqual(self.m.snapshot()[0]['status'],'awaiting_reading')
        self.assertEqual(self.i.job['state'],'complete')

    def test_publisher_is_bounded_and_heartbeats_without_attaching_sensor_attributes(self):
        p=Mock();self.m.publish(p);self.assertEqual(p.publish.call_count,23)
        self.m.publish(p);self.assertEqual(p.publish.call_count,23)
        self.advance(30);self.m.publish(p);self.assertEqual(p.publish.call_count,46)
        for c in p.publish.call_args_list:
            self.assertEqual(c.kwargs,{'qos':0,'retain':False})
            json.loads(c.args[1])

    def test_empty_and_invalid_rosters_fail_closed_without_loading_measurements(self):
        for change in [{'devices':[]},{'devices':config()['devices']*2},{'refresh_seconds':1},
                       {'stale_seconds':900},{'schema':True},{'schema':2}]:
            bad=config();bad.update(change)
            with self.assertRaises(ValueError):DeviceMonitor(self.i,bad)
        for field,value in [('unit',True),('unit',255),('capability','unsupported'),('baseline_errors',256),('type','\n')]:
            bad=config();bad['devices'][0][field]=value
            with self.assertRaises(ValueError):DeviceMonitor(self.i,bad)
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(DeviceMonitor.from_file(self.i,Path(td)/'absent').config)
            p=Path(td)/'bad';p.write_text('{')
            with self.assertRaises(ValueError):DeviceMonitor.from_file(self.i,p)

    def test_repeated_days_keep_bounded_roster_state_and_no_startup_scan(self):
        self.m=DeviceMonitor(self.i,config(1),clock=lambda:self.now[0],wall=lambda:self.wall[0])
        for _ in range(30*24*4):
            self.advance(900);self.m.tick(True);self.result()
            self.assertLessEqual(len(self.m.good),1);self.assertLessEqual(len(self.m.last_scan),1)
        self.assertTrue(all(bytes.fromhex(c.args[0])[3:5]==b'\x21\x04' for c in self.send.call_args_list))

    def test_background_monitor_respects_worker_ramp_priority(self):
        b,w,now=previous.BridgeTests().make()
        m=DeviceMonitor(b.inventory,config(1),clock=lambda:now[0]);now[0]=10;m.tick(True)
        b.ramps.entries[(56,13)]={};b.queue_inventory(w);self.assertEqual(w.snapshot()['depth'],0)
        b.ramps.entries.clear();w.submit(lambda lease:None,interactive=True)
        b.queue_inventory(w);self.assertEqual(w.snapshot()['depth'],1);w.run_one()
        b.queue_inventory(w);w.run_one();b.cni.send_hex_frame.assert_called_once()


class HaArtifactsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('prepare_monitor',Path(__file__).resolve().parents[1]/'tools/prepare_device_monitor.py')
        cls.tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.tool)

    def test_entities_preserve_voltage_history_exclude_timestamps_no_raw_attributes(self):
        package,view=self.tool.artifacts(config(2))
        ids=[]
        for domain,entities in package['mqtt'].items():
            for e in entities:
                ids.append(e['unique_id']);self.assertNotIn('json_attributes_topic',e)
                if 'state_topic' in e:self.assertEqual(e['expire_after'],90)
        self.assertEqual(len(ids),len(set(ids)))
        excluded=package['recorder']['exclude']
        self.assertEqual(excluded['entity_globs'],['sensor.cbus_unit_*_last_observed'])
        voltage=[e for e in package['mqtt']['sensor'] if e.get('device_class')=='voltage']
        self.assertEqual(len(voltage),2)
        self.assertTrue(all(e['state_class']=='measurement' for e in voltage))
        flags=package['mqtt']['binary_sensor']
        self.assertTrue(all('diagnostics_available' in e['availability'][1]['value_template'] for e in flags))
        self.assertEqual(view['path'],'devices')

    def test_button_never_retained_and_refresh_command_has_no_light_actions(self):
        p,v=self.tool.artifacts(config(1));button=p['mqtt']['button'][0]
        self.assertFalse(button['retain']);self.assertEqual(json.loads(button['payload_press']),{'device_monitor':'refresh'})
        self.assertEqual(button['command_topic'],'cbus/bridge/command')

    def test_cli_checks_do_not_implicitly_read_live_monitor_config(self):
        from cbus_bridge.main import build_arg_parser
        args=build_arg_parser().parse_args(['--cni-host','unused','--ha-config','unused'])
        self.assertIsNone(args.device_monitor_config)

    def test_generator_requires_completed_consistent_survey(self):
        for s in [{},{'job':{'state':'running','mode':'health'}},
                  {'job':{'state':'complete','mode':'health','id':2,'requested_units':[1]},'devices':[]}]:
            with self.assertRaises(ValueError):self.tool.roster(s)


if __name__=='__main__':unittest.main()
