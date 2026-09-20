"""
CRSF (Crossfire) protocol helpers.

Constants and layouts verified against the ExpressLRS firmware source
(src/include/crsf_protocol.h, src/lib/Handset/CRSFHandset.cpp).

Frame layout:
    [sync/addr] [len] [type] [payload ...] [crc8]
  where len = number of bytes after the len field (type + payload + crc)
  and crc8 is computed over [type] + [payload] with poly 0xD5, init 0x00.

An RC channels frame is therefore 26 bytes total:
    C8 18 16 <22 bytes of 16 x 11-bit channels, LSB first> <crc>
"""

from __future__ import annotations

# ---------------------------------------------------------------- addresses
CRSF_SYNC_BYTE = 0xC8  # == CRSF_ADDRESS_FLIGHT_CONTROLLER
ADDRESS_FLIGHT_CONTROLLER = 0xC8
ADDRESS_RADIO_TRANSMITTER = 0xEA
ADDRESS_CRSF_RECEIVER = 0xEC
ADDRESS_CRSF_TRANSMITTER = 0xEE

# ExpressLRS accepts either 0xEE or 0xC8 as the leading byte of a frame
# coming from the handset (CRSFHandset.cpp: "inBuffer[i] == CRSF_ADDRESS_
# CRSF_TRANSMITTER || inBuffer[i] == CRSF_SYNC_BYTE").
VALID_TX_SYNC_BYTES = (CRSF_SYNC_BYTE, ADDRESS_CRSF_TRANSMITTER)

# ------------------------------------------------------------- frame types
FRAMETYPE_GPS = 0x02
FRAMETYPE_VARIO = 0x07
FRAMETYPE_BATTERY_SENSOR = 0x08
FRAMETYPE_BARO_ALTITUDE = 0x09
FRAMETYPE_LINK_STATISTICS = 0x14
FRAMETYPE_RC_CHANNELS_PACKED = 0x16
FRAMETYPE_ATTITUDE = 0x1E
FRAMETYPE_FLIGHT_MODE = 0x21
FRAMETYPE_DEVICE_INFO = 0x29
FRAMETYPE_PARAMETER_SETTINGS_ENTRY = 0x2B
FRAMETYPE_ELRS_STATUS = 0x2E

# ---------------------------------------------------------- channel scaling
# 172 = 988us (-100%), 992 = 1500us (centre), 1811 = 2012us (+100%)
CHANNEL_MIN = 172
CHANNEL_MID = 992
CHANNEL_MAX = 1811
CHANNEL_EXT_MIN = 0  # only reachable with E.Limits enabled on the RX
CHANNEL_EXT_MAX = 1984

NUM_CHANNELS = 16
RC_FRAME_LEN = 26

# Baud rates ExpressLRS will auto-detect on its handset UART.
SUPPORTED_BAUDS = (400000, 921600, 1870000, 2250000, 3750000, 5250000, 115200)

# Max CRSF frame rate ELRS allows per baud (CRSFHandset::UARTwdt logic).
MAX_RATE_FOR_BAUD = {115200: 250, 400000: 500}

_CRC_POLY = 0xD5


def _build_crc_table(poly: int) -> bytes:
    table = bytearray(256)
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
        table[i] = crc
    return bytes(table)


_CRC8_TABLE = _build_crc_table(_CRC_POLY)


def crc8(data: bytes, crc: int = 0) -> int:
    """CRSF CRC-8, poly 0xD5, init 0x00, MSB first, no reflection."""
    for b in data:
        crc = _CRC8_TABLE[crc ^ b]
    return crc


def clamp_channel(value: int) -> int:
    return CHANNEL_EXT_MIN if value < CHANNEL_EXT_MIN else (
        CHANNEL_EXT_MAX if value > CHANNEL_EXT_MAX else int(value))


def pack_rc_channels(channels, sync: int = CRSF_SYNC_BYTE) -> bytes:
    """Build a complete RC_CHANNELS_PACKED frame from 16 channel values."""
    if len(channels) != NUM_CHANNELS:
        raise ValueError(f"expected {NUM_CHANNELS} channels, got {len(channels)}")

    bits = 0
    nbits = 0
    payload = bytearray()
    for value in channels:
        bits |= (clamp_channel(value) & 0x7FF) << nbits
        nbits += 11
        while nbits >= 8:
            payload.append(bits & 0xFF)
            bits >>= 8
            nbits -= 8
    if nbits:  # never happens for 16ch (176 bits = 22 bytes exactly)
        payload.append(bits & 0xFF)

    frame = bytearray(2)
    frame[0] = sync
    frame[1] = len(payload) + 2  # type + payload + crc
    frame.append(FRAMETYPE_RC_CHANNELS_PACKED)
    frame += payload
    frame.append(crc8(frame[2:]))
    return bytes(frame)


def unpack_rc_channels(payload: bytes):
    """Inverse of pack_rc_channels' payload section (22 bytes -> 16 values)."""
    bits = int.from_bytes(payload[:22], "little")
    return [(bits >> (11 * i)) & 0x7FF for i in range(NUM_CHANNELS)]


# ------------------------------------------------------------- conversions
def crsf_to_us(value: int) -> float:
    """Convert a CRSF channel value to the servo pulse width it represents."""
    return 988.0 + (value - CHANNEL_MIN) * (2012.0 - 988.0) / (CHANNEL_MAX - CHANNEL_MIN)


def us_to_crsf(us: float) -> int:
    return round(CHANNEL_MIN + (us - 988.0) * (CHANNEL_MAX - CHANNEL_MIN) / (2012.0 - 988.0))


def norm_to_crsf(value: float) -> int:
    """-1.0 .. +1.0  ->  172 .. 1811 (linear, no expo, no mixing)."""
    value = -1.0 if value < -1.0 else (1.0 if value > 1.0 else value)
    out = round(CHANNEL_MID + value * (CHANNEL_MAX - CHANNEL_MIN) / 2.0)
    # the span is one unit asymmetric around 992, so pin the endpoints
    return CHANNEL_MIN if out < CHANNEL_MIN else (CHANNEL_MAX if out > CHANNEL_MAX else out)


def unit_to_crsf(value: float) -> int:
    """0.0 .. 1.0  ->  172 .. 1811."""
    value = 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)
    return round(CHANNEL_MIN + value * (CHANNEL_MAX - CHANNEL_MIN))


# ------------------------------------------------------------- telemetry
class Parser:
    """Incremental parser for frames coming back from the TX module."""

    # Telemetry relayed by an ELRS TX arrives addressed to the handset (0xEA)
    # or with the FC sync byte (0xC8); Lua/parameter traffic uses 0xEE/0xEC.
    _ACCEPTED = (0xC8, 0xEA, 0xEC, 0xEE, 0xEF)
    _MIN_LEN = 2
    _MAX_LEN = 62

    def __init__(self):
        self._buf = bytearray()
        self.crc_errors = 0

    def feed(self, data: bytes):
        """Feed received bytes, yield (address, frametype, payload) tuples."""
        out = []
        self._buf += data
        buf = self._buf
        i = 0
        n = len(buf)
        while True:
            # resync to a plausible address byte
            while i < n and buf[i] not in self._ACCEPTED:
                i += 1
            if i + 1 >= n:
                break
            length = buf[i + 1]
            if length < self._MIN_LEN or length > self._MAX_LEN:
                i += 1
                continue
            end = i + 2 + length
            if end > n:
                break  # wait for more bytes
            body = buf[i + 2:end - 1]          # type + payload
            if crc8(body) == buf[end - 1]:
                out.append((buf[i], body[0], bytes(body[1:])))
                i = end
            else:
                self.crc_errors += 1
                i += 1
        del self._buf[:i]
        if len(self._buf) > 512:               # never let garbage accumulate
            del self._buf[:-64]
        return out


def parse_link_statistics(payload: bytes):
    """CRSF_FRAMETYPE_LINK_STATISTICS (0x14) -> dict."""
    if len(payload) < 10:
        return None
    up_rssi1, up_rssi2, up_lq, up_snr, ant, rf_mode, tx_power, \
        dn_rssi, dn_lq, dn_snr = payload[:10]
    return {
        "up_rssi_1": -up_rssi1,
        "up_rssi_2": -up_rssi2,
        "up_lq": up_lq,
        "up_snr": up_snr - 256 if up_snr > 127 else up_snr,
        "antenna": ant,
        "rf_mode": rf_mode,
        "tx_power_mw": _TX_POWER_TABLE.get(tx_power, None),
        "dn_rssi": -dn_rssi,
        "dn_lq": dn_lq,
        "dn_snr": dn_snr - 256 if dn_snr > 127 else dn_snr,
    }


_TX_POWER_TABLE = {0: 0, 1: 10, 2: 25, 3: 100, 4: 500, 5: 1000, 6: 2000, 7: 250, 8: 50}


def parse_battery(payload: bytes):
    """CRSF_FRAMETYPE_BATTERY_SENSOR (0x08) -> dict. All big-endian."""
    if len(payload) < 8:
        return None
    voltage = int.from_bytes(payload[0:2], "big") / 10.0      # 0.1V
    current = int.from_bytes(payload[2:4], "big") / 10.0      # 0.1A
    capacity = int.from_bytes(payload[4:7], "big")            # mAh used
    remaining = payload[7]                                    # %
    return {"voltage": voltage, "current": current,
            "capacity_used": capacity, "remaining": remaining}


def parse_attitude(payload: bytes):
    """CRSF_FRAMETYPE_ATTITUDE (0x1E) -> dict of degrees."""
    if len(payload) < 6:
        return None

    def rad(idx):
        raw = int.from_bytes(payload[idx:idx + 2], "big", signed=True)
        return raw / 10000.0 * 57.29577951308232

    return {"pitch": rad(0), "roll": rad(2), "yaw": rad(4)}


def parse_gps(payload: bytes):
    """CRSF_FRAMETYPE_GPS (0x02) -> dict."""
    if len(payload) < 15:
        return None
    lat = int.from_bytes(payload[0:4], "big", signed=True) / 1e7
    lon = int.from_bytes(payload[4:8], "big", signed=True) / 1e7
    speed = int.from_bytes(payload[8:10], "big") / 10.0       # km/h
    heading = int.from_bytes(payload[10:12], "big") / 100.0   # degrees
    altitude = int.from_bytes(payload[12:14], "big") - 1000   # metres
    sats = payload[14]
    return {"lat": lat, "lon": lon, "speed_kmh": speed,
            "heading": heading, "altitude_m": altitude, "sats": sats}


def parse_flight_mode(payload: bytes):
    """CRSF_FRAMETYPE_FLIGHT_MODE (0x21) -> dict.

    A NUL-terminated string straight from the flight controller: RTL, AUTO,
    FBWA and so on. It is the only telemetry that says outright what the
    model is doing, rather than leaving it to be inferred from channel
    values - which is exactly the question a failsafe raises.
    """
    if not payload:
        return None
    try:
        mode, _ = _cstr(payload, 0)
    except ValueError:
        # Some senders leave the terminator off the last frame.
        mode = payload.decode("ascii", "replace")
    mode = mode.strip()
    return {"mode": mode} if mode else None


def parse_vario(payload: bytes):
    """CRSF_FRAMETYPE_VARIO (0x07) -> dict. Vertical speed, cm/s on the wire."""
    if len(payload) < 2:
        return None
    return {"vertical_speed_ms":
            int.from_bytes(payload[0:2], "big", signed=True) / 100.0}


def _baro_metres(raw):
    """Decode the packed baro altitude.

    Two encodings share the field: with the top bit set the rest is whole
    metres, otherwise it is decimetres biased by 10000 so that a value below
    launch height still fits an unsigned number.
    """
    if raw & 0x8000:
        return float(raw & 0x7FFF)
    return (raw - 10000) / 10.0


def parse_baro_altitude(payload: bytes):
    """CRSF_FRAMETYPE_BARO_ALTITUDE (0x09) -> dict.

    Newer senders append vertical speed to the same frame; older ones send
    the altitude alone, so the second field is taken only if it is there.
    """
    if len(payload) < 2:
        return None
    out = {"altitude_m": _baro_metres(int.from_bytes(payload[0:2], "big"))}
    if len(payload) >= 4:
        out["vertical_speed_ms"] = int.from_bytes(
            payload[2:4], "big", signed=True) / 100.0
    return out


FRAME_PARSERS = {
    FRAMETYPE_LINK_STATISTICS: ("link", parse_link_statistics),
    FRAMETYPE_BATTERY_SENSOR: ("battery", parse_battery),
    FRAMETYPE_ATTITUDE: ("attitude", parse_attitude),
    FRAMETYPE_GPS: ("gps", parse_gps),
    FRAMETYPE_FLIGHT_MODE: ("mode", parse_flight_mode),
    FRAMETYPE_VARIO: ("vario", parse_vario),
    FRAMETYPE_BARO_ALTITUDE: ("baro", parse_baro_altitude),
}


# ===========================================================================
#  Parameter / settings protocol
#
#  This is what the OpenTX/EdgeTX Lua script speaks to configure a module:
#  packet rate, TX power, telemetry ratio and so on. None of it is reachable
#  by sending RC frames faster or slower - the RF packet rate is a setting
#  stored inside the module, quite separate from how often we hand it frames.
#
#  Field entries arrive split across chunks. Ask for (index, chunk), append
#  what comes back, and keep going until chunks_remaining is 0. Requests must
#  be issued one at a time: a reply to the previous request that is still in
#  flight is indistinguishable from the one you are waiting for.
# ===========================================================================

FRAMETYPE_DEVICE_PING = 0x28
FRAMETYPE_PARAMETER_READ = 0x2C
FRAMETYPE_PARAMETER_WRITE = 0x2D

PARAM_UINT8 = 0
PARAM_INT8 = 1
PARAM_FLOAT = 8
PARAM_SELECT = 9
PARAM_STRING = 10
PARAM_FOLDER = 11
PARAM_INFO = 12
PARAM_COMMAND = 13

PARAM_TYPE_NAMES = {
    0: "uint8", 1: "int8", 2: "uint16", 3: "int16", 4: "uint32", 5: "int32",
    6: "uint64", 7: "int64", 8: "float", 9: "select", 10: "string",
    11: "folder", 12: "info", 13: "command",
}

# Width and signedness of the numeric types, for value/min/max/default.
_NUMERIC = {
    0: (1, False), 1: (1, True), 2: (2, False), 3: (2, True),
    4: (4, False), 5: (4, True), 8: (4, True),
}

# Command fields step through these. Writing START runs the command; the
# module then reports PROGRESS until it finishes, or CONFIRMATION_NEEDED if
# it wants a yes first.
CMD_READY = 0
CMD_START = 1
CMD_PROGRESS = 2
CMD_CONFIRMATION_NEEDED = 3
CMD_CONFIRM = 4
CMD_CANCEL = 5
CMD_POLL = 6

CMD_STATUS_NAMES = {
    0: "ready", 1: "start", 2: "running", 3: "confirm?",
    4: "confirmed", 5: "cancelled", 6: "poll",
}


def pack_extended(ftype: int, payload: bytes = b"",
                  dest: int = ADDRESS_CRSF_TRANSMITTER,
                  origin: int = ADDRESS_RADIO_TRANSMITTER,
                  sync: int = CRSF_SYNC_BYTE) -> bytes:
    """Build an extended-header frame: [sync][len][type][dest][origin][...][crc]."""
    body = bytes([ftype, dest, origin]) + payload
    return bytes([sync, len(body) + 1]) + body + bytes([crc8(body)])


def device_ping_frame() -> bytes:
    """Broadcast ping. Every device on the bus answers with DEVICE_INFO."""
    return pack_extended(FRAMETYPE_DEVICE_PING, dest=0x00)


def param_read_frame(index: int, chunk: int = 0) -> bytes:
    return pack_extended(FRAMETYPE_PARAMETER_READ, bytes([index & 0xFF, chunk & 0xFF]))


def param_value_width(ftype: int) -> int:
    """How many bytes a write to a field of this type carries.

    Selects, commands and the status pseudo-fields are one byte; the numeric
    types are as wide as they were parsed.
    """
    return _NUMERIC.get(ftype, (1, False))[0]


def param_write_frame(index: int, value: int, width: int = 1) -> bytes:
    """Write a value to a settings field.

    The value is sent big-endian in `width` bytes, matching the width the
    entry was parsed with. Sending one byte for a uint16 field leaves the
    module reading our CRC as the second half of the value, so it either
    CRC-fails or applies a number nobody asked for.
    """
    width = max(1, min(4, int(width)))
    masked = int(value) & ((1 << (8 * width)) - 1)   # two's complement for negatives
    return pack_extended(FRAMETYPE_PARAMETER_WRITE,
                         bytes([index & 0xFF]) + masked.to_bytes(width, "big"))


def _cstr(buf: bytes, i: int):
    """Read a NUL-terminated string starting at i; returns (text, next_index)."""
    end = buf.index(b"\x00", i)
    return buf[i:end].decode("ascii", "replace"), end + 1


def parse_device_info(payload: bytes):
    """DEVICE_INFO (0x29) -> dict. Payload starts with dest, origin."""
    try:
        name, i = _cstr(payload, 2)
        serial_no = int.from_bytes(payload[i:i + 4], "big"); i += 4
        hardware = int.from_bytes(payload[i:i + 4], "big"); i += 4
        software = int.from_bytes(payload[i:i + 4], "big"); i += 4
        return {
            "name": name,
            "serial": serial_no,
            "hardware": hardware,
            "software": software,
            "version": f"{(software >> 16) & 0xFF}.{(software >> 8) & 0xFF}."
                       f"{software & 0xFF}",
            "field_count": payload[i],
            "param_version": payload[i + 1],
        }
    except (ValueError, IndexError):
        return None


class ParamField:
    """One entry from the module's settings list."""

    __slots__ = ("index", "parent", "type", "hidden", "name", "options",
                 "value", "vmin", "vmax", "vdefault", "unit", "children",
                 "status", "timeout", "info", "text")

    def __init__(self, index, parent, ftype, hidden, name, options=None,
                 value=None, vmin=None, vmax=None, unit=""):
        self.index = index
        self.parent = parent
        self.type = ftype
        self.hidden = hidden
        self.name = name
        self.options = options or []
        self.value = value
        self.vmin = vmin
        self.vmax = vmax
        self.vdefault = None
        self.unit = unit
        self.children = []      # folders: the field ids they contain
        self.status = None      # commands: CMD_* state
        self.timeout = 0        # commands: how long to allow, in 10 ms units
        self.info = ""          # commands: prompt or progress text
        self.text = ""          # info/string fields: the value shown

    @property
    def editable(self):
        return self.type in _NUMERIC or self.type in (PARAM_SELECT, PARAM_STRING)

    @property
    def display(self):
        """What to show as this field's current value."""
        if self.type == PARAM_SELECT:
            return f"{self.current_label} {self.unit}".strip()
        if self.type in (PARAM_INFO, PARAM_STRING):
            return self.text
        if self.type == PARAM_COMMAND:
            return CMD_STATUS_NAMES.get(self.status, "")
        if self.value is not None:
            return f"{self.value} {self.unit}".strip()
        return ""

    @property
    def type_name(self):
        return PARAM_TYPE_NAMES.get(self.type, f"type{self.type}")

    def choices(self):
        """(value, label) for every selectable option.

        ExpressLRS pads the list with empty strings for rates the current
        hardware or configuration cannot do, so those are dropped here - but
        the surviving entries keep their original index, which is what a
        write actually sends.
        """
        return [(i, label) for i, label in enumerate(self.options)
                if label.strip()]

    def label_for(self, value):
        if 0 <= value < len(self.options) and self.options[value].strip():
            return self.options[value]
        return str(value)

    @property
    def current_label(self):
        return self.label_for(self.value) if self.value is not None else ""

    def __repr__(self):
        return (f"ParamField({self.index}, {self.name!r}, {self.type_name}, "
                f"value={self.value})")


# EdgeTX packs a couple of its own glyphs into option labels; 0xC0 and 0xC1
# are the up and down arrows used for "AUX1 high / AUX1 low".
_GLYPHS = {0xC0: "↑", 0xC1: "↓"}


def _decode_text(raw: bytes) -> str:
    out = []
    for b in raw:
        if b in _GLYPHS:
            out.append(_GLYPHS[b])
        elif 32 <= b < 127:
            out.append(chr(b))
        elif b >= 160:
            out.append(chr(b))       # latin-1 passthrough
    return "".join(out)


def _raw_cstr(buf: bytes, i: int):
    """Like _cstr but returns the raw bytes, so glyphs survive."""
    end = buf.index(b"\x00", i)
    return buf[i:end], end + 1


def parse_param_entry(index: int, body: bytes):
    """Turn a fully assembled PARAMETER_SETTINGS_ENTRY body into a ParamField.

    `body` is the concatenation of every chunk's payload, with the leading
    dest/origin/index/chunks_remaining header of each chunk already stripped.
    Layout after the name is decided by the type:

        numeric   value, min, max, default (each 1-4 bytes), then a unit
        select    "a;b;c", value, min, max, default, then a unit
        string    the text
        folder    the ids of the fields it contains, terminated by 0xFF
        info      the text to display
        command   status, timeout (10 ms units), prompt text
    """
    if len(body) < 3:
        return None
    parent = body[0]
    raw_type = body[1]
    ftype = raw_type & 0x7F
    hidden = bool(raw_type & 0x80)
    try:
        name_raw, i = _raw_cstr(body, 2)
    except ValueError:
        return None

    field = ParamField(index, parent, ftype, hidden, _decode_text(name_raw))

    try:
        if ftype == PARAM_SELECT:
            opts_raw, i = _raw_cstr(body, i)
            field.options = [_decode_text(o) for o in opts_raw.split(b";")]
            field.value, field.vmin = body[i], body[i + 1]
            field.vmax, field.vdefault = body[i + 2], body[i + 3]
            try:
                unit_raw, _ = _raw_cstr(body, i + 4)
                field.unit = _decode_text(unit_raw)
            except (ValueError, IndexError):
                field.unit = ""

        elif ftype in _NUMERIC:
            width, signed = _NUMERIC[ftype]

            def take(pos):
                # Slicing past the end yields b"" and int.from_bytes turns
                # that into 0, so a truncated entry would parse as a real
                # value of zero with a 0..0 range. Every other branch indexes
                # and raises; make this one behave the same.
                if pos + width > len(body):
                    raise IndexError("numeric parameter entry is truncated")
                return int.from_bytes(body[pos:pos + width], "big", signed=signed)

            field.value = take(i)
            field.vmin = take(i + width)
            field.vmax = take(i + 2 * width)
            field.vdefault = take(i + 3 * width)
            try:
                unit_raw, _ = _raw_cstr(body, i + 4 * width)
                field.unit = _decode_text(unit_raw)
            except (ValueError, IndexError):
                field.unit = ""

        elif ftype in (PARAM_STRING, PARAM_INFO):
            text_raw, _ = _raw_cstr(body, i)
            field.text = _decode_text(text_raw)

        elif ftype == PARAM_FOLDER:
            # ids of the fields inside, terminated by 0xFF
            for b in body[i:]:
                if b == 0xFF:
                    break
                field.children.append(b)

        elif ftype == PARAM_COMMAND:
            field.status = body[i]
            field.timeout = body[i + 1]
            try:
                info_raw, _ = _raw_cstr(body, i + 2)
                field.info = _decode_text(info_raw)
            except (ValueError, IndexError):
                field.info = ""
    except (ValueError, IndexError):
        pass    # a short or odd entry is still worth showing by name

    return field


class ParamReader:
    """Reassembles chunked PARAMETER_SETTINGS_ENTRY replies for one field."""

    def __init__(self, index: int):
        self.index = index
        self.chunk = 0
        self.body = b""
        self.done = False
        self._last_remaining = None

    def next_request(self) -> bytes:
        return param_read_frame(self.index, self.chunk)

    def feed(self, payload: bytes) -> bool:
        """Accept a 0x2B payload. Returns True if it belonged to this field.

        chunks_remaining counts down, so a reply repeating a count we have
        already taken is a duplicate - a late answer to a request we resent -
        and appending it would corrupt the field. Those are dropped.
        """
        if self.done or len(payload) < 4 or payload[2] != self.index:
            return False
        remaining = payload[3]
        if self._last_remaining is not None and remaining >= self._last_remaining:
            return False
        self._last_remaining = remaining
        self.body += payload[4:]
        if remaining == 0:
            self.done = True
        else:
            self.chunk += 1
        return True

    def field(self):
        return parse_param_entry(self.index, self.body) if self.done else None


# ---------------------------------------------------------------- RF sync
# ExpressLRS broadcasts the CRSF frame interval it wants from the handset,
# so the handset can line its frames up with the RF slots. EdgeTX uses it to
# phase-lock. We do not try to phase-lock over USB - the CDC buffering adds
# more jitter than the alignment would buy - but the stated interval is still
# the authoritative answer to "how fast should we be sending?".
FRAMETYPE_RADIO_ID = 0x3A
OPENTX_SYNC_SUBTYPE = 0x10


def parse_opentx_sync(payload: bytes):
    """RADIO_ID (0x3A) subtype 0x10 -> dict, or None if it is something else.

    Payload is dest, origin, subtype, then interval and offset as big-endian
    32-bit counts of 100 ns.
    """
    if len(payload) < 11 or payload[2] != OPENTX_SYNC_SUBTYPE:
        return None
    interval = int.from_bytes(payload[3:7], "big")
    offset = int.from_bytes(payload[7:11], "big", signed=True)
    if interval <= 0:
        return None
    return {
        "interval_us": interval / 10.0,
        "rate_hz": 10_000_000.0 / interval,
        "offset_us": offset / 10.0,
    }


def recommended_crsf_rate(requested_hz: float, baud: int) -> int:
    """Pick a CRSF frame rate for a module asking for `requested_hz`.

    Matching it exactly is the one thing not to do: the PC clock and the
    module's RF clock free-run against each other, so at 1:1 some RF slots
    find no new frame and resend stale stick positions. Oversampling fixes
    that without any phase locking, so aim well above the requested rate and
    spend whatever headroom the serial link has.
    """
    cap = MAX_RATE_FOR_BAUD.get(baud)
    if cap is None:
        # No published cap for this baud; keep bus utilisation near 70%.
        cap = int(baud / (RC_FRAME_LEN * 10 * 1.4))
    cap = max(50, min(cap, 500))
    if requested_hz <= 0:
        return cap
    return int(max(50, min(cap, requested_hz * 3)))


# ------------------------------------------------------------ ELRS status
# ExpressLRS will not apply some settings in some states, and says so rather
# than erroring: ask for the status frame and it answers with flags plus a
# sentence, e.g. "Not while connected" when a receiver is linked. Without
# this a refused write just looks like the value silently reverting.
#
# The request is a parameter write to field 0. Bit 0 of the flags means a
# receiver is connected; anything above 0x1F is a warning carrying text,
# which is acknowledged by writing 0 to field 0x2E.
ELRS_STATUS_FIELD = 0x00
ELRS_FLAGS_CLEAR_FIELD = 0x2E
ELRS_FLAG_CONNECTED = 0x01
ELRS_FLAG_WARNING = 0x20


def elrs_status_request_frame() -> bytes:
    return param_write_frame(ELRS_STATUS_FIELD, 0)


def elrs_clear_warning_frame() -> bytes:
    return param_write_frame(ELRS_FLAGS_CLEAR_FIELD, 0)


def parse_elrs_status(payload: bytes):
    """CRSF_FRAMETYPE_ELRS_STATUS (0x2E) -> dict, or None."""
    if len(payload) < 7:
        return None
    try:
        info, _ = _cstr(payload, 6)
    except ValueError:
        info = ""
    flags = payload[5]
    return {
        "bad_packets": payload[2],
        "good_packets": int.from_bytes(payload[3:5], "big"),
        "flags": flags,
        "connected": bool(flags & ELRS_FLAG_CONNECTED),
        "warning": flags > 0x1F,
        "info": info,
    }
