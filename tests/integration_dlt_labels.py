"""Full bridge process, disposable loopback MQTT/CNI only. No real bus writes."""
import json,os,subprocess,sys,tempfile,threading,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from integration_release14 import Broker,Cni,wait,packet,mqtt_string

class LabelCni(Cni):
    def __init__(self):
        self.labels=[];self.ack_labels=True
        super().__init__()
    def serve(self,s):
        def stream():
            while True:
                try:self.send(self.report(),s)
                except OSError:return
                time.sleep(.05)
        threading.Thread(target=stream,daemon=True).start()
        buf=b''
        try:
            while True:
                chunk=s.recv(4096)
                if not chunk:return
                buf+=chunk
                while b'\n' in buf:
                    line,buf=buf.split(b'\n',1);line=line.strip()
                    if not line.startswith(b'\\'):continue
                    raw=bytes.fromhex(line[1:-1].decode());tag=line[-1:]
                    assert sum(raw)%256==0
                    if raw[3]&0xE0==0xA0:
                        self.labels.append(raw)
                        # Real monitored envelope has source unit inserted.
                        echo=raw[:1]+bytes((2,))+raw[1:-1]
                        self.send((echo+bytes((-sum(echo)&255,))).hex().encode()+b'\r\n',s)
                        if self.ack_labels:self.send(tag+b'.',s)
                    else:
                        self.commands.append(raw)
                        self.level=0 if raw[3]==1 else 255 if raw[3]==0x79 else raw[5]
                        self.send(tag+b'.',s)
        except (OSError,ValueError):pass

if __name__=='__main__':
    root=Path(__file__).resolve().parents[1];broker,cni=Broker(),LabelCni();out={}
    with tempfile.TemporaryDirectory() as td:
        td=Path(td);cfg=td/'mqtt.yaml';cfg.write_text('''light:
- name: Study
  state_topic: cbus/read/254/56/13/state
  command_topic: cbus/write/254/56/13/switch
  brightness_state_topic: cbus/read/254/56/13/level
  brightness_command_topic: cbus/write/254/56/13/ramp
  brightness_scale: 100
''')
        args=['--cni-host','127.0.0.1','--cni-port',str(cni.port),'--mqtt-host','127.0.0.1',
              '--mqtt-port',str(broker.port),'--ha-config',str(cfg),'--dlt-label-target','254/56/91',
              '--diagnostics-every','1','--log-level','WARNING']
        topic='cbus/write/254/56/91/label';result_topic='cbus/read/254/56/91/label_result'
        def result():return json.loads(broker.latest.get(result_topic,'{}'))
        def label(text,**extra):broker.command(topic,json.dumps({'text':text,**extra}))
        log=td/'child.log'
        with log.open('w') as stream:
            proc=subprocess.Popen([sys.executable,'-B',str(root/'run_bridge.py'),*args],
                env=dict(os.environ,PYTHONPATH=str(root)),stdout=stream,stderr=stream)
            try:
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online')
                label('Night time');wait(lambda:result().get('result')=='ack_success')
                assert cni.labels[0][:-1].hex()=='053800ad5b00014e696768742074696d65'
                assert result()['display_confirmed'] is False
                assert not any(t.startswith('cbus/read/254/56/91/') and not t.endswith('label_result') for t in broker.latest)
                out['mqtt_json_to_exact_text_frame_ack_and_monitored_label_isolation']='passed'
                label('Night time');wait(lambda:result().get('result')=='duplicate_skipped')
                assert len(cni.labels)==1
                label('Jazz');wait(lambda:result().get('result')=='rate_limited');assert len(cni.labels)==1
                out['duplicate_and_rate_limit']='passed'
                broker.send(packet(0x31,mqtt_string(topic)+b'{"text":"Retained"}'))
                broker.command('cbus/write/254/56/13/label','{"text":"No"}')
                label('A'*17);time.sleep(.2);assert len(cni.labels)==1
                out['retained_unconfigured_oversized_text_rejected']='passed'
                broker.command('cbus/write/254/56/13/switch','OFF');wait(lambda:bool(cni.commands))
                assert cni.commands[-1][3:5]==bytes((1,13))
                out['ordinary_lighting_write_and_ack_unchanged']='passed'
                time.sleep(1.1);label('Jazz',variant=2);wait(lambda:len(cni.labels)==2)
                wait(lambda:result().get('text')=='Jazz' and result().get('result')=='ack_success')
                assert cni.labels[-1][5]==0x20
                out['toolkit_variant_two_maps_to_wire_flavour_one']='passed'
                previous=len(cni.labels);connections=broker.connects;broker.drop()
                wait(lambda:broker.connects>connections,15)
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online' and
                     broker.latest_generation.get('cbus/bridge/availability')==id(broker.current),15)
                time.sleep(.3);assert len(cni.labels)==previous
                time.sleep(1.1);label('Jazz',variant=2);wait(lambda:len(cni.labels)==previous+1)
                out['reconnect_does_not_replay_labels_but_accepts_fresh_request']='passed'
                time.sleep(1.1);cni.ack_labels=False;connections=cni.connects
                label('No ACK');wait(lambda:result().get('result')=='ack_timeout')
                wait(lambda:cni.connects>connections,15)
                count=len(cni.labels);time.sleep(.3);assert len(cni.labels)==count
                out['missing_ack_discards_session_without_label_retry']='passed'
                assert not any(t.endswith('/event') for t in broker.latest)
            except BaseException:
                print(log.read_text(),file=sys.stderr);raise
            finally:
                proc.terminate();proc.wait(timeout=5)
    print(json.dumps(out,indent=2))
