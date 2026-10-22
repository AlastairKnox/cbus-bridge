"""Disposable loopback MQTT/CNI peers; never contacts production hardware."""
import json,os,socket,subprocess,sys,tempfile,threading,time
from pathlib import Path


def packet(kind,data=b''):
    n=len(data);size=bytearray()
    while True:
        v=n%128;n//=128;size.append(v|(128 if n else 0))
        if not n:break
    return bytes([kind])+size+data

def mqtt_string(s):
    b=s.encode();return len(b).to_bytes(2,'big')+b

def receive(s,n):
    out=b''
    while len(out)<n:
        part=s.recv(n-len(out))
        if not part:raise EOFError()
        out+=part
    return out

class Peer:
    def __init__(self):
        self.listener=socket.socket();self.listener.bind(('127.0.0.1',0));self.listener.listen()
        self.port=self.listener.getsockname()[1];self.current=None;self.send_lock=threading.Lock();self.connects=0
        threading.Thread(target=self.accept,daemon=True).start()
    def accept(self):
        while True:
            try:s,_=self.listener.accept()
            except OSError:return
            self.current=s;self.connects+=1
            threading.Thread(target=self.serve,args=(s,),daemon=True).start()
    def send(self,data,s=None):
        with self.send_lock:(s or self.current).sendall(data)
    def drop(self):
        s=self.current
        if s:
            try:s.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            s.close()

class Broker(Peer):
    def __init__(self):self.latest={};self.latest_generation={};self.ack=True;self.withheld=[];super().__init__()
    def serve(self,s):
        try:
            while True:
                kind=receive(s,1)[0];n=0;mult=1
                while True:
                    byte=receive(s,1)[0];n+=(byte&127)*mult;mult*=128
                    if byte<128:break
                body=receive(s,n)
                if kind>>4==1:self.send(b'\x20\x02\x00\x00',s)
                elif kind>>4==8:self.send(packet(0x90,body[:2]+b'\x00'),s)
                elif kind>>4==12:self.send(b'\xd0\x00',s)
                elif kind>>4==3:
                    length=int.from_bytes(body[:2],'big');topic=body[2:2+length].decode();offset=2+length
                    if (kind>>1)&3:
                        mid=body[offset:offset+2];offset+=2
                        if self.ack:self.send(packet(0x40,mid),s)
                        else:self.withheld.append((s,mid))
                    if self.current is s:
                        self.latest[topic]=body[offset:].decode()
                        self.latest_generation[topic]=id(s)
        except (OSError,EOFError):pass
    def command(self,topic,payload):self.send(packet(0x30,mqtt_string(topic)+payload.encode()))
    def release_acks(self):
        self.ack=True
        for s,mid in self.withheld:
            try:self.send(packet(0x40,mid),s)
            except OSError:pass
        self.withheld.clear()

class Cni(Peer):
    def __init__(self):self.level=255;self.commands=[];super().__init__()
    def report(self,start=64):
        levels=[]
        for group in range(start,start+14):
            value=self.level if group==72 else 0
            packed=['A','9','6','5'];x=(value>>2)&3;y=value&3;z=(value>>6)&3;w=(value>>4)&3
            levels+=bytes.fromhex(''.join(packed[n] for n in (x,y,z,w)))
        data=bytes([0xFF,7,56,start])+bytes(levels)
        return (data+bytes([-sum(data)&255])).hex().upper().encode()+b'\r\n'
    def serve(self,s):
        def stream():
            while True:
                try:self.send(self.report(),s)
                except OSError:return
                time.sleep(.005)
        threading.Thread(target=stream,daemon=True).start()
        buf=b''
        try:
            while True:
                chunk=s.recv(4096)
                if not chunk:break
                buf+=chunk
                while b'\n' in buf:
                    line,buf=buf.split(b'\n',1);line=line.strip()
                    if not line.startswith(b'\\'):continue
                    data=line[1:];tag=None
                    if data[-1:] in [bytes([x]) for x in range(ord('g'),ord('z')+1)]:tag=data[-1:];data=data[:-1]
                    raw=bytes.fromhex(data.decode())
                    if raw[3]==0x73:self.send(self.report(raw[6]),s)
                    else:
                        self.commands.append(raw.hex())
                        self.level=0 if raw[3]==1 else 255 if raw[3]==0x79 else raw[5]
                        if tag:self.send(tag+b'.',s)
        except (OSError,ValueError):pass


def wait(test,timeout=10):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if test():return
        time.sleep(.02)
    raise AssertionError('Timed out waiting for loopback peer condition')

if __name__=='__main__':
    root=Path(__file__).resolve().parents[1];broker=Broker();cni=Cni();out={}
    with tempfile.TemporaryDirectory() as td:
        cfg=Path(td)/'mqtt.yaml';cfg.write_text('''light:
- state_topic: cbus/read/254/56/72/state
  command_topic: cbus/write/254/56/72/switch
  brightness_state_topic: cbus/read/254/56/72/level
  brightness_command_topic: cbus/write/254/56/72/ramp
  brightness_scale: 100
''')
        args=['--cni-host','127.0.0.1','--cni-port',str(cni.port),'--mqtt-host','127.0.0.1','--mqtt-port',str(broker.port),'--ha-config',str(cfg),'--diagnostics-every','1','--log-level','WARNING']
        env=dict(os.environ,PYTHONPATH=str(root))
        log=Path(td)/'child.log'
        with log.open('w') as stream:
            proc=subprocess.Popen([sys.executable,'-c','import sys;from cbus_bridge.main import main; main(sys.argv[1:])',*args],env=env,stdout=stream,stderr=stream)
            try:
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online')
                out['continuous_tcp_traffic_readiness']='passed'
                broker.command('cbus/write/254/56/72/ramp','50');wait(lambda:cni.level==128)
                out['mqtt_to_cni_command_and_ack']='passed'
                broker.ack=False
                wait(lambda:len(broker.withheld)>0)
                time.sleep(2)
                view=json.loads(broker.latest['cbus/bridge/status/diagnostics'])['mqtt_publications']
                assert view['pending']<=16 and view['topics']<=1024
                out['withheld_pubacks']=view
                broker.command('cbus/write/254/56/72/switch','OFF');wait(lambda:cni.level==0)
                broker.release_acks();wait(lambda:broker.latest.get('cbus/read/254/56/72/level')=='0')
                out['latest_off_after_puback_recovery']='passed'
                before=cni.connects;cni.drop();wait(lambda:cni.connects>before)
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online')
                out['cni_disconnect_recovery']='passed'
                before=broker.connects;broker.drop();wait(lambda:broker.connects>before,15)
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online' and broker.latest_generation.get('cbus/bridge/availability')==id(broker.current),15)
                broker.command('cbus/write/254/56/72/ramp','20');wait(lambda:cni.level==51)
                out['broker_disconnect_and_command_recovery']='passed'
            except BaseException:
                print(log.read_text(),file=sys.stderr)
                raise
            finally:
                proc.terminate();proc.wait(timeout=5)
            # Exercise process exit independently of systemd with an injected stall.
            code="import sys;from cbus_bridge import main as a;a.FunctionalHealth.stalled=lambda self:['injected_stall'];a.main(sys.argv[1:])"
            proc=subprocess.Popen([sys.executable,'-c',code,*args],env=env,stdout=stream,stderr=stream)
            try:
                rc=proc.wait(timeout=8);assert rc==1,rc
                out['watchdog_process_exit_code']=rc
            finally:
                if proc.poll() is None:proc.kill();proc.wait()
        print(json.dumps(out,indent=2))
