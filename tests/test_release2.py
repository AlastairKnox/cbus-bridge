from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
import yaml

from cbus_bridge.bridge import CbusMqttBridge
from cbus_bridge.ha_profiles import load_ha_mqtt_profiles
from cbus_bridge import main as app


def light(group=1, dimmer=True):
    root = f'cbus/read/254/56/{group}'
    entity = {'state_topic': root + '/state', 'command_topic': f'cbus/write/254/56/{group}/switch'}
    if dimmer:
        entity.update(brightness_state_topic=root + '/level',
                      brightness_command_topic=f'cbus/write/254/56/{group}/ramp', brightness_scale=100)
    return entity


def load(doc):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'mqtt.yaml'
        path.write_text(yaml.safe_dump(doc))
        return load_ha_mqtt_profiles(str(path))


class ConfigTests(unittest.TestCase):
    def test_current_formats_and_shared_level_state_topic(self):
        e = light()
        e['state_topic'] = e['brightness_state_topic']
        profiles = load({'mqtt': {'light': [e, light(2, False)]}})
        self.assertEqual(len(profiles), 2)
        self.assertEqual(profiles[(254, 56, 1)].pct_scale, 100)

    def test_invalid_scales_fail_instead_of_falling_back(self):
        for scale in (0, -1, 'nonsense', 1.5, True, None):
            with self.subTest(scale=scale):
                e = light(); e['brightness_scale'] = scale
                with self.assertRaisesRegex(ValueError, 'brightness_scale'):
                    load({'light': [e]})

    def test_mismatched_or_out_of_range_addresses_rejected(self):
        for topic in ('cbus/write/254/56/2/switch', 'cbus/write/254/256/1/switch',
                      'cbus/write/256/56/1/switch', 'cbus/write/254/56/256/switch',
                      'cbus/write/254/56/-1/switch', 'cbus/write/254/56/1/switch/extra'):
            with self.subTest(topic=topic):
                e = light(); e['command_topic'] = topic
                with self.assertRaises(ValueError):
                    load({'light': [e]})

    def test_duplicate_groups_rejected(self):
        with self.assertRaisesRegex(ValueError, 'conflicts|duplicates'):
            load({'light': [light(), light()]})

    def test_incomplete_brightness_pair_rejected(self):
        e = light(); del e['brightness_command_topic']
        with self.assertRaisesRegex(ValueError, 'requires both'):
            load({'light': [e]})

    def test_malformed_mqtt_structure_rejected(self):
        for doc in ({'mqtt': []}, {'light': {}}, {'light': ['bad']}):
            with self.subTest(doc=doc):
                with self.assertRaises(ValueError):
                    load(doc)

    def test_unrelated_mqtt_entities_ignored(self):
        profiles = load({'light': [{'state_topic': 'other/state', 'command_topic': 'other/set'}]})
        self.assertEqual(profiles, {})

    def test_non_cbus_command_cannot_grant_access(self):
        e = light(); e['command_topic'] = 'other/set'
        with self.assertRaises(ValueError):
            load({'light': [e]})

    def test_fan_off_conventions_preserved(self):
        for off in (0, 5):
            with self.subTest(off=off):
                e = light(1, False)
                e.update(preset_mode_state_topic='cbus/read/254/56/1/level',
                         preset_mode_command_topic='cbus/write/254/56/1/ramp',
                         preset_mode_value_template=f'{{% if value | int == {off} %}}off{{% endif %}}')
                p = load({'fan': [e]})[(254, 56, 1)]
                self.assertEqual(p.fan_off_value_for_ha, off)
                mc = Mock()
                CbusMqttBridge(Mock(), mc, {}, False).publish_level(p, 0)
                self.assertEqual(mc.publish.call_args_list[0].kwargs['payload'], str(off))


class CommandTests(unittest.TestCase):
    def make_bridge(self, entity=None, readonly=False):
        profiles = load({'light': [entity or light()]})
        cni, mqtt = Mock(), Mock()
        bridge = CbusMqttBridge(cni, mqtt, profiles, readonly, ack_timeout_s=.001)
        def send(frame): bridge._complete_ack(frame[-1], '.')
        cni.send_hex_frame.side_effect = send
        return bridge, cni, mqtt

    def test_allowed_dimmer_switch_and_ramp_still_work(self):
        bridge, cni, mqtt = self.make_bridge()
        for action, payload in [('switch', b'ON'), ('switch', b'OFF'), ('ramp', b'50,34')]:
            bridge.handle_mqtt_command(f'cbus/write/254/56/1/{action}', payload)
        self.assertEqual(cni.send_hex_frame.call_count, 3)
        self.assertTrue(mqtt.publish.called)

    def test_relay_ramp_is_not_implicitly_enabled(self):
        bridge, cni, mqtt = self.make_bridge(light(1, False))
        bridge.handle_mqtt_command('cbus/write/254/56/1/ramp', b'255')
        cni.send_hex_frame.assert_not_called()
        mqtt.publish.assert_not_called()

    def test_binary_sensor_profile_does_not_allow_writes(self):
        profiles = load({'binary_sensor': [{'state_topic': 'cbus/read/254/56/1/state'}]})
        cni, mc = Mock(), Mock()
        bridge = CbusMqttBridge(cni, mc, profiles, False)
        for action in ('switch', 'ramp'):
            bridge.handle_mqtt_command(f'cbus/write/254/56/1/{action}', b'ON')
        cni.send_hex_frame.assert_not_called()
        mc.publish.assert_not_called()

    def test_topic_alias_does_not_bypass_explicit_permission(self):
        bridge, cni, mqtt = self.make_bridge()
        bridge.handle_mqtt_command('cbus/write/0254/56/1/switch', b'ON')
        cni.send_hex_frame.assert_not_called()
        mqtt.publish.assert_not_called()

    def test_readonly_never_sends_or_publishes_command_success(self):
        bridge, cni, mqtt = self.make_bridge(readonly=True)
        for action, payload in [('switch', b'ON'), ('switch', b'OFF'), ('ramp', b'50')]:
            bridge.handle_mqtt_command(f'cbus/write/254/56/1/{action}', payload)
        bridge.send_init(56)
        bridge.request_status_blocks(56, {1, 250}, True)
        bridge.request_resync('configured')
        bridge.request_resync('full')
        cni.send_hex_frame.assert_not_called()
        cni.send_line.assert_not_called()
        mqtt.publish.assert_not_called()
        self.assertEqual(bridge.profiles[(254, 56, 1)].last_nonzero_255, 255)
        bridge.handle_cbus_level_update(56, 1, 128)
        self.assertTrue(mqtt.publish.called)

    def test_readonly_conflicting_cli_flags_fail_before_connections(self):
        with patch.object(app.mqtt, 'Client') as mc, patch.object(app, 'CniConnection') as cni:
            with self.assertRaises(SystemExit) as exc:
                app.main(['--cni-host', 'unused', '--ha-config', 'unused', '--readonly', '--enable-writes'])
            self.assertEqual(exc.exception.code, 2)
            mc.assert_not_called(); cni.assert_not_called()


class StopRun(BaseException): pass


class RetainTests(unittest.TestCase):
    def test_retained_device_and_resync_commands_are_ignored(self):
        client, cni, bridge = Mock(), Mock(), Mock()
        def loop_start():
            for retained in (True, False):
                for topic, payload in [('cbus/write/254/56/1/switch', b'ON'), ('cbus/bridge/command', b'resync')]:
                    client.on_message(client, None, SimpleNamespace(topic=topic, payload=payload, retain=retained))
            raise StopRun()
        client.loop_start.side_effect = loop_start
        cni.failover_status.return_value = {}
        with patch.object(app, 'load_ha_mqtt_profiles', return_value={}), \
             patch.object(app.mqtt, 'Client', return_value=client), \
             patch.object(app, 'CniConnection', return_value=cni), \
             patch.object(app, 'CbusMqttBridge', return_value=bridge):
            with self.assertRaises(StopRun):
                app.main(['--cni-host', 'unused', '--ha-config', 'unused'])
        bridge.handle_mqtt_command.assert_called_once_with('cbus/write/254/56/1/switch', b'ON')
        bridge.request_resync.assert_called_once_with(mode='configured')
