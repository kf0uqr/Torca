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
    # Un-rstripped destination callsign chars -- ONLY needed for Mic-E
    # (parse_mic_e below), where the destination address field isn't a
    # real callsign at all but 6 digit-substitution-encoded bytes, and
    # a trailing space IS meaningful data (e.g. the Mic-E destination
    # table's own "K space = 1(Custom)" row) that _decode_address's
    # ordinary .rstrip(" ") -- correct for real callsigns -- would
    # otherwise destroy. Every other caller just ignores this key.
    destination_raw = "".join(chr(b >> 1) for b in payload[0:6])
    return {
        "source": source,
        "destination": destination,
        "destination_raw": destination_raw,
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


# ---- Compressed position reports (APRS101.PDF Chapter 9) ------------
#
# An alternative to the plain DDMM.mm lat/lon format, used in place of
# it anywhere a position can appear (plain position reports, Objects,
# Items, weather). Distinguished from plain format by checking whether
# the byte right after the data-type identifier (or after the 7-byte
# timestamp, for '/'/'@') is a digit/space (plain) or '/'/'\' (a
# compressed report's own Symbol Table Identifier standing in that
# position instead) -- the safe, universally-used disambiguation (a
# fuller check also allowing digit/letter overlay characters there
# would be ambiguous with plain format's own leading latitude digit).
#
# Base-91: subtract 33 from each ASCII code. Verified against the
# spec's own full worked example end-to-end: "/5L!!<*e7>7P[" decodes to
# lat 49*30'00"N, lon 72*45'00"W, speed 36.2kt, course 88 degrees,
# symbol '>' (Car) -- see test_aprs.py's test_compressed_position_
# spec_example.

def _base91_decode(chars):
    value = 0
    for ch in chars:
        value = value * 91 + (ord(ch) - 33)
    return value


def _parse_compressed_position(text):
    """text starts at the Symbol Table Identifier ('/' or '\\'),
    exactly the 13-byte compressed field: table_id + 4-char lat +
    4-char lon + symbol_code + 2-char cs + 1-byte T. Returns
    (lat, lon, symbol_table, symbol_code, course_deg_or_None,
    speed_knots_or_None, range_miles_or_None, altitude_ft_or_None,
    consumed_length) or None if text is too short/malformed."""
    if len(text) < 13:
        return None
    sym_table = text[0]
    lat = 90.0 - _base91_decode(text[1:5]) / 380926.0
    lon = -180.0 + _base91_decode(text[5:9]) / 190463.0
    sym_code = text[9]
    c_char, s_char = text[10], text[11]
    t_char = text[12]

    course_deg = speed_knots = range_miles = altitude_ft = None
    if c_char != " ":
        c_val = ord(c_char) - 33
        s_val = ord(s_char) - 33
        if c_char == "{":
            range_miles = 2.0 * (1.08 ** s_val)
        else:
            t_val = ord(t_char) - 33
            # GPS Fix=bit2(0x04 of the 6-bit T value), NMEA Source bits
            # 3-2 (0x18); source==GGA (0b10) carries altitude in cs
            # instead of course/speed, per the spec's own T-byte table.
            nmea_source = (t_val >> 3) & 0x03
            if nmea_source == 0b10:
                altitude_ft = 1.002 ** (c_val * 91 + s_val)
            elif 0 <= c_val <= 89:
                course_deg = c_val * 4
                speed_knots = (1.08 ** s_val) - 1.0

    return sym_table, lat, lon, sym_code, course_deg, speed_knots, range_miles, altitude_ft, 13


def _position_from_compressed(course_deg, speed_knots, range_miles, altitude_ft):
    """Builds a comment_extension-shaped dict from a compressed
    position's own decoded course/speed/range/altitude -- same field
    names parse_comment_extensions() uses for plain-format positions,
    so aprs_window.py's formatting code needs no branch for which
    format produced them."""
    ext = {}
    if course_deg is not None:
        ext["course_deg"] = round(course_deg)
        ext["speed_knots"] = round(speed_knots, 1)
    if range_miles is not None:
        ext["range_miles"] = round(range_miles)
    if altitude_ft is not None:
        ext["altitude_ft"] = round(altitude_ft)
    return ext


# ---- Mic-E (APRS101.PDF Chapter 10) ----------------------------------
#
# Position/course/speed/message-type packed into the AX.25 DESTINATION
# ADDRESS field (6 digit-substitution-encoded bytes, decoded via
# _MIC_E_DEST_TABLE below -- transcribed directly from the spec's own
# table, aprs101.txt lines ~2288-2318) plus the info field (longitude/
# speed/course/symbol via a +28 byte-offset encoding, decoded per the
# spec's own documented algorithm, not just its encoding table).
# Verified against the spec's own full worked example end-to-end:
# destination "T4SQZZ" (lat 33*25.64'N, standard message 1/0/0) + info
# bytes "'(_f n \"Oj/" decodes to lon 112*7.74'W, speed 20kt, course
# 251 degrees, symbol "/j" (Jeep) -- see test_aprs.py's
# test_mic_e_spec_example.

# char: (digit_or_None, message_bit, message_kind, ns_or_None, long_offset_or_None, we_or_None)
# digit_or_None is None for the "space" (ambiguous-digit) rows.
_MIC_E_DEST_TABLE = {}
for _i, _c in enumerate("0123456789"):
    _MIC_E_DEST_TABLE[_c] = (_i, 0, None, "S", 0, "E")
for _i, _c in enumerate("ABCDEFGHIJ"):
    _MIC_E_DEST_TABLE[_c] = (_i, 1, "custom", None, None, None)
_MIC_E_DEST_TABLE["K"] = (None, 1, "custom", None, None, None)
_MIC_E_DEST_TABLE["L"] = (None, 0, None, "S", 0, "E")
for _i, _c in enumerate("PQRSTUVWXY"):
    _MIC_E_DEST_TABLE[_c] = (_i, 1, "std", "N", 100, "W")
_MIC_E_DEST_TABLE["Z"] = (None, 1, "std", "N", 100, "W")
del _i, _c

# (bitA, bitB, bitC) -> (standard code, standard name, custom code, custom name)
_MIC_E_MESSAGE_TYPES = {
    (1, 1, 1): ("M0", "Off Duty", "C0", "Custom-0"),
    (1, 1, 0): ("M1", "En Route", "C1", "Custom-1"),
    (1, 0, 1): ("M2", "In Service", "C2", "Custom-2"),
    (1, 0, 0): ("M3", "Returning", "C3", "Custom-3"),
    (0, 1, 1): ("M4", "Committed", "C4", "Custom-4"),
    (0, 1, 0): ("M5", "Special", "C5", "Custom-5"),
    (0, 0, 1): ("M6", "Priority", "C6", "Custom-6"),
}

_MAIDENHEAD_RE = re.compile(r"^([A-Ra-r]{2}[0-9]{2}(?:[A-Xa-x]{2})?)(?=\s|$)")
_MIC_E_ALTITUDE_RE = re.compile(r"([!-~]{3})\}")


def _mic_e_message_type(bits, kinds):
    if bits == (0, 0, 0):
        return "Emergency"
    entry = _MIC_E_MESSAGE_TYPES.get(bits)
    if entry is None:
        return "Unknown"
    std_code, std_name, custom_code, custom_name = entry
    kinds_used = {k for k, b in zip(kinds, bits) if b}
    if kinds_used == {"std"}:
        return f"{std_code}: {std_name}"
    if kinds_used == {"custom"}:
        return f"{custom_code}: {custom_name}"
    return "Unknown"  # a mixture of Standard 1s and Custom 1s -- spec's own "unknown" case


def _decode_mic_e_destination(destination_raw):
    """destination_raw: the UN-rstripped 6 chars of the AX.25
    destination address (see parse_ax25_frame's own docstring for why
    rstripped won't do). Returns (lat, message_type_str, long_offset,
    we) or None if any of the 6 chars isn't in the Mic-E table at all
    (not a real Mic-E frame) or the position-defining bytes 4-6 came
    back ambiguous in a way that leaves N/S, longitude offset, or E/W
    undetermined."""
    if len(destination_raw) < 6:
        return None
    rows = []
    for ch in destination_raw[:6].upper():
        entry = _MIC_E_DEST_TABLE.get(ch)
        if entry is None:
            return None
        rows.append(entry)
    digits = [row[0] for row in rows]
    bits = tuple(row[1] for row in rows[:3])
    kinds = tuple(row[2] for row in rows[:3])
    ns, long_offset, we = rows[3][3], rows[4][4], rows[5][5]
    if ns is None or long_offset is None or we is None:
        return None
    message_type = _mic_e_message_type(bits, kinds)
    lat_digit_str = "".join(str(d) if d is not None else "0" for d in digits)
    lat = int(lat_digit_str[0:2]) + float(f"{lat_digit_str[2:4]}.{lat_digit_str[4:6]}") / 60.0
    if ns == "S":
        lat = -lat
    return lat, message_type, long_offset, we


def _decode_mic_e_longitude(d_char, m_char, h_char, long_offset, we):
    d = ord(d_char) - 28
    if long_offset == 100:
        d += 100
    if 180 <= d <= 189:
        d -= 80
    elif 190 <= d <= 199:
        d -= 190
    m = ord(m_char) - 28
    if m >= 60:
        m -= 60
    hundredths = ord(h_char) - 28
    lon = d + (m + hundredths / 100.0) / 60.0
    return -lon if we == "W" else lon


def _decode_mic_e_speed_course(sp_char, dc_char, se_char):
    sp = ord(sp_char) - 28
    dc = ord(dc_char) - 28
    se = ord(se_char) - 28
    speed = sp * 10 + dc // 10
    course = (dc % 10) * 100 + se
    if speed >= 800:
        speed -= 800
    if course >= 400:
        course -= 400
    return speed, course


def _parse_mic_e_telemetry(rest):
    """rest starts at the Telemetry Flag byte (the byte right after
    the Symbol Table Identifier). Returns {"channels": {1: v, ...}} or
    None if the flag/body don't match one of the spec's 3 documented
    shapes."""
    if not rest:
        return None
    flag = rest[0]
    body = rest[1:]
    try:
        if flag == "`" and len(body) >= 4:
            return {"channels": {1: int(body[0:2], 16), 3: int(body[2:4], 16)}}
        if flag == "'" and len(body) >= 10:
            values = [int(body[i:i + 2], 16) for i in range(0, 10, 2)]
            return {"channels": {i + 1: v for i, v in enumerate(values)}}
        if flag == "\x1d" and len(body) >= 5:
            return {"channels": {i + 1: ord(c) for i, c in enumerate(body[:5])}}
    except ValueError:
        pass
    return None


def _parse_mic_e_status_text(rest):
    """Everything after byte 9 (when it's not telemetry, per parse_mic_e)
    -- plain status text, optionally carrying a Kenwood device-type
    marker as its first char, a Maidenhead locator, and/or an altitude
    ("xxx}", base-91-in-3-chars relative to 10km below sea level -- a
    DIFFERENT encoding from parse_comment_extensions's own plain-feet
    "/A=nnnnnn", since Mic-E's info field is byte-offset-encoded
    throughout, not free text). Returns (comment_text, device_or_None,
    grid_square_or_None, altitude_ft_or_None)."""
    device = None
    if rest[:1] == ">":
        device, rest = "Kenwood TH-D7", rest[1:]
    elif rest[:1] == "]":
        device, rest = "Kenwood TM-D700", rest[1:]

    grid_square = None
    grid_match = _MAIDENHEAD_RE.match(rest)
    if grid_match:
        grid_square = grid_match.group(1).upper()

    altitude_ft = None
    alt_match = _MIC_E_ALTITUDE_RE.search(rest)
    if alt_match:
        alt_m = _base91_decode(alt_match.group(1)) - 10000
        altitude_ft = round(alt_m * 3.28084)

    return rest, device, grid_square, altitude_ft


def parse_mic_e(destination_raw: str, info_bytes: bytes):
    """Decodes a Mic-E position report. See this section's own module-
    level comment for the verified worked example. Returns a dict
    shaped exactly like a plain position report (same "type": "position"
    -- so it automatically gets symbol-name translation, map plotting,
    and Details formatting with no changes anywhere else) plus
    "source_format": "mic_e" and "mic_e_message_type". Returns None if
    destination_raw/info_bytes don't decode as valid Mic-E data."""
    dest = _decode_mic_e_destination(destination_raw)
    if dest is None:
        return None
    lat, message_type, long_offset, we = dest

    try:
        text = info_bytes.decode("latin-1")  # some Mic-E info bytes are non-ASCII offset values, not plain text
    except Exception:
        return None
    # Spec: "if the Information field appears to be less than 9 bytes
    # long, the packet must be ignored."
    if len(text) < 9 or text[0] not in ("'", "`", "\x1c", "\x1d"):
        return None

    try:
        lon = _decode_mic_e_longitude(text[1], text[2], text[3], long_offset, we)
        speed_knots, course_deg = _decode_mic_e_speed_course(text[4], text[5], text[6])
        sym_code, sym_table = text[7], text[8]
    except (ValueError, IndexError):
        return None

    rest = text[9:]
    telemetry = None
    comment = ""
    device = grid_square = None
    mic_e_altitude_ft = None
    if rest:
        if rest[0] in ("`", "'", "\x1d"):
            telemetry = _parse_mic_e_telemetry(rest)
        if telemetry is None:
            comment, device, grid_square, mic_e_altitude_ft = _parse_mic_e_status_text(rest)

    comment_extension = {"course_deg": course_deg, "speed_knots": speed_knots}
    if mic_e_altitude_ft is not None:
        comment_extension["altitude_ft"] = mic_e_altitude_ft

    result = {
        "type": "position", "has_timestamp": False,
        "lat": lat, "lon": lon,
        "symbol_table": sym_table, "symbol_code": sym_code,
        "comment": comment,
        "comment_extension": comment_extension,
        "source_format": "mic_e",
        "mic_e_message_type": message_type,
    }
    if device is not None:
        result["mic_e_device"] = device
    if grid_square is not None:
        result["mic_e_grid_square"] = grid_square
    if telemetry is not None:
        result["mic_e_telemetry"] = telemetry
    return result


# ---- Status reports (APRS101.PDF Chapter 16) -------------------------

# H code (heading/10) -> degrees: '0'-'9' = 0-90 in 10-degree steps,
# then 'A'-'Z' = 100-350 in 10-degree steps (aprs101.txt:4192-4200).
_BEAM_HEADING_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# ERP code -> watts (aprs101.txt:4202-4218), transcribed directly from
# the spec's own table.
_ERP_TABLE = {
    "1": 10, "2": 40, "3": 90, "4": 160, "5": 250, "6": 360, "7": 490, "8": 640, "9": 810,
    ":": 1000, ";": 1210, "<": 1440, "=": 1690, ">": 1960, "?": 2250, "@": 2560, "A": 2890,
    "B": 3240, "C": 3610, "D": 4000, "E": 4410, "F": 4840, "G": 5290, "H": 5760, "I": 6250,
    "J": 6760, "K": 7290,
}
_BEAM_HEADING_RE = re.compile(r"\^([0-9A-Za-z])([1-9:-K])$")


def _parse_status_report(text):
    """text starts right after the '>' data type identifier."""
    timestamp = None
    body = text
    # A DHMz timestamp is 7 bytes ending in 'z' -- position reports use
    # the same shape (see _parse_latitude's callers), reused here.
    if len(text) >= 7 and text[6] == "z" and text[0:6].isdigit():
        timestamp, body = text[0:7], text[7:]

    beam_heading_deg = erp_watts = None
    match = _BEAM_HEADING_RE.search(body)
    if match:
        heading_char, erp_char = match.group(1).upper(), match.group(2)
        if heading_char in _BEAM_HEADING_CHARS and erp_char in _ERP_TABLE:
            beam_heading_deg = _BEAM_HEADING_CHARS.index(heading_char) * 10
            erp_watts = _ERP_TABLE[erp_char]

    return {
        "type": "status", "timestamp": timestamp, "text": body,
        "beam_heading_deg": beam_heading_deg, "erp_watts": erp_watts,
    }


# ---- Object / Item reports (APRS101.PDF Chapter 11) ------------------

def _parse_position_and_comment(text):
    """text starts right at either a plain position's latitude digits
    or a compressed position's Symbol Table Identifier -- shared by
    Object/Item report parsing (which embed exactly this shape after
    their own name/live-kill/timestamp header) and could equally serve
    plain position parsing, though that stays inline in parse_aprs_info
    for its own existing, already-tested call sites. Returns (lat, lon,
    symbol_table, symbol_code, comment, comment_extension) or None."""
    if not text:
        return None
    if text[0] in ("/", "\\"):
        parsed = _parse_compressed_position(text)
        if parsed is None:
            return None
        sym_table, lat, lon, sym_code, course, speed, rng, alt, consumed = parsed
        comment = text[consumed:]
        ext = _position_from_compressed(course, speed, rng, alt)
        ext.update(parse_comment_extensions(comment))  # a compressed position's own comment can ALSO carry e.g. /A=
        return lat, lon, sym_table, sym_code, comment, ext
    if len(text) < 19:
        return None
    try:
        lat = _parse_latitude(text[0:8])
        sym_table = text[8]
        lon = _parse_longitude(text[9:18])
        sym_code = text[18]
    except (ValueError, IndexError):
        return None
    comment = text[19:]
    return lat, lon, sym_table, sym_code, comment, parse_comment_extensions(comment)


def _parse_object_report(text):
    """text starts right after the ';' data type identifier."""
    if len(text) < 18:
        return None
    name = text[0:9]
    live_char = text[9]
    if live_char not in ("*", "_"):
        return None
    timestamp = text[10:17]
    position = _parse_position_and_comment(text[17:])
    if position is None:
        return None
    lat, lon, sym_table, sym_code, comment, ext = position
    return {
        "type": "position", "has_timestamp": True,
        "lat": lat, "lon": lon,
        "symbol_table": sym_table, "symbol_code": sym_code,
        "comment": comment, "comment_extension": ext,
        "source_format": "object",
        "object_name": name.rstrip(), "object_live": live_char == "*",
        "object_timestamp": timestamp,
    }


def _parse_item_report(text):
    """text starts right after the ')' data type identifier. Unlike
    Object names (fixed 9 chars), Item names are 3-9 variable-length
    chars, terminated by the live/kill marker itself -- so the marker
    has to be searched for rather than read from a fixed offset."""
    for end in range(3, min(10, len(text))):
        if text[end] in ("!", "_"):
            name, live_char = text[:end], text[end]
            position = _parse_position_and_comment(text[end + 1:])
            if position is None:
                return None
            lat, lon, sym_table, sym_code, comment, ext = position
            return {
                "type": "position", "has_timestamp": False,
                "lat": lat, "lon": lon,
                "symbol_table": sym_table, "symbol_code": sym_code,
                "comment": comment, "comment_extension": ext,
                "source_format": "item",
                "object_name": name, "object_live": live_char == "!",
            }
    return None


# ---- Messages (APRS101.PDF Chapter 14) --------------------------------

_TELEMETRY_DEFINITION_PREFIXES = {
    "PARM.": "parameter_names", "UNIT.": "units_labels",
    "EQNS.": "equation_coefficients", "BITS.": "bit_sense",
}


def _parse_message(text):
    """text starts right after the ':' data type identifier."""
    if len(text) < 10 or text[9] != ":":
        return None
    addressee = text[0:9].rstrip()
    body = text[10:]

    if body[:3] == "ack" and body[3:8].strip():
        return {"type": "message_ack", "addressee": addressee, "message_id": body[3:8].strip()}
    if body[:3] == "rej" and body[3:8].strip():
        return {"type": "message_reject", "addressee": addressee, "message_id": body[3:8].strip()}

    for prefix, kind in _TELEMETRY_DEFINITION_PREFIXES.items():
        if body.startswith(prefix):
            fields = body[len(prefix):].split(",")
            return {
                "type": "telemetry_definition", "kind": kind,
                "addressee": addressee, "raw_fields": fields,
            }

    message_id = None
    if "{" in body:
        text_part, _, id_part = body.rpartition("{")
        if 1 <= len(id_part) <= 5 and id_part.isalnum():
            body, message_id = text_part, id_part
    return {"type": "message", "addressee": addressee, "text": body, "message_id": message_id}


# ---- Telemetry (APRS101.PDF Chapter 13) -------------------------------

def _parse_telemetry(text):
    """text starts right after the 'T' data type identifier."""
    if len(text) < 5 or text[0] != "#":
        return None
    sequence = text[1:4]
    rest = text[4:].lstrip(",")
    fields = rest.split(",")
    if len(fields) < 6:
        return None
    try:
        analog = [int(fields[i]) for i in range(5)]
    except ValueError:
        return None
    digital_str = fields[5][:8]
    if len(digital_str) < 8 or not all(c in "01" for c in digital_str):
        return None
    digital = [c == "1" for c in digital_str]
    return {"type": "telemetry", "sequence": sequence, "analog": analog, "digital": digital}


# ---- Complete Weather Reports (APRS101.PDF Chapter 12) ----------------
#
# Piggyback on an already-parsed position report whose symbol is the
# Weather Station symbol ('_', in either table) -- see the call site in
# parse_aprs_info. The comment's own leading 7-byte Data Extension slot
# (already parsed as course/speed by parse_comment_extensions) is
# reinterpreted as WIND direction/speed instead (same "ddd/sss" byte
# shape, aprs101.txt:3363-3370); the rest of the comment is scanned for
# the documented single-letter+fixed-width fields, in any order,
# stopping at the first unrecognized byte (leftover software-type/
# unit-type/free text, shown verbatim as part of the still-unmodified
# raw comment, not further decoded). Verified against the spec's own
# example: "_10090556c220s004g005t077r000p000P000h50b09900wRSW".

_WEATHER_FIELD_RE = re.compile(
    r"(?:g(?P<gust>\d{3}))|"
    r"(?:t(?P<temp>-?\d{1,3}))|"
    r"(?:r(?P<rain_hr>\d{3}))|"
    r"(?:p(?P<rain_24h>\d{3}))|"
    r"(?:P(?P<rain_midnight>\d{3}))|"
    r"(?:h(?P<humidity>\d{2}))|"
    r"(?:b(?P<pressure>\d{5}))|"
    r"(?:L(?P<lum_low>\d{3}))|"
    r"(?:l(?P<lum_high>\d{3}))|"
    r"(?:s(?P<snow>\d{3}))"
)


def _parse_weather_fields(comment):
    """Scans comment (a position report's own comment text, AFTER any
    leading Data Extension bytes) for the documented weather fields,
    consuming consecutive matches from the start and stopping at the
    first byte that isn't a recognized field (leftover software-type/
    unit-type codes or free text -- not parsed further)."""
    fields = {}
    pos = 0
    while pos < len(comment):
        match = _WEATHER_FIELD_RE.match(comment, pos)
        if not match:
            break
        for name, value in match.groupdict().items():
            if value is not None:
                fields[name] = int(value)
        pos = match.end()
    if not fields:
        return None
    result = {}
    if "gust" in fields:
        result["gust_mph"] = fields["gust"]
    if "temp" in fields:
        result["temp_f"] = fields["temp"]
    if "rain_hr" in fields:
        result["rain_last_hr_in"] = fields["rain_hr"] / 100.0
    if "rain_24h" in fields:
        result["rain_24hr_in"] = fields["rain_24h"] / 100.0
    if "rain_midnight" in fields:
        result["rain_since_midnight_in"] = fields["rain_midnight"] / 100.0
    if "humidity" in fields:
        result["humidity_pct"] = 100 if fields["humidity"] == 0 else fields["humidity"]
    if "pressure" in fields:
        result["pressure_mbar"] = fields["pressure"] / 10.0
    if "snow" in fields:
        result["snow_24hr_in"] = fields["snow"]
    return result


def _attach_weather_if_present(position_dict):
    """Mutates position_dict in place, adding a "weather" key if its
    symbol is the Weather Station symbol and its comment contains
    recognizable weather fields -- called from every position-report
    code path in parse_aprs_info (plain, compressed, Mic-E, Object,
    Item) so weather-carrying positions from any of them get
    translated the same way, regardless of which wire format carried
    them."""
    if position_dict.get("symbol_code") != "_":
        return position_dict
    ext = position_dict.get("comment_extension") or {}
    wind_deg, wind_speed = ext.get("course_deg"), ext.get("speed_knots")
    comment = position_dict.get("comment", "")
    # The wind ddd/sss slot occupies the SAME leading 7 comment bytes
    # parse_comment_extensions already consumed to produce course_deg/
    # speed_knots above -- skip past it before scanning for the
    # letter-coded fields, or the scan starts mid-digit-string and
    # matches nothing (confirmed live: without this it silently found
    # zero fields on the spec's own worked example).
    field_text = comment[7:] if wind_deg is not None else comment
    weather = _parse_weather_fields(field_text)
    if wind_deg is None and weather is None:
        return position_dict
    weather = dict(weather) if weather else {}
    if wind_deg is not None:
        weather["wind_deg"] = wind_deg
        weather["wind_speed_mph"] = wind_speed  # wind speed's own unit is mph, not knots, despite reusing the course/speed byte shape
    position_dict["weather"] = weather
    return position_dict


def parse_aprs_info(info_bytes: bytes, destination_raw: str = None):
    """Parses an APRS info field. Structured support for: plain and
    compressed position reports (data type '!'/'='/'/'/'@'), Mic-E
    position reports (data type '`'/"'" -- needs destination_raw, the
    UN-rstripped AX.25 destination address chars, see parse_ax25_frame),
    Object/Item reports (';'/')'),  status reports ('>'), messages/acks/
    rejects/telemetry-definitions (':'), telemetry ('T'), and Complete
    Weather Reports (any position whose symbol is Weather Station '_').
    Everything else (raw/positionless weather station dumps, capability
    queries, third-party/tunneled packets, user-defined data -- see
    aprs.py's own module docstring for the deliberate scope decision on
    each) comes back as {"type": "other", "raw": <decoded text>} rather
    than guessing an unverified layout.

    Position report dict (from ANY of the position-shaped sources
    above): {"type": "position", "has_timestamp": bool, "lat": float,
    "lon": float, "symbol_table": str, "symbol_code": str, "comment":
    str, "comment_extension": dict, "weather": dict|absent}. Non-plain
    sources add "source_format" ("mic_e"/"object"/"item") plus their
    own extra fields (see parse_mic_e/_parse_object_report/_parse_item_
    report's own docstrings). comment_extension is parse_comment_
    extensions(comment)'s own return for plain-format positions (see
    its docstring); comment itself always stays the full, untouched
    raw text regardless of what was found. Returns None if info_bytes
    is empty or not decodable as text at all."""
    if not info_bytes:
        return None
    try:
        text = info_bytes.decode("ascii", errors="replace")
    except Exception:
        return None

    data_type = text[0]
    try:
        if data_type in ("!", "="):
            if len(text) > 1 and text[1] in ("/", "\\"):
                parsed = _parse_compressed_position(text[1:])
                if parsed is not None:
                    sym_table, lat, lon, sym_code, course, speed, rng, alt, consumed = parsed
                    comment = text[1 + consumed:]
                    ext = _position_from_compressed(course, speed, rng, alt)
                    ext.update(parse_comment_extensions(comment))
                    return _attach_weather_if_present({
                        "type": "position", "has_timestamp": False,
                        "lat": lat, "lon": lon,
                        "symbol_table": sym_table, "symbol_code": sym_code,
                        "comment": comment, "comment_extension": ext,
                        "source_format": "compressed",
                    })
            lat = _parse_latitude(text[1:9])
            sym_table = text[9]
            lon = _parse_longitude(text[10:19])
            sym_code = text[19]
            comment = text[20:]
            return _attach_weather_if_present({
                "type": "position", "has_timestamp": False,
                "lat": lat, "lon": lon,
                "symbol_table": sym_table, "symbol_code": sym_code,
                "comment": comment,
                "comment_extension": parse_comment_extensions(comment),
            })
        if data_type in ("/", "@"):
            if len(text) > 8 and text[8] in ("/", "\\"):
                parsed = _parse_compressed_position(text[8:])
                if parsed is not None:
                    sym_table, lat, lon, sym_code, course, speed, rng, alt, consumed = parsed
                    comment = text[8 + consumed:]
                    ext = _position_from_compressed(course, speed, rng, alt)
                    ext.update(parse_comment_extensions(comment))
                    return _attach_weather_if_present({
                        "type": "position", "has_timestamp": True,
                        "lat": lat, "lon": lon,
                        "symbol_table": sym_table, "symbol_code": sym_code,
                        "comment": comment, "comment_extension": ext,
                        "source_format": "compressed",
                    })
            lat = _parse_latitude(text[8:16])
            sym_table = text[16]
            lon = _parse_longitude(text[17:26])
            sym_code = text[26]
            comment = text[27:]
            return _attach_weather_if_present({
                "type": "position", "has_timestamp": True,
                "lat": lat, "lon": lon,
                "symbol_table": sym_table, "symbol_code": sym_code,
                "comment": comment,
                "comment_extension": parse_comment_extensions(comment),
            })
        if data_type in ("`", "'") and destination_raw:
            mic_e = parse_mic_e(destination_raw, info_bytes)
            if mic_e is not None:
                return _attach_weather_if_present(mic_e)
        if data_type == ">":
            return _parse_status_report(text[1:])
        if data_type == ";":
            obj = _parse_object_report(text[1:])
            if obj is not None:
                return _attach_weather_if_present(obj)
        if data_type == ")":
            item = _parse_item_report(text[1:])
            if item is not None:
                return _attach_weather_if_present(item)
        if data_type == ":":
            message = _parse_message(text[1:])
            if message is not None:
                return message
        if data_type == "T":
            telemetry = _parse_telemetry(text[1:])
            if telemetry is not None:
                return telemetry
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
            parsed["info"] = (
                parse_aprs_info(parsed["info"], parsed.get("destination_raw"))
                if parsed["info"] else None
            )
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
