"""Full-process loopback discovery, quiet-unit identification and partial-map stop."""
import json,os,subprocess,sys,tempfile,threading,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from integration_release14 import Broker,Cni,wait,packet,mqtt_string
from integration_release16 import frame
from test_release17 import presence_frame


class DiscoveryCni(Cni):
    def __init__(self):
        self.queries=[];self.presence_requests=[];self.partial=False
        super().__init__()

    def serve(self,s):
        def stream():
            while True:
                try:self.send(self.report(),s)
                except OSError:return
                time.sleep(.01)
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
                    if data[-1:] in [bytes([x]) for x in range(ord('g'),ord('z')+1)]:tag=data[-1:];data=data[:-1]
                    raw=bytes.fromhex(data.decode());assert sum(raw)%256==0
                    if raw[0]==6:
                        assert len(raw)==6 and raw[2:4]==b'\x00\x21'
                        unit,attr=raw[1],raw[4];assert unit in (3,99) and attr in (0,1,2)
                        self.queries.append((unit,attr,time.monotonic()))
                        text={0:b'CLIPSAL ',1:b'DIMMER  ' if unit==3 else b'PC_CNIED',2:b'5.5.00  '}[attr]
                        self.send(frame(bytes([0x86,unit,99,0,0x89,attr])+text),s)
                    elif raw[3]==0x7A:
                        assert raw.hex().upper()=='05FF007AFF0083'
                        self.presence_requests.append(time.monotonic())
                        parts=[presence_frame(start,count,{3:1,99:1}).encode()+b'\r\n'
                               for start,count in [(0,88),(88,88),(176,80)]]
                        # Include fragmented transport, reordering and a duplicate.
                        sequence=[parts[0]] if self.partial else [parts[2],parts[0],parts[0],parts[1]]
                        for part in sequence:
                            self.send(part[:9],s);self.send(part[9:],s)
                    elif raw[3]==0x73:self.send(self.report(raw[6]),s)
                    else:
                        self.commands.append(raw.hex())
                        self.level=0 if raw[3]==1 else 255 if raw[3]==0x79 else raw[5]
                        if tag:self.send(tag+b'.',s)
        except (OSError,ValueError):pass


if __name__=='__main__':
    root=Path(__file__).resolve().parents[1];cni,broker=DiscoveryCni(),Broker();out={}
    with tempfile.TemporaryDirectory() as td:
        cfg=Path(td)/'mqtt.yaml';cfg.write_text('''light:
- state_topic: cbus/read/254/56/72/state
  command_topic: cbus/write/254/56/72/switch
  brightness_state_topic: cbus/read/254/56/72/level
  brightness_command_topic: cbus/write/254/56/72/ramp
  brightness_scale: 100
''')
        args=['--cni-host','127.0.0.1','--cni-port',str(cni.port),'--mqtt-host','127.0.0.1',
              '--mqtt-port',str(broker.port),'--ha-config',str(cfg),'--init','--diagnostics-every','1',
              '--log-level','WARNING']
        log=Path(td)/'children.log'
        def inventory():return json.loads(broker.latest.get('cbus/bridge/inventory','{}'))
        def start(stream):
            connections=broker.connects
            p=subprocess.Popen([sys.executable,'-B',str(root/'run_bridge.py'),*args],
                               env=dict(os.environ,PYTHONPATH=str(root)),stdout=stream,stderr=stream)
            try:
                wait(lambda:broker.connects>connections)
                wait(lambda:inventory().get('interface_unit')==99 and inventory().get('job',{}).get('state')=='idle')
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online' and
                     broker.latest_generation.get('cbus/bridge/availability')==id(broker.current))
            except BaseException:
                p.terminate();p.wait(timeout=5);raise
            return p
        with log.open('w') as stream:
            proc=start(stream)
            try:
                time.sleep(.5);assert cni.presence_requests==[] and cni.queries==[]
                broker.send(packet(0x31,mqtt_string('cbus/bridge/command')+b'{"inventory":"discover"}'))
                time.sleep(.3);assert cni.presence_requests==[]
                broker.command('cbus/bridge/command','{"inventory":"discover"}')
                wait(lambda:len(cni.presence_requests)==1)
                wait(lambda:inventory().get('presence',{}).get('state')=='complete')
                started=time.monotonic();broker.command('cbus/write/254/56/72/ramp','50')
                wait(lambda:cni.level==128,2)
                out['lighting_during_discovery_seconds']=round(time.monotonic()-started,3)
                wait(lambda:inventory().get('job',{}).get('state')=='complete',25)
                view=inventory();rows={r['unit']:r for r in view['devices']}
                assert view['observed_units']==[]
                assert view['presence']['reported_units']==[3,99]
                assert view['presence']['covered_slots']==256
                assert rows[3]['type']=='DIMMER' and rows[99]['type']=='PC_CNIED'
                assert all(r['outcome']=='identified' for r in rows.values())
                assert len(cni.queries)==6
                assert cni.queries[0][2]-cni.presence_requests[0]>=2.95
                assert all(b[2]-a[2]>=2.95 for a,b in zip(cni.queries,cni.queries[1:]))
                out['quiet_output_discovered_and_identified']='passed'
                out['reordered_duplicate_transport_fragments']='passed'
                out['retained_discovery_ignored']='passed'
                out['shared_three_second_budget']='passed'
            except BaseException:
                print(log.read_text(),file=sys.stderr);raise
            finally:
                proc.terminate();proc.wait(timeout=5)
            # Fresh process avoids artificial time manipulation or a cooldown sleep.
            cni.partial=True;before=len(cni.queries);requests=len(cni.presence_requests)
            proc=start(stream)
            try:
                broker.command('cbus/bridge/command','{"inventory":"discover"}')
                wait(lambda:len(cni.presence_requests)>requests)
                wait(lambda:inventory().get('job',{}).get('state')=='discovery_incomplete',15)
                assert inventory()['presence']['covered_slots']==88
                assert inventory()['job']['requested_units']==[]
                assert len(cni.queries)==before
                broker.command('cbus/write/254/56/72/switch','OFF');wait(lambda:cni.level==0)
                out['partial_presence_stops_without_identity_or_sweep']='passed'
                out['lighting_after_partial_discovery']='passed'
            except BaseException:
                print(log.read_text(),file=sys.stderr);raise
            finally:
                proc.terminate();proc.wait(timeout=5)
        print(json.dumps(out,indent=2))
