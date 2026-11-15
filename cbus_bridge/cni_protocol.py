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


@dataclass(frozen=True)
class DltLabelRequest:
    group: int
    text: str
    language: int = 1
    variant: int = 1  # Toolkit numbering; wire flavours are 0..3.

    @property
    def topic(self):
        return f"cbus/write/254/56/{self.group}/label"


@dataclass(frozen=True)
class MonitoredLabel:
    dev: int
    app: int
    group: int
    text: str
    language: int
    variant: int


def parse_dlt_label_targets(specs):
    """Explicit local Lighting targets, independent of physical HA profiles."""
    targets = set()
    if len(specs) > 16:
        raise ValueError("At most 16 --dlt-label-target entries are supported")
    for spec in specs:
        match = re.fullmatch(r"254/56/(0|[1-9][0-9]{0,2})", spec)
        if not match or int(match[1]) > 254 or int(match[1]) in targets:
            raise ValueError("DLT targets must be unique 254/56/GROUP addresses (0..254)")
        targets.add(int(match[1]))
    return frozenset(targets)


def validate_dlt_label(request):
    if not isinstance(request, DltLabelRequest):
        raise ValueError("Expected a DLT label request")
    if type(request.group) is not int or not 0 <= request.group <= 254:
        raise ValueError("Invalid label group")
    if type(request.language) is not int or request.language not in (1, 2):
        raise ValueError("Text labels currently support English (1) or English Australia (2)")
    if type(request.variant) is not int or not 1 <= request.variant <= 4:
        raise ValueError("Label variant must be Toolkit variant 1..4")
    if not isinstance(request.text, str) or not 1 <= len(request.text) <= 16:
        raise ValueError("Label text must contain 1..16 printable ASCII characters")
    if not request.text.strip() or any(not 32 <= ord(c) <= 126 for c in request.text):
        raise ValueError("Label text must contain 1..16 printable ASCII characters")
    return request


def parse_dlt_label_request(group, payload):
    import json
    def unique_pairs(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("Duplicate label field")
            obj[key] = value
        return obj
    try:
        obj = json.loads(payload, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ValueError("Label payload must be a JSON object") from exc
    if not isinstance(obj, dict) or 'text' not in obj or set(obj) - {'text', 'language', 'variant'}:
        raise ValueError("Label fields are text, language and variant only")
    return validate_dlt_label(DltLabelRequest(group, obj['text'], obj.get('language', 1), obj.get('variant', 1)))


def build_dlt_label_frame(request):
    """CBUS-APP/02 issue1.5 section2.6.5; Class4 local SAL, no ACK tag.

    Text LABEL: 101LLLLL, group, options, language, up to16 ASCII bytes.
    Text options use only bits5..6 for zero-based flavour. Empty text deletes
    labels in the protocol and is deliberately not exposed by this writer.
    """
    validate_dlt_label(request)
    text = request.text.encode('ascii')
    raw = bytes((5, 56, 0, 0xA0 | (3 + len(text)), request.group,
                 (request.variant - 1) << 5, request.language)) + text
    return (raw + bytes((-sum(raw) & 255,))).hex().upper()


def _monitored_commands(hexline):
    b = _checked_frame(hexline)
    if b is None or len(b) < 7 or b[0] != 5:
        return None
    if b[3] == 0:
        start = 4
    elif b[3:5] == b"\x01\x00":
        start = 5
    else:
        return None
    end, commands = len(b) - 1, []
    while start < end:
        cmd = b[start]
        width = (1 + (cmd & 31) if cmd & 0xE0 == 0xA0 else
                 3 if cmd in RAMP_COMMANDS else 2 if cmd in (1, 0x79, 9) else None)
        if width is None or start + width > end:
            return None
        args = b[start + 1:start + width]
        if cmd & 0xE0 == 0xA0:
            # Decode plain text only; icons/preferred-language are unsupported,
            # and can never be interpreted as lighting commands or levels.
            if not 3 <= len(args) <= 19 or args[0] == 255 or args[1] & ~0x60:
                return None
            if any(not 32 <= c <= 126 for c in args[3:]):
                return None
            commands.append(MonitoredLabel(b[1], b[2], args[0], args[3:].decode('ascii'),
                                           args[2], 1 + ((args[1] >> 5) & 3)))
        else:
            commands.append(MonitoredSAL(b[1], b[2], cmd, args[0],
                                        args[1] if len(args) == 2 else None, hexline))
        start += width
    return commands or None


def parse_monitored_sals(hexline: str) -> List[MonitoredSAL]:
    """Atomically validate the packet, then return only lighting actions.

    Valid text LABELs are skipped by their declared length, never interpreted as
    actions. Unsupported/truncated commands reject the packet as before.
    """
    return [c for c in (_monitored_commands(hexline) or []) if isinstance(c, MonitoredSAL)]


def parse_monitored_labels(hexline):
    return [c for c in (_monitored_commands(hexline) or []) if isinstance(c, MonitoredLabel)]


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


@dataclass(frozen=True)
class IdentityReply:
    unit: int
    destination: int
    attribute: int
    value: str


def build_presence_request():
    """SIUG 4.2.9.2: switched status on master application FF, from slot zero.

    This reads unit presence; it is not a lighting ON/ramp command or a change
    to the interface's monitored application configuration.
    """
    raw = bytes((0x05, 0xFF, 0x00, 0x7A, 0xFF, 0x00))
    return (raw + bytes((-sum(raw) & 255,))).hex().upper()


def build_identity_request(unit, attribute):
    """SIUG 4.2.9.1/7.2: local unicast IDENTIFY only; never RESET/STORE."""
    if type(unit) is not int or not 0 <= unit <= 254:
        raise ValueError('Unit must be 0..254; broadcast is forbidden')
    if type(attribute) is not int or attribute not in (0, 1, 2):
        raise ValueError('Only manufacturer/type/firmware IDENTIFY is supported')
    raw = bytes((0x06, unit, 0x00, 0x21, attribute))
    return (raw + bytes((-sum(raw) & 255,))).hex().upper()


def parse_identity_reply(hexline):
    """Local SMART CAL REPLY: 86 source destination 00 89 attr ASCII[8] checksum.

    Routed, short-form, malformed and non-identity replies remain unsupported.
    Wire replies have no transaction identifier; a caller must match an active
    unit/destination/attribute request within its response window.
    """
    b = _checked_frame(hexline)
    if (b is None or len(b) != 15 or b[0] != 0x86 or b[1] == 255 or
            b[2] == 255 or b[3] != 0 or b[4] != 0x89 or b[5] not in (0, 1, 2)):
        return None
    text = b[6:-1].rstrip(b' \x00')
    if not text or any(ch < 32 or ch > 126 for ch in text):
        return None
    return IdentityReply(b[1], b[2], b[5], text.decode('ascii').strip())


def build_device_error_clear_request(unit: int) -> str:
    """Local unicast CAL RESET ($08): clear latched errors, SIUG 1.17 §7.1.

    This is not the serial-interface '~' reset. No broadcast or routed form.
    """
    if type(unit) is not int or not 0 <= unit <= 254:
        raise ValueError('Expected one local unit address 0..254')
    raw = bytes((0x06, unit, 0x00, 0x08))
    return (raw + bytes((-sum(raw) & 255,))).hex().upper()


DEVICE_ERROR_FLAGS = ('power_on_reset', 'internal_stack_overflow', 'communications_tx_error',
                      'micro_reset', 'eeprom_data_error', 'eeprom_checksum_error',
                      'eeprom_write_error', 'installation_mmi_error')


@dataclass(frozen=True)
class DeviceHealthReply:
    unit: int
    destination: int
    attribute: int
    value: dict


def build_device_health_request(unit):
    """SIUG 7.2 IDENTIFY 04: read extended diagnostic summary, never clear it."""
    if type(unit) is not int or not 0 <= unit <= 254:
        raise ValueError('Unit must be 0..254; broadcast is forbidden')
    raw = bytes((0x06, unit, 0x00, 0x21, 0x04))
    return (raw + bytes((-sum(raw) & 255,))).hex().upper()


def parse_device_health_reply(hexline):
    """Strict local long CAL reply: attribute 04, exactly twelve data bytes.

    SIUG 7.2: data offsets 9=voltage ADC, 10=common status, 11=latched
    errors. The first nine bytes are retained raw, not interpreted as identity.
    Flags are observations, not proof of a current fault or a healthy bus.
    """
    b = _checked_frame(hexline)
    if (b is None or len(b) != 19 or b[0] != 0x86 or b[1] == 255 or
            b[2] == 255 or b[3] != 0 or b[4:6] != bytes((0x8D, 4))):
        return None
    data = b[6:-1]
    common, errors = data[10:12]
    value = {'data_hex':data.hex().upper(), 'voltage_raw':data[9],
             'voltage_v':round(data[9] / 6.375, 3),
             'common_status_raw':common, 'latched_errors_raw':errors,
             'output_unit':bool(common & 1), 'checksum_alarm_enabled':bool(common & 2),
             'voltage_marginal':bool(common & 32), 'voltage_low':bool(common & 64),
             'learn_mode':bool(common & 128),
             'latched_errors':[name for bit,name in enumerate(DEVICE_ERROR_FLAGS) if errors & (1 << bit)]}
    return DeviceHealthReply(b[1], b[2], 4, value)


def build_device_voltage_request(unit):
    """SIUG 7.2 IDENTIFY 07: read voltage text only, no parameter changes."""
    if type(unit) is not int or not 0 <= unit <= 254:
        raise ValueError('Unit must be 0..254; broadcast is forbidden')
    raw = bytes((0x06, unit, 0x00, 0x21, 0x07))
    return (raw + bytes((-sum(raw) & 255,))).hex().upper()


def parse_device_voltage_reply(hexline):
    """Local IDENTIFY 07: exactly five ASCII bytes, e.g. 28.7V."""
    b = _checked_frame(hexline)
    if (b is None or len(b) != 12 or b[0] != 0x86 or b[1] == 255 or
            b[2] == 255 or b[3] != 0 or b[4:6] != bytes((0x86, 7)) or
            re.fullmatch(rb'[0-9]{2}\.[0-9]V', b[6:-1]) is None):
        return None
    data = b[6:-1]
    value = {'voltage_v':int(data[:2]) + (data[3] - 48) / 10,
             'voltage_text':data.decode('ascii'), 'voltage_data_hex':data.hex().upper()}
    return DeviceHealthReply(b[1], b[2], 7, value)


@dataclass(frozen=True)
class DeviceHealthEvidence:
    unit: int
    destination: int
    attribute: int
    raw_hex: str


def parse_device_health_evidence(hexline):
    """Bounded, checksum-valid local CAL envelope; never decode unknown data.

    For correlation/debugging only. The caller must match an outstanding read.
    At most the first matching alternative per read is retained by inventory.
    """
    b = _checked_frame(hexline)
    if (b is None or not 7 <= len(b) <= 25 or b[0] != 0x86 or
            b[1] == 255 or b[2] == 255 or b[3] != 0 or
            (b[4] & 0xE0) != 0x80 or (b[4] & 31) != len(b) - 6 or b[5] not in (4,7)):
        return None
    return DeviceHealthEvidence(b[1], b[2], b[5], b.hex().upper())


def diagnose_frame(hexline: str) -> str:
    """Classify supported envelopes without labelling unrelated traffic corrupt."""
    if not hexline or len(hexline) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", hexline):
        return "nonhex"
    b = bytes.fromhex(hexline)
    identity = len(b) >= 6 and b[0] == 0x86 and b[3] == 0 and b[4] == 0x89 and b[5] in (0, 1, 2)
    health = len(b) >= 6 and b[0] == 0x86 and b[3] == 0 and (b[4] & 0xE0) == 0x80 and b[5] in (4,7)
    binary = 0xC0 <= b[0] < 0xE0
    supported = identity or health or binary or b[0] == 0x05 or (b[0] >= 0xE0 and len(b) > 1 and b[1] in (0x07, 0x47))
    if not supported:
        return "unsupported"
    if sum(b) % 256:
        return "checksum_failure"
    if health and b[5] == 7:
        return "device_voltage" if parse_device_voltage_reply(hexline) else "malformed"
    if health:
        return "device_health" if parse_device_health_reply(hexline) else "malformed"
    if identity:
        return "identity" if parse_identity_reply(hexline) else "malformed"
    if binary:
        if len(b) > 1 and b[1] not in (0x38, 0xFF):
            return "unsupported"
        if not parse_binary_status_reply(hexline):
            return "malformed"
        return "presence" if b[1] == 0xFF else "binary_status"
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
    saw_label = False
    while pos < end:
        cmd = b[pos]
        saw_label = saw_label or cmd & 0xE0 == 0xA0
        width = (1 + (cmd & 31) if cmd & 0xE0 == 0xA0 else
                 3 if cmd in RAMP_COMMANDS else 2 if cmd in (1, 0x79, 9) else None)
        if width is None:
            return "unsupported"
        if pos + width > end:
            return "malformed"
        pos += width
    commands = _monitored_commands(hexline)
    if commands:
        return "lighting" if any(isinstance(c, MonitoredSAL) for c in commands) else "label"
    return "unsupported" if saw_label else "malformed"


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
