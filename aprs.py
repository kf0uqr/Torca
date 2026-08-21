"""
APRS (Automatic Packet Reporting System) decoder: 1200-baud Bell 202
AFSK over AX.25/HDLC framing, the standard VHF packet configuration
(144.390 MHz in North America). Decode only -- no transmit -- for the
same reason cw.py/rtty.py's decode-only siblings are: sending needs
AFSK tone generation over the radio's TX audio path, infrastructure
this app doesn't have for any mode yet.

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


def _parse_ax25_frame(payload: bytes):
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
    str, "comment": str}. Returns None if info_bytes is empty or not
    decodable as text at all."""
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
            }
    except (ValueError, IndexError):
        pass  # malformed -- fall through to the generic raw-text form
    return {"type": "other", "raw": text}


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
            parsed = _parse_ax25_frame(raw)
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
