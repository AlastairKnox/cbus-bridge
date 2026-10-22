"""Real bridge process + loopback-only MQTT/CNI peers, simultaneous fade regression."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from integration_release14 import Broker, Cni, wait


def lighting(cni, group, level):
    return any(bytes.fromhex(frame)[4:6] == bytes([group, level]) for frame in cni.commands)


if __name__ == '__main__':
    root=Path(__file__).resolve().parents[1]
    broker, cni = Broker(), Cni()
    with tempfile.TemporaryDirectory() as td:
        config=Path(td)/'mqtt.yaml'
        config.write_text('light:\n'+''.join(f'''- state_topic: cbus/read/254/56/{group}/state
  command_topic: cbus/write/254/56/{group}/switch
  brightness_state_topic: cbus/read/254/56/{group}/level
  brightness_command_topic: cbus/write/254/56/{group}/ramp
  brightness_scale: 100
''' for group in (13,72)))
        args=['--cni-host','127.0.0.1','--cni-port',str(cni.port),'--mqtt-host','127.0.0.1',
              '--mqtt-port',str(broker.port),'--ha-config',str(config),'--diagnostics-every','1',
              '--ramp-readback-every','3','--log-level','INFO']
        log=Path(td)/'child.log'
        with log.open('w') as stream:
            proc=subprocess.Popen([sys.executable,'-B',str(root/'run_bridge.py'),*args],
                                  env=dict(os.environ,PYTHONPATH=str(root)),stdout=stream,stderr=stream)
            try:
                wait(lambda:broker.latest.get('cbus/bridge/availability')=='online')
                started=time.monotonic()
                for group,level in ((13,20),(72,50)):
                    broker.command(f'cbus/write/254/56/{group}/ramp',json.dumps({'level':level,'transition_seconds':20}))
                wait(lambda:lighting(cni,13,51))
                wait(lambda:'MQTT cbus/write/254/56/72/ramp' in log.read_text())
                # A normal zero-time UI operation interrupts the second acquisition;
                # it must execute promptly and the other group's queued fade survives.
                broker.command('cbus/write/254/56/13/ramp','{"level":100,"transition_seconds":0}')
                wait(lambda:lighting(cni,13,255),2)
                wait(lambda:lighting(cni,72,128),12)
                elapsed=time.monotonic()-started
                assert elapsed < 15,elapsed  # commands need not wait for physical 20s fade
                wait(lambda:json.loads(broker.latest['cbus/bridge/status/diagnostics'])['diagnostics']['counters'].get('duration_transitions_transmitted_total',0)>=3)
                diagnostic=json.loads(broker.latest['cbus/bridge/status/diagnostics'])
                assert diagnostic['diagnostics']['counters'].get('command_failures_total',0)==0,diagnostic['diagnostics']['counters']
                assert diagnostic['diagnostics']['counters'].get('duration_deferred_total',0)>=1,diagnostic
                print(json.dumps({'two_group_fade_burst':'passed','zero_time_control_during_acquisition':'passed',
                                  'deferred_fade_resumed':'passed','command_failures':0,
                                  'elapsed_seconds':round(elapsed,3)},indent=2))
            except BaseException:
                print(log.read_text(),file=sys.stderr)
                raise
            finally:
                proc.terminate();proc.wait(timeout=5)
