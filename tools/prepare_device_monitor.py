#!/usr/bin/env python3
"""Generate reviewed monitor roster and additive HA files from a completed survey.

Offline only; no credentials, network calls or live file writes. Outputs go into
an explicitly selected new directory so installation remains a separate step.
"""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import yaml
from cbus_bridge.utils import DeviceMonitor


def roster(survey):
    job=survey.get('job',{})
    if job.get('state') != 'complete' or job.get('mode') != 'health':
        raise ValueError('Expected a complete health survey')
    selected=set(job.get('requested_units',[]));rows=[]
    for row in survey.get('devices',[]):
        if row['unit'] not in selected: continue
        h=row.get('health',{})
        if h.get('scan_id') != job['id'] or h.get('outcome') not in ('reply','voltage_only','no_reply','unrecognized_reply'):
            raise ValueError('Missing/incomplete health results in selected survey')
        rows.append({'unit':row['unit'],'type':row.get('type','unknown'),
                     'manufacturer':row.get('manufacturer','unknown'),'firmware':row.get('firmware','unknown'),
                     'capability':{'reply':'full','voltage_only':'voltage'}.get(h['outcome'],'unavailable'),
                     'baseline_errors':h['latched_errors_raw'] if h['outcome']=='reply' else 0})
    if len(rows) != len(selected): raise ValueError('Missing or duplicate selected device rows')
    return DeviceMonitor.validate({'schema':1,'refresh_seconds':900,
        'stale_seconds':max(2700,62*sum(d['capability']!='unavailable' for d in rows)), 'devices':rows})


def artifacts(config):
    config=DeviceMonitor.validate(config)
    sensors=[];binary=[]
    device_base={'identifiers':['cbus_device_monitor'],'name':'C-Bus Device Monitor'}
    button={'name':'Refresh device health','unique_id':'cbus_device_health_refresh',
            'default_entity_id':'button.cbus_device_health_refresh','device':device_base,
            'command_topic':'cbus/bridge/command','payload_press':'{"device_monitor":"refresh"}',
            'qos':0,'retain':False,'entity_category':'diagnostic',
            'availability_mode':'all','availability':[{'topic':'cbus/bridge/availability'},
                {'topic':'cbus/bridge/device_monitor','value_template':"{{ 'online' if value_json.enabled else 'offline' }}"}]}
    sensors.append({'name':'Fresh device readings','unique_id':'cbus_devices_fresh',
        'default_entity_id':'sensor.cbus_devices_fresh','device':device_base,'entity_category':'diagnostic',
        'state_topic':'cbus/bridge/device_monitor','value_template':'{{ value_json.fresh }}',
        'expire_after':90,'availability_topic':'cbus/bridge/availability'})
    rows=[];voltages=[];findings=[];observations=[]
    for d in config['devices']:
        u=d['unit'];prefix=f'cbus_unit_{u}';topic=f'cbus/bridge/devices/{u}/state'
        device={'identifiers':[prefix],'name':f"C-Bus unit {u} ({d['type']})",'manufacturer':d['manufacturer'],
                'model':d['type'],'sw_version':d['firmware'],'via_device':'cbus_device_monitor'}
        base={'state_topic':topic,'expire_after':90,'entity_category':'diagnostic','device':device,
              'availability':[{'topic':'cbus/bridge/availability'}],'availability_mode':'all'}
        def entity(key,name,template,condition=None,domain='sensor',**extra):
            e={**base,'name':name,'unique_id':f'{prefix}_{key}',
               'default_entity_id':f'{domain}.{prefix}_{key}','value_template':template,**extra}
            e['availability']=list(base['availability'])
            if condition:
                e['availability'].append({'topic':topic,'value_template':
                    "{{ 'online' if "+condition+" else 'offline' }}"})
            (binary if domain=='binary_sensor' else sensors).append(e)
        entity('health','Health','{{ value_json.status }}')
        rows.append(f"| {u} | {d['type']} | {d['firmware']} | {{{{ states('sensor.{prefix}_health') | replace('_',' ') }}}} |")
        if d['capability']=='unavailable': continue
        entity('voltage','Voltage','{{ value_json.voltage_v | default(none) }}',
               "value_json.status == 'fresh'",device_class='voltage',unit_of_measurement='V',state_class='measurement',suggested_display_precision=1)
        entity('last_observed','Last observed',"{{ value_json.observed_ts | timestamp_utc if value_json.observed_ts is number else none }}",
               'value_json.observed_ts is number',device_class='timestamp')
        voltages.append({'entity':f'sensor.{prefix}_voltage','name':f"Unit {u} · {d['type']}"})
        observations.append({'entity':f'sensor.{prefix}_last_observed','name':f'Unit {u}'})
        if d['capability'] != 'full': continue
        condition="value_json.status == 'fresh' and value_json.diagnostics_available"
        entity('latched_errors','Latched errors',"{{ value_json.latched_errors | default([]) | join(', ') or 'none' }}",condition)
        entity('new_errors','New flags since baseline',"{{ value_json.new_errors | default([]) | join(', ') or 'none' }}",condition)
        for key,name in [('voltage_low','Low voltage'),('voltage_marginal','Marginal voltage'),('learn_mode','Learn mode')]:
            entity(key,name,"{{ 'ON' if value_json.get('"+key+"', false) else 'OFF' }}",condition,domain='binary_sensor')
        findings.append({'type':'entities','title':f"Unit {u} · {d['type']}",'show_header_toggle':False,'entities':[
            {'entity':f'sensor.{prefix}_health','name':'Reading status'},
            {'entity':f'sensor.{prefix}_latched_errors','name':'Historical latched flags'},
            {'entity':f'sensor.{prefix}_new_errors','name':'New flags since baseline'},
            *[{'entity':f'binary_sensor.{prefix}_{k}','name':n} for k,n in [('voltage_low','Low voltage'),('voltage_marginal','Marginal voltage'),('learn_mode','Learn mode')]]]})
    package={'mqtt':{'sensor':sensors,'binary_sensor':binary,'button':[button]},
             'recorder':{'exclude':{'entity_globs':['sensor.cbus_unit_*_last_observed'],
                                    'entities':['button.cbus_device_health_refresh','sensor.cbus_devices_fresh']}}}
    cards=[{'type':'markdown','title':'Device health','content':
        'Read-only observations, not a certification of the bus. Reads are spread across 15 minutes and yield to lighting/fades. '
        'Diagnostics unavailable describes the survey result; it does not mean offline. '
        'Latched flags can be historical. New bits are compared with the saved commissioning baseline; nothing is cleared automatically. '
        'Refresh queues one paced cycle of responding devices.'},
        {'type':'entities','show_header_toggle':False,'entities':[
            {'entity':'button.cbus_device_health_refresh','name':'Refresh responding devices'},
            {'entity':'sensor.cbus_devices_fresh','name':'Fresh device readings'}]},
        {'type':'markdown','title':'Inventory · address / type / firmware',
         'content':'| Unit | Type | Firmware | Reading status |\n|---:|---|---|---|\n'+'\n'.join(rows)},
        {'type':'entities','title':'Bus voltage','show_header_toggle':False,'entities':voltages},
        {'type':'history-graph','title':'Voltage history','hours_to_show':24,'entities':voltages},
        {'type':'entities','title':'Last successful observations','show_header_toggle':False,'entities':observations},
        *findings]
    return package,{'title':'Devices','path':'devices','icon':'mdi:chip','cards':cards}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('survey');ap.add_argument('output');ap.add_argument('--dashboard')
    args=ap.parse_args();config=roster(json.loads(Path(args.survey).read_text()))
    package,view=artifacts(config);out=Path(args.output);out.mkdir(exist_ok=False)
    (out/'device_monitor.json').write_text(json.dumps(config,indent=2)+'\n')
    (out/'cbus_device_health.yaml').write_text(yaml.safe_dump(package,sort_keys=False,allow_unicode=True))
    (out/'devices-view.yaml').write_text(yaml.safe_dump(view,sort_keys=False,allow_unicode=True))
    if args.dashboard:
        original=Path(args.dashboard).read_text();dashboard=yaml.safe_load(original)
        if any(v.get('path')=='devices' for v in dashboard['views']): raise ValueError('Devices view already exists')
        (out/'cbus_observatory.before.yaml').write_text(original)
        # Append this view without rewriting any existing dashboard cards/comments.
        (out/'cbus_observatory.yaml').write_text(original.rstrip()+'\n'+yaml.safe_dump([view],sort_keys=False,allow_unicode=True))
    print(f"Prepared {len(config['devices'])} devices; {sum(d['capability']!='unavailable' for d in config['devices'])} polled")


if __name__=='__main__':main()
