"""
APRS (Automatic Packet Reporting System) decoder/encoder: 1200-baud
Bell 202 AFSK over AX.25/HDLC framing, the standard VHF packet
configuration (144.390 MHz in North America).

Send support (position reports only, matching the decode side's own
scope) was added after confirming this app DOES have a working path to
push arbitrary synthesized PCM into a real radio's TX audio input --
rigplane's push_audio_tx_pcm()/push_tx() family (see radio_worker.py's
send_tx_audio_pcm, a generic "key PTT, stream this PCM, unkey"
primitive not specific to APRS) -- unlike cw.py's send (the radio's
own built-in keyer, no audio at all) and rtty.py/sstv.py's still-
decode-only siblings (not yet wired up to this same TX audio path,
though nothing about it is APRS-specific).

Standard parameters (verified against public references, not guessed --
same "public standard, not project-specific" reasoning cw.py's
MORSE_CODE_TABLE and rtty.py's Baudot table document for themselves):
mark 1200 Hz / space 2200 Hz Bell 202 AFSK, 1200 baud, HDLC framing
with NRZI encoding (a "1" bit is NO tone transition, a "0" bit IS a
tone transition -- confirmed via multiple independent sources), flag
byte 0x7E, bit-stuffing after 5 consecutive 1s, AX.25 address/control/
PID field layout and the CRC-16/X-25 FCS algorithm (poly 0x1021
reflected = 0x8408, init 0xFFFF, complemented on output) all confirmed
via public AX.25/Bell-202 references. The uncompressed APRS position-
report info-field format (leading '!'/'='/'@'/'/' character, 8-char
lat/9-char lon/symbol table+code layout) is quoted directly from the
official APRS Protocol Reference 1.0.1 (aprs.org/doc/APRS101.PDF,
Chapter 8) -- the only info-field format actually implemented here;
every other packet type (messages, objects, status, Mic-E, compressed
positions, weather, telemetry) is deliberately left as a raw-text
fallback rather than guessing an unverified layout.

Demodulation: an FM/quadrature discriminator (the technique rtty.py
and sstv.py both use) was tried FIRST here and confirmed NOT to work --
Bell 202's NRZI encoding of a repeating flag byte (the HDLC idle/sync
pattern) produces a genuine single-bit-wide tone pulse (~0.83ms at
1200 baud) between longer runs, and no FIR filter short enough to
settle within that one bit period also gives the phase-derivative
estimate a stable, low-noise amplitude to differentiate -- confirmed
via test_aprs.py across a wide sweep of filter lengths/bandwidths, all
of which left the discriminator's frequency estimate sitting right at
the mark/space threshold for that pulse, an effective coin flip.
Reused instead: cw.py's GoertzelDetector, one per tone, each computed
over exactly ONE bit's own aligned sample window (not a continuously
sliding window, and not per-sample) -- since Bell 202's tones are 1000
Hz apart (vs RTTY's 170 Hz), even a single bit period's worth of
samples gives Goertzel plenty of resolution to tell them apart, no
discriminator settling-time tradeoff involved at all. Bit-clock
recovery and tone discrimination are one combined step here (see
_AfskBitSync) rather than two separate stages, precisely because each
bit's Goertzel window has to be sample-aligned to that bit's own
boundaries to mean anything.
"""

import math
import re
import struct

from cw import GoertzelDetector

MARK_HZ = 1200.0
SPACE_HZ = 2200.0
BAUD = 1200.0

FLAG_BYTE = 0x7E


class _AfskBitSync:
    """Combined AFSK demodulation + bit-clock recovery: buffers raw
    PCM samples and, once enough have arrived to complete the next
    bit's aligned window (tracked via a running elapsed-sample-count
    threshold, advanced by a fixed samples_per_bit each time -- the
    same "elapsed time crosses a threshold -> sample now" shape
    rtty.py's RttyDecoder uses per-character, just applied
    continuously instead), computes that bit's tone via comparative
    Goertzel energy (mark vs. space) and NRZI-decodes it against the
    previous bit's tone. Stateful across process() calls so bit timing
    and any partial trailing window carry across arbitrary chunk
    boundaries.

    Bell 202/AX.25 uses NRZI: a "1" bit is NO tone transition since the
    previous bit, a "0" bit IS a transition -- confirmed via multiple
    independent sources (this module's own docstring). The very first
    sampled bit can never itself be decoded (NRZI needs a preceding
    reference tone to compare against) -- harmless in practice, always
    landing somewhere in the idle/flag preamble before real data
    starts, never in the data itself."""

    def __init__(self, sample_rate: int):
        self._mark_detector = GoertzelDetector(sample_rate, MARK_HZ)
        self._space_detector = GoertzelDetector(sample_rate, SPACE_HZ)
        self._samples_per_bit = sample_rate / BAUD
        self._sample_buffer = []       # raw samples not yet consumed into a completed bit window
        self._samples_consumed = 0.0   # total samples already sliced off into completed bit windows
        self._next_boundary = self._samples_per_bit  # cumulative sample count where the NEXT bit's window ends
        self._prev_bit_tone = None     # True=mark -- NRZI compares against this

    def process(self, samples) -> list:
        self._sample_buffer.extend(samples)
        bits = []
        while self._samples_consumed + len(self._sample_buffer) >= self._next_boundary:
            end_idx = max(1, round(self._next_boundary - self._samples_consumed))
            window = self._sample_buffer[:end_idx]
            del self._sample_buffer[:end_idx]
            self._samples_consumed += end_idx
            is_mark = self._mark_detector.power(window) >= self._space_detector.power(window)
            if self._prev_bit_tone is not None:
                bits.append(is_mark == self._prev_bit_tone)  # NRZI: same tone as last bit = 1, changed = 0
            self._prev_bit_tone = is_mark
            self._next_boundary += self._samples_per_bit
        return bits


def _crc16_x25(data: bytes) -> int:
    """AX.25's FCS algorithm -- CRC-16/X-25 (poly 0x1021 bit-reflected
    = 0x8408), init 0xFFFF, complemented on output. Same well-known
    reflected-CRC construction used identically by several other
    protocols (e.g. PPP)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc ^ 0xFFFF


class _HdlcDeframer:
    """Consumes a raw (not yet destuffed) NRZI bit stream and yields
    complete, CRC-valid AX.25 frames as raw bytes (address+control+PID+
    info, FCS already stripped and verified -- never returned if the
    CRC doesn't check out). Stateful across process() calls."""

    def __init__(self):
        self._recent_bits = 0          # last up-to-8 raw bits, for flag-pattern detection (LSB-most-recent)
        self._recent_bit_count = 0
        self._in_frame = False
        self._frame_bits = []          # destuffed bits accumulated since the opening flag
        self._ones_run = 0             # consecutive 1s seen since the last 0 (for destuffing)

    def process(self, bits) -> list:
        frames = []
        for bit in bits:
            self._recent_bits = ((self._recent_bits << 1) | (1 if bit else 0)) & 0xFF
            self._recent_bit_count = min(self._recent_bit_count + 1, 8)
            is_flag = self._recent_bit_count == 8 and self._recent_bits == FLAG_BYTE

            if is_flag:
                # The flag's own 8 bits (0,1,1,1,1,1,1,0) were fed
                # through the ordinary data path above BEFORE this
                # check could recognize them (is_flag only becomes
                # true once all 8 bits have gone by) -- bit 8 (this
                # one) never got appended (we're continuing before
                # reaching the append below), but bits 1-6 did, since
                # they look exactly like five genuine data 1s preceded
                # by a 0 until the moment this check fires. Bit 7 (the
                # flag's 6th one) is handled separately below, in the
                # ones_run==5 branch, which discards it without
                # appending. So exactly 6 spurious bits (the flag's own
                # bits 1-6) are sitting at the tail of _frame_bits right
                # now and must be stripped before treating it as real
                # payload -- confirmed via test_aprs.py that without
                # this, no frame -- not even the correctly-decoded
                # bitstream from _AfskBitSync -- ever survives to
                # _bits_to_frame at all.
                if self._in_frame:
                    real_bits = self._frame_bits[:-6] if len(self._frame_bits) >= 6 else []
                    if len(real_bits) >= 8 * 18:
                        frame = self._bits_to_frame(real_bits)
                        if frame is not None:
                            frames.append(frame)
                self._in_frame = True
                self._frame_bits = []
                self._ones_run = 0
                continue

            if not self._in_frame:
                continue

            if self._ones_run == 5:
                # This bit is either a stuffed 0 (real data, discard
                # it) or the flag/abort pattern's own 6th consecutive 1
                # (also never real data -- either it's about to be
                # recognized as a flag by is_flag above, in which case
                # _frame_bits gets trimmed/reset there, or it's a
                # genuine line error with no valid flag ever following,
                # in which case this frame attempt silently never
                # reaches the CRC check and is naturally abandoned).
                # Never appended to _frame_bits either way -- 6
                # consecutive 1s is never valid stuffed data content
                # (the sender guarantees at most 5 in a row).
                self._ones_run = 0
                continue

            self._frame_bits.append(bit)
            self._ones_run = self._ones_run + 1 if bit else 0

        return frames

    @staticmethod
    def _bits_to_frame(bits):
        usable_len = len(bits) - (len(bits) % 8)
        if usable_len < 8 * 18:  # 2 addresses (14) + control (1) + PID (1) + FCS (2), minimum
            return None
        raw = bytearray()
        for i in range(0, usable_len, 8):
            byte = 0
            for j in range(8):
                # LSB-first over the wire -- the first-received bit of
                # each byte is bit 0.
                if bits[i + j]:
                    byte |= 1 << j
            raw.append(byte)
        payload, fcs_bytes = bytes(raw[:-2]), raw[-2:]
        received_fcs = fcs_bytes[0] | (fcs_bytes[1] << 8)
        if _crc16_x25(payload) != received_fcs:
            return None
        return payload


# ---- AX.25 address / APRS info-field parsing ------------------------------

def _decode_address(raw7):
    """One 7-byte AX.25 address field -> (callsign, ssid, is_last)."""
    chars = "".join(chr(b >> 1) for b in raw7[:6]).rstrip(" ")
    ssid_byte = raw7[6]
    ssid = (ssid_byte >> 1) & 0x0F
    is_last = bool(ssid_byte & 0x01)
    return (f"{chars}-{ssid}" if ssid else chars), is_last


def parse_ax25_frame(payload: bytes):
    """Parses address/control/PID/info out of a CRC-verified AX.25
    frame payload (no FCS). Returns None if the address field or
    control/PID bytes don't look like a valid AX.25 UI frame."""
    if len(payload) < 15:
        return None
    addresses = []
    offset = 0
    while offset + 7 <= len(payload):
        callsign, is_last = _decode_address(payload[offset:offset + 7])
        addresses.append(callsign)
        offset += 7
        if is_last:
            break
    else:
        return None  # ran off the end without ever seeing the last-address bit
    if len(addresses) < 2 or offset + 2 > len(payload):
        return None
    control, pid = payload[offset], payload[offset + 1]
    if control != 0x03 or pid != 0xF0:
        return None  # not a plain APRS UI frame -- only thing this decoder understands
    info = payload[offset + 2:]
    destination, source = addresses[0], addresses[1]
    digipeaters = addresses[2:]
    return {
        "source": source,
        "destination": destination,
        "digipeaters": digipeaters,
        "info": info,
    }


def _parse_latitude(text):
    text = text.replace(" ", "0")
    degrees = int(text[0:2])
    minutes = float(text[2:7])
    value = degrees + minutes / 60.0
    return -value if text[7] == "S" else value


def _parse_longitude(text):
    text = text.replace(" ", "0")
    degrees = int(text[0:3])
    minutes = float(text[3:8])
    value = degrees + minutes / 60.0
    return -value if text[8] == "W" else value


# Official APRS symbol table -- public reference, not project-specific
# (same "public standard, not project-specific" framing cw.py's own
# MORSE_CODE_TABLE and rtty.py's Baudot table document for themselves),
# transcribed directly from aprs.org/symbols/symbolsX.txt (Bob
# Bruninga WB4APR, the APRS protocol's own originator -- THE master
# symbol list). Two tables of 94 printable-ASCII symbol codes each
# ("!" through "~"), selected by the position report's own table
# character ("/" = primary, "\" = alternate -- see parse_aprs_info).
# Codes the source document itself marks unassigned/reserved/"TBD"/
# "AVAIL" are simply absent here (symbol_description() below returns
# None for those, same "no entry -- nothing to translate" convention
# CwDecoder uses for an unmapped mark/space sequence) rather than
# guessing a name for something the spec itself doesn't define yet.
SYMBOL_TABLE_PRIMARY = {
    "!": "Police, Sheriff", "#": "Digipeater", "$": "Phone", "%": "DX Cluster",
    "&": "HF Gateway", "'": "Small Aircraft", "(": "Mobile Satellite Station",
    ")": "Wheelchair (Handicapped)", "*": "Snowmobile", "+": "Red Cross",
    ",": "Boy Scouts", "-": "House QTH (VHF)", ".": "X", "/": "Red Dot",
    ":": "Fire", ";": "Campground (Portable Ops)", "<": "Motorcycle",
    "=": "Railroad Engine", ">": "Car", "?": "Server for Files",
    "@": "Hurricane Predicted Track", "A": "Aid Station", "B": "BBS or PBBS",
    "C": "Canoe", "E": "Eyeball (Events, etc)", "F": "Farm Vehicle (Tractor)",
    "G": "Grid Square (6 digit)", "H": "Hotel", "I": "TCP/IP on Air Network Station",
    "K": "School", "L": "PC User", "M": "MacAPRS", "N": "NTS Station",
    "O": "Balloon", "P": "Police", "R": "Recreational Vehicle", "S": "Shuttle",
    "T": "SSTV", "U": "Bus", "V": "ATV", "W": "National WX Service Site",
    "X": "Helicopter", "Y": "Yacht (Sail)", "Z": "WinAPRS", "[": "Human/Person",
    "\\": "Triangle (DF Station)", "]": "Mail/Post Office", "^": "Large Aircraft",
    "_": "Weather Station", "`": "Dish Antenna",
    "a": "Ambulance", "b": "Bike", "c": "Incident Command Post",
    "d": "Fire Department", "e": "Horse (Equestrian)", "f": "Fire Truck",
    "g": "Glider", "h": "Hospital", "i": "IOTA (Islands on the Air)",
    "j": "Jeep", "k": "Truck", "l": "Laptop", "m": "Mic-E Repeater",
    "n": "Node (Black Bulls-eye)", "o": "EOC", "p": "Rover (Dog)",
    "q": "Grid Square Shown Above 128m", "r": "Repeater", "s": "Ship (Power Boat)",
    "t": "Truck Stop", "u": "Truck (18 Wheeler)", "v": "Van", "w": "Water Station",
    "x": "xAPRS (Unix)", "y": "Yagi @ QTH", "|": "TNC Stream Switch",
    "~": "TNC Stream Switch",
}
SYMBOL_TABLE_ALTERNATE = {
    "!": "Emergency", "#": "Overlay Digipeater (Green Star)", "$": "Bank or ATM",
    "%": "Power Plant (with Overlay)", "&": "Gateway", "'": "Crash / Incident Site",
    "(": "Cloudy", ")": "Firenet MEO, MODIS Earth Observation", "+": "Church",
    ",": "Girl Scouts", "-": "House", ".": "Ambiguous (Big Question Mark)",
    "/": "Waypoint Destination", "0": "Circle (IRLP/Echolink/WIRES)",
    "8": "802.11 or Other Network Node", "9": "Gas Station", ";": "Park/Picnic",
    "<": "Advisory (Single WX Flag)", ">": "Overlayed Car/Vehicle",
    "?": "Info Kiosk", "@": "Hurricane/Tropical Storm",
    "A": "Overlay Box: DTMF, RFID, XO", "C": "Coast Guard", "D": "Depots",
    "E": "Smoke (& Other Visibility Codes)", "H": "Haze (& Overlay Hazards)",
    "I": "Rain Shower", "K": "Kenwood HT", "L": "Lighthouse",
    "M": "MARS (Army/Navy/Air Force)", "N": "Navigation Buoy",
    "O": "Overlay Balloon (Rocket)", "P": "Parking", "Q": "Earthquake",
    "R": "Restaurant", "S": "Satellite/Pacsat", "T": "Thunderstorm", "U": "Sunny",
    "V": "VORTAC Nav Aid", "W": "NWS Site", "X": "Pharmacy (Rx)",
    "Y": "Radios and Devices", "[": "W. Cloud (& Humans with Overlay)",
    "\\": "New Overlayable GPS Symbol", "^": "Aircraft (with Overlay)",
    "_": "WX Site (Green Digi)", "`": "Rain (All Types, with Overlay)",
    "a": "ARRL, ARES, WinLINK, D-STAR, etc", "c": "CD Triangle: RACES/SATERN/etc",
    "d": "DX Spot by Callsign", "e": "Sleet", "f": "Funnel Cloud", "g": "Gale Flags",
    "h": "Store / Hamfest", "i": "Box or Points of Interest",
    "j": "Work Zone (Steam Shovel)", "k": "Special Vehicle (SUV, ATV, 4x4)",
    "l": "Area (Box, Circle, etc)", "m": "Value Sign (3-digit Display)",
    "n": "Overlay Triangle", "o": "Small Circle", "r": "Restrooms",
    "s": "Overlay Ship/Boat", "t": "Tornado", "u": "Overlayed Truck",
    "v": "Overlayed Van", "w": "Flooding (Avalanches/Slides)",
    "x": "Wreck or Obstruction", "y": "Skywarn", "z": "Overlayed Shelter",
    "|": "TNC Stream Switch", "~": "TNC Stream Switch",
}


def symbol_description(symbol_table: str, symbol_code: str):
    """Human-readable name for a decoded position report's symbol_
    table/symbol_code pair (e.g. "/" + "-" -> "House QTH (VHF)"), or
    None if that combination isn't in the official symbol set above
    (either genuinely unassigned in the spec, or an overlay-table digit/
    letter '0'-'9'/'A'-'Z' selecting an alphanumeric OVERLAY on the
    alternate table rather than one of the two base tables themselves --
    APRS101's own overlay mechanism, not something with a single fixed
    name to report)."""
    table = SYMBOL_TABLE_PRIMARY if symbol_table == "/" else SYMBOL_TABLE_ALTERNATE
    return table.get(symbol_code)


# ---- Comment "Data Extensions" (APRS101.PDF Chapter 7) --------------
#
# A position report's comment text MAY start with one of several
# mutually-exclusive fixed-length 7-byte structured sub-fields (course/
# speed, PHG, RNG, or DFS -- quoted directly from the spec, verified via
# pdftotext extraction of the real PDF, not guessed/summarized), plus
# altitude ("/A=aaaaaa"), which unlike the other four may appear
# ANYWHERE in the comment rather than only at that fixed leading
# position. This never modifies/consumes info["comment"] itself -- it's
# purely additive, parsed out separately for callers (aprs_window.py's
# packet list) that want to show a translated summary alongside the
# still-shown-verbatim raw comment.

_ALTITUDE_RE = re.compile(r"/A=(\d{6})")

# Shared PHG/DFS code table (APRS101.PDF's own "PHG Codes"/"DFS Codes"
# tables, identical shape for both -- height/gain/directivity use the
# same encoding either way, only the first code letter's meaning
# differs: transmitter power for PHG, relative signal strength for
# DFS). Directivity 0 = omni (no offset direction); 1-8 step clockwise
# from NE in 45-degree increments (spec table: 45=NE, 90=E, 135=SE,
# 180=S, 225=SW, 270=W, 315=NW, 360=N); 9 is undefined in the spec's
# own table (not listed), so is deliberately left out of this mapping.
_DIRECTIVITY_DEG = {0: None, 1: 45, 2: 90, 3: 135, 4: 180, 5: 225, 6: 270, 7: 315, 8: 360}


def _phg_code_value(char):
    """'0'-'9' -> 0-9, and beyond (the spec explicitly allows any ASCII
    character 0-9-and-above for the HEIGHT code specifically, so taller
    heights for balloons/aircraft/satellites can be represented -- e.g.
    ':' = 10, giving 10*2**10 = 10240ft)."""
    return ord(char) - 0x30  # ord('0') == 0x30


def _parse_phg_or_dfs(first_code, height_c, gain_c, dir_c):
    """Shared decode for the PHGphgd/DFSshgd Data Extensions' last 3
    code characters (height/gain/directivity) plus the caller-supplied
    first code (power for PHG, signal strength for DFS -- same 0-9
    code range either way). Returns None if any code character isn't a
    plain digit 0-9 (a malformed/corrupted extension, not a real one --
    height's extended ASCII range only applies to that one field, per
    the spec, not to power/strength/gain/directivity)."""
    if not (first_code.isdigit() and gain_c.isdigit() and dir_c.isdigit()):
        return None
    dir_value = _phg_code_value(dir_c)
    if dir_value not in _DIRECTIVITY_DEG:
        return None
    return {
        "height_ft": 10 * (2 ** _phg_code_value(height_c)),
        "gain_db": _phg_code_value(gain_c),
        "directivity_deg": _DIRECTIVITY_DEG[dir_value],
        "_first_code": _phg_code_value(first_code),
    }


def parse_comment_extensions(comment: str) -> dict:
    """Parses the optional Data Extension sub-fields documented above
    out of a position report's comment text. Returns a dict with only
    the keys actually found -- {} for an ordinary free-text comment
    with none of these:

    - {"course_deg": int, "speed_knots": int} -- CSE/SPD extension
    - {"phg": {"power_w", "height_ft", "gain_db", "directivity_deg"}}
    - {"df": {"strength_s", "height_ft", "gain_db", "directivity_deg"}}
    - {"range_miles": int} -- RNG extension
    - {"altitude_ft": int} -- "/A=aaaaaa", found independently of the
      four above since it can appear anywhere in the comment, not just
      the fixed leading 7 bytes those share."""
    result = {}
    head = comment[:7]
    if len(head) == 7:
        if head[:3] == "PHG":
            phg = _parse_phg_or_dfs(head[3], head[4], head[5], head[6])
            if phg is not None:
                result["phg"] = {
                    "power_w": phg["_first_code"] ** 2,
                    "height_ft": phg["height_ft"],
                    "gain_db": phg["gain_db"],
                    "directivity_deg": phg["directivity_deg"],
                }
        elif head[:3] == "RNG" and head[3:7].isdigit():
            result["range_miles"] = int(head[3:7])
        elif head[:3] == "DFS":
            dfs = _parse_phg_or_dfs(head[3], head[4], head[5], head[6])
            if dfs is not None:
                result["df"] = {
                    "strength_s": dfs["_first_code"],
                    "height_ft": dfs["height_ft"],
                    "gain_db": dfs["gain_db"],
                    "directivity_deg": dfs["directivity_deg"],
                }
        elif head[:3].isdigit() and head[3] == "/" and head[4:7].isdigit():
            result["course_deg"] = int(head[:3])
            result["speed_knots"] = int(head[4:7])

    altitude_match = _ALTITUDE_RE.search(comment)
    if altitude_match:
        result["altitude_ft"] = int(altitude_match.group(1))

    return result


def parse_aprs_info(info_bytes: bytes):
    """Parses an APRS info field. Full structured support for
    uncompressed position reports (data type '!'/'='/'/'/'@' -- format
    quoted directly from APRS101.PDF Chapter 8, the only format
    implemented here); everything else (messages ':', objects ';',
    status '>', Mic-E, compressed positions, weather, telemetry, ...)
    comes back as {"type": "other", "raw": <decoded text>} rather than
    guessing an unverified layout.

    Position report dict: {"type": "position", "has_timestamp": bool,
    "lat": float, "lon": float, "symbol_table": str, "symbol_code":
    str, "comment": str, "comment_extension": dict}. comment_extension
    is parse_comment_extensions(comment)'s own return -- see its
    docstring; comment itself always stays the full, untouched raw
    text regardless of what was found. Returns None if info_bytes is
    empty or not decodable as text at all."""
    if not info_bytes:
        return None
    try:
        text = info_bytes.decode("ascii", errors="replace")
    except Exception:
        return None

    data_type = text[0]
    try:
        if data_type in ("!", "="):
            lat = _parse_latitude(text[1:9])
            sym_table = text[9]
            lon = _parse_longitude(text[10:19])
            sym_code = text[19]
            comment = text[20:]
            return {
                "type": "position", "has_timestamp": False,
                "lat": lat, "lon": lon,
                "symbol_table": sym_table, "symbol_code": sym_code,
                "comment": comment,
                "comment_extension": parse_comment_extensions(comment),
            }
        if data_type in ("/", "@"):
            lat = _parse_latitude(text[8:16])
            sym_table = text[16]
            lon = _parse_longitude(text[17:26])
            sym_code = text[26]
            comment = text[27:]
            return {
                "type": "position", "has_timestamp": True,
                "lat": lat, "lon": lon,
                "symbol_table": sym_table, "symbol_code": sym_code,
                "comment": comment,
                "comment_extension": parse_comment_extensions(comment),
            }
    except (ValueError, IndexError):
        pass  # malformed -- fall through to the generic raw-text form
    return {"type": "other", "raw": text}


# ---- Encoding (send) -------------------------------------------------

def _format_latitude(lat: float) -> str:
    """Inverse of _parse_latitude -- ddmm.hhN/S, per APRS101.PDF
    Chapter 8 (see parse_aprs_info's own docstring)."""
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"Latitude out of range: {lat}")
    hemisphere = "N" if lat >= 0 else "S"
    lat = abs(lat)
    degrees = int(lat)
    minutes = (lat - degrees) * 60.0
    return f"{degrees:02d}{minutes:05.2f}{hemisphere}"


def _format_longitude(lon: float) -> str:
    """Inverse of _parse_longitude -- dddmm.hhE/W."""
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"Longitude out of range: {lon}")
    hemisphere = "E" if lon >= 0 else "W"
    lon = abs(lon)
    degrees = int(lon)
    minutes = (lon - degrees) * 60.0
    return f"{degrees:03d}{minutes:05.2f}{hemisphere}"


def encode_position_info(lat: float, lon: float, symbol_table: str, symbol_code: str, comment: str = "") -> bytes:
    """Builds an APRS info field for an uncompressed position report
    without a timestamp ('!', no APRS messaging capability declared --
    matches this app's own scope, which never sends/receives APRS
    messages, only position reports). Inverse of parse_aprs_info's
    "!"/"=" branch. Comment is truncated to APRS101.PDF's own stated
    43-character max for this field."""
    text = "!" + _format_latitude(lat) + symbol_table + _format_longitude(lon) + symbol_code + comment[:43]
    return text.encode("ascii", errors="replace")


def _encode_address(callsign: str, is_last: bool) -> bytes:
    """One 7-byte AX.25 address field -- inverse of _decode_address.
    `callsign` may include a "-N" SSID suffix (e.g. "N0CALL-9"); bare
    callsigns get SSID 0. Bits 7-5 of the SSID byte are set to 1
    (reserved/C-bit, matching what real TNCs transmit and what
    _decode_address already ignores on receive), bit 0 is the last-
    address flag."""
    if "-" in callsign:
        call, ssid_text = callsign.split("-", 1)
        ssid = int(ssid_text)
    else:
        call, ssid = callsign, 0
    if not 0 <= ssid <= 15:
        raise ValueError(f"SSID out of range 0-15: {ssid}")
    call = call.upper().ljust(6)[:6]
    raw = bytearray(b << 1 for b in call.encode("ascii"))
    raw.append(0b01100000 | (ssid << 1) | (1 if is_last else 0))
    return bytes(raw)


def _bit_stuff(bits: list) -> list:
    """Inserts a 0 after every 5 consecutive 1s -- inverse of the
    destuffing _HdlcDeframer.process() does on receive."""
    out = []
    ones_run = 0
    for bit in bits:
        out.append(bit)
        if bit:
            ones_run += 1
            if ones_run == 5:
                out.append(False)
                ones_run = 0
        else:
            ones_run = 0
    return out


def _byte_to_bits(byte: int) -> list:
    """LSB-first bit order -- the wire order every other byte in an
    AX.25 frame uses too (see _HdlcDeframer._bits_to_frame's own
    LSB-first unpacking on receive)."""
    return [bool((byte >> i) & 1) for i in range(8)]


def _bytes_to_bits(data: bytes) -> list:
    bits = []
    for byte in data:
        bits.extend(_byte_to_bits(byte))
    return bits


def _nrzi_encode(bits: list) -> list:
    """NRZI: a "1" bit is no tone change, a "0" bit toggles the tone.
    Starts on mark (True), matching the idle-line convention this
    module's own docstring documents. Returns one bool (True=mark) per
    input bit."""
    tones = []
    current = True
    for bit in bits:
        if not bit:
            current = not current
        tones.append(current)
    return tones


def _synthesize_afsk_pcm(tones: list, sample_rate: int) -> bytes:
    """Continuous-phase Bell 202 AFSK audio (int16 mono PCM bytes) for
    a sequence of per-bit tone booleans (True=mark/1200Hz, False=
    space/2200Hz). Tracks cumulative ideal sample count (not a fixed
    round() per bit) so per-bit rounding error doesn't accumulate into
    real drift over a long packet -- confirmed necessary via
    test_aprs.py's own encoder, which had exactly this bug until
    fixed."""
    samples_per_bit = sample_rate / BAUD
    phase = 0.0
    out = []
    emitted = 0.0
    for i, tone in enumerate(tones):
        freq = MARK_HZ if tone else SPACE_HZ
        target_total = (i + 1) * samples_per_bit
        n = round(target_total - emitted)
        emitted += n
        angular = 2.0 * math.pi * freq / sample_rate
        for _ in range(n):
            out.append(int(round(12000 * math.sin(phase))))
            phase += angular
    return struct.pack(f"<{len(out)}h", *out)


def build_ax25_ui_frame_bits(source: str, destination: str, info: bytes, digipeaters=(),
                              preamble_flags: int = 50, postamble_flags: int = 3) -> list:
    """Builds the complete bit sequence (preamble flags + bit-stuffed
    address/control/PID/info/FCS + postamble flags) for one AX.25 UI
    frame, ready for _nrzi_encode/_synthesize_afsk_pcm. `source`/
    `destination`/each entry in `digipeaters` may include a "-N" SSID
    suffix. Control=0x03/PID=0xF0 (UI frame, no layer 3) and the CRC-16/
    X-25 FCS are the same verified values parse_ax25_frame/_crc16_x25
    check for on receive.

    preamble_flags/postamble_flags: NOT part of the AX.25/APRS
    protocol itself -- purely a settling margin so the receiving
    radio/TNC's own PLL and squelch have time to lock before real data
    starts (and so the transmission doesn't end exactly as the last
    real bit does). 50 preamble flags is roughly 333ms at 1200 baud,
    in the same ballpark as common real-world TNC TXDELAY defaults
    (e.g. Direwolf's own default is 300ms) -- a reasonable convention,
    not a verified protocol constant, and callers are free to pass a
    different value."""
    addresses = [_encode_address(destination, False)]
    chain = [source] + list(digipeaters)
    for i, callsign in enumerate(chain):
        addresses.append(_encode_address(callsign, i == len(chain) - 1))
    payload = b"".join(addresses) + bytes([0x03, 0xF0]) + info
    crc = _crc16_x25(payload)
    fcs = bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    full_payload = payload + fcs

    flag_bits = _byte_to_bits(FLAG_BYTE)
    data_bits = _bit_stuff(_bytes_to_bits(full_payload))
    return flag_bits * preamble_flags + data_bits + flag_bits * postamble_flags


def build_position_packet_pcm(source: str, destination: str, lat: float, lon: float,
                               symbol_table: str, symbol_code: str, comment: str,
                               sample_rate: int, digipeaters=()) -> bytes:
    """Top-level convenience: builds a complete, ready-to-transmit AFSK
    PCM byte string for one APRS position report. Used by aprs_window.py's
    Send Packet dialog via RadioWorker.send_tx_audio_pcm (radio_worker.py),
    which handles the actual PTT-key/push/unkey sequencing -- this
    function only produces audio bytes, it never touches a radio."""
    info = encode_position_info(lat, lon, symbol_table, symbol_code, comment)
    bits = build_ax25_ui_frame_bits(source, destination, info, digipeaters)
    tones = _nrzi_encode(bits)
    return _synthesize_afsk_pcm(tones, sample_rate)


class AprsDecoder:
    """Feed it raw PCM audio (int16, mono, `sample_rate` Hz) via
    .feed(pcm_bytes) -- returns a list of newly-decoded packets from
    that call (usually empty), each a dict: {"source", "destination",
    "digipeaters", "info": <parsed via parse_aprs_info, or None if the
    info field was empty>}. All state is internal; construct a fresh
    instance to start a new decode session."""

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self._bit_sync = _AfskBitSync(sample_rate)
        self._deframer = _HdlcDeframer()
        self._odd_byte_buffer = bytearray()

    def feed(self, pcm_bytes: bytes) -> list:
        # rigplane's audio chunks are arbitrary sizes, not guaranteed
        # to land on int16 (2-byte) boundaries -- carry a trailing
        # unpaired byte across calls rather than assuming alignment.
        self._odd_byte_buffer.extend(pcm_bytes)
        usable_len = len(self._odd_byte_buffer) - (len(self._odd_byte_buffer) % 2)
        samples = _int16_samples(self._odd_byte_buffer[:usable_len])
        del self._odd_byte_buffer[:usable_len]
        if not samples:
            return []

        bits = self._bit_sync.process(samples)
        raw_frames = self._deframer.process(bits)

        packets = []
        for raw in raw_frames:
            parsed = parse_ax25_frame(raw)
            if parsed is None:
                continue
            parsed["info"] = parse_aprs_info(parsed["info"]) if parsed["info"] else None
            packets.append(parsed)
        return packets


def _int16_samples(block) -> list:
    """Signed 16-bit samples from raw little-endian PCM bytes -- same
    manual unpack rtty.py's own _int16_samples uses, no numpy needed
    for this module now that the AFSK demod is pure-Python Goertzel
    (see _AfskBitSync)."""
    out = []
    for i in range(0, len(block) - 1, 2):
        value = block[i] | (block[i + 1] << 8)
        if value >= 32768:
            value -= 65536
        out.append(value)
    return out
