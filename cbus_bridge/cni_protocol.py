from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, List
import re


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

# CBUS-QS sections 4.3, 7; CBUS-SIUG sections 6 and 7.4.
RAMP_COMMANDS = frozenset(range(0x02, 0x7B, 8))


def _checked_frame(hexline: str) -> Optional[bytes]:
    if not hexline or len(hexline) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", hexline):
        return None
    frame = bytes.fromhex(hexline)
    return frame if sum(frame) % 256 == 0 else None


def parse_monitored_sals(hexline: str) -> List[MonitoredSAL]:
    """Decode all supported commands atomically from a local SMART-mode packet.

    Unknown/truncated commands reject the whole packet; bytes must never be guessed
    to be levels. TERMINATE RAMP has no level and is consumed without inventing one.
    Routed traffic is deliberately unsupported until routing is implemented.
    """
    b = _checked_frame(hexline)
    if b is None or len(b) < 7 or b[0] != 0x05:
        return []
    if b[3] == 0x00:
        start = 4
    elif b[3:5] == b"\x01\x00":
        start = 5
    else:
        return []
    end = len(b) - 1  # The checksum is not SAL payload.
    commands = []
    while start < end:
        cmd = b[start]
        width = 3 if cmd in RAMP_COMMANDS else 2 if cmd in (0x01, 0x79, 0x09) else None
        if width is None or start + width > end:
            return []
        commands.append(MonitoredSAL(
            dev=b[1], app=b[2], cmd=cmd, group=b[start + 1],
            level=b[start + 2] if width == 3 else None, raw_hex=hexline,
        ))
        start += width
    return commands


def parse_monitored_sal(hexline: str) -> Optional[MonitoredSAL]:
    """Compatibility helper for callers expecting exactly one command."""
    commands = parse_monitored_sals(hexline)
    return commands[0] if len(commands) == 1 else None


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
    """Short-form extended LEVEL status; header count excludes outer checksum."""
    b = _checked_frame(hexline)
    if b is None or len(b) < 7 or b[0] < 0xE0 or b[1] not in (0x07, 0x47):
        return None
    declared = b[0] - 0xE0
    payload_bytes = declared - 3  # coding, application and block start
    if payload_bytes <= 0 or payload_bytes % 2 or len(b) != declared + 2:
        return None
    count = payload_bytes // 2
    if b[3] + count > 256:
        return None
    levels = {}
    for i in range(count):
        packed = b[4 + 2 * i:6 + 2 * i].hex().upper()
        # Invalid/absent group encodings remain unknown, never fabricated OFF.
        levels[b[3] + i] = decode_status_packed4(packed)
    return StatusReply(hdr=b[0], coding=b[1], app=b[2], start_group=b[3],
                       levels=levels, raw_hex=hexline)
