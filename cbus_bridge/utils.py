from __future__ import annotations

import logging
import re
from typing import Optional

LOG = logging.getLogger("cbus_bridge.utils")

HEX_RE = re.compile(r"[0-9A-Fa-f]")

# ----------------------------
# Utilities
# ----------------------------

def normalize_hex_from_bytes(raw: bytes) -> str:
    """
    Keep only hex characters; uppercase result; enforce even nibble count.

    Why enforce even nibble count:
      Some CNI/PCI streams can include non-hex terminators or markers (e.g. 'cn').
      Because 'c' is a hex character but 'n' is not, naive filtering can leave an
      odd number of hex characters, which breaks bytes.fromhex() and causes drops
      of otherwise valid frames.

      The Arduino parser naturally reads in *pairs* (bytes), so dropping a trailing
      nibble matches Arduino behaviour better than rejecting the whole line.
    """
    chars: list[str] = []
    for b in raw:
        c = chr(b)
        if HEX_RE.match(c):
            chars.append(c.upper())

    # Enforce byte alignment (2 hex chars per byte).
    if len(chars) % 2 == 1:
        LOG.debug(
            "normalize_hex_from_bytes: dropping trailing nibble %r (raw=%r)",
            chars[-1],
            raw,
        )
        chars.pop()

    return "".join(chars)


def checksum_2s_complement(frame_bytes: bytes) -> int:
    """C-Bus checksum used in your status request examples: (-sum(bytes)) mod 256."""
    return (-sum(frame_bytes)) & 0xFF


def clamp(n: int, lo: int, hi: int) -> int:
    return lo if n < lo else hi if n > hi else n


def level255_to_pct(level255: int, scale: int = 100) -> int:
    return clamp(int(round((level255 / 255.0) * scale)), 0, scale)


def pct_to_level255(pct: int, scale: int = 100) -> int:
    pct = clamp(pct, 0, scale)
    return clamp(int(round((pct / scale) * 255.0)), 0, 255)


def quantize_fan_level(level255: int) -> int:
    """Map arbitrary level to nearest of (0,10,128,255)."""
    if level255 <= 0:
        return 0
    choices = [10, 128, 255]
    return min(choices, key=lambda x: abs(x - level255))


def parse_first_int(payload_text: str) -> Optional[int]:
    """
    HA (or other clients) sometimes send ramp payloads like "255,34".
    We take the first integer token.
    """
    s = payload_text.strip()
    if not s:
        return None
    first = s.split(",", 1)[0].strip()
    if not first:
        return None
    try:
        return int(first)
    except ValueError:
        return None


