
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import re
import yaml

# ----------------------------
# HA-derived config model
# ----------------------------

@dataclass
class GroupProfile:
    net: int
    app: int
    group: int

    # Human-friendly metadata (from HA YAML)
    name: Optional[str] = None
    unique_id: Optional[str] = None

    # Topics as defined in HA config (use these verbatim)
    state_topic: str = ""
    command_topic: Optional[str] = None  # .../switch
    level_state_topic: Optional[str] = None  # .../level
    level_command_topic: Optional[str] = None  # .../ramp

    # Behaviour
    level_mode: str = "none"  # "none" | "pct" | "fan"
    pct_scale: int = 100

    # Fan quirk: some templates map "off" to 5 instead of 0
    fan_off_value_for_ha: int = 0  # 0 or 5

    # Track last non-zero level for better ON behaviour
    last_nonzero_255: int = 255


def parse_group_from_topic(topic: str) -> Optional[Tuple[int, int, int]]:
    """
    Expected: cbus/(read|write)/<net>/<app>/<group>/...
    """
    m = re.match(r"^cbus/(?:read|write)/(\d+)/(\d+)/(\d+)/", topic)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def load_ha_mqtt_profiles(path: str) -> Dict[Tuple[int, int, int], GroupProfile]:
    """
    Read HA mqtt: YAML and build a per-group profile that preserves topics.
    Works whether the YAML file is the full config or just the mqtt: block.
    """
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}

    if not isinstance(doc, dict):
        raise ValueError("YAML root is not a mapping")

    mqtt_root = doc.get("mqtt", doc)
    if "mqtt" in doc and isinstance(doc["mqtt"], dict):
        mqtt_root = doc["mqtt"]

    profiles: Dict[Tuple[int, int, int], GroupProfile] = {}

    def upsert_profile(
        net: int,
        app: int,
        group: int,
        state_topic: str,
        *,
        name: Optional[str] = None,
        unique_id: Optional[str] = None,
    ) -> GroupProfile:
        key = (net, app, group)
        if key not in profiles:
            profiles[key] = GroupProfile(
                net=net,
                app=app,
                group=group,
                state_topic=state_topic or "",
                name=name,
                unique_id=unique_id,
            )
        else:
            p = profiles[key]
            if state_topic and p.state_topic != state_topic:
                p.state_topic = state_topic

            # Only populate metadata if missing; do not overwrite an existing value.
            if name and not p.name:
                p.name = name
            if unique_id and not p.unique_id:
                p.unique_id = unique_id

        return profiles[key]

    # ----------------------------
    # Lights
    # ----------------------------
    for light in (mqtt_root.get("light") or []):
        if not isinstance(light, dict):
            continue

        state_topic = light.get("state_topic")
        cmd_topic = light.get("command_topic")
        if not (isinstance(state_topic, str) and isinstance(cmd_topic, str)):
            continue

        ginfo = parse_group_from_topic(state_topic) or parse_group_from_topic(cmd_topic)
        if not ginfo:
            continue
        net, app, group = ginfo

        p = upsert_profile(
            net, app, group, state_topic,
            name=light.get("name") if isinstance(light.get("name"), str) else None,
            unique_id=light.get("unique_id") if isinstance(light.get("unique_id"), str) else None,
        )
        p.command_topic = cmd_topic

        # Brightness
        b_state = light.get("brightness_state_topic")
        b_cmd = light.get("brightness_command_topic")
        if isinstance(b_state, str) and isinstance(b_cmd, str):
            p.level_state_topic = b_state
            p.level_command_topic = b_cmd
            p.level_mode = "pct"
            try:
                p.pct_scale = int(light.get("brightness_scale", 100))
            except Exception:
                p.pct_scale = 100

    # ----------------------------
    # Fans (preset modes)
    # ----------------------------
    for fan in (mqtt_root.get("fan") or []):
        if not isinstance(fan, dict):
            continue

        state_topic = fan.get("state_topic")
        cmd_topic = fan.get("command_topic")
        if not (isinstance(state_topic, str) and isinstance(cmd_topic, str)):
            continue

        ginfo = parse_group_from_topic(state_topic) or parse_group_from_topic(cmd_topic)
        if not ginfo:
            continue
        net, app, group = ginfo

        p = upsert_profile(
            net, app, group, state_topic,
            name=fan.get("name") if isinstance(fan.get("name"), str) else None,
            unique_id=fan.get("unique_id") if isinstance(fan.get("unique_id"), str) else None,
        )
        p.command_topic = cmd_topic

        pm_state = fan.get("preset_mode_state_topic")
        pm_cmd = fan.get("preset_mode_command_topic")
        if isinstance(pm_state, str) and isinstance(pm_cmd, str):
            p.level_state_topic = pm_state
            p.level_command_topic = pm_cmd
            p.level_mode = "fan"

            tmpl = fan.get("preset_mode_value_template") or ""
            if isinstance(tmpl, str) and "== 5" in tmpl and "off" in tmpl:
                p.fan_off_value_for_ha = 5
            else:
                p.fan_off_value_for_ha = 0

    # ----------------------------
    # Binary sensors (read-only for us)
    # ----------------------------
    for bs in (mqtt_root.get("binary_sensor") or []):
        if not isinstance(bs, dict):
            continue

        state_topic = bs.get("state_topic")
        if not isinstance(state_topic, str):
            continue

        ginfo = parse_group_from_topic(state_topic)
        if not ginfo:
            continue
        net, app, group = ginfo

        upsert_profile(
            net, app, group, state_topic,
            name=bs.get("name") if isinstance(bs.get("name"), str) else None,
            unique_id=bs.get("unique_id") if isinstance(bs.get("unique_id"), str) else None,
        )

    return profiles

