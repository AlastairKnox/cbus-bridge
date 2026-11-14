"""Real process on disposable loopback peers, never production CNI or MQTT."""
import json,os,subprocess,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from integration_release14 import Broker,Cni,wait
from integration_release16 import frame

if __name__=='__main__':
    root=Path(__file__).resolve().parents[1];cni,broker=Cni(),Broker();out={}
    with tempfile.TemporaryDirectory() as td:
        td=Path(td);cfg=td/'mqtt.yaml';cfg.write_text('''light:
- name: Stereo
  state_topic: cbus/read/254/56/250/level
  command_topic: cbus/write/254/56/250/switch
  brightness_state_topic: cbus/read/254/56/250/level
  brightness_command_topic: cbus/write/254/56/250/ramp
  brightness_scale: 100
- name: Study
  state_topic: cbus/read/254/56/13/state
  command_topic: cbus/write/254/56/13/switch
  brightness_state_topic: cbus/read/254/56/13/level
  brightness_command_topic: cbus/write/254/56/13/ramp
''')
        args=['--cni-host','127.0.0.1','--cni-port',str(cni.port),'--mqtt-host','127.0.0.1',
              '--mqtt-port',str(broker.port),'--ha-config',str(cfg),'--init','--virtual-control','254/56/250@110',
              '--ramp-readback-every','3','--diagnostics-every','1','--log-level','WARNING']
        event_topic='cbus/bridge/controls/254/56/250/event';level_topic='cbus/read/254/56/250/level'
        availability='cbus/read/254/56/250/control_availability'
        def event():return json.loads(broker.latest.get(event_topic,'{}'))
        def inject(raw):cni.send(raw.encode()+b'\r\n')
        log=td/'child.log'
        with log.open('w') as stream:
            p=subprocess.Popen([sys.executable,'-B',str(root/'run_bridge.py'),*args],
                env=dict(os.environ,PYTHONPATH=str(root)),stdout=stream,stderr=stream)
            try:
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online')
                wait(lambda:broker.latest.get(availability)=='offline')
                inject('056E380079FAE2');wait(lambda:broker.latest.get(level_topic)=='100')
                wait(lambda:event().get('action')=='on');first=event();inject('056E380079FAE2')
                time.sleep(.2);assert event()==first
                inject('056E38000AFAFF52');inject('056E380002FA6DEC')
                wait(lambda:broker.latest.get(level_topic)=='43');wait(lambda:event().get('level')==109)
                assert event()['physical_confirmed'] is False
                out['captured_edlt_frames_to_normal_mqtt_level_and_fresh_event']='passed'
                assert broker.latest.get(availability)=='online'
                cni.send(frame(bytes([5,110,56,0,2,13,109])))
                time.sleep(.3);assert 'cbus/read/254/56/13/level' not in broker.latest
                out['physical_load_still_waits_for_status']='passed'
                # Ramp down at full-scale 4s, terminate before zero, then verify freeze.
                cni.send(frame(bytes([5,110,56,0,0x0A,250,0])))
                wait(lambda:event().get('estimated') and event().get('level',255)<109)
                cni.send(frame(bytes([5,110,56,0,9,250])))
                wait(lambda:event().get('action')=='stop');stopped=event()
                time.sleep(1);assert event()==stopped
                out['timed_ramp_and_stop_do_not_invent_a_physical_level']='passed'
                # Reconnect clears virtual freshness and must not replay the last action.
                connections=cni.connects;cni.drop();wait(lambda:cni.connects>connections,15)
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online',15)
                wait(lambda:broker.latest.get(availability)=='offline')
                time.sleep(.3);assert event()==stopped
                inject('056E380002FA6DEC');wait(lambda:event().get('session')!=stopped['session'])
                assert event()['level']==109
                out['reconnect_requires_new_input_without_replaying_old_controls']='passed'
                cni.send(frame(bytes([5,110,56,0,1,250])))
                wait(lambda:broker.latest.get(level_topic)=='0');wait(lambda:event().get('action')=='off')
                out['off_publishes_zero_and_stop_action']='passed'
            except BaseException:
                print(log.read_text(),file=sys.stderr);raise
            finally:
                p.terminate();p.wait(timeout=5)
    print(json.dumps(out,indent=2))
