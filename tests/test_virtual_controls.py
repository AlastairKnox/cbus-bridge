"""Opted-in command-only groups without weakening physical-light readback."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge.ha_profiles import GroupProfile,configure_virtual_controls
from cbus_bridge.cni_protocol import parse_monitored_sals,parse_status_reply,parse_binary_status_reply
from cbus_bridge import main as app


def frame(raw):return (bytes(raw)+bytes([-sum(raw)&255])).hex().upper()


class VirtualTests(unittest.TestCase):
    def setUp(self):
        self.p=GroupProfile(254,56,250,state_topic='cbus/read/254/56/250/level',
            command_topic='cbus/write/254/56/250/switch',level_state_topic='cbus/read/254/56/250/level',
            level_command_topic='cbus/write/254/56/250/ramp',level_mode='pct')
        self.physical=GroupProfile(254,56,13,state_topic='cbus/read/254/56/13/state',
            level_state_topic='cbus/read/254/56/13/level',level_mode='pct')
        ps={(254,56,250):self.p,(254,56,13):self.physical}
        configure_virtual_controls(ps,['254/56/250@110'])
        self.mqtt=Mock();self.mqtt.publish.return_value=SimpleNamespace(rc=0)
        self.b=CbusMqttBridge(Mock(),self.mqtt,ps,False,ramp_readback_every=3)
        self.v=self.b.virtual_controls;self.now=[0.];self.v.clock=lambda:self.now[0];self.v.ready()
    def events(self):return [json.loads(c.kwargs['payload']) for c in self.mqtt.publish.call_args_list if c.args[0].endswith('/event')]
    def levels(self):return [c.kwargs['payload'] for c in self.mqtt.publish.call_args_list if c.args[0]==self.p.level_state_topic]
    def command(self,cmd,level=None,unit=110):return self.v.command(56,250,cmd,level,unit=unit)

    def test_only_explicit_writable_profile_and_valid_source_can_opt_in(self):
        for spec in ('254/56/250@255','254/56/256@110','254/56/250@True','255/56/250@110',
                     '254/57/250@110','254/56/0250@110','254/56/250@110 ', '254/56/249@110'):
            with self.assertRaises(ValueError):configure_virtual_controls({(254,56,250):self.p},[spec])
        with self.assertRaises(ValueError):configure_virtual_controls({(254,56,250):self.p},['254/56/250@110']*2)
        self.p.command_topic=None
        with self.assertRaises(ValueError):configure_virtual_controls({(254,56,250):self.p},['254/56/250@110'])
        self.assertFalse(self.v.contains(56,13))
        self.assertFalse(self.command(0x79,unit=2));self.assertFalse(self.command(0x79,unit=111))
        self.assertEqual(self.events(),[])

    def test_actual_reported_frames_produce_on_and_43_percent_with_no_bus_writes(self):
        for raw in ('056E380079FAE2','056E38000AFAFF52','056E380002FA6DEC'):
            for sal in parse_monitored_sals(raw):self.v.command(sal.app,sal.group,sal.cmd,sal.level,unit=sal.dev)
        self.assertEqual(self.levels(),['100','43'])
        self.assertEqual([e['action'] for e in self.events()],['on','level'])
        self.assertEqual(self.events()[-1]['level'],109)
        self.assertTrue(all(e['physical_confirmed'] is False for e in self.events()))
        group=next(g for g in self.b.diagnostics.group_snapshot()['groups'] if g['group']==250)
        self.assertEqual(group['profile_kind'],'virtual_control')
        self.assertEqual(group['confidence'],'virtual_control')
        self.assertFalse(group['session_level_seen'])
        self.b.cni.send_hex_frame.assert_not_called()

    def test_reader_routes_virtual_sals_and_physical_load_still_waits_for_readback(self):
        class End(BaseException):pass
        self.b.cni.recv_line_bytes.side_effect=[b'056E380079FAE2',b'056E380002FA6DEC',
            frame([5,110,56,0,2,13,109]).encode(),End()]
        with self.assertRaises(End):self.b.run_forever(False,False)
        self.assertEqual(self.levels(),['100','43'])
        self.assertFalse(any(c.args[0]==self.physical.level_state_topic for c in self.mqtt.publish.call_args_list))
        self.assertNotIn((56,250),self.b.ramps.entries)
        self.assertIn((56,13),self.b.ramps.entries)

    def test_status_and_binary_status_never_override_virtual_control(self):
        self.command(2,109);n=len(self.mqtt.publish.call_args_list)
        self.b.handle_cbus_level_update(56,250,255,source='STATUS')
        self.b.handle_binary_status(SimpleNamespace(app=56,start_group=250,states={250:'off'}))
        self.assertEqual(len(self.mqtt.publish.call_args_list),n)
        self.assertEqual(self.levels(),['43'])

    def test_full_scale_ramp_interpolation_stop_freezes_and_off_is_explicit(self):
        self.command(2,0);self.now[0]=2;self.command(0x0A,255)
        self.now[0]=3;self.v.tick();self.assertEqual(self.events()[-1]['level'],64)
        self.now[0]=4;self.command(9);stopped=self.events()[-1]
        self.assertEqual(stopped['action'],'stop');self.assertEqual(stopped['level'],128)
        self.assertTrue(stopped['estimated']);self.assertFalse(stopped['ramping'])
        self.now[0]=100;self.v.tick();self.assertEqual(self.events()[-1],stopped)
        self.command(1);self.assertEqual(self.levels()[-1],'0');self.assertEqual(self.events()[-1]['action'],'off')

    def test_ramp_reaching_zero_and_stop_exactly_at_endpoint_publish_final_off(self):
        self.command(0x79);self.now[0]=2;self.command(0x0A,0)
        self.now[0]=6;self.command(9)
        self.assertEqual(self.levels()[-1],'0');self.assertEqual(self.events()[-1]['level'],0)
        self.v.tick();self.assertEqual(self.levels()[-1],'0')

    def test_unknown_start_does_not_invent_level_or_volume_and_stop_stays_unknown(self):
        self.command(0x0A,255);self.now[0]=100;self.v.tick();self.command(9)
        self.assertEqual(self.levels(),[])
        self.assertEqual(self.events()[-1]['action'],'stop');self.assertIsNone(self.events()[-1]['level'])
        self.command(2,109);self.assertEqual(self.levels(),['43'])

    def test_duplicate_on_level_and_ramp_never_restart_or_replay_actions(self):
        self.command(0x79);self.command(0x79);self.now[0]=20;self.command(0x79)
        self.assertEqual(len(self.events()),1)
        self.command(0x0A,0);self.now[0]=21;self.command(0x0A,0)
        self.now[0]=22;self.v.tick();self.assertEqual(self.events()[-1]['level'],128)
        self.now[0]=24;self.v.tick();self.command(1);self.assertEqual(self.events()[-1]['action'],'off')
        self.assertEqual(sum(e['action']=='off' for e in self.events()),1)

    def test_reset_cancels_ramp_discards_level_and_waits_for_fresh_session(self):
        self.command(0x79);self.now[0]=2;self.command(0x0A,0);session=self.events()[0]['session']
        self.v.reset();n=len(self.events());self.now[0]=10;self.v.tick();self.command(0x79)
        self.assertEqual(len(self.events()),n)
        self.assertEqual(self.mqtt.publish.call_args.kwargs['payload'],'offline')
        self.v.ready();self.command(0x0A,255);self.assertIsNone(self.events()[-1]['level'])
        self.command(2,109);self.assertNotEqual(self.events()[-1]['session'],session)

    def test_only_display_state_is_retained_never_control_events(self):
        self.command(0x79)
        for c in self.mqtt.publish.call_args_list:
            if c.args[0].endswith('/event'):
                self.assertEqual((c.kwargs['qos'],c.kwargs['retain']),(0,False))
            elif c.args[0]==self.p.level_state_topic:
                self.assertEqual((c.kwargs['qos'],c.kwargs['retain']),(0,True))

    def test_ack_updates_virtual_only_after_success_and_physical_ack_is_suppressed(self):
        self.b.write_ramp_to_level=Mock(return_value=False)
        self.b.handle_mqtt_command(self.p.level_command_topic,b'43');self.assertEqual(self.events(),[])
        self.b.write_ramp_to_level.return_value=True
        self.b.handle_mqtt_command(self.p.level_command_topic,b'43');self.assertEqual(self.levels(),['43'])
        self.assertEqual(self.events()[-1]['origin'],'transmitted_command')
        self.b.publish_level(self.physical,100,source='ACK')
        self.assertFalse(any(c.args[0]==self.physical.level_state_topic for c in self.mqtt.publish.call_args_list))

    def test_mqtt_explicit_stop_and_fixed_rate_work_but_duration_never_requests_physical_state(self):
        self.command(0x79);self.b.write_ramp_to_level=Mock(return_value=True)
        self.b.handle_mqtt_command(self.p.level_command_topic,b'{"level":0,"ramp_seconds":4}')
        self.now[0]=2;self.b._send_lighting_command=Mock(return_value=True)
        self.b.handle_mqtt_command(self.p.level_command_topic,b'{"stop":true}')
        self.assertEqual(self.events()[-1]['action'],'stop')
        self.b.handle_mqtt_command(self.p.level_command_topic,b'{"level":50,"transition_seconds":20}')
        self.b.cni.send_hex_frame.assert_not_called()

    def test_cli_default_does_not_opt_in_any_group(self):
        args=app.build_arg_parser().parse_args(['--cni-host','unused','--ha-config','unused'])
        self.assertEqual(args.virtual_control,[])


    def test_stop_before_first_progress_tick_still_publishes_new_positive_control(self):
        self.command(2,0);self.now[0]=2;self.command(0x0A,255)
        self.now[0]=2.1;self.command(9)
        self.assertEqual(self.events()[-1]['action'],'on')
        self.assertEqual(self.events()[-1]['command'],9)
        self.assertFalse(self.events()[-1]['ramping'])
        self.assertTrue(self.events()[-1]['estimated'])

    def test_invalid_virtual_cli_fails_before_opening_any_connection(self):
        with patch.object(app,'load_ha_mqtt_profiles',return_value={(254,56,250):self.p}), \
             patch.object(app.mqtt,'Client') as client, self.assertRaises(SystemExit):
            app.run(['--cni-host','unused','--ha-config','unused','--virtual-control','254/56/13@110'])
        client.assert_not_called()

    def test_long_running_model_state_is_bounded_by_explicit_profiles(self):
        for n in range(1000):
            self.now[0]=n*900.
            self.command(2,109 if n%2 else 0)
        self.assertEqual(len(self.v.states),1)
        self.assertLess(len(json.dumps(self.v.states[(56,250)])),600)
