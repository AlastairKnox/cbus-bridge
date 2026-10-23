"""Real bridge + disposable loopback peers: identity survey and lighting priority."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from integration_release14 import Broker,Cni,wait,packet,mqtt_string


def frame(raw):return (raw+bytes([-sum(raw)&255])).hex().upper().encode()+b'\r\n'


class IdentityCni(Cni):
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
                    raw=bytes.fromhex(data.decode())
                    assert sum(raw)%256==0,raw
                    if raw[0]==6:
                        assert len(raw)==6 and raw[2:4]==b'\x00\x21'
                        unit,attr=raw[1],raw[4];assert unit<255 and attr in (0,1,2)
                        self.queries.append((unit,attr,time.monotonic()))
                        if self.answer and unit==3:
                            text={0:b'CLIPSAL ',1:b'DIMMER  ',2:b'1.2.43  '}[attr]
                            # Another controller's reply must not satisfy our request.
                            self.send(frame(bytes([0x86,unit,98,0,0x89,attr])+text),s)
                            self.send(frame(bytes([0x86,unit,99,0,0x89,attr])+text),s)
                    elif raw[3]==0x73:self.send(self.report(raw[6]),s)
                    else:
                        self.commands.append(raw.hex())
                        self.level=0 if raw[3]==1 else 255 if raw[3]==0x79 else raw[5]
                        if tag:self.send(tag+b'.',s)
        except (OSError,ValueError):pass


if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    cni,broker=IdentityCni(),Broker();out={}
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
        log=Path(td)/'child.log'
        def inventory():return json.loads(broker.latest.get('cbus/bridge/inventory','{}'))
        with log.open('w') as stream:
            proc=subprocess.Popen([sys.executable,'-B',str(root/'run_bridge.py'),*args],
                                  env=dict(os.environ,PYTHONPATH=str(root)),stdout=stream,stderr=stream)
            try:
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online')
                wait(lambda:inventory().get('interface_unit')==99)
                time.sleep(1)
                assert cni.queries==[]
                out['no_inventory_queries_after_startup']='passed'
                broker.send(packet(0x31,mqtt_string('cbus/bridge/command')+b'{"inventory":{"units":[3]}}'))
                time.sleep(.3);assert cni.queries==[]
                out['retained_inventory_command_ignored']='passed'
                broker.command('cbus/bridge/command','{"inventory":{"units":[255]}}')
                wait(lambda:inventory().get('last_request_error') is not None)
                assert cni.queries==[]
                broker.command('cbus/bridge/command','{"inventory":{"units":[3,4]}}')
                wait(lambda:len(cni.queries)>0)
                started=time.monotonic()
                broker.command('cbus/write/254/56/72/ramp','50')
                wait(lambda:cni.level==128,2)
                out['lighting_command_during_survey_seconds']=round(time.monotonic()-started,3)
                wait(lambda:inventory().get('job',{}).get('state')=='complete',20)
                rows={r['unit']:r for r in inventory()['devices']}
                assert rows[3]['outcome']=='identified' and rows[3]['manufacturer']=='CLIPSAL'
                assert rows[4]['outcome']=='no_reply'
                assert [(u,a) for u,a,t in cni.queries]==[(3,1),(3,0),(3,2),(4,1)]
                assert all(b[2]-a[2]>=2.95 for a,b in zip(cni.queries,cni.queries[1:]))
                out['identity_and_missing_unit_results']='passed'
                out['three_second_wire_spacing']='passed'
                diag=json.loads(broker.latest['cbus/bridge/status/diagnostics'])['diagnostics']['counters']
                assert diag.get('inventory_unmatched_replies_total',0)>=3
                assert diag.get('command_failures_total',0)==0,diag
                out['unrelated_destination_replies_ignored']='passed'
                # A running survey is cancelled on socket loss and never resumed.
                time.sleep(30.2)
                cni.answer=False
                broker.command('cbus/bridge/command','{"inventory":{"units":[3,4]}}')
                count=len(cni.queries);wait(lambda:len(cni.queries)>count)
                before=cni.connects;cni.drop();wait(lambda:cni.connects>before)
                wait(lambda:inventory().get('job',{}).get('state')=='connection_lost')
                queries_after_loss=len(cni.queries);time.sleep(4)
                assert len(cni.queries)==queries_after_loss
                out['connection_loss_cancels_without_replay']='passed'
                broker.command('cbus/write/254/56/72/switch','OFF');wait(lambda:cni.level==0)
                out['lighting_after_reconnect']='passed'
                print(json.dumps(out,indent=2))
            except BaseException:
                print(log.read_text(),file=sys.stderr);raise
            finally:
                proc.terminate();proc.wait(timeout=5)
