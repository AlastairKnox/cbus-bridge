"""Manual unicast error clearing: protocol, transaction, persistence and recovery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from cbus_bridge.cni_protocol import build_device_error_clear_request,parse_device_health_reply
from cbus_bridge.utils import DeviceInventory,DeviceMonitor,DeviceErrorStore,BridgeDiagnostics
from test_release18 import health_frame
from test_release20 import config


class ClearTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'audit.json'
        self.now=[0.];self.wall=[1000.]
        self.i=DeviceInventory(BridgeDiagnostics({}),clock=lambda:self.now[0],wall=lambda:self.wall[0])
        self.i.set_interface_unit(99)
        self.m=DeviceMonitor(self.i,config(1),clock=lambda:self.now[0],wall=lambda:self.wall[0])
        self.m.enable_error_clearing(self.path)
        self.send=Mock()

    def advance(self,n=3):self.now[0]+=n;self.wall[0]+=n
    def request(self,unit=0,request_id=1,**kw):
        return self.m.clear_errors({'unit':unit,'request_id':request_id,'confirm':True},True,**kw)
    def transmit(self):
        t=self.i.reserve();self.assertIsNotNone(t)
        self.assertTrue(self.i.transmit(t,self.send));return t
    def reply(self,errors=5,unit=0,dest=99):
        return self.i.receive(parse_device_health_reply(health_frame(unit=unit,dest=dest,errors=errors)))
    def preflight(self):
        self.assertTrue(self.request());self.transmit();self.assertTrue(self.reply())
    def verified(self,errors=0):
        self.preflight();self.advance();self.transmit();self.advance();self.transmit();self.reply(errors)
        self.i.reserve();self.m.tick(True)

    def test_protocol_exact_unicast_and_checksum_boundaries(self):
        self.assertEqual(build_device_error_clear_request(3),'06030008EF')
        for u in (0,2,254):
            raw=bytes.fromhex(build_device_error_clear_request(u))
            self.assertEqual(raw[:-1],bytes((6,u,0,8)));self.assertEqual(sum(raw)%256,0)
        for u in (True,False,-1,255,256,3.,'3',None):
            with self.assertRaises(ValueError):build_device_error_clear_request(u)

    def test_only_one_confirmed_configured_full_capability_unit(self):
        for spec in (None,0,{}, {'unit':0}, {'unit':[0],'request_id':1,'confirm':True},
                     {'unit':0,'request_id':True,'confirm':True},
                     {'unit':True,'request_id':1,'confirm':True},
                     {'unit':0,'request_id':0,'confirm':True},
                     {'unit':0,'request_id':1,'confirm':'yes'},
                     {'unit':0,'request_id':1,'confirm':True,'extra':1}):
            self.assertFalse(self.m.clear_errors(spec,True))
        for u in (1,2,255,-1):self.assertFalse(self.request(unit=u))
        self.assertFalse(self.request(readonly=True))
        self.assertFalse(self.m.clear_errors({'unit':0,'request_id':1,'confirm':True},False))
        self.i.interface_unit=None;self.assertFalse(self.request());self.send.assert_not_called()

    def test_disabled_without_state_and_unwritable_or_invalid_state(self):
        self.i.error_store=None;self.assertFalse(self.request())
        self.m.enable_error_clearing(self.path.parent/'missing'/'state.json')
        self.assertIsNone(self.m.error_store)
        # Invalid state does not fall back to an empty replay history.
        self.path.write_text('{broken')
        m=DeviceMonitor(self.i,config(1));m.enable_error_clearing(self.path)
        self.assertIsNone(m.error_store)

    def test_fresh_before_reset_then_verified_read_updates_baseline_only_after_reply(self):
        self.preflight()
        r=self.m.error_store.record(0);self.assertEqual(r['before_flags'],5)
        self.assertEqual(r['outcome'],'prepared');self.assertEqual(self.m.error_store.baseline(0,1),1)
        self.advance();self.transmit()
        self.assertEqual(self.send.call_args.args[0],build_device_error_clear_request(0))
        self.assertEqual(self.m.error_store.record(0)['outcome'],'sent_unverified')
        self.assertEqual(self.m.error_store.baseline(0,1),1)
        self.advance();self.transmit();self.assertTrue(self.reply(0));self.i.reserve();self.m.tick(True)
        self.assertEqual(self.send.call_count,3)
        r=self.m.snapshot()[0];self.assertEqual(r['last_clear']['outcome'],'verified_clear')
        self.assertEqual(r['last_clear']['after_flags'],0);self.assertEqual(r['new_error_bits'],0)
        self.assertEqual(self.m.error_store.baseline(0,1),0)
        self.assertEqual(self.i.job['state'],'complete')

    def test_flags_remaining_preserves_old_baseline(self):
        self.verified(errors=4)
        self.assertEqual(self.m.error_store.record(0)['outcome'],'flags_remaining')
        self.assertEqual(self.m.error_store.baseline(0,1),1)

    def test_no_preflight_reply_never_sends_clear_or_voltage_fallback(self):
        self.assertTrue(self.request());self.transmit();self.advance();self.assertIsNone(self.i.reserve())
        self.assertEqual(self.send.call_count,1)
        self.assertEqual(self.m.error_store.record(0)['outcome'],'preflight_no_reply')
        self.assertEqual(self.m.error_store.baseline(0,1),1)

    def test_no_post_reply_never_retries_and_never_changes_baseline(self):
        self.preflight();self.advance();self.transmit();self.advance();self.transmit()
        self.advance();self.i.reserve();self.advance(60);self.i.reserve()
        self.assertEqual(self.send.call_count,3)
        self.assertEqual(self.m.error_store.record(0)['outcome'],'unverified_no_reply')
        self.assertEqual(self.m.error_store.baseline(0,1),1)

    def test_wrong_unit_destination_and_expired_replies_are_rejected(self):
        self.assertTrue(self.request());self.transmit()
        self.assertFalse(self.reply(unit=1));self.assertFalse(self.reply(dest=98))
        self.advance();self.assertFalse(self.reply());self.i.reserve()
        self.assertEqual(self.send.call_count,1)

    def test_connection_loss_or_cancel_at_every_stage_never_replays(self):
        for stage in range(4):
            with self.subTest(stage=stage):
                self.setUp();self.assertTrue(self.request())
                if stage>=1:self.transmit();self.reply()
                if stage>=2:self.advance();self.transmit()
                if stage>=3:self.advance();self.transmit()
                self.i.cancel();self.assertIsNone(self.i.reserve())
                self.assertEqual(self.m.error_store.baseline(0,1),1)
                self.assertEqual(self.m.error_store.record(0)['outcome'],'connection_lost')
                restarted=DeviceErrorStore(self.path,config(1))
                self.assertEqual(restarted.baseline(0,1),1)

    def test_restart_before_or_after_write_marks_interrupted_without_replay(self):
        for sent in (False,True):
            with self.subTest(sent=sent):
                self.setUp();self.preflight()
                if sent:self.advance();self.transmit()
                restarted=DeviceErrorStore(self.path,config(1))
                self.assertEqual(restarted.record(0)['outcome'],
                                 'interrupted_unverified' if sent else 'interrupted_before_send')
                self.assertEqual(restarted.baseline(0,1),1)
                with self.assertRaises(ValueError):restarted.begin(0,1)

    def test_verified_baseline_survives_restart_and_recurring_tx_is_new(self):
        self.verified()
        m=DeviceMonitor(self.i,config(1),clock=lambda:self.now[0],wall=lambda:self.wall[0])
        m.enable_error_clearing(self.path)
        self.advance(31);self.assertTrue(self.i.request({'health':[0]},True))
        self.transmit();self.reply(4);self.i.reserve();m.tick(True)
        self.assertEqual(m.snapshot()[0]['new_errors'],['communications_tx_error'])
        self.assertEqual(m.snapshot()[0]['last_clear']['before_flags'],5)
        self.assertFalse(m.clear_errors({'unit':0,'request_id':1,'confirm':True},True))

    def test_new_request_replay_and_busy_do_not_replace_running_transaction(self):
        self.assertTrue(self.request());self.assertFalse(self.request(request_id=2))
        self.assertFalse(self.i.request({'units':[1]},True))
        self.assertEqual(self.i.job['mode'],'clear_errors')
        self.assertEqual(self.m.error_store.record(0)['request_id'],1)

    def test_storage_failure_before_reset_prevents_the_write(self):
        self.assertTrue(self.request());self.transmit()
        with patch('os.replace',side_effect=OSError('disk')):
            self.reply()
        self.assertIsNone(self.i.reserve());self.assertEqual(self.send.call_count,1)
        self.assertFalse(self.m.error_store.enabled)
        self.assertEqual(self.m.error_store.baseline(0,1),1)

    def test_storage_failure_marking_send_prevents_reset(self):
        self.preflight();self.advance()
        with patch('os.replace',side_effect=OSError('disk')):
            token=self.i.reserve();self.assertFalse(self.i.transmit(token,self.send))
        self.assertEqual(self.send.call_count,1);self.assertIsNone(self.i.reserve())

    def test_storage_failure_after_zero_reply_preserves_baseline_and_durable_uncertainty(self):
        self.preflight();self.advance();self.transmit();self.advance();self.transmit()
        with patch('os.replace',side_effect=OSError('disk')):self.reply(0)
        self.assertEqual(self.m.error_store.baseline(0,1),1)
        restarted=DeviceErrorStore(self.path,config(1))
        self.assertEqual(restarted.record(0)['outcome'],'interrupted_unverified')
        self.assertEqual(restarted.baseline(0,1),1)

    def test_send_exception_is_not_retried(self):
        self.preflight();self.advance();self.send.side_effect=OSError('link')
        with self.assertRaises(OSError):self.transmit()
        self.assertIsNone(self.i.reserve());self.assertEqual(self.send.call_count,2)
        self.assertEqual(self.m.error_store.baseline(0,1),1)

    def test_deadline_never_sends_late_write_and_duplicate_tokens_cannot_send_twice(self):
        self.preflight();self.advance();t=self.transmit()
        self.assertFalse(self.i.transmit(t,self.send));self.assertEqual(self.send.call_count,2)
        self.advance(60);self.assertIsNone(self.i.reserve())
        self.assertEqual(self.m.error_store.baseline(0,1),1)

    def test_atomic_state_permissions_roster_binding_and_bounded_records(self):
        self.verified()
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        self.assertEqual(list(self.path.parent.glob('.error-clear-*')),[])
        changed=config(1);changed['devices'][0]['firmware']='2.00'
        with self.assertRaises(ValueError):DeviceErrorStore(self.path,changed)
        for rid in range(2,1002):
            self.m.error_store.begin(0,rid);self.m.error_store.finish(0,'cancelled')
        self.assertEqual(len(self.m.error_store.data['records']),1)
        self.assertEqual(self.m.error_store.baseline(0,1),0)
        self.assertLess(self.path.stat().st_size,1000)

    def test_publisher_exposes_audit_scalar_fields_without_retaining_commands(self):
        self.verified();p=Mock();self.m.publish(p)
        row=json.loads(next(c.args[1] for c in p.publish.call_args_list if c.args[0].endswith('/0/state')))
        self.assertTrue(row['error_clear_enabled']);self.assertEqual(row['last_clear']['outcome'],'verified_clear')
        self.assertTrue(all(c.kwargs['retain'] is False for c in p.publish.call_args_list))


class ClearRoutingTests(unittest.TestCase):
    def test_retained_readonly_and_oversized_commands_never_reach_clear_manager(self):
        from types import SimpleNamespace
        from cbus_bridge import main as app
        for readonly in (False,True):
            client,cni,bridge,worker,monitor=Mock(),Mock(),Mock(),Mock(),Mock()
            cni.failover_status.return_value={};bridge.ramps.snapshot.return_value={}
            worker.snapshot.return_value={'ready':True}
            class End(BaseException):pass
            command={'clear_device_errors':{'unit':0,'request_id':1,'confirm':True}}
            def loop():
                for retained,payload in [(True,json.dumps(command).encode()),(False,b' '*257),
                                          (False,json.dumps(command).encode())]:
                    client.on_message(client,None,SimpleNamespace(topic='cbus/bridge/command',
                                                                   payload=payload,retain=retained))
                raise End()
            client.loop_start.side_effect=loop
            with patch.object(app,'load_ha_mqtt_profiles',return_value={}), \
                 patch.object(app.mqtt,'Client',return_value=client), \
                 patch.object(app,'CniConnection',return_value=cni), \
                 patch.object(app,'CbusMqttBridge',return_value=bridge), \
                 patch.object(app,'CommandWorker',return_value=worker), \
                 patch.object(app,'DeviceMonitor',return_value=monitor), \
                 patch.object(app.threading,'Timer'):
                with self.assertRaises(End):app.main(['--cni-host','unused','--ha-config','unused']+
                                                    (['--readonly'] if readonly else []))
            if readonly:monitor.clear_errors.assert_not_called()
            else:monitor.clear_errors.assert_called_once_with(command['clear_device_errors'],True,False)
            bridge.handle_mqtt_command.assert_not_called()

    def test_clear_uses_worker_budget_yields_to_lights_and_ramps_then_expires(self):
        import test_release16 as previous
        with tempfile.TemporaryDirectory() as td:
            b,w,now=previous.BridgeTests().make()
            m=DeviceMonitor(b.inventory,config(1),clock=lambda:now[0])
            m.enable_error_clearing(Path(td)/'audit.json')
            self.assertTrue(m.clear_errors({'unit':0,'request_id':1,'confirm':True},True))
            b.queue_inventory(w);w.run_one()
            self.assertEqual(b.cni.send_hex_frame.call_count,1)
            b.inventory.receive(parse_device_health_reply(health_frame(unit=0,errors=5)))
            now[0]=2;b.queue_inventory(w);self.assertEqual(w.snapshot()['depth'],0)
            now[0]=3;b.ramps.entries[(56,13)]={};b.queue_inventory(w)
            self.assertEqual(w.snapshot()['depth'],0)
            b.ramps.entries.clear();w.submit(lambda lease:None,interactive=True)
            b.queue_inventory(w);self.assertEqual(w.snapshot()['depth'],1)
            w.run_one();b.queue_inventory(w)
            seen=[];w.submit(lambda lease:seen.append('light'),interactive=True);w.run_one()
            self.assertEqual(seen,['light']);self.assertEqual(b.cni.send_hex_frame.call_count,1)
            w.run_one();self.assertEqual(b.cni.send_hex_frame.call_count,2)
            self.assertEqual(b.cni.send_hex_frame.call_args.args[0],build_device_error_clear_request(0))
            now[0]=60;b.queue_inventory(w)
            self.assertEqual(b.cni.send_hex_frame.call_count,2)
            self.assertEqual(m.error_store.record(0)['outcome'],'survey_deadline')

    def test_cli_clear_feature_defaults_disabled(self):
        from cbus_bridge.main import build_arg_parser
        args=build_arg_parser().parse_args(['--cni-host','unused','--ha-config','unused'])
        self.assertIsNone(args.device_error_state)


class ClearHaTests(unittest.TestCase):
    def test_generator_additive_confirmation_explicit_unit_dynamic_id_and_no_recorder_json(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('prepare_error_clear',Path(__file__).resolve().parents[1]/'tools/prepare_device_error_clear.py')
        tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
        package,card=tool.artifacts(config(2))
        self.assertEqual(len(package['input_select']['cbus_error_clear_unit']['options']),2)
        buttons=[c for c in card['cards'] if c['type']=='button']
        self.assertTrue(buttons[0]['tap_action']['confirmation']['text'])
        action=package['script']['cbus_clear_selected_unit_errors']['sequence'][-1]
        self.assertFalse(action['data']['retain']);self.assertEqual(action['data']['qos'],0)
        self.assertIn('request_id',action['data']['payload']);self.assertIn('now().timestamp()',action['data']['payload'])
        sensors=package['mqtt']['sensor'];self.assertEqual(len(sensors),5)
        self.assertTrue(all('json_attributes_topic' not in s for s in sensors))
        self.assertTrue(all(s['expire_after']==90 for s in sensors))
        self.assertIn('sensor.cbus_unit_*_error_clear_attempt',package['recorder']['exclude']['entity_globs'])
        self.assertNotIn('views',package);self.assertNotIn('automation',package)
        # A generated package must survive the same YAML load used by HA.
        import yaml
        self.assertEqual(yaml.safe_load(yaml.safe_dump(package)),package)


class ClearStorageIsolationTests(unittest.TestCase):
    def test_audit_failure_does_not_drop_cni_or_discard_waiting_lighting(self):
        import test_release16 as previous
        with tempfile.TemporaryDirectory() as td:
            b,w,now=previous.BridgeTests().make()
            m=DeviceMonitor(b.inventory,config(1),clock=lambda:now[0])
            m.enable_error_clearing(Path(td)/'audit.json')
            self.assertTrue(m.clear_errors({'unit':0,'request_id':1,'confirm':True},True))
            b.queue_inventory(w);w.run_one()
            b.inventory.receive(parse_device_health_reply(health_frame(unit=0,errors=5)))
            now[0]=3;b.queue_inventory(w)
            with patch('os.replace',side_effect=OSError('disk')):w.run_one()
            self.assertTrue(w.snapshot()['ready']);b.cni.close.assert_not_called()
            self.assertEqual(b.cni.send_hex_frame.call_count,1)
            self.assertEqual(m.error_store.record(0)['outcome'],'audit_failed')
            seen=[];w.submit(lambda lease:seen.append('light'),interactive=True);w.run_one()
            self.assertEqual(seen,['light'])

    def test_directory_sync_failure_after_rename_never_submits_clear(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'audit.json';i=DeviceInventory(BridgeDiagnostics({}));i.set_interface_unit(99)
            m=DeviceMonitor(i,config(1));m.enable_error_clearing(path)
            self.assertTrue(m.clear_errors({'unit':0,'request_id':1,'confirm':True},True))
            send=Mock();i.transmit(i.reserve(),send)
            i.receive(parse_device_health_reply(health_frame(unit=0,errors=5)))
            # Fail the directory durability barrier after the write-attempt record
            # was renamed. The socket must still remain untouched.
            with patch('os.fsync',side_effect=[None,OSError('directory fsync')]):
                self.assertFalse(i.transmit(i.reserve(),send))
            self.assertEqual(send.call_count,1)
            self.assertFalse(m.error_store.enabled)
            restart=DeviceErrorStore(path,config(1))
            self.assertEqual(restart.record(0)['outcome'],'interrupted_unverified')
            self.assertEqual(restart.baseline(0,1),1)
