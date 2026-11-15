#!/usr/bin/env python3
"""Offline additive HA package/card preparation. Never contacts HA or CNI."""
import argparse
import json
from pathlib import Path
import sys
import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cbus_bridge.utils import DeviceMonitor


def artifacts(config):
    config=DeviceMonitor.validate(config)
    devices=[d for d in config['devices'] if d['capability']=='full']
    if not devices:raise ValueError('No configured full-diagnostics responders')
    units=[d['unit'] for d in devices]
    options=[f"{d['unit']} — {d['type']}" for d in devices]
    support='sensor.cbus_device_error_clear_support'
    script='cbus_clear_selected_unit_errors'
    selector='cbus_error_clear_unit'
    script_config={'alias':'C-Bus: request clear of selected unit errors','mode':'single',
        'description':'Manual use only. Clears all latched error bits on one selected unit; check the outcome.',
        'sequence':[
            {'variables':{'unit':"{{ states('input_select.cbus_error_clear_unit').split(' — ')[0] | int(-1) }}"}},
            {'condition':'template','value_template':"{{ (unit | int) in "+str(units)+" }}"},
            {'condition':'state','entity_id':support,'state':'enabled'},
            {'action':'mqtt.publish','data':{'topic':'cbus/bridge/command','qos':0,'retain':False,
                'payload':"{{ {'clear_device_errors': {'unit': unit | int, 'request_id': (now().timestamp() * 1000000) | int, 'confirm': true}} | to_json }}"}}]}
    available=[{'topic':'cbus/bridge/availability'}]
    sensors=[{'name':'Error clearing support','unique_id':'cbus_device_error_clear_support',
        'default_entity_id':support,'state_topic':'cbus/bridge/device_monitor','expire_after':90,
        'value_template':"{{ 'enabled' if value_json.error_clear_enabled | default(false) else 'disabled' }}",
        'availability':available,'entity_category':'diagnostic'}]
    for d in devices:
        u=d['unit'];topic=f'cbus/bridge/devices/{u}/state';prefix=f'cbus_unit_{u}'
        sensors.append({'name':'Last error clear outcome','unique_id':prefix+'_error_clear_outcome',
            'default_entity_id':'sensor.'+prefix+'_error_clear_outcome',
            'device':{'identifiers':[prefix]},'entity_category':'diagnostic',
            'state_topic':topic,'expire_after':90,'availability':available,
            'value_template':"{{ value_json.last_clear.outcome if value_json.last_clear is mapping else 'never_requested' }}"})
        sensors.append({'name':'Last error clear attempt','unique_id':prefix+'_error_clear_attempt',
            'default_entity_id':'sensor.'+prefix+'_error_clear_attempt','device_class':'timestamp',
            'device':{'identifiers':[prefix]},'entity_category':'diagnostic',
            'state_topic':topic,'expire_after':90,'availability_mode':'all',
            'availability':available+[{'topic':topic,'value_template':
                "{{ 'online' if value_json.last_clear is mapping else 'offline' }}"}],
            'value_template':"{{ as_datetime(value_json.last_clear.requested_ts).isoformat() if value_json.last_clear is mapping else none }}"})
    package={'input_select':{selector:{'name':'C-Bus unit to clear','options':options,'icon':'mdi:chip'}},
             'script':{script:script_config},'mqtt':{'sensor':sensors},
             'recorder':{'exclude':{'entities':[support,'script.'+script,'input_select.'+selector],
                                    'entity_globs':['sensor.cbus_unit_*_error_clear_attempt']}}}
    table=['| Unit | Type | Last clear outcome |','| --- | --- | --- |']
    for d in devices:
        table.append(f"| {d['unit']} | {d['type']} | {{{{ states('sensor.cbus_unit_{d['unit']}_error_clear_outcome') | replace('_',' ') }}}} |")
    card={'type':'vertical-stack','cards':[
        {'type':'markdown','content':'### Clear latched device errors\nSelect one unit, then confirm. This clears **all** its latched error flags. The bridge saves the old flags and verifies the result. A failed or uncertain attempt is never retried automatically.'},
        {'type':'entities','entities':[support,'input_select.'+selector]},
        {'type':'button','name':'Request clear for selected unit','icon':'mdi:notification-clear-all','icon_height':'32px',
         'tap_action':{'action':'perform-action','perform_action':'script.'+script,
                       'confirmation':{'text':'Clear all latched error flags on the selected C-Bus unit? The saved audit will retain the previous flags.'}},
         'hold_action':{'action':'none'}},
        {'type':'markdown','content':'\n'.join(table)}]}
    return package,card


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('roster');parser.add_argument('output')
    args=parser.parse_args()
    package,card=artifacts(json.loads(Path(args.roster).read_text()))
    out=Path(args.output);out.mkdir(exist_ok=False)
    (out/'cbus_device_error_clear.yaml').write_text(yaml.safe_dump(package,sort_keys=False,allow_unicode=True))
    (out/'error-clear-card.yaml').write_text(yaml.safe_dump(card,sort_keys=False,allow_unicode=True))
    print('Prepared additive HA package and card; no live files changed.')


if __name__=='__main__':main()
