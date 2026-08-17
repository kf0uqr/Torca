"""
SSTV (Slow Scan Television) decoder -- pure Python + numpy, no Qt
dependency, mirroring cw.py's independence (see that module's own
docstring for the same design rationale: independently testable
against synthesized signals, all Qt/threading plumbing lives in the
caller).

Reuses cw.py's GoertzelDetector directly (a well-known, decades-old
DSP technique, not project-specific) for discrete on/off tone
classification: the VIS (Vertical Interval Signalling) mode-ID header,
and per-line horizontal sync pulse detection/re-anchoring. One-
directional dependency -- cw.py is untouched and has no idea sstv.py
exists.

For the actual image data -- a continuously-varying tone across
1500-2300 Hz, unlike CW's simple on/off single-tone signal -- this
module adds FmDemodulator: quadrature (IQ) downconversion + a lowpass
FIR + phase-derivative frequency discriminator, an all-numpy substitute
for scipy.signal.hilbert (scipy isn't a dependency here, only numpy
is, added specifically for this module). Standard technique, not
specific to this codebase.

Protocol timing constants (VIS codes, sync/porch/channel durations)
are the public, universally-implemented SSTV standard -- cross-checked
against real published implementations (xdsopl/robot36's decoder
source for Robot 36, dnet/pySSTV's encoder source for Martin M1 and
Scottie S1) rather than guessed, same "public standard, not project-
specific" framing cw.py uses for MORSE_CODE_TABLE.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from cw import GoertzelDetector

# ---- Standard SSTV tone frequencies (Hz) ---------------------------------
FREQ_VIS_LEADER = 1900.0
FREQ_SYNC = 1200.0          # also the VIS start/stop-bit and break tone
FREQ_VIS_BIT_1 = 1100.0     # binary 1
FREQ_VIS_BIT_0 = 1300.0     # binary 0
FREQ_BLACK = 1500.0         # 0 luminance/chrominance
FREQ_WHITE = 2300.0         # 255 luminance/chrominance

# ---- VIS (Vertical Interval Signalling) header timing (ms) ---------------
_VIS_LEADER_MS = 300.0
_VIS_BREAK_MS = 10.0
_VIS_BIT_MS = 30.0
# Real-world tolerance on the two 300ms leader tones -- accept anywhere
# from 70% of nominal up, since a hard 300ms-or-nothing cutoff would be
# too brittle against real audio-path timing jitter.
_VIS_LEADER_MIN_MS = _VIS_LEADER_MS * 0.7

# Block granularity for VIS-header tone classification -- matches
# cw.py's own ~4ms choice, fine enough for 30ms VIS bit windows.
_BLOCK_MS = 4.0

# How many consecutive expected line starts can fail to find a sync
# pulse before the whole transmission is treated as lost (see
# SstvDecoder._consume_sync_segment / _abort_image).
_MAX_CONSECUTIVE_SYNC_MISSES = 3


def freq_to_value(freq_hz) -> "int | np.ndarray":
    """Standard SSTV luminance/chrominance mapping: 1500 Hz = 0 (black),
    2300 Hz = 255 (white/full chroma), linear in between, clamped.
    Works on a scalar or a numpy array. Same formula for every channel
    of every mode -- not mode-specific."""
    value = (freq_hz - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK) * 255.0
    return np.clip(value, 0.0, 255.0)


def value_to_freq(value) -> "float | np.ndarray":
    """Inverse of freq_to_value. Used by the test-only synthetic
    encoder (test_sstv.py) -- kept here so both directions of the
    mapping live in one place rather than risking drift between them."""
    return FREQ_BLACK + (np.asarray(value) / 255.0) * (FREQ_WHITE - FREQ_BLACK)


# ---- Mode table: table-driven, adding a mode later is a new row ---------

@dataclass(frozen=True)
class Segment:
    kind: str                    # "sync" | "porch" | "channel"
    duration_ms: float
    channel_index: int | None = None   # index into SstvModeSpec.channel_order, for kind=="channel"


@dataclass(frozen=True)
class SstvModeSpec:
    name: str
    vis_code: int
    width: int
    height: int
    color_space: str              # "rgb" | "robot_yuv"
    channel_order: tuple
    segments: tuple                # tuple[Segment, ...], one line's worth, in transmission order
    line_ms: float                 # sum(segments' durations) -- computed, not hand-duplicated


def _line_ms(segments):
    return sum(s.duration_ms for s in segments)


# Robot 36 -- values from xdsopl/robot36's decode.c (a real, working
# open-source SSTV decoder): sync_porch_sec=0.003, porch_sec=0.0015,
# y_sec=0.088, uv_sec=0.044, hor_sec=0.15, hor_sync_sec=0.009,
# seperator_sec=0.0045 -- these sum to exactly 0.150s (9+3+88+4.5+1.5+44
# = 150), matching the independently-reported ~150ms/line total.
#
# VIS code: the source compares the received byte against 0x88, which
# is the plain 7-bit code 0x08 (0001000) with its even-parity bit
# already folded in as the MSB (0001000 has one set bit, needs
# parity=1 for even parity -> 10001000 = 0x88). This module keeps VIS
# codes as the plain 7-bit value and checks parity as a separate step
# (see _VisDetector), so 0x08 is what belongs here.
#
# Chroma channel (R-Y vs B-Y) alternates by line parity -- a real,
# documented characteristic of Robot 36's 4:2:0-like chroma
# subsampling. The "separator" segment's own tone frequency also
# encodes which chroma channel follows on real hardware (1500 Hz vs
# 2300 Hz); this decoder uses line parity instead, which is simpler
# and matches the alternation exactly as long as no line is dropped.
_ROBOT36_SEGMENTS = (
    Segment("sync", 9.0),
    Segment("porch", 3.0),
    Segment("channel", 88.0, channel_index=0),   # Y
    Segment("porch", 4.5),                        # "separator"
    Segment("porch", 1.5),
    Segment("channel", 44.0, channel_index=1),   # R-Y or B-Y, alternating by line parity
)
ROBOT36 = SstvModeSpec(
    name="Robot 36", vis_code=0x08, width=320, height=240,
    color_space="robot_yuv", channel_order=("Y", "C"),
    segments=_ROBOT36_SEGMENTS, line_ms=_line_ms(_ROBOT36_SEGMENTS),
)

# Martin M1 -- values from dnet/pySSTV's color.py (a real, working
# open-source SSTV encoder): SYNC=4.862, SCAN=146.432 (per channel),
# INTER_CH_GAP=0.572, VIS_CODE=0x2c, channel order green/blue/red.
# Verified: 4.862 + 4*0.572 + 3*146.432 = 446.446 ms, matching the
# independently-reported Martin M1 total line time (four gap instances:
# one after sync, one between each pair of channels).
_MARTIN_M1_SEGMENTS = (
    Segment("sync", 4.862),
    Segment("porch", 0.572),
    Segment("channel", 146.432, channel_index=0),  # G
    Segment("porch", 0.572),
    Segment("channel", 146.432, channel_index=1),  # B
    Segment("porch", 0.572),
    Segment("channel", 146.432, channel_index=2),  # R
    Segment("porch", 0.572),
)
MARTIN_M1 = SstvModeSpec(
    name="Martin M1", vis_code=0x2C, width=320, height=256,
    color_space="rgb", channel_order=("G", "B", "R"),
    segments=_MARTIN_M1_SEGMENTS, line_ms=_line_ms(_MARTIN_M1_SEGMENTS),
)

# Scottie S1 -- values from dnet/pySSTV's color.py: SYNC=9,
# INTER_CH_GAP=1.5, SCAN=138.24-1.5=136.74 (per channel), VIS_CODE=0x3c,
# same G/B/R channel order as Martin, but -- the deliberately different
# structural case this decoder exists to cover -- pySSTV overrides
# horizontal_sync() to emit NOTHING for Scottie, confirming the sync
# pulse does not lead the line; it trails the R channel instead, right
# before the next line's G. Verified: 9 + 6*1.5 + 3*136.74 = 428.22 ms,
# matching the independently-reported Scottie S1 total line time (a
# gap on both sides of each of the 3 channels = 6 gap instances).
_SCOTTIE_S1_SEGMENTS = (
    Segment("porch", 1.5),
    Segment("channel", 136.74, channel_index=0),  # G
    Segment("porch", 1.5),
    Segment("porch", 1.5),
    Segment("channel", 136.74, channel_index=1),  # B
    Segment("porch", 1.5),
    Segment("porch", 1.5),
    Segment("channel", 136.74, channel_index=2),  # R
    Segment("porch", 1.5),
    Segment("sync", 9.0),
)
SCOTTIE_S1 = SstvModeSpec(
    name="Scottie S1", vis_code=0x3C, width=320, height=256,
    color_space="rgb", channel_order=("G", "B", "R"),
    segments=_SCOTTIE_S1_SEGMENTS, line_ms=_line_ms(_SCOTTIE_S1_SEGMENTS),
)

SSTV_MODES = {m.vis_code: m for m in (ROBOT36, MARTIN_M1, SCOTTIE_S1)}


# ---- Continuous frequency demodulation (image-tone decode) --------------

class FmDemodulator:
    """Continuous instantaneous-frequency estimator via quadrature (IQ)
    downconversion -- mixes the raw PCM signal down against a complex
    local oscillator centered in the SSTV image-tone range (1500-2300
    Hz), lowpass-filters to isolate the baseband component, then takes
    the derivative of the unwrapped phase to get frequency directly.
    Standard DSP technique (an all-numpy substitute for
    scipy.signal.hilbert), not specific to any one SSTV implementation.

    Deliberately NOT used for VIS/sync tone detection (1200/1100/1300/
    1900 Hz all fall outside this demodulator's ~1400-2400 Hz passband
    by design) -- those use cw.py's GoertzelDetector instead. Two
    complementary techniques, not one forced to do both jobs.

    Stateful across process() calls -- carries the LO phase, FIR filter
    tail, and last unwrapped phase value across arbitrary chunk
    boundaries, since rigplane's audio chunks are not fixed-size.

    Filter length is a real, deliberate tradeoff, not an arbitrary
    choice: mixing a real signal down against a complex LO produces
    BOTH the wanted baseband component (+-400 Hz around 0, from the
    1500-2300 Hz image-tone range) AND an unwanted "sum" image around
    -(f+1900) -- roughly -3800 to -4200 Hz, confirmed by working the
    mixing math out by hand. That's a wide gap, so the lowpass doesn't
    need to be narrow OR long to keep the unwanted component out --
    and it's important that it isn't: this filter never runs
    continuously across a whole image, only within each channel
    segment (sync/porch samples are skipped, not fed through it, so
    every new channel segment starts from a stale filter tail that
    needs to re-settle). Confirmed live: the original, much narrower/
    longer design (500 Hz half-bandwidth, 127 taps) took several
    hundred samples to settle -- more than half of Robot 36's shortest
    (44ms chroma) channel segment at typical sample rates -- producing
    exactly the "values decay toward zero across the row" corruption
    seen in early testing. A short, wide filter settles in a small
    fraction of even the shortest real channel segment instead."""

    _CENTER_HZ = 1900.0
    _HALF_BANDWIDTH_HZ = 700.0   # lowpass passes roughly center +/- this
    _FIR_TAPS = 15

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self._fir = self._design_lowpass_fir(sample_rate, self._HALF_BANDWIDTH_HZ, self._FIR_TAPS)
        self._filter_tail = np.zeros(len(self._fir) - 1, dtype=np.complex128)
        self._sample_index = 0       # running count, for LO phase continuity across chunks
        self._last_phase = None      # last unwrapped phase value, for diff continuity across chunks

    @staticmethod
    def _design_lowpass_fir(sample_rate, half_bandwidth_hz, taps):
        """Windowed-sinc lowpass, precomputed once. Standard FIR design
        technique (sinc times a Hamming window, normalized to unity DC
        gain), not project-specific."""
        cutoff = half_bandwidth_hz / (sample_rate / 2.0)  # normalized 0-1 (Nyquist=1)
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
        lo = np.exp(-1j * 2.0 * np.pi * self._CENTER_HZ / self.sample_rate * idx)
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
            # Correct any 2*pi*k wrap discontinuity at the chunk seam
            # without introducing a fake jump -- only whole-cycle
            # offsets get folded in.
            offset_cycles = round((self._last_phase - phase[0]) / (2.0 * np.pi))
            phase = phase + offset_cycles * 2.0 * np.pi
            phase_with_prev = np.concatenate([[self._last_phase], phase])
        freq = np.diff(phase_with_prev) * self.sample_rate / (2.0 * np.pi)

        self._last_phase = phase[-1]
        self._sample_index += n
        return self._CENTER_HZ + freq


# ---- VIS header detection state machine ----------------------------------

class _VisDetector:
    """Detects the SSTV calibration header + VIS code: 300ms leader
    @1900Hz, 10ms break @1200Hz, 300ms leader @1900Hz, then a start bit
    (1200Hz), 7 data bits (1100Hz=1 / 1300Hz=0, LSB first -- confirmed
    against the public SSTV standard, not guessed), an even parity
    bit, and a stop bit (1200Hz). Fully resettable: any timing/parity
    mismatch resets straight back to IDLE, so it naturally re-arms for
    the next transmission attempt with no external intervention.

    Two consumption modes, deliberately different:

    - IDLE/LEADER_1/BREAK/LEADER_2: duration is variable (we're
      WAITING for a tone to end, don't know exactly when) -- consumed
      one small ~_BLOCK_MS block at a time, tone-tracking style.
    - START_BIT/DATA_BITS/PARITY_BIT/STOP_BIT: duration is exactly
      _VIS_BIT_MS (30ms) every time -- consumed as one exact sample
      count in a single shot, classified with ONE Goertzel power call
      over the whole segment. Confirmed live that doing this via the
      same small-block-plus-elapsed-ms-threshold approach as the
      tracking states was a real bug: 30ms doesn't divide evenly into
      a ~4ms block at most sample rates (e.g. 11025 Hz: 44 samples =
      3.99ms, not 4.0ms), so accumulating in blocks and firing once
      elapsed >= 30ms always overshoots by up to one block -- compounds
      across the header's 9 back-to-back 30ms segments (start + 7 data
      + parity + stop) until, by the last data bit, the decoder's
      window had drifted far enough to sample mostly into the NEXT
      segment's tone and misclassify it. Exact sample-count consumption
      has no such drift, matching how image line segments are already
      handled.

    feed(raw_buffer) consumes some prefix of raw_buffer (a numpy int16
    array) and returns (samples_consumed, code_or_None) -- code is the
    decoded 7-bit VIS value once a complete, parity-valid header has
    been seen, else None."""

    _STATE_IDLE = "idle"
    _STATE_LEADER_1 = "leader_1"
    _STATE_BREAK = "break"
    _STATE_LEADER_2 = "leader_2"
    _STATE_START_BIT = "start_bit"
    _STATE_DATA_BITS = "data_bits"
    _STATE_PARITY_BIT = "parity_bit"
    _STATE_STOP_BIT = "stop_bit"

    _TRACKING_STATES = (_STATE_IDLE, _STATE_LEADER_1, _STATE_BREAK, _STATE_LEADER_2)

    def __init__(self, sample_rate: int):
        self._detectors = {
            freq: GoertzelDetector(sample_rate, freq)
            for freq in (FREQ_VIS_LEADER, FREQ_SYNC, FREQ_VIS_BIT_1, FREQ_VIS_BIT_0)
        }
        self._block_size = max(1, int(round(sample_rate * _BLOCK_MS / 1000.0)))
        self._block_ms = self._block_size * 1000.0 / sample_rate
        self._bit_samples = max(1, int(round(_VIS_BIT_MS / 1000.0 * sample_rate)))
        self.reset()

    def reset(self):
        self._state = self._STATE_IDLE
        self._state_elapsed_ms = 0.0
        self._data_bits = []
        self.has_signal = False

    def _dominant(self, block) -> "str | None":
        """Which of the 4 candidate tones has the most energy in this
        block, or None if none is clearly dominant (near-silence)."""
        powers = {freq: det.power(block) for freq, det in self._detectors.items()}
        best_freq = max(powers, key=powers.get)
        total = sum(powers.values())
        if total <= 0 or powers[best_freq] / total < 0.35:
            return None
        return best_freq

    def feed(self, raw_buffer: np.ndarray) -> "tuple[int, int | None]":
        if self._state in self._TRACKING_STATES:
            if len(raw_buffer) < self._block_size:
                return 0, None
            block = raw_buffer[: self._block_size]
            consumed = self._feed_tracking_block(block)
            return (self._block_size if consumed is None else consumed), None
        else:
            if len(raw_buffer) < self._bit_samples:
                return 0, None
            segment = raw_buffer[: self._bit_samples]
            code = self._feed_bit_segment(segment)
            return self._bit_samples, code

    def _find_leader_sync_edge(self, block) -> "int | None":
        """Pinpoint, within this one classification block, the sample
        offset where the tone actually flips from the (1900 Hz) leader
        to the (1200 Hz) sync/start-bit tone -- a small sliding-window
        scan, not the whole-block-granularity guess _feed_tracking_block
        otherwise uses. Only worth doing for this ONE specific
        transition (LEADER_2 -> START_BIT): it's the last variable-
        duration tracking state before a run of fixed, exact-sample-
        count segments (START_BIT..STOP_BIT) that lead straight into
        image data, so any imprecision here is the ONLY tracking-state
        boundary that bleeds into image alignment -- confirmed live,
        consuming this whole ~4ms block unconditionally (the previous
        approach) routinely ate 40-50 samples of what should have been
        line 0's sync pulse, corrupting the tail of every segment in
        every line for the rest of the transmission (fixed-duration
        segment consumption never resyncs after this point). The
        earlier three tracking transitions (IDLE/LEADER_1/BREAK) don't
        need this -- their imprecision only affects _state_elapsed_ms
        bookkeeping, which is already tolerant of it."""
        probe = 12  # small enough for fine resolution, plenty to tell 1200 Hz from 1900 Hz apart
        if len(block) < probe:
            return None
        leader_det = self._detectors[FREQ_VIS_LEADER]
        sync_det = self._detectors[FREQ_SYNC]
        for offset in range(0, len(block) - probe + 1):
            window = block[offset:offset + probe]
            if sync_det.power(window) > leader_det.power(window):
                return offset
        return None

    def _feed_tracking_block(self, block) -> "int | None":
        dominant = self._dominant(block)
        if dominant is not None:
            self.has_signal = self._state != self._STATE_IDLE or dominant == FREQ_VIS_LEADER

        # A block with no clearly-dominant tone (dominant is None) is
        # exactly what one straddling a tone transition looks like --
        # treat it as a no-op (neither advances nor resets) rather than
        # aborting the whole header on it (confirmed live: a hard reset
        # here broke decoding on synthetic, phase-clean audio purely
        # from ordinary transition-block ambiguity). Only a CLEARLY
        # WRONG tone resets.
        if self._state == self._STATE_IDLE:
            if dominant == FREQ_VIS_LEADER:
                self._state = self._STATE_LEADER_1
                self._state_elapsed_ms = self._block_ms
            return None

        if self._state == self._STATE_LEADER_1:
            if dominant == FREQ_VIS_LEADER:
                self._state_elapsed_ms += self._block_ms
                return None
            if dominant is None:
                return None
            if dominant == FREQ_SYNC and self._state_elapsed_ms >= _VIS_LEADER_MIN_MS:
                self._state = self._STATE_BREAK
                self._state_elapsed_ms = self._block_ms
                return None
            self.reset()
            return None

        if self._state == self._STATE_BREAK:
            if dominant == FREQ_SYNC:
                self._state_elapsed_ms += self._block_ms
                return None
            if dominant is None:
                return None
            if dominant == FREQ_VIS_LEADER:
                self._state = self._STATE_LEADER_2
                self._state_elapsed_ms = self._block_ms
                return None
            self.reset()
            return None

        if self._state == self._STATE_LEADER_2:
            if dominant == FREQ_VIS_LEADER:
                self._state_elapsed_ms += self._block_ms
                return None
            if dominant is None:
                return None
            if dominant == FREQ_SYNC and self._state_elapsed_ms >= _VIS_LEADER_MIN_MS:
                edge = self._find_leader_sync_edge(block)
                self._state = self._STATE_START_BIT
                self._data_bits = []
                return edge
            self.reset()
            return None
        return None

    def _feed_bit_segment(self, segment) -> "int | None":
        if self._state == self._STATE_START_BIT:
            self._state = self._STATE_DATA_BITS
            self._data_bits = []
            return None

        if self._state == self._STATE_DATA_BITS:
            bit = self._classify_bit(segment)
            self._data_bits.append(bit)
            if len(self._data_bits) == 7:
                self._state = self._STATE_PARITY_BIT
            return None

        if self._state == self._STATE_PARITY_BIT:
            parity_bit = self._classify_bit(segment)
            self._state = self._STATE_STOP_BIT
            expected_parity = sum(self._data_bits) % 2
            if parity_bit != expected_parity:
                # Bad parity -- don't trust the code, drop this attempt
                # entirely rather than decode into a possibly-wrong mode.
                self.reset()
                return None
            self._pending_code = 0
            for i, bit in enumerate(self._data_bits):
                self._pending_code |= (bit << i)   # LSB first
            return None

        if self._state == self._STATE_STOP_BIT:
            code = getattr(self, "_pending_code", None)
            self.reset()
            return code

        self.reset()
        return None

    def _classify_bit(self, segment) -> int:
        power1 = self._detectors[FREQ_VIS_BIT_1].power(segment)
        power0 = self._detectors[FREQ_VIS_BIT_0].power(segment)
        return 1 if power1 >= power0 else 0


# ---- Top-level decoder ----------------------------------------------------

class SstvDecoder:
    """Feed it raw PCM audio (int16, mono, `sample_rate` Hz) via
    .feed(pcm_bytes). Unlike CwDecoder.feed(), returns nothing -- there
    is no discrete "newly decoded" event to hand back, just a
    continuously-mutating image buffer (see .image). All state is
    internal; construct a fresh instance (or call .reset()) to start a
    new decode session."""

    _SYNC_SEARCH_BLOCK_MS = 2.0     # granularity for tracking peak 1200 Hz power during the sync window

    # One-time realignment search performed exactly once, right when VIS
    # detection hands off to image reception (see _start_image /
    # _consume_resync_step). Confirmed live: the VIS detector's block-
    # granularity tracking states (IDLE/LEADER_1/BREAK/LEADER_2) don't
    # land exactly on the true 300/10/300ms leader/break boundaries, so
    # a small but nonzero number of samples (~50 at 11025 Hz in testing)
    # of real line-0 sync-pulse audio routinely get consumed as part of
    # the VIS header rather than the image. Since every segment after
    # that is consumed by fixed nominal duration with no ongoing re-
    # anchoring (see _consume_sync_segment's docstring for why a fragile
    # per-line dynamic search was abandoned), this constant offset does
    # NOT grow line-to-line, but it also never corrects itself -- it
    # silently shifts the tail of every single line's every segment by
    # the same amount for the entire rest of the transmission, corrupting
    # the last several pixels of every channel with the *next* segment's
    # tone. A single accurate resync right here, before any segment
    # consumption begins, fixes the whole image with a bounded, cheap,
    # one-shot search (not the fragile repeated per-line heuristic).
    _RESYNC_SEARCH_MS = 20.0        # how far past the current buffer position to search
    _RESYNC_STRIDE_SAMPLES = 1      # scan every sample for best precision (cheap: done once per image)

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self._sync_block_size = max(1, int(round(sample_rate * self._SYNC_SEARCH_BLOCK_MS / 1000.0)))
        self._sync_detector = GoertzelDetector(sample_rate, FREQ_SYNC)
        self._fm_demod = FmDemodulator(sample_rate)
        self._resync_search_samples = max(1, int(round(sample_rate * self._RESYNC_SEARCH_MS / 1000.0)))
        self.reset()

    def reset(self):
        self._raw_buffer = np.zeros(0, dtype=np.int16)
        self._vis = _VisDetector(self.sample_rate)
        self._mode = "listening_for_vis"   # or "receiving_image"
        self._spec: "SstvModeSpec | None" = None
        self.detected_mode_name = None
        self._image = None
        self.is_image_complete = False
        self._line_index = 0
        self._segment_index = 0
        self._segment_samples_needed = 0
        self._segment_samples_done = 0
        self._pixel_sum = None
        self._pixel_count = None
        self._sync_search_peak_power = -1.0
        self._consecutive_sync_misses = 0
        self.status = "Listening for VIS..."

    @property
    def has_signal(self) -> bool:
        if self._mode in ("receiving_image", "resyncing"):
            return True
        return self._vis.has_signal

    @property
    def progress_fraction(self) -> float:
        if self._spec is None:
            return 0.0
        return max(0.0, min(1.0, self._line_index / self._spec.height))

    @property
    def image(self) -> "np.ndarray | None":
        return self._image

    def feed(self, pcm_bytes: bytes) -> None:
        new_samples = np.frombuffer(
            pcm_bytes[: len(pcm_bytes) - (len(pcm_bytes) % 2)], dtype=np.int16
        )
        self._raw_buffer = np.concatenate([self._raw_buffer, new_samples])
        while True:
            if self._mode == "listening_for_vis":
                if not self._consume_vis_block():
                    break
            elif self._mode == "resyncing":
                if not self._consume_resync_step():
                    break
            else:
                if not self._consume_image_step():
                    break

    def _consume_vis_block(self) -> bool:
        consumed, code = self._vis.feed(self._raw_buffer)
        if consumed == 0:
            return False
        self._raw_buffer = self._raw_buffer[consumed:]
        if self._vis.has_signal and self._mode == "listening_for_vis":
            self.status = "Listening for VIS..." if code is None else self.status
        if code is not None:
            spec = SSTV_MODES.get(code)
            if spec is None:
                self.status = f"Unsupported VIS code: {code}"
                self._vis.reset()
            else:
                self._start_image(spec)
        return True

    def _start_image(self, spec: SstvModeSpec) -> None:
        self._spec = spec
        self.detected_mode_name = spec.name
        self._image = np.zeros((spec.height, spec.width, 3), dtype=np.uint8)
        self.is_image_complete = False
        self._line_index = 0
        self._segment_index = 0
        self._consecutive_sync_misses = 0
        self._pending_y = None
        self._last_cr = np.zeros(spec.width, dtype=np.float64)   # neutral (chroma=128) until real data arrives
        self._last_cb = np.zeros(spec.width, dtype=np.float64)
        if spec.segments[0].kind == "sync":
            # Line 0 starts with a sync pulse -- realign to its true
            # onset before consuming anything (see _RESYNC_SEARCH_MS's
            # comment). Modes that don't start with sync (e.g. Scottie,
            # whose sync trails each line instead) have no single tone
            # to search for here, so skip straight to normal consumption
            # -- same behavior as before this fix, not a regression for
            # them.
            self._mode = "resyncing"
            self.status = f"Receiving: {spec.name} (aligning...)"
        else:
            self._mode = "receiving_image"
            self._begin_segment()
            self.status = f"Receiving: {spec.name} (line 0/{spec.height})"

    def _consume_resync_step(self) -> bool:
        sync_ms = self._spec.segments[0].duration_ms
        sync_samples = max(1, int(round(sync_ms / 1000.0 * self.sample_rate)))
        needed = sync_samples + self._resync_search_samples
        if len(self._raw_buffer) < needed:
            return False
        best_offset = 0
        best_power = -1.0
        for offset in range(0, self._resync_search_samples, self._RESYNC_STRIDE_SAMPLES):
            block = self._raw_buffer[offset:offset + sync_samples]
            power = self._sync_detector.power(block)
            if power > best_power:
                best_power = power
                best_offset = offset
        self._raw_buffer = self._raw_buffer[best_offset:]
        self._mode = "receiving_image"
        self._begin_segment()
        self.status = f"Receiving: {self._spec.name} (line 0/{self._spec.height})"
        return True

    def _begin_segment(self) -> None:
        spec = self._spec
        segment = spec.segments[self._segment_index]
        self._segment_samples_done = 0
        if segment.kind == "sync":
            self._sync_search_peak_power = -1.0
            self._segment_samples_needed = max(1, int(round(segment.duration_ms / 1000.0 * self.sample_rate)))
        else:
            self._segment_samples_needed = max(1, int(round(segment.duration_ms / 1000.0 * self.sample_rate)))
            if segment.kind == "channel":
                width = spec.width
                self._pixel_sum = np.zeros(width, dtype=np.float64)
                self._pixel_count = np.zeros(width, dtype=np.float64)
                self._samples_per_pixel = self._segment_samples_needed / float(width)

    def _consume_image_step(self) -> bool:
        if self._spec is None or len(self._raw_buffer) == 0:
            return False
        segment = self._spec.segments[self._segment_index]
        if segment.kind == "sync":
            return self._consume_sync_segment(segment)
        elif segment.kind == "porch":
            return self._consume_skip_segment()
        else:
            return self._consume_channel_segment(segment)

    def _consume_skip_segment(self) -> bool:
        remaining = self._segment_samples_needed - self._segment_samples_done
        take = min(remaining, len(self._raw_buffer))
        if take <= 0:
            self._advance_segment()
            return True
        self._raw_buffer = self._raw_buffer[take:]
        self._segment_samples_done += take
        if self._segment_samples_done >= self._segment_samples_needed:
            self._advance_segment()
        return take > 0

    def _consume_sync_segment(self, segment) -> bool:
        # Consumes exactly the nominal sync duration -- NOT a dynamic
        # search for the pulse's real edge. An earlier version tried to
        # dynamically detect where the pulse ends (peak power, then
        # stop once it's "falling") to re-anchor line timing against
        # real-world clock drift, per the original plan. Confirmed live
        # that approach was too fragile even against clean synthetic
        # audio: ordinary block-to-block Goertzel power noise around an
        # otherwise perfectly constant tone looked like "falling" after
        # only one or two blocks, ending far short of the true pulse
        # and shifting every later segment in the line out of alignment
        # with the real audio -- an active correctness bug, not just
        # reduced robustness. Fixed-duration consumption, like every
        # other segment, is simple and correct; the tradeoff is that
        # real-world sample-rate drift within a line isn't corrected
        # for. Peak 1200 Hz power observed during the window is still
        # tracked and used as a quality signal for _consecutive_sync_
        # misses/_abort_image (a genuinely silent/wrong-tone window is
        # still detected), just not for re-timing.
        remaining = self._segment_samples_needed - self._segment_samples_done
        take = min(remaining, len(self._raw_buffer))
        if take <= 0:
            self._finish_sync_segment()
            return True
        # Search block size may not evenly divide the remaining amount
        # on the last iteration -- take whatever's left, still fine for
        # a single Goertzel power() call (it accepts any block size).
        block = self._raw_buffer[: min(take, self._sync_block_size)]
        take = len(block)
        self._raw_buffer = self._raw_buffer[take:]
        self._segment_samples_done += take

        power = self._sync_detector.power(block)
        if power > self._sync_search_peak_power:
            self._sync_search_peak_power = power

        if self._segment_samples_done >= self._segment_samples_needed:
            self._finish_sync_segment()
        return True

    def _finish_sync_segment(self) -> None:
        # A weak peak (essentially zero 1200 Hz energy anywhere in the
        # window -- true silence/dead air) means this line's sync pulse
        # was effectively not found. Deliberately lenient (not a tuned
        # relative threshold) -- see _consume_sync_segment's docstring
        # for why a more precise, tuned check was actively unreliable;
        # this is a safety net against total signal loss, not a
        # precision detector.
        found_pulse = self._sync_search_peak_power > 0.0
        if found_pulse:
            self._consecutive_sync_misses = 0
        else:
            self._consecutive_sync_misses += 1
            if self._consecutive_sync_misses >= _MAX_CONSECUTIVE_SYNC_MISSES:
                self._abort_image()
                return
        self._advance_segment()

    def _consume_channel_segment(self, segment) -> bool:
        remaining = self._segment_samples_needed - self._segment_samples_done
        take = min(remaining, len(self._raw_buffer))
        if take <= 0:
            self._finalize_channel_segment(segment)
            self._advance_segment()
            return True
        block = self._raw_buffer[:take]
        self._raw_buffer = self._raw_buffer[take:]
        freqs = self._fm_demod.process(block)

        start_pos = self._segment_samples_done
        positions = start_pos + np.arange(take)
        pixel_indices = np.clip(
            (positions / self._samples_per_pixel).astype(np.int64), 0, self._spec.width - 1
        )
        width = self._spec.width
        self._pixel_sum += np.bincount(pixel_indices, weights=freqs, minlength=width)[:width]
        self._pixel_count += np.bincount(pixel_indices, minlength=width)[:width]

        self._segment_samples_done += take
        if self._segment_samples_done >= self._segment_samples_needed:
            self._finalize_channel_segment(segment)
            self._advance_segment()
        return True

    def _finalize_channel_segment(self, segment) -> None:
        counts = np.maximum(self._pixel_count, 1.0)
        mean_freq = self._pixel_sum / counts
        values = freq_to_value(mean_freq).astype(np.uint8)
        self._write_channel(segment.channel_index, values)
        self._pixel_sum = None
        self._pixel_count = None

    def _write_channel(self, channel_index: int, values: np.ndarray) -> None:
        spec = self._spec
        row = self._line_index
        if row >= spec.height:
            return
        if spec.color_space == "rgb":
            # channel_order e.g. ("G","B","R") -> RGB image index
            rgb_index = {"R": 0, "G": 1, "B": 2}[spec.channel_order[channel_index]]
            self._image[row, :, rgb_index] = values
        elif spec.color_space == "robot_yuv":
            if spec.channel_order[channel_index] == "Y":
                self._pending_y = values.astype(np.float64)
                # Seed with a grayscale render immediately (Y in all 3
                # channels) so the live preview shows *something* for
                # this row right away, corrected to real color the
                # instant its chroma segment lands a few tens of ms
                # later.
                gray = np.clip(self._pending_y, 0, 255).astype(np.uint8)
                self._image[row, :, 0] = gray
                self._image[row, :, 1] = gray
                self._image[row, :, 2] = gray
            else:
                # Robot 36 alternates R-Y (Cr) / B-Y (Cb) by line parity
                # -- full color needs BOTH, so hold the most recently
                # seen value of the OTHER chroma channel across lines
                # (standard chroma-subsampling reconstruction, same
                # idea real Robot 36 decoders use) and recombine with
                # this row's fresh Y + one fresh chroma value into a
                # real YCbCr->RGB conversion (standard BT.601-ish
                # matrix), rather than leaving G as an uncorrected copy
                # of Y.
                is_cr_line = (row % 2) == 0
                y = getattr(self, "_pending_y", None)
                if y is None:
                    return
                fresh = values.astype(np.float64) - 128.0
                if is_cr_line:
                    self._last_cr = fresh
                    cr, cb = self._last_cr, self._last_cb
                else:
                    self._last_cb = fresh
                    cr, cb = self._last_cr, self._last_cb
                r = np.clip(y + 1.402 * cr, 0, 255)
                g = np.clip(y - 0.344136 * cb - 0.714136 * cr, 0, 255)
                b = np.clip(y + 1.772 * cb, 0, 255)
                self._image[row, :, 0] = r.astype(np.uint8)
                self._image[row, :, 1] = g.astype(np.uint8)
                self._image[row, :, 2] = b.astype(np.uint8)

    def _advance_segment(self) -> None:
        self._segment_index += 1
        if self._segment_index >= len(self._spec.segments):
            self._segment_index = 0
            self._line_index += 1
            if self._line_index >= self._spec.height:
                self._complete_image()
                return
            self.status = f"Receiving: {self._spec.name} (line {self._line_index}/{self._spec.height})"
        self._begin_segment()

    def _complete_image(self) -> None:
        self.is_image_complete = True
        self.status = f"Image complete: {self._spec.name}"
        self._mode = "listening_for_vis"
        self._vis.reset()
        self._spec_finished = self._spec
        self._spec = None

    def _abort_image(self) -> None:
        self.status = "Signal lost -- image incomplete"
        self._mode = "listening_for_vis"
        self._vis.reset()
        self._spec = None
