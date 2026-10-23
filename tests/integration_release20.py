"""Real bridge on localhost: configured monitor, refresh, unavailable roster and recovery."""
import json,os,subprocess,sys,tempfile,time
from pathlib import Path
from integration_release14 import Broker,wait,packet,mqtt_string
from integration_release19 import FallbackCni


if __name__=='__main__':
    root=Path(__file__).resolve().parents[1];cni,broker=FallbackCni(),Broker();out={}
    with tempfile.TemporaryDirectory() as td:
        cfg=Path(td)/'mqtt.yaml';cfg.write_text('''light:
- state_topic: cbus/read/254/56/72/state
  command_topic: cbus/write/254/56/72/switch
  brightness_state_topic: cbus/read/254/56/72/level
  brightness_command_topic: cbus/write/254/56/72/ramp
  brightness_scale: 100
''')
        monitor=Path(td)/'devices.json';monitor.write_text(json.dumps({'schema':1,'refresh_seconds':900,
            'stale_seconds':2700,'devices':[{'unit':u,'type':'DIMDN8','manufacturer':'CLIPSAL','firmware':'1.00',
                'capability':'unavailable' if u==21 else 'full','baseline_errors':1} for u in (20,21,22)]}))
        args=['--cni-host','127.0.0.1','--cni-port',str(cni.port),'--mqtt-host','127.0.0.1',
              '--mqtt-port',str(broker.port),'--ha-config',str(cfg),'--device-monitor-config',str(monitor),
              '--init','--diagnostics-every','1','--log-level','WARNING']
        log=Path(td)/'child.log'
        def state(u):return json.loads(broker.latest.get(f'cbus/bridge/devices/{u}/state','{}'))
        with log.open('w') as stream:
            proc=subprocess.Popen([sys.executable,'-B',str(root/'run_bridge.py'),*args],
                env=dict(os.environ,PYTHONPATH=str(root)),stdout=stream,stderr=stream)
            try:
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online')
                wait(lambda:state(21).get('status')=='diagnostics_unavailable')
                assert state(20)['status']=='awaiting_reading' and 'voltage_v' not in state(20)
                broker.send(packet(0x31,mqtt_string('cbus/bridge/command')+b'{"device_monitor":"refresh"}'))
                time.sleep(.5);assert cni.queries==[]
                broker.command('cbus/bridge/command','{"device_monitor":"refresh"}')
                wait(lambda:len(cni.queries)>=1,15)
                started=time.monotonic();broker.command('cbus/write/254/56/72/ramp','50')
                wait(lambda:cni.level==128,2)
                out['lighting_during_monitor_seconds']=round(time.monotonic()-started,3)
                wait(lambda:state(20).get('status')=='fresh' and state(22).get('status')=='unrecognized_reply',35)
                assert state(20)['voltage_v']==32 and state(20)['new_error_bits']==0
                assert state(22).get('observed_ts') is None
                assert [(u,a) for u,a,t in cni.queries]==[(20,4),(22,4),(22,7)]
                assert all(b[2]-a[2]>=2.95 for a,b in zip(cni.queries,cni.queries[1:]))
                out['manual_refresh_only_configured_responders']='passed'
                out['baseline_flags_not_new_and_unrecognized_data_unknown']='passed'
                out['retained_command_ignored']='passed'
                before=cni.connects;cni.drop();wait(lambda:cni.connects>before)
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online')
                wait(lambda:state(20).get('status')=='awaiting_reading',35)
                assert len(cni.queries)==3
                broker.command('cbus/write/254/56/72/switch','OFF');wait(lambda:cni.level==0)
                out['reconnect_invalidates_old_measurement_without_replay']='passed'
                out['normal_control_after_reconnect']='passed'
                print(json.dumps(out,indent=2))
            except BaseException:
                print(log.read_text(),file=sys.stderr);raise
            finally:
                proc.terminate();proc.wait(timeout=5)
