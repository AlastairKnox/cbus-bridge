"""Real bridge: clear accepted during monitoring cooldown, then executed once.

Never connects to production. Run explicitly, outside unittest discovery.
"""
import json,os,subprocess,sys,tempfile,threading,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from integration_release14 import Broker,Cni,wait,packet,mqtt_string
from integration_release16 import frame
from test_release18 import health_frame


class ClearCni(Cni):
    def __init__(self):
        self.errors=5;self.management=[]
        super().__init__()

    def serve(self,s):
        def stream():
            while True:
                try:self.send(self.report(),s)
                except OSError:return
                time.sleep(.02)
        threading.Thread(target=stream,daemon=True).start()
        buf=b''
        try:
            while True:
                chunk=s.recv(4096)
                if not chunk:return
                buf+=chunk
                while b'\n' in buf:
                    line,buf=buf.split(b'\n',1);line=line.strip()
                    if line.startswith(b'A3'):
                        self.send(frame(bytes([0x86,99,99,0,0x32,int(line[2:4],16),0])),s)
                        continue
                    if not line.startswith(b'\\'):continue
                    data=line[1:];tag=None
                    if data[-1:] in [bytes([x]) for x in range(ord('g'),ord('z')+1)]:
                        tag=data[-1:];data=data[:-1]
                    raw=bytes.fromhex(data.decode());assert sum(raw)%256==0
                    if raw[0]==6:
                        assert raw[1:3]==bytes((20,0))
                        self.management.append((raw.hex().upper(),time.monotonic()))
                        if raw[3]==8:
                            assert len(raw)==5;self.errors=0
                        else:
                            assert raw[3:5]==bytes((0x21,4)) and len(raw)==6
                            # Foreign destination and fragmented valid reply.
                            self.send(health_frame(unit=20,dest=98,errors=self.errors).encode()+b'\r\n',s)
                            reply=health_frame(unit=20,errors=self.errors).encode()+b'\r\n'
                            self.send(reply[:7],s);self.send(reply[7:],s)
                    elif raw[3]==0x73:self.send(self.report(raw[6]),s)
                    else:
                        self.commands.append(raw.hex())
                        self.level=0 if raw[3]==1 else 255 if raw[3]==0x79 else raw[5]
                        if tag:self.send(tag+b'.',s)
        except (OSError,ValueError):pass


if __name__=='__main__':
    root=Path(__file__).resolve().parents[1];cni,broker=ClearCni(),Broker();out={}
    with tempfile.TemporaryDirectory() as directory:
        td=Path(directory);cfg=td/'mqtt.yaml';cfg.write_text('''light:
- state_topic: cbus/read/254/56/72/state
  command_topic: cbus/write/254/56/72/switch
  brightness_state_topic: cbus/read/254/56/72/level
  brightness_command_topic: cbus/write/254/56/72/ramp
  brightness_scale: 100
''')
        roster=td/'devices.json';roster.write_text(json.dumps({'schema':1,'refresh_seconds':900,
            'stale_seconds':2700,'devices':[{'unit':20,'type':'DIMDN8','manufacturer':'CLIPSAL',
                'firmware':'1.00','capability':'full','baseline_errors':5}]}))
        audit=td/'audit.json';log=td/'child.log'
        args=['--cni-host','127.0.0.1','--cni-port',str(cni.port),'--mqtt-host','127.0.0.1',
              '--mqtt-port',str(broker.port),'--ha-config',str(cfg),'--device-monitor-config',str(roster),
              '--device-error-state',str(audit),'--init','--diagnostics-every','1','--log-level','WARNING']
        def inventory():return json.loads(broker.latest.get('cbus/bridge/inventory','{}'))
        def state():return json.loads(broker.latest.get('cbus/bridge/devices/20/state','{}'))
        def start(stream):
            broker.latest.clear();connections=broker.connects
            p=subprocess.Popen([sys.executable,'-B',str(root/'run_bridge.py'),*args],
                               env=dict(os.environ,PYTHONPATH=str(root)),stdout=stream,stderr=stream)
            try:
                wait(lambda:broker.connects>connections)
                wait(lambda:inventory().get('interface_unit')==99)
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online')
            except BaseException:p.terminate();p.wait(timeout=5);raise
            return p
        with log.open('w') as stream:
            proc=start(stream)
            try:
                command=json.dumps({'clear_device_errors':{'unit':20,'request_id':1,'confirm':True}})
                broker.send(packet(0x31,mqtt_string('cbus/bridge/command')+command.encode()))
                time.sleep(.3);assert cni.management==[]
                # Reproduce production: a background health read just completed.
                broker.command('cbus/bridge/command',json.dumps({'inventory':{'health':[20]}}))
                wait(lambda:len(cni.management)==1)
                wait(lambda:inventory().get('job',{}).get('state')=='complete')
                broker.command('cbus/bridge/command',command)
                wait(lambda:(state().get('last_clear') or {}).get('outcome')=='waiting')
                assert len(cni.management)==1
                summary=json.loads(broker.latest['cbus/bridge/device_monitor'])
                assert summary['error_clear_request']['state']=='waiting'
                out['cooldown_request_durably_accepted_and_waiting_visible']='passed'
                began=time.monotonic();broker.command('cbus/write/254/56/72/ramp','50')
                wait(lambda:cni.level==128,2)
                out['lighting_during_clear_seconds']=round(time.monotonic()-began,3)
                wait(lambda:(state().get('last_clear') or {}).get('outcome')=='verified_clear',50)
                saved=json.loads(audit.read_text());record=saved['records']['20']
                assert record['outcome']=='verified_clear' and record['before_flags']==5 and record['after_flags']==0
                assert saved['baselines']=={'20':0}
                assert [bytes.fromhex(raw)[3] for raw,t in cni.management]==[0x21,0x21,8,0x21]
                assert all(b[1]-a[1]>=2.95 for a,b in zip(cni.management,cni.management[1:]))
                assert cni.management[1][1]-cni.management[0][1]>=29.9
                out['retained_rejected_and_one_paced_clear_verified']='passed'
                proc.terminate();proc.wait(timeout=5)
                cni.errors=4
                proc=start(stream)
                wait(lambda:state().get('status')=='fresh',40)
                assert state()['last_clear']['outcome']=='verified_clear'
                assert state()['new_errors']==['communications_tx_error']
                assert sum(bytes.fromhex(raw)[3]==8 for raw,t in cni.management)==1
                broker.command('cbus/bridge/command',command);time.sleep(.5)
                assert sum(bytes.fromhex(raw)[3]==8 for raw,t in cni.management)==1
                out['restart_preserves_baseline_and_recurring_tx_error_is_new']='passed'
                out['no_clear_replay_after_restart']='passed'
                broker.command('cbus/write/254/56/72/switch','OFF');wait(lambda:cni.level==0,2)
                out['ordinary_lighting_after_restart']='passed'
            except BaseException:
                print(log.read_text(),file=sys.stderr);raise
            finally:
                if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
    print(json.dumps(out,indent=2))
