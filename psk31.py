"""
PSK31 (BPSK31) send AND decode: 31.25-baud differentially-encoded
binary phase-shift-keyed text mode, the classic amateur-radio "PSK31"
you hear as two close-together warbling tones inside a narrow ~60 Hz
slice of an SSB passband.

Protocol facts below are verified directly against the ARRL's own
official PSK31 spec page (https://www.arrl.org/psk31-spec, fetched via
curl + HTML-stripping, not summarized) and cross-checked against a
real, working encoder/decoder writeup with worked C# reference code
(Scott Harden, "Experiments in PSK-31 Synthesis",
https://swharden.com/blog/2022-10-16-psk31-synthesis/) -- same
"fetch the primary source directly, don't guess" discipline this
project already applies to APRS (aprs.py), the FCC band plan
(band_plan.py), and RepeaterBook's CSV format (repeater_import.py).

Protocol:
  - 31.25 baud, one bit per symbol (no QPSK/FEC -- see below).
  - Differential encoding: 0 = phase reversal (180 degrees) relative
    to the previous symbol, 1 = no change (steady carrier). "The
    codes are transmitted left bit first, with 0 representing a phase
    reversal ... and 1 representing a steady carrier" (ARRL spec).
  - Varicode: 1-10 bit self-synchronizing codes for all 128 ASCII
    characters (NUL-DEL). No code contains two consecutive zero bits
    and none starts or ends with a 0 bit, so "two or more consecutive
    zero bits" unambiguously marks the gap between characters --
    shorter codes go to more common characters (space=1, the single
    shortest code; 'e'=11, the next-shortest). The table below is
    transcribed verbatim from a real, tested PSK31 implementation
    (bitglue/gr-psk31's generate_varicodes.py, a GNU Radio PSK31
    block), spot-checked against the ARRL spec's own table (NUL,
    space, 'e', 'a' all match).
  - Preamble: continuous 0 bits (continuous phase reversals) before
    the message. Postamble: continuous 1 bits (steady carrier) after.
  - TX output is amplitude-shaped (a raised-cosine/sine envelope dip
    to near-zero exactly at each phase transition) specifically to
    avoid the spectral splatter a hard, full-amplitude phase jump
    would cause -- required by the spec, and the reason PSK31 is
    narrow enough to fit dozens of QSOs in one SSB channel's worth of
    space.

Scope: BPSK only (QPSK + convolutional/Viterbi FEC is an optional,
materially more complex add-on mode per the spec's own Section 3-5 --
a 32-entry phase-shift lookup table plus a Viterbi decoder with an
800ms one-way delay -- not implemented here, matching how most
PSK31 software treats it: negotiated, not required for a basic
contact).
"""

import math

import numpy as np

BAUD = 31.25
DEFAULT_CENTER_HZ = 1000.0  # a conventional PSK31 audio offset within an SSB passband, not a protocol constant -- adjustable in the UI to match wherever the operator is actually tuned

# Verified 128-entry Varicode table (NUL through DEL), transcribed
# directly from bitglue/gr-psk31's generate_varicodes.py -- see this
# module's own docstring for provenance. Keys are the code's bit
# pattern as an int (transmitted left-bit-first == MSB-first, and
# since no code ever starts with a 0 bit, converting straight to/from
# a Python int loses no information).
_VARICODE_DECODE = {
    0b1010101011: '\x00', 0b1011011011: '\x01',
    0b1011101101: '\x02', 0b1101110111: '\x03',
    0b1011101011: '\x04', 0b1101011111: '\x05',
    0b1011101111: '\x06', 0b1011111101: '\x07',
    0b1011111111: '\x08', 0b11101111: '\x09',
    0b11101: '\x0A', 0b1101101111: '\x0B',
    0b1011011101: '\x0C', 0b11111: '\x0D',
    0b1101110101: '\x0E', 0b1110101011: '\x0F',
    0b1011110111: '\x10', 0b1011110101: '\x11',
    0b1110101101: '\x12', 0b1110101111: '\x13',
    0b1101011011: '\x14', 0b1101101011: '\x15',
    0b1101101101: '\x16', 0b1101010111: '\x17',
    0b1101111011: '\x18', 0b1101111101: '\x19',
    0b1110110111: '\x1A', 0b1101010101: '\x1B',
    0b1101011101: '\x1C', 0b1110111011: '\x1D',
    0b1011111011: '\x1E', 0b1101111111: '\x1F',
    0b1: ' ', 0b111111111: '!',
    0b101011111: '"', 0b111110101: '#',
    0b111011011: '$', 0b1011010101: '%',
    0b1010111011: '&', 0b101111111: "'",
    0b11111011: '(', 0b11110111: ')',
    0b101101111: '*', 0b111011111: '+',
    0b1110101: ',', 0b110101: '-',
    0b1010111: '.', 0b110101111: '/',
    0b10110111: '0', 0b10111101: '1',
    0b11101101: '2', 0b11111111: '3',
    0b101110111: '4', 0b101011011: '5',
    0b101101011: '6', 0b110101101: '7',
    0b110101011: '8', 0b110110111: '9',
    0b11110101: ':', 0b110111101: ';',
    0b111101101: '<', 0b1010101: '=',
    0b111010111: '>', 0b1010101111: '?',
    0b1010111101: '@', 0b1111101: 'A',
    0b11101011: 'B', 0b10101101: 'C',
    0b10110101: 'D', 0b1110111: 'E',
    0b11011011: 'F', 0b11111101: 'G',
    0b101010101: 'H', 0b1111111: 'I',
    0b111111101: 'J', 0b101111101: 'K',
    0b11010111: 'L', 0b10111011: 'M',
    0b11011101: 'N', 0b10101011: 'O',
    0b11010101: 'P', 0b111011101: 'Q',
    0b10101111: 'R', 0b1101111: 'S',
    0b1101101: 'T', 0b101010111: 'U',
    0b110110101: 'V', 0b101011101: 'W',
    0b101110101: 'X', 0b101111011: 'Y',
    0b1010101101: 'Z', 0b111110111: '[',
    0b111101111: '\\', 0b111111011: ']',
    0b1010111111: '^', 0b101101101: '_',
    0b1011011111: '`', 0b1011: 'a',
    0b1011111: 'b', 0b101111: 'c',
    0b101101: 'd', 0b11: 'e',
    0b111101: 'f', 0b1011011: 'g',
    0b101011: 'h', 0b1101: 'i',
    0b111101011: 'j', 0b10111111: 'k',
    0b11011: 'l', 0b111011: 'm',
    0b1111: 'n', 0b111: 'o',
    0b111111: 'p', 0b110111111: 'q',
    0b10101: 'r', 0b10111: 's',
    0b101: 't', 0b110111: 'u',
    0b1111011: 'v', 0b1101011: 'w',
    0b11011111: 'x', 0b1011101: 'y',
    0b111010101: 'z', 0b1010110111: '{',
    0b110111011: '|', 0b1010110101: '}',
    0b1011010111: '~', 0b1110110101: '\x7F',
}
_VARICODE_ENCODE = {char: bin(code)[2:] for code, char in _VARICODE_DECODE.items()}

_PREAMBLE_BITS = 20  # repeated 0s -- matches the verified swharden.com reference implementation
_POSTAMBLE_BITS = 20  # repeated 1s
_CHAR_SEPARATOR_BITS = "00"  # spec requires >=2 consecutive zero bits between characters


def text_to_varicode_bits(text: str) -> str:
    """Converts `text` to a '0'/'1' string: preamble + each character's
    Varicode code (separated by _CHAR_SEPARATOR_BITS) + postamble.
    Characters with no Varicode mapping (anything outside 7-bit ASCII)
    are silently skipped, same "skip what the table doesn't cover"
    convention rtty.py's text_to_baudot_codes already uses."""
    bits = ["0"] * _PREAMBLE_BITS
    for char in text:
        code = _VARICODE_ENCODE.get(char)
        if code is None:
            continue
        bits.append(code)
        bits.append(_CHAR_SEPARATOR_BITS)
    bits.append("1" * _POSTAMBLE_BITS)
    return "".join(bits)


# ---- Send (encode) ----
#
# Amplitude-envelope-shaped differential BPSK synthesis, following the
# verified algorithm/structure from swharden.com's "Experiments in
# PSK-31 Synthesis" (see this module's docstring): a continuous carrier
# whose PHASE steps by 0 or pi at each symbol boundary (differential
# encoding), with an AMPLITUDE envelope that dips to near-zero only in
# the immediate neighborhood of an actual phase transition -- each
# symbol's envelope is one lobe of a sine curve spanning the whole
# symbol, applied to whichever half of the symbol borders a real
# transition (the other half, or the whole symbol if there's no
# transition on either side, stays at full amplitude). This is what
# makes the classic "two offset tones" sound of a PSK31 preamble (the
# envelope's own ~15.6 Hz sine component) and is what keeps the
# transmitted bandwidth narrow.
_AMPLITUDE = 12000  # matches rtty.py/aprs.py's own synthesized-tone peak amplitude convention


def _differential_phases(bits: str) -> list:
    """bits: a '0'/'1' string. Returns a list of phase offsets (0.0 or
    math.pi), one per bit, per the spec's differential rule: 0 = phase
    reversal, 1 = steady (unchanged) carrier. Starts from an assumed
    phase-0 idle carrier, matching swharden's reference (phase1=0)."""
    phases = []
    prev_phase = 0.0
    for bit in bits:
        if bit == "1":
            phase = prev_phase
        else:
            phase = math.pi if prev_phase == 0.0 else 0.0
        phases.append(phase)
        prev_phase = phase
    return phases


def build_psk31_pcm(text: str, sample_rate: int, center_hz: float = DEFAULT_CENTER_HZ) -> bytes:
    """Top-level convenience: builds complete, ready-to-transmit BPSK31
    PCM (int16 mono) for `text` -- preamble, Varicode-encoded/
    differentially-phase-encoded/envelope-shaped characters, postamble.
    Raises ValueError if `text` has no Varicode-mappable characters."""
    bits = text_to_varicode_bits(text)
    if bits.strip("01") != "" or len(bits) <= _PREAMBLE_BITS + _POSTAMBLE_BITS:
        raise ValueError("No sendable characters (Varicode has no mapping for this text).")
    phases = _differential_phases(bits)
    n_symbols = len(phases)
    samples_per_symbol = sample_rate / BAUD
    boundaries = [round(i * samples_per_symbol) for i in range(n_symbols + 1)]
    total_samples = boundaries[-1]
    wave = np.zeros(total_samples, dtype=np.float64)
    angular = 2.0 * math.pi * center_hz / sample_rate

    for i in range(n_symbols):
        start, end = boundaries[i], boundaries[i + 1]
        length = end - start
        if length <= 0:
            continue
        n = np.arange(length)
        carrier = np.cos(angular * (start + n) + phases[i])
        env = np.ones(length, dtype=np.float64)
        prev_same = i > 0 and phases[i - 1] == phases[i]
        next_same = i < n_symbols - 1 and phases[i + 1] == phases[i]
        half = length // 2
        # A full-symbol sine hump (envelope(t) = sin(pi*(t+0.5)/length))
        # -- ramp-up half applied only if this symbol's start is a real
        # transition, ramp-down half only if its end is.
        if not prev_same and half > 0:
            t = np.arange(half)
            env[:half] = np.sin((t + 0.5) * math.pi / length)
        if not next_same and half > 0:
            t = np.arange(length - half, length)
            env[length - half:] = np.sin((t + 0.5) * math.pi / length)
        wave[start:end] = carrier * env

    pcm = np.clip(wave * _AMPLITUDE, -32767, 32767).astype(np.int16)
    return pcm.tobytes()


# ---- Decode ----
#
# Differential detection via a per-symbol matched-filter phase
# measurement, standard technique for PSK31 (see e.g. the "Simple
# BPSK31 decoding with Python" writeup swharden.com itself links to):
# mix the incoming audio down to baseband against a local oscillator
# at center_hz, then for each symbol integrate (correlate) over the
# CENTRE portion of that symbol's samples only -- by construction
# (build_psk31_pcm's own envelope shaping above) amplitude is
# always at its full, undistorted value at a symbol's midpoint whether
# or not that symbol borders a phase transition, so measuring there
# avoids the envelope-dip distortion right at the edges. Comparing
# each symbol's measured phase against the previous symbol's recovers
# the differentially-encoded bit directly, with no need to ever know
# the absolute carrier phase.
#
# Symbol-clock timing uses a free-running estimate (cumulative-target-
# sample-count rounding, the same technique aprs.py/rtty.py's own
# encoders already use to avoid long-run drift) rather than an
# adaptive PLL/Costas loop -- acceptable given typical soundcard clock
# accuracy over a single QSO's duration; a full carrier/clock-recovery
# loop is out of scope for this first version (see psk31.py's module
# docstring on QPSK/FEC also being out of scope).
class Psk31Decoder:
    """Feed it raw PCM audio (int16, mono, `sample_rate` Hz) via
    .feed(pcm_bytes) -- returns any newly-decoded text from that call
    (usually empty; a character once a full self-synchronizing
    Varicode code has been assembled). All state is internal;
    construct a fresh instance (or call .reset()) to start a new
    decode session."""

    # Fraction of each symbol, centered on its midpoint, used for the
    # phase-measurement integration window -- narrow enough to stay
    # clear of the envelope's ramped edges even on an adjacent-
    # transition symbol, wide enough to average out a useful amount of
    # noise.
    _MEASURE_FRACTION = 0.4

    # SNR-style detection threshold, not a simple peak-relative
    # squelch: a peak-relative threshold doesn't work here because pure
    # noise sets its OWN peak (confirmed empirically -- an earlier
    # "25% of the slowly-tracked peak magnitude" version still let
    # plenty of pure white noise through, since noise's own random
    # fluctuations regularly exceed 25% of a peak that noise itself
    # established). Instead, compare the coherent correlation magnitude
    # against what pure noise of the SAME measured power in that same
    # window would be expected to produce by chance: for N uncorrelated
    # samples of power P mixed against a unit-power local oscillator,
    # the correlation magnitude is a random-walk of expected size
    # roughly sqrt(N * P / 2); a real, phase-locked BPSK carrier's
    # magnitude instead grows linearly with N, so it clears a multiple
    # of that noise-floor estimate by a wide, growing margin. Confirmed
    # empirically (this module's own test script): a real signal at
    # noise_std up to ~1500 (vs. a 12000-amplitude carrier, roughly
    # 18dB SNR) decodes perfectly; heavier noise degrades gracefully
    # (partial or empty decode, never garbage or a crash) rather than
    # cleanly, since startup sync acquisition anchors on the preamble
    # -- whose own envelope is weaker on average than steady mid-
    # message data (every preamble symbol borders a transition on BOTH
    # sides, so its envelope is a full sine hump rather than staying at
    # full amplitude for most of the symbol). Pure noise with no signal
    # at all produces zero spurious characters at any noise level
    # tested.
    _DETECTION_SNR_MULTIPLIER = 6.0

    # How many consecutive symbols' worth of correlation magnitude to
    # sum when scoring one candidate symbol-boundary offset during
    # startup sync acquisition -- see _try_acquire_sync. More symbols
    # gives a more reliable score (rejects a lucky one-off noise peak)
    # at the cost of needing more buffered audio before a decision.
    _SYNC_SEARCH_SYMBOLS = 16
    # Candidate offsets are tried every this-many samples across one
    # whole symbol period -- coarse (not sample-exact) on purpose: the
    # _MEASURE_FRACTION measurement window already tolerates a few
    # samples of misalignment, so a fine per-sample search would just
    # be many times slower for no real accuracy gain.
    _SYNC_CANDIDATE_STEP = 4
    # Safety cap on how large the pre-lock buffer is allowed to grow
    # while no signal has been acquired yet (e.g. the operator started
    # decoding on a dead frequency) -- old samples are dropped from the
    # front once exceeded, so a long-idle decoder doesn't grow without
    # bound; new sync attempts just use whatever's left.
    _MAX_UNLOCKED_BUFFER_SYMBOLS = _SYNC_SEARCH_SYMBOLS * 4
    # How many consecutive below-threshold symbols in a row count as a
    # real, lasting signal dropout (worth dropping the lock and re-
    # running startup sync) rather than just one noisy symbol on an
    # otherwise-good signal -- see _process_symbol.
    _MAX_CONSECUTIVE_MISSES = 20

    def __init__(self, sample_rate: int, center_hz: float = DEFAULT_CENTER_HZ):
        self.sample_rate = sample_rate
        self.center_hz = center_hz
        self.reset()

    def reset(self):
        self._odd_byte_buffer = bytearray()
        # Persistent rolling buffer of not-yet-consumed samples, plus
        # the absolute sample index of self._sample_buffer[0] -- unlike
        # RttyDecoder's whole-block-at-a-time consumption, PSK31 symbol
        # boundaries (samples_per_symbol is generally non-integer) don't
        # line up with feed()-call chunk boundaries, so leftover samples
        # after the last complete symbol in one feed() call MUST carry
        # over into the next rather than being dropped.
        self._sample_buffer = np.zeros(0, dtype=np.float64)
        self._buffer_start_index = 0
        self._next_boundary_target = 0.0  # fractional absolute sample count -- cumulative-rounding symbol clock
        self._pending_bits = []  # bit values accumulated since the last recognized character
        self._prev_phase = None  # previous symbol's measured phase (radians), or None before the first real symbol
        # True once _try_acquire_sync has found a symbol-boundary
        # offset that actually clears the SNR gate -- see that method's
        # own docstring for why this step exists at all (an incoming
        # audio stream is never guaranteed to start aligned with the
        # transmitting station's own symbol clock).
        self._locked = False
        self._consecutive_misses = 0

    def feed(self, pcm_bytes: bytes) -> str:
        self._odd_byte_buffer.extend(pcm_bytes)
        usable_len = len(self._odd_byte_buffer) - (len(self._odd_byte_buffer) % 2)
        new_samples = np.frombuffer(bytes(self._odd_byte_buffer[:usable_len]), dtype="<i2").astype(np.float64)
        del self._odd_byte_buffer[:usable_len]
        self._sample_buffer = np.concatenate([self._sample_buffer, new_samples])

        if not self._locked:
            self._try_acquire_sync()
            if not self._locked:
                self._trim_unlocked_buffer()
                return ""

        samples_per_symbol = self.sample_rate / BAUD
        out = []
        while True:
            start_abs = self._next_boundary_target
            end_abs = start_abs + samples_per_symbol
            start_idx = round(start_abs) - self._buffer_start_index
            end_idx = round(end_abs) - self._buffer_start_index
            if end_idx > len(self._sample_buffer):
                break  # not enough samples buffered yet for a whole symbol -- wait for more
            char = self._process_symbol(round(start_abs), round(end_abs), start_idx, end_idx)
            if char:
                out.append(char)
            self._next_boundary_target = end_abs

        trim_idx = round(self._next_boundary_target) - self._buffer_start_index
        if trim_idx > 0:
            self._sample_buffer = self._sample_buffer[trim_idx:]
            self._buffer_start_index += trim_idx
        return "".join(out)

    def _trim_unlocked_buffer(self):
        max_len = round(self.sample_rate / BAUD * self._MAX_UNLOCKED_BUFFER_SYMBOLS)
        if len(self._sample_buffer) > max_len:
            drop = len(self._sample_buffer) - max_len
            self._sample_buffer = self._sample_buffer[drop:]
            self._buffer_start_index += drop

    def _try_acquire_sync(self):
        """Brute-force search for the symbol-boundary phase offset (0..
        one symbol period) that best lines up with whatever real BPSK
        signal is present in the buffered audio, if any -- there is no
        reason to expect the moment .feed() starts being called lines
        up with the remote station's own symbol clock. Scores each
        candidate offset by the total correlation magnitude across
        _SYNC_SEARCH_SYMBOLS consecutive symbols measured at that
        offset (a correctly-aligned grid measures each symbol's
        undistorted, full-amplitude midpoint every time and so scores
        much higher than a misaligned one, which straddles a mix of
        two different phases -- and adjacent phases -- every "symbol"
        and partially cancels out). Only actually locks (sets self.
        _locked = True and plants self._next_boundary_target at the
        winning offset) if that best-scoring offset's own SNR clears
        the same detection gate _process_symbol uses -- otherwise
        there's no real signal to sync to yet (e.g. a dead frequency,
        or genuinely still just noise), and this returns having
        changed nothing, ready to be retried once more audio arrives."""
        samples_per_symbol = self.sample_rate / BAUD
        symbol_samples = round(samples_per_symbol)
        needed = round(samples_per_symbol * (self._SYNC_SEARCH_SYMBOLS + 1))
        if len(self._sample_buffer) < needed:
            return
        step = max(1, self._SYNC_CANDIDATE_STEP)
        best_score = -1.0
        best_offset = 0
        best_expected_noise = 0.0
        for offset in range(0, max(1, symbol_samples), step):
            score = 0.0
            expected_noise_sum = 0.0
            ok = True
            for i in range(self._SYNC_SEARCH_SYMBOLS):
                start_abs = self._buffer_start_index + offset + i * samples_per_symbol
                end_abs = start_abs + samples_per_symbol
                start_idx = round(start_abs) - self._buffer_start_index
                end_idx = round(end_abs) - self._buffer_start_index
                if end_idx > len(self._sample_buffer):
                    ok = False
                    break
                magnitude, _phase, noise_power = self._measure_symbol(start_idx, end_idx, round(start_abs), round(end_abs))
                score += magnitude
                expected_noise_sum += math.sqrt(max(noise_power, 1e-9) * (end_idx - start_idx) / 2.0)
            if ok and score > best_score:
                best_score = score
                best_offset = offset
                best_expected_noise = expected_noise_sum
        if best_score < 0:
            return
        # Gate on the AGGREGATE magnitude/noise across the whole
        # _SYNC_SEARCH_SYMBOLS window, not a single re-measured symbol
        # -- the very first symbol of an otherwise-correctly-aligned
        # winning offset can legitimately be near-silent (e.g. it lands
        # right at the tail of a leading silence/dead-air gap, one
        # symbol before the real transmission actually starts), which
        # a single-symbol re-check would wrongly read as "just noise"
        # and refuse to lock even though the timing it found is right.
        if best_score < self._DETECTION_SNR_MULTIPLIER * best_expected_noise:
            return  # winning candidate is still just noise -- nothing real to lock onto yet
        self._next_boundary_target = float(self._buffer_start_index + best_offset)
        self._prev_phase = None
        self._pending_bits = []
        self._locked = True

    def _measure_symbol(self, start_idx, end_idx, start_abs, end_abs):
        """Returns (correlation_magnitude, phase_radians, noise_floor_
        power) for the centre _MEASURE_FRACTION of the symbol spanning
        [start_idx, end_idx) in self._sample_buffer (start_abs/end_abs
        are the same span's absolute sample indices, needed to keep
        the local-oscillator phase continuous across the whole
        session). Shared by both _try_acquire_sync's candidate scoring
        and _process_symbol's real bit measurement so the two always
        agree on exactly what "this symbol's measurement" means."""
        length = end_idx - start_idx
        if length <= 1:
            return 0.0, 0.0, 0.0
        margin = int(length * (1.0 - self._MEASURE_FRACTION) / 2.0)
        m_start_idx, m_end_idx = start_idx + margin, end_idx - margin
        m_start_abs, m_end_abs = start_abs + margin, end_abs - margin
        if m_end_idx <= m_start_idx:
            m_start_idx, m_end_idx = start_idx, end_idx
            m_start_abs, m_end_abs = start_abs, end_abs
        window = self._sample_buffer[m_start_idx:m_end_idx]
        n_idx = np.arange(m_start_abs, m_end_abs)
        lo = np.exp(-1j * 2.0 * math.pi * self.center_hz / self.sample_rate * n_idx)
        correlation = np.sum(window * lo)
        noise_floor_power = float(np.mean(window * window))
        return abs(correlation), math.atan2(correlation.imag, correlation.real), noise_floor_power

    def _process_symbol(self, start_abs, end_abs, start_idx, end_idx) -> str:
        magnitude, phase, noise_floor_power = self._measure_symbol(start_idx, end_idx, start_abs, end_abs)
        length = end_idx - start_idx
        expected_noise_magnitude = math.sqrt(max(noise_floor_power, 1e-9) * length / 2.0)
        if magnitude < self._DETECTION_SNR_MULTIPLIER * expected_noise_magnitude:
            # Not enough above what pure noise of this same power would
            # produce by chance -- could just be one noisy symbol on an
            # otherwise-solid signal (confirmed empirically: real noise
            # at a moderate level occasionally dips a single symbol
            # below the gate even while the message overall decodes
            # fine), so don't drop the lock on the very first miss.
            # Only treat it as a genuine, lasting dropout -- worth a
            # full re-acquisition, since the old timing grid might no
            # longer be trustworthy either -- once several consecutive
            # symbols in a row have missed.
            self._consecutive_misses += 1
            if self._consecutive_misses >= self._MAX_CONSECUTIVE_MISSES:
                self._locked = False
                self._prev_phase = None
            return ""
        self._consecutive_misses = 0

        if self._prev_phase is None:
            self._prev_phase = phase
            return ""  # first real symbol only establishes the reference phase
        diff = phase - self._prev_phase
        diff = math.atan2(math.sin(diff), math.cos(diff))  # wrap to [-pi, pi]
        self._prev_phase = phase
        bit = "1" if math.cos(diff) > 0 else "0"
        return self._on_bit(bit)

    def _on_bit(self, bit: str) -> str:
        if bit == "0" and len(self._pending_bits) > 0 and self._pending_bits[-1] == "0":
            # Two consecutive zeros -- character separator (or part of
            # the preamble). Decode whatever bits were pending before
            # the first of these two zeros, then clear.
            code_bits = "".join(self._pending_bits[:-1])
            self._pending_bits = []
            if not code_bits:
                return ""
            char = _VARICODE_DECODE.get(int(code_bits, 2))
            return char or ""
        self._pending_bits.append(bit)
        return ""
