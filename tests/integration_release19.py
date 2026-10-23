"""Real bridge loopback voltage fallback and bounded alternative evidence."""
import json,os,subprocess,sys,tempfile,threading,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from integration_release14 import Broker,Cni,wait,packet,mqtt_string
from integration_release16 import frame
from test_release18 import health_frame
from test_release19 import voltage_frame


class FallbackCni(Cni):
    def __init__(self):
        self.queries=[];self.answer=True
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
                        unit,attr=raw[1],raw[4];assert unit in (20,21,22,23) and attr in (4,7)
                        self.queries.append((unit,attr,time.monotonic()))
                        if self.answer:
                            if unit==20 and attr==4:
                                self.send(health_frame(unit=unit,dest=98).encode()+b'\r\n',s)
                                data=health_frame(unit=unit,common=1,errors=1).encode()+b'\r\n'
                            elif unit==21 and attr==7:
                                self.send(voltage_frame(unit=unit,dest=98).encode()+b'\r\n',s)
                                data=voltage_frame(unit=unit,text=b'27.3V').encode()+b'\r\n'
                            elif unit==22:
                                # Well-formed CAL carrying unknown attribute data.
                                data=voltage_frame(unit=unit,attr=attr,text=b'UNKNOWN').encode()+b'\r\n'
                                for _ in range(20):self.send(data,s)
                            else:continue
                            self.send(data[:7],s);self.send(data[7:],s)
                    elif raw[3]==0x73:self.send(self.report(raw[6]),s)
                    else:
                        self.commands.append(raw.hex())
                        self.level=0 if raw[3]==1 else 255 if raw[3]==0x79 else raw[5]
                        if tag:self.send(tag+b'.',s)
        except (OSError,ValueError):pass


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
                time.sleep(.5);assert cni.queries==[]
                broker.send(packet(0x31,mqtt_string('cbus/bridge/command')+b'{"inventory":{"health":[20]}}'))
                time.sleep(.3);assert cni.queries==[]
                broker.command('cbus/bridge/command','{"inventory":{"health":[20,21,22,23]}}')
                wait(lambda:len(cni.queries)==1)
                started=time.monotonic();broker.command('cbus/write/254/56/72/ramp','50')
                wait(lambda:cni.level==128,2)
                out['lighting_during_health_seconds']=round(time.monotonic()-started,3)
                wait(lambda:inventory().get('job',{}).get('state')=='complete',30)
                view=inventory();rows={r['unit']:r for r in view['devices']}
                assert view['job']['mode']=='health' and view['job']['units_finished']==4
                assert rows[20]['health']['outcome']=='reply'
                assert rows[20]['health']['voltage_v']==32
                assert rows[20]['health']['latched_errors']==['power_on_reset']
                assert rows[21]['health']['outcome']=='voltage_only'
                assert rows[21]['health']['voltage_v']==27.3
                assert rows[21]['health']['voltage_source']=='identify_07'
                assert not rows[21]['health']['diagnostics_available']
                assert 'latched_errors' not in rows[21]['health']
                assert rows[22]['health']['outcome']=='unrecognized_reply'
                assert set(rows[22]['health']['reads'])=={'summary','voltage'}
                assert all('reply_hex' in read for read in rows[22]['health']['reads'].values())
                assert 'voltage_v' not in rows[22]['health']
                assert rows[23]['health']['outcome']=='no_reply'
                assert 'voltage_v' not in rows[23]['health']
                assert len(cni.queries)==7
                assert all(b[2]-a[2]>=2.95 for a,b in zip(cni.queries,cni.queries[1:]))
                assert [(u,a) for u,a,t in cni.queries]==[(20,4),(21,4),(21,7),(22,4),(22,7),(23,4),(23,7)]
                out['voltage_only_no_invented_diagnostics']='passed'
                out['bounded_alternative_evidence_for_two_reads']='passed'
                out['fragmented_replies_and_silent_unit_unknown']='passed'
                out['retained_health_request_ignored']='passed'
                out['shared_three_second_budget_readonly_opcode']='passed'
                diag=json.loads(broker.latest['cbus/bridge/status/diagnostics'])['diagnostics']['counters']
                assert diag.get('inventory_unmatched_replies_total',0)>=2
                assert diag.get('command_failures_total',0)==0
                out['unrelated_destination_replies_ignored']='passed'
            except BaseException:
                print(log.read_text(),file=sys.stderr);raise
            finally:
                proc.terminate();proc.wait(timeout=5)
            # Fresh process: there must be no retained-command execution or cache resumption.
            cni.answer=False;before=len(cni.queries)
            proc=start(stream)
            try:
                assert inventory()['devices']==[]
                broker.command('cbus/bridge/command','{"inventory":{"health":[20,22]}}')
                wait(lambda:len(cni.queries)>before)
                connections=cni.connects;cni.drop();wait(lambda:cni.connects>connections)
                wait(lambda:inventory().get('job',{}).get('state')=='connection_lost')
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online')
                count=len(cni.queries);time.sleep(3.2);assert len(cni.queries)==count
                assert all(r['health']['outcome']=='connection_lost' for r in inventory()['devices'])
                broker.command('cbus/write/254/56/72/switch','OFF');wait(lambda:cni.level==0)
                out['disconnect_cancels_without_replay']='passed'
                out['lighting_after_reconnect']='passed'
            except BaseException:
                print(log.read_text(),file=sys.stderr);raise
            finally:
                proc.terminate();proc.wait(timeout=5)
        print(json.dumps(out,indent=2))
