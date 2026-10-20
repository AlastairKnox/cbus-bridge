from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


# ----------------------------
# CNI parsing
# ----------------------------

@dataclass
class MonitoredSAL:
    dev: int
    app: int
    cmd: int
    group: int
    level: Optional[int]  # 0..255 if present
    raw_hex: str


@dataclass
class StatusReply:
    hdr: int
    coding: int
    app: int
    start_group: int
    levels: Dict[int, Optional[int]]  # group -> level255 (None if N/A)
    raw_hex: str

def parse_monitored_sal(hexline: str) -> Optional[MonitoredSAL]:
    # Expect at least: 05 ss 38 00 cc gg zz  (7 bytes)
    if len(hexline) < 14:
        return None
    try:
        b = bytes.fromhex(hexline)
    except ValueError:
        return None
    if b[0] != 0x05 or len(b) < 7:
        return None

    dev = b[1]
    app = b[2]

    # Two valid formats per Clipsal docs:
    #   05 ss 38 00 cc gg [tt] zz
    #   05 ss 38 01 00 cc gg [tt] zz
    if b[3] == 0x00:
        cmd = b[4]
        group = b[5]
        level = b[6] if len(b) >= 8 else None
        return MonitoredSAL(dev=dev, app=app, cmd=cmd, group=group, level=level, raw_hex=hexline)

    if b[3] == 0x01 and len(b) >= 8 and b[4] == 0x00:
        cmd = b[5]
        group = b[6]
        level = b[7] if len(b) >= 9 else None
        return MonitoredSAL(dev=dev, app=app, cmd=cmd, group=group, level=level, raw_hex=hexline)

    return None


def decode_status_packed4(packed4: str) -> Optional[int]:
    """
    packed4 is 4 chars (each one of A,9,6,5,0).
    '0' means group doesn't exist -> return None.

    Mapping from your Arduino:
      A -> 0b00, 9 -> 0b01, 6 -> 0b10, 5 -> 0b11
    Shift order: [2,0,6,4]
    """
    if len(packed4) != 4:
        return None
    if "0" in packed4:
        return None
    map2 = {"A": 0, "9": 1, "6": 2, "5": 3}
    shifts = [2, 0, 6, 4]
    result = 0
    for i, ch in enumerate(packed4):
        if ch not in map2:
            return None
        result |= (map2[ch] << shifts[i])
    return result


def parse_status_reply(hexline: str) -> Optional[StatusReply]:
    if len(hexline) < 8:
        return None
    try:
        hdr = int(hexline[0:2], 16)
        coding = int(hexline[2:4], 16)
        app = int(hexline[4:6], 16)
        start_group = int(hexline[6:8], 16)
    except ValueError:
        return None

    if hdr < 0xE0:
        return None
    if coding not in (0x07, 0x47):
        return None

    declared_bytes = hdr - 0xE0
    group_count = (declared_bytes - 3) // 2

    payload = hexline[8:]
    if len(payload) < group_count * 4:
        return None

    levels: Dict[int, Optional[int]] = {}
    for i in range(group_count):
        g = start_group + i
        packed4 = payload[i * 4: (i + 1) * 4]
        levels[g] = decode_status_packed4(packed4)

    return StatusReply(
        hdr=hdr, coding=coding, app=app, start_group=start_group, levels=levels, raw_hex=hexline
    )

