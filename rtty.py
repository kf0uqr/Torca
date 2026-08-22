"""
RTTY (radioteletype) decode AND send: 45.45-baud Baudot/ITA2 FSK, the
standard amateur-radio RTTY configuration.

Send: build_rtty_pcm(text, sample_rate) synthesizes continuous-phase
mark/space AFSK PCM (same technique as aprs.py's build_position_packet_
pcm -- cumulative-target-sample-count rounding so per-bit rounding
error doesn't accumulate into real drift over a long message), handed
to RadioWorker.send_tx_audio_pcm -- the same generic PTT-key/push-PCM/
unkey primitive APRS send already uses; nothing here is APRS-specific,
this module just became its second consumer.

Demodulation: a quadrature (IQ) FM discriminator -- the same standard
DSP technique sstv.py's FmDemodulator already uses for its continuous
image-tone tracking, re-tuned here for RTTY's much narrower 170 Hz
mark/space shift (own implementation, not an import -- the two need
very different filter parameters: sstv.py's is tuned for a ~700 Hz-
wide sweep with a fast-settling short filter; RTTY needs a much
narrower passband to resolve two tones only 170 Hz apart, closer to a
real FSK terminal unit's discriminator). A first attempt at comparing
two independent Goertzel detectors (cw.py's GoertzelDetector, one per
tone) per audio block was tried and confirmed NOT to work: either the
block is short enough for good timing resolution and its frequency
resolution is too coarse to tell 2125 Hz from 2295 Hz apart at all, or
it's long enough for good frequency resolution and the resulting
smearing/lag across bit transitions corrupts the framing (both
confirmed via test_rtty.py before switching to this approach) -- an FM
discriminator doesn't have that tradeoff, since a proper lowpass filter
gives both good selectivity AND settles within a small fraction of one
bit period.

Standard parameters (verified against public references, not guessed --
same "public standard, not project-specific" reasoning cw.py's
MORSE_CODE_TABLE and sstv.py's VIS codes document for themselves):
mark 2125 Hz / space 2295 Hz (170 Hz shift) is the near-universal
amateur AFSK RTTY tone pair; 45.45 baud (~22.0 ms/bit) with 1 start bit
+ 5 Baudot/ITA2 data bits + 1.5 stop bits is the standard asynchronous
framing. Baudot/ITA2 code table (US-TTY figures variant) per Wikipedia's
"Baudot code" article.
"""

import math
import struct

import numpy as np

MARK_HZ = 2125.0
SPACE_HZ = 2295.0
CENTER_HZ = (MARK_HZ + SPACE_HZ) / 2.0   # 2210.0 -- discriminator's own LO/threshold reference
BAUD = 45.45
BIT_MS = 1000.0 / BAUD          # ~22.0 ms
STOP_BITS = 1.5                  # standard ham RTTY framing (1 start + 5 data + 1.5 stop)
_FRAME_BITS = 1 + 5              # start + 5 data -- stop is checked separately, see below

# Baudot/ITA2, indexed by the 5-bit value formed from the data bits in
# TRANSMISSION order (first-received bit = LSB) -- matches the standard
# table directly, no bit-reversal needed. FIGS/LTRS are shift markers,
# not printable characters; None is NUL (index 0) or BEL (figures index
# 5), both non-printable and simply produce no output.
_LETTERS = [
    None, "E", "\n", "A", " ", "S", "I", "U",
    "\r", "D", "R", "J", "N", "F", "C", "K",
    "T", "Z", "L", "W", "H", "Y", "P", "Q",
    "O", "B", "G", "FIGS", "M", "X", "V", "LTRS",
]
_FIGURES = [
    None, "3", "\n", "-", " ", None, "8", "7",
    "\r", "$", "4", "'", ",", "!", ":", "(",
    "5", '"', ")", "2", "#", "6", "0", "1",
    "9", "?", "&", "FIGS", ".", "/", ";", "LTRS",
]

# Reverse lookup for encode (send) -- character -> 5-bit code. FIGS/
# LTRS are excluded here (they're control codes, inserted by
# text_to_baudot_codes as needed, never looked up directly). Note
# space/CR/LF sit at the SAME index in both _LETTERS and _FIGURES
# (indices 4/8/2), so they're transmittable without a shift either
# way -- confirmed by inspection of the two tables above, not
# incidental.
_LETTERS_REV = {ch: i for i, ch in enumerate(_LETTERS) if ch not in (None, "FIGS", "LTRS")}
_FIGURES_REV = {ch: i for i, ch in enumerate(_FIGURES) if ch not in (None, "FIGS", "LTRS")}
FIGS_CODE = _LETTERS.index("FIGS")  # 27 -- same value in both tables (control codes, not shift-dependent)
LTRS_CODE = _LETTERS.index("LTRS")  # 31


class _FmDiscriminator:
    """Continuous instantaneous-frequency estimator via quadrature (IQ)
    downconversion, centered on RTTY's mark/space midpoint -- mixes the
    raw PCM signal down against a complex LO at CENTER_HZ, lowpass-
    filters to isolate the baseband component, then takes the
    derivative of the unwrapped phase to get frequency directly (an
    all-numpy substitute for scipy.signal.hilbert). See this module's
    own docstring for why this replaced an earlier two-Goertzel-
    detector attempt.

    Stateful across process() calls -- carries the LO phase, FIR filter
    tail, and last unwrapped phase value across arbitrary chunk
    boundaries, since rigplane's audio chunks are not fixed-size."""

    _HALF_BANDWIDTH_HZ = 150.0  # passes the +-85 Hz mark/space deviation with margin for the baud rate's own bandwidth
    _FIR_TAPS = 63               # long enough for a clean ~150 Hz cutoff, short enough to settle well within one ~22ms bit

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self._fir = self._design_lowpass_fir(sample_rate, self._HALF_BANDWIDTH_HZ, self._FIR_TAPS)
        self._filter_tail = np.zeros(len(self._fir) - 1, dtype=np.complex128)
        self._sample_index = 0
        self._last_phase = None

    @staticmethod
    def _design_lowpass_fir(sample_rate, half_bandwidth_hz, taps):
        """Windowed-sinc lowpass, precomputed once -- standard FIR
        design technique, not project-specific."""
        cutoff = half_bandwidth_hz / (sample_rate / 2.0)
        m = np.arange(taps) - (taps - 1) / 2.0
        h = np.sinc(cutoff * m) * cutoff
        h = h * np.hamming(taps)
        total = np.sum(h)
        if total != 0:
            h = h / total
        return h.astype(np.float64)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """samples: 1-D array of PCM samples (int16 or float), any
        length including zero. Returns an array of the same length:
        estimated instantaneous frequency in Hz per input sample."""
        n = len(samples)
        if n == 0:
            return np.zeros(0, dtype=np.float64)
        idx = self._sample_index + np.arange(n)
        lo = np.exp(-1j * 2.0 * np.pi * CENTER_HZ / self.sample_rate * idx)
        iq = samples.astype(np.float64) * lo
        padded = np.concatenate([self._filter_tail, iq])
        filtered_full = np.convolve(padded, self._fir, mode="full")
        start = len(self._fir) - 1
        filtered = filtered_full[start:start + n]
        tail_len = len(self._fir) - 1
        self._filter_tail = padded[-tail_len:] if tail_len > 0 else np.zeros(0, dtype=np.complex128)

        phase = np.unwrap(np.angle(filtered))
        if self._last_phase is None:
            phase_with_prev = np.concatenate([[phase[0]], phase])
        else:
            offset_cycles = round((self._last_phase - phase[0]) / (2.0 * np.pi))
            phase = phase + offset_cycles * 2.0 * np.pi
            phase_with_prev = np.concatenate([[self._last_phase], phase])
        freq = np.diff(phase_with_prev) * self.sample_rate / (2.0 * np.pi)

        self._last_phase = phase[-1]
        self._sample_index += n
        return CENTER_HZ + freq


class RttyDecoder:
    """Feed it raw PCM audio (int16, mono, `sample_rate` Hz) via
    .feed(pcm_bytes) -- returns any newly-decoded text from that call
    (usually empty; a character once a full, validly-framed 5-bit code
    has been sampled). All state is internal; construct a fresh
    instance (or call .reset()) to start a new decode session.

    Async-serial edge-triggered design, same as a real UART/RTTY
    terminal unit: idles watching for a mark->space transition (the
    start bit), then samples each subsequent bit at its expected
    midpoint relative to that edge -- not by continuously tracking a
    long-running clock, so timing jitter in one character never
    accumulates into the next; every character re-syncs off its own
    start-bit edge. A start bit that doesn't actually read as space, or
    a stop-bit check that doesn't read as mark, means this was a false
    trigger (noise, or mid-character corruption) -- the whole frame is
    silently discarded rather than emitting a wrong character, and the
    decoder resumes watching for the next real transition.
    """

    # Each step of the framing state machine consumes one "block" of
    # discriminator output, sized this many ms -- fine enough to
    # resolve BIT_MS (~22ms) reliably, coarse enough to keep the
    # (cheap, numpy-vectorized) per-block majority vote meaningful
    # against any brief discriminator noise right at a transition.
    _BLOCK_MS = 1.0

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self._discriminator = _FmDiscriminator(sample_rate)
        self._block_samples = max(1, round(sample_rate * self._BLOCK_MS / 1000.0))
        self._sample_buffer = np.zeros(0, dtype=np.int16)
        self._odd_byte_buffer = bytearray()  # holds a trailing unpaired byte across feed() calls
        self.reset()

    # Blocks to ignore for edge detection right after construction/
    # reset, before ever checking for a mark->space transition --
    # _FmDiscriminator's FIR filter hasn't settled yet on the very
    # first block or two (its internal state starts at zero, not
    # matched to whatever tone is actually playing), which can read as
    # a bogus, momentary opposite-polarity glitch even on a perfectly
    # steady tone. Confirmed via test_rtty.py: without this, that one
    # glitch falsely triggers full framing before the real signal has
    # even started, and the ~166ms it then wastes on a bogus frame
    # desyncs the decoder from every real character that follows.
    _PRIMING_BLOCKS = 20

    def reset(self):
        self._shift = "LTRS"      # current Baudot shift state -- LTRS or FIGS
        self._prev_is_mark = True  # idle line state is mark
        self._state = "IDLE"       # IDLE, FRAMING, STOP_WAIT
        self._bit_clock_ms = 0.0   # elapsed ms since the current frame's start-bit edge
        self._next_sample_ms = 0.0  # when (relative to the edge) to sample the next bit
        self._bit_index = 0        # 0=start bit, 1..5=data bits (LSB first)
        self._sampled_bits = []    # collected polarities (True=mark/1) for this frame
        self._priming_remaining = self._PRIMING_BLOCKS
        self._sample_buffer = np.zeros(0, dtype=np.int16)
        self._odd_byte_buffer = bytearray()

    def feed(self, pcm_bytes: bytes) -> str:
        # rigplane's audio chunks are arbitrary sizes, not guaranteed
        # to land on int16 (2-byte) boundaries -- carry a trailing
        # unpaired byte across calls rather than assuming alignment.
        self._odd_byte_buffer.extend(pcm_bytes)
        usable_len = len(self._odd_byte_buffer) - (len(self._odd_byte_buffer) % 2)
        new_samples = np.frombuffer(bytes(self._odd_byte_buffer[:usable_len]), dtype="<i2")
        del self._odd_byte_buffer[:usable_len]
        self._sample_buffer = np.concatenate([self._sample_buffer, new_samples])
        out = []
        while len(self._sample_buffer) >= self._block_samples:
            block = self._sample_buffer[:self._block_samples]
            self._sample_buffer = self._sample_buffer[self._block_samples:]
            freq = self._discriminator.process(block)
            # Majority vote across the block -- mark(2125Hz) is BELOW
            # CENTER_HZ, space(2295Hz) is above.
            is_mark = bool(np.mean(freq < CENTER_HZ) >= 0.5)
            char = self._process_block(is_mark)
            if char:
                out.append(char)
        return "".join(out)

    def _process_block(self, is_mark: bool) -> str:
        if self._priming_remaining > 0:
            self._priming_remaining -= 1
            self._prev_is_mark = is_mark
            return ""
        result = ""
        if self._state == "IDLE":
            if self._prev_is_mark and not is_mark:
                # Falling edge (mark -> space): candidate start bit
                # begins right here (time 0 for this frame).
                self._state = "FRAMING"
                self._bit_clock_ms = 0.0
                self._bit_index = 0
                self._sampled_bits = []
                self._next_sample_ms = 0.5 * BIT_MS
        elif self._state == "FRAMING":
            self._bit_clock_ms += self._BLOCK_MS
            if self._bit_clock_ms >= self._next_sample_ms:
                self._sampled_bits.append(is_mark)
                self._bit_index += 1
                if self._bit_index > _FRAME_BITS - 1:
                    # Collected the start bit + all 5 data bits --
                    # move on to the stop-bit sanity check instead of
                    # sampling further data bits.
                    self._state = "STOP_WAIT"
                else:
                    self._next_sample_ms = (self._bit_index + 0.5) * BIT_MS
        elif self._state == "STOP_WAIT":
            self._bit_clock_ms += self._BLOCK_MS
            # Stop-bit sanity check at the middle of the first stop
            # unit -- a real UART framing-error check, not just cosmetic:
            # rejects frames where a false start-bit trigger (noise) or
            # mid-character corruption left the line in the wrong state.
            # Returns to IDLE the SAME block this fires (not after
            # waiting out the rest of the 1.5 stop bits) -- confirmed
            # via test_rtty.py that waiting the full nominal frame
            # duration before re-arming is actually less reliable, not
            # more: BIT_MS (~22.0ms) isn't an exact multiple of
            # _BLOCK_MS, so that wait can overshoot the real next-
            # character edge by a block or two, and since IDLE only
            # checks for transitions (never mid-STOP_WAIT), overshooting
            # the edge means missing it entirely -- the decoder would
            # then resync on some unrelated later transition inside the
            # NEXT character's own data bits instead. Returning to IDLE
            # right away is safe: the remaining ~0.5 stop bit is
            # guaranteed real mark in a clean signal, plenty for IDLE to
            # observe prev_is_mark=True at least once before the actual
            # next edge arrives.
            stop_check_ms = (_FRAME_BITS + 0.5) * BIT_MS
            if self._bit_clock_ms >= stop_check_ms:
                stop_ok = is_mark
                result = self._decode_frame(stop_ok)
                self._state = "IDLE"
        self._prev_is_mark = is_mark
        return result

    def _decode_frame(self, stop_ok: bool) -> str:
        start_bit_is_mark = self._sampled_bits[0]
        if start_bit_is_mark or not stop_ok:
            return ""  # not a real start bit, or bad framing -- discard silently
        data_bits = self._sampled_bits[1:6]
        value = sum((1 if bit else 0) << i for i, bit in enumerate(data_bits))
        table = _LETTERS if self._shift == "LTRS" else _FIGURES
        symbol = table[value]
        if symbol == "LTRS":
            self._shift = "LTRS"
            return ""
        if symbol == "FIGS":
            self._shift = "FIGS"
            return ""
        return symbol or ""


# ---- Send (encode) ----
#
# Idle-mark padding before/after the real character frames -- NOT part
# of the RTTY protocol itself, purely a settling margin so the
# receiving terminal unit/decoder has steady mark tone to lock onto
# before real data starts, and so the transmission doesn't end exactly
# as the last real bit does. Same reasoning as aprs.py's
# preamble_flags/postamble_flags (see its own docstring) -- a
# reasonable convention, not a verified protocol constant.
_PREAMBLE_MS = 300.0
_POSTAMBLE_MS = 200.0

# Peak amplitude for synthesized tones -- matches aprs.py's own
# build_position_packet_pcm (12000 of int16's 32767 max, comfortable
# headroom against clipping).
_AMPLITUDE = 12000


def text_to_baudot_codes(text: str) -> list:
    """Converts `text` to a list of 5-bit Baudot/ITA2 codes (0-31),
    inserting FIGS_CODE/LTRS_CODE shift codes wherever the current
    shift state doesn't already cover the next character. Characters
    with no Baudot mapping (lowercase is uppercased first; anything
    still unmapped -- e.g. most non-ASCII input) are silently skipped,
    same "skip what the table doesn't cover" convention cw.
    estimate_cw_send_duration_ms already uses for unsupported
    punctuation. Starts (and, if any shift codes were emitted, doesn't
    necessarily end) in LTRS, matching RttyDecoder.reset()'s own
    self._shift = "LTRS" starting assumption."""
    codes = []
    shift = "LTRS"
    for char in text.upper():
        in_letters = char in _LETTERS_REV
        in_figures = char in _FIGURES_REV
        if not in_letters and not in_figures:
            continue
        if in_letters and in_figures:
            # Same code in both tables (space/CR/LF) -- no shift needed.
            codes.append(_LETTERS_REV[char])
            continue
        required_shift = "LTRS" if in_letters else "FIGS"
        if required_shift != shift:
            codes.append(LTRS_CODE if required_shift == "LTRS" else FIGS_CODE)
            shift = required_shift
        codes.append(_LETTERS_REV[char] if in_letters else _FIGURES_REV[char])
    return codes


def _synthesize_fsk_pcm(units: list, sample_rate: int) -> bytes:
    """units: list of (is_mark: bool, duration_units: float) -- one
    entry per bit-equivalent segment, duration in BAUD unit-periods
    (1.0 = one bit, 1.5 for the stop-bit segment). Continuous-phase
    synthesis with cumulative-target-sample-count rounding (tracks
    total elapsed units, not a fresh round() per segment) so per-
    segment rounding error doesn't accumulate into real drift over a
    long message -- same technique, same reason, as aprs.py's
    _synthesize_afsk_pcm."""
    samples_per_unit = sample_rate / BAUD
    phase = 0.0
    out = []
    emitted = 0.0
    cumulative_units = 0.0
    for is_mark, duration_units in units:
        freq = MARK_HZ if is_mark else SPACE_HZ
        cumulative_units += duration_units
        target_total = cumulative_units * samples_per_unit
        n = round(target_total - emitted)
        emitted += n
        angular = 2.0 * math.pi * freq / sample_rate
        for _ in range(n):
            out.append(int(round(_AMPLITUDE * math.sin(phase))))
            phase += angular
    return struct.pack(f"<{len(out)}h", *out)


def build_rtty_pcm(text: str, sample_rate: int) -> bytes:
    """Top-level convenience: builds complete, ready-to-transmit AFSK
    PCM (int16 mono) for `text` -- idle-mark preamble, one start+5-
    data+1.5-stop-bit frame per Baudot code (mark=1/space=0 per bit,
    matching RttyDecoder._decode_frame's own bit-value convention),
    idle-mark postamble. Raises ValueError if `text` contains nothing
    with a Baudot mapping (nothing useful to send)."""
    codes = text_to_baudot_codes(text)
    if not codes:
        raise ValueError("No sendable characters (Baudot/ITA2 has no mapping for this text).")
    units = [(True, _PREAMBLE_MS / BIT_MS)]
    for code in codes:
        units.append((False, 1.0))  # start bit: always space
        for bit_index in range(5):
            bit = (code >> bit_index) & 1
            units.append((bool(bit), 1.0))  # LSB-first, mark=1/space=0
        units.append((True, STOP_BITS))  # stop: always mark
    units.append((True, _POSTAMBLE_MS / BIT_MS))
    return _synthesize_fsk_pcm(units, sample_rate)
