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

@dataclass
class BinaryStatusReply:
    hdr: int
    app: int
    start_group: int
    states: Dict[int, str]  # absent / on / off / error; never brightness
    raw_hex: str


# CBUS-QS sections 4.3, 7; CBUS-SIUG sections 6 and 7.4.
# CBUS-QS 6.3: nominal time for a complete 0..255 change, not a
# guaranteed point-to-point duration. Hardware performs the ramp.
RAMP_SECONDS = (0, 4, 8, 12, 20, 30, 40, 60, 90, 120, 180, 300, 420, 600, 900, 1020)
RAMP_CODES = dict(zip(RAMP_SECONDS, range(0x02, 0x7B, 8)))
RAMP_COMMANDS = frozenset(RAMP_CODES.values())


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


def parse_binary_status_reply(hexline: str) -> Optional[BinaryStatusReply]:
    """CBUS-SIUG 7.3: standard short-form status (EXSTAT off).

    The header counts application, start and packed data; excludes checksum.
    Four groups per byte, least significant bit pair first. Error can mean
    inconsistent outputs OR a unit requesting initialization, not proven noise.
    """
    b = _checked_frame(hexline)
    if b is None or len(b) < 5 or not 0xC0 <= b[0] < 0xE0:
        return None
    declared = b[0] - 0xC0
    if len(b) != declared + 2 or declared < 3:
        return None
    count = (declared - 2) * 4
    if b[2] + count > 256:
        return None
    names = ('absent', 'on', 'off', 'error')
    states = {b[2] + i * 4 + offset: names[(byte >> (offset * 2)) & 3]
              for i, byte in enumerate(b[3:-1]) for offset in range(4)}
    return BinaryStatusReply(b[0], b[1], b[2], states, hexline)


def diagnose_frame(hexline: str) -> str:
    """Classify supported envelopes without labelling unrelated traffic corrupt."""
    if not hexline or len(hexline) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", hexline):
        return "nonhex"
    b = bytes.fromhex(hexline)
    binary = 0xC0 <= b[0] < 0xE0
    supported = binary or b[0] == 0x05 or (b[0] >= 0xE0 and len(b) > 1 and b[1] in (0x07, 0x47))
    if not supported:
        return "unsupported"
    if sum(b) % 256:
        return "checksum_failure"
    if binary:
        if len(b) > 1 and b[1] != 0x38:
            return "unsupported"
        return "binary_status" if parse_binary_status_reply(hexline) else "malformed"
    if len(b) > 2 and b[2] != 0x38:
        return "unsupported"
    if b[0] >= 0xE0:
        return "status" if parse_status_reply(hexline) else "malformed"
    if len(b) < 7:
        return "malformed"
    if b[3] != 0 and b[3:5] != b"\x01\x00":
        return "unsupported"
    # Distinguish unsupported command opcodes from a truncated known command.
    pos, end = (4 if b[3] == 0 else 5), len(b) - 1
    while pos < end:
        cmd = b[pos]
        width = 3 if cmd in RAMP_COMMANDS else 2 if cmd in (1, 0x79, 9) else None
        if width is None:
            return "unsupported"
        if pos + width > end:
            return "malformed"
        pos += width
    return "lighting" if parse_monitored_sals(hexline) else "malformed"


def parse_interface_reply(text, expected_setup=None):
    """Strict parameter acknowledgements; echoed setup requires a pending match.

    Return metadata only. These replies never imply a lighting state or ACK tag.
    """
    value = text.upper()
    echo = None
    if expected_setup:
        for command in expected_setup:
            command = command.upper()
            if value.startswith(command):
                suffix = value[len(command):]
                if suffix.startswith('G'):
                    suffix = suffix[1:]
                candidate = parse_interface_reply(suffix)
                if candidate and candidate['parameter'] == int(command[2:4], 16):
                    return {**candidate, 'echoed_setup': command}
    if not re.fullmatch(r'[0-9A-F]+', value) or len(value) not in (8, 16):
        return None
    data = bytes.fromhex(value)
    if sum(data) % 256:
        return None
    if len(data) == 4 and data[0] == 0x32 and data[2] == 0:
        return {'parameter': data[1], 'unit': None, 'echoed_setup': echo}
    if (len(data) == 8 and data[0] == 0x86 and data[1] == data[2]
            and data[3] == 0 and data[4] == 0x32 and data[6] == 0):
        return {'parameter': data[5], 'unit': data[1], 'echoed_setup': echo}
    return None
