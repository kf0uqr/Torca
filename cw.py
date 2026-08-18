"""
CW (Morse code) logic -- pure, no Qt/audio-hardware dependency, so it's
independently testable against synthesized signals. Two independent
pieces:

- MORSE_CODE_TABLE / decode lookup: the standard ITU International
  Morse Code mapping (public domain, not any one project's IP).
- GoertzelDetector + CwDecoder: audio-tone-to-text decoding. Sending
  doesn't need any of this at all -- the radios this app supports key
  CW themselves from plain text over CI-V (see radio_worker.py's
  send_cw_text) -- this module exists purely for the RECEIVE side.

GoertzelDetector implements the standard single-bin Goertzel algorithm
(an efficient way to compute the signal energy at one target frequency
over a block of samples, without a full FFT) -- a well-known, decades-
old DSP technique, not code specific to any one open-source CW decoder
project. Pure Python (array/math), no numpy -- this app has no numpy
dependency anywhere else either (confirmed: not in requirements.txt).

CwDecoder builds on that: it slices incoming PCM audio into small
blocks, computes each block's tone energy, thresholds that into a
tone-present/absent envelope, times the on/off runs, and classifies
them into dots/dashes/gaps relative to an adaptively-tracked "unit"
length (the duration of one dot) -- the same technique every CW
decoder, open-source or otherwise, is built on.
"""

import array
import math
from collections import deque

# Standard ITU International Morse Code. Letters/digits/common
# punctuation/prosigns. Public domain -- this is the universal
# standard, not project-specific content.
MORSE_CODE_TABLE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".",
    "F": "..-.", "G": "--.", "H": "....", "I": "..", "J": ".---",
    "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---",
    "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-",
    "U": "..-", "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--",
    "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "'": ".----.",
    "!": "-.-.--", "/": "-..-.", "(": "-.--.", ")": "-.--.-",
    "&": ".-...", ":": "---...", ";": "-.-.-.", "=": "-...-",
    "+": ".-.-.", "-": "-....-", "_": "..--.-", '"': ".-..-.",
    "$": "...-..-", "@": ".--.-.",
}
# Reverse lookup for decode -- dot/dash pattern -> character.
_PATTERN_TO_CHAR = {pattern: char for char, pattern in MORSE_CODE_TABLE.items()}

# Standard PARIS timing ratios, in "units" (a unit = one dot length):
# dot=1, dash=3, intra-character gap=1, inter-character gap=3, word
# gap=7. The classification thresholds below split the difference
# between adjacent categories.
_DOT_DASH_THRESHOLD_UNITS = 2.0     # below -> dot, at/above -> dash
_INTRA_INTER_GAP_THRESHOLD_UNITS = 2.0   # below -> still same character, at/above -> character boundary
_INTER_WORD_GAP_THRESHOLD_UNITS = 5.0    # below -> character boundary, at/above -> word boundary (space)

# A dot length corresponding to 20 WPM (1200/wpm ms per dot -- the
# standard PARIS-word timing formula) -- used only as the decoder's
# starting guess before it's seen any real marks to adapt from.
_DEFAULT_UNIT_MS = 1200.0 / 20.0

# How much weight a newly-observed dot-like mark gets when updating the
# running unit-length estimate (exponential moving average) -- low
# enough that one anomalously short/long mark (noise, a misread dash)
# doesn't yank the estimate around, high enough to track a genuine
# speed change within a few characters.
_UNIT_ADAPT_RATE = 0.3

# Placeholder emitted for a dot/dash sequence that doesn't match any
# known character (noise, a misread mark, or genuinely unsupported
# input) -- better than silently dropping it, so the operator can see
# something didn't decode cleanly rather than watching characters go
# missing with no indication why.
UNKNOWN_CHAR_PLACEHOLDER = "*"

# Accepted WPM range for decoded output -- per explicit instruction:
# a character is only appended to the transcript while the adaptive
# unit_ms estimate implies a speed in this range (see CwDecoder.
# current_wpm/_finalize_character). Ordinary audio-band noise/voice
# rarely produces on/off timing that happens to land in a plausible
# human CW-sending range, so this is an effective, cheap garbage
# filter -- real amateur CW traffic is overwhelmingly sent somewhere
# in 10-30 WPM; the rare faster QRQ operator falls outside it, a
# deliberate trade-off for killing noise decode.
MIN_ACCEPTED_WPM = 10.0
MAX_ACCEPTED_WPM = 30.0


def estimate_cw_send_duration_ms(text: str, wpm: int) -> float:
    """Estimates how long the radio's own keyer will take to key out
    `text` at `wpm`, in milliseconds -- standard PARIS-word timing
    (dot=1 unit, dash=3, intra-character gap=1, inter-character gap=3,
    inter-word gap=7; unit_ms = 1200/wpm, same constants as CwDecoder's
    classification thresholds above).

    There's no live "done" signal from the radio for a CW send --
    rigplane's send_cw_text() coroutine only confirms the CI-V command
    was accepted, not that keying has actually finished; the radio
    paces the real Morse timing itself, independently, after that
    returns. This estimate is what lets calling code (cw_window.py's
    PTT-around-the-CW-send sequencing) know roughly when to expect it
    to be over, the same way an external contest keyer/sequencer
    without any read-back from the rig would.

    Characters with no MORSE_CODE_TABLE entry (unsupported
    punctuation, stray whitespace) are skipped -- contribute no time --
    on the assumption that the radio's own keyer does the same rather
    than erroring outright on them."""
    if wpm <= 0:
        return 0.0
    unit_ms = 1200.0 / wpm
    total_units = 0.0
    words = text.strip().split(" ")
    first_word = True
    for word in words:
        if not word:
            continue  # collapses accidental repeated spaces
        if not first_word:
            total_units += 7.0
        first_word = False
        first_char = True
        for char in word.upper():
            pattern = MORSE_CODE_TABLE.get(char)
            if pattern is None:
                continue
            if not first_char:
                total_units += 3.0
            first_char = False
            for i, symbol in enumerate(pattern):
                if i > 0:
                    total_units += 1.0
                total_units += 1.0 if symbol == "." else 3.0
    return total_units * unit_ms


class GoertzelDetector:
    """Computes the signal energy at `target_hz` for a block of int16
    samples. Doesn't need the block size in advance -- the Goertzel
    recurrence coefficient depends only on target_hz/sample_rate, so
    blocks can be (and here, are) an arbitrary/varying number of
    samples."""

    def __init__(self, sample_rate: int, target_hz: float):
        self.sample_rate = sample_rate
        self.target_hz = target_hz
        omega = 2.0 * math.pi * target_hz / sample_rate
        self._coeff = 2.0 * math.cos(omega)

    def power(self, samples) -> float:
        """Returns the (unnormalized, relative) energy at target_hz
        across `samples` (any iterable of numbers). Returns 0.0 for an
        empty block."""
        q1 = q2 = 0.0
        n = 0
        for sample in samples:
            q0 = self._coeff * q1 - q2 + sample
            q2 = q1
            q1 = q0
            n += 1
        if n == 0:
            return 0.0
        return q1 * q1 + q2 * q2 - self._coeff * q1 * q2


class CwDecoder:
    """Feed it raw PCM audio (int16, mono, `sample_rate` Hz) via
    .feed(pcm_bytes) -- returns any newly-decoded text from that call
    (usually empty; a character or a space once enough audio has
    accumulated to resolve one). Tracks its own running "dot length"
    (WPM) estimate, adapting as marks come in -- no manual speed
    setting needed. All state is internal; construct a fresh instance
    (or call .reset()) to start a new decode session."""

    # ~4ms blocks -- fine enough time resolution for marks down to
    # roughly 48 WPM (a dot is ~25ms there) while still cheap to
    # compute in pure Python at typical radio audio sample rates.
    _BLOCK_MS = 4.0

    # Blocks spent priming the noise floor/peak trackers before trying
    # to classify anything as on/off. Needed because naively locking
    # both trackers to the very first block's power (as if that block
    # were simultaneously the reference floor AND ceiling) can leave
    # the adaptive threshold permanently undefined if that first block
    # happens to land mid-tone rather than in silence -- e.g. decoding
    # starts right as a transmission is already underway, with no
    # quiet moment yet observed to calibrate against. Priming over a
    # short window and seeding from its actual min/max instead makes
    # that far less likely (the window only has to catch ANY on/off
    # variation, not specifically start on silence).
    _PRIMING_BLOCKS = 15

    # How many recent mark durations to cluster over when classifying
    # dot vs. dash (see _classify_mark) -- enough to span a couple of
    # characters' worth of marks so both a dot and a dash have
    # normally been seen, small enough that a genuine speed change
    # ages out of it quickly.
    _MARK_HISTORY_SIZE = 6

    def __init__(self, sample_rate: int, tone_hz: float):
        self.sample_rate = sample_rate
        self.tone_hz = tone_hz
        self._block_size = max(1, int(round(sample_rate * self._BLOCK_MS / 1000.0)))
        self._block_duration_ms = self._block_size * 1000.0 / sample_rate
        self._detector = GoertzelDetector(sample_rate, tone_hz)
        self.reset()

    def reset(self):
        """Clears all decode state -- pending audio, tone envelope
        tracking, timing, and the accumulated (but not yet finalized)
        character -- without needing a new instance. current_wpm
        estimate is reset to the default starting guess too."""
        self._sample_buffer = array.array("h")
        self._noise_floor = None   # adaptive, learned from observed block power
        self._peak = None          # adaptive, learned from observed block power
        self._priming_powers = []  # collected until _PRIMING_BLOCKS is reached, then discarded
        self._mark_history = deque(maxlen=self._MARK_HISTORY_SIZE)
        self._tone_on = False
        self._state_elapsed_ms = 0.0   # time spent in the current on/off state so far
        self._unit_ms = _DEFAULT_UNIT_MS
        self._current_symbols = ""     # dots/dashes accumulated for the in-progress character
        self._have_signal = False      # true once at least one full mark has been seen -- for UI feedback

    @property
    def current_wpm(self) -> float:
        """Current adaptive speed estimate, in WPM (1200/unit_ms)."""
        return 1200.0 / self._unit_ms if self._unit_ms > 0 else 0.0

    @property
    def has_signal(self) -> bool:
        return self._have_signal

    def feed(self, pcm_bytes: bytes) -> str:
        """Processes one chunk of raw int16 mono PCM audio. Returns
        any newly-decoded text (characters and/or spaces) produced by
        this chunk -- most calls return an empty string, since marks/
        gaps span many chunks before a symbol or character boundary is
        actually resolved."""
        self._sample_buffer.frombytes(pcm_bytes[: len(pcm_bytes) - (len(pcm_bytes) % 2)])
        output = []
        while len(self._sample_buffer) >= self._block_size:
            block = self._sample_buffer[: self._block_size]
            del self._sample_buffer[: self._block_size]
            output.append(self._process_block(block))
        return "".join(output)

    def flush(self) -> str:
        """Finalizes any in-progress character that hasn't been closed
        out by a subsequent gap yet (the very last character of a
        decode session never gets a trailing gap to resolve it
        otherwise) -- call when the operator stops decoding. Returns
        the finalized character, or "" if there was nothing pending."""
        if not self._current_symbols:
            return ""
        return self._finalize_character()

    def _process_block(self, block) -> str:
        power = self._detector.power(block)
        if self._noise_floor is None:
            self._priming_powers.append(power)
            if len(self._priming_powers) < self._PRIMING_BLOCKS:
                return ""  # still priming -- treat as "off", no marks detected yet
            self._noise_floor = min(self._priming_powers)
            self._peak = max(self._priming_powers)
            self._priming_powers = None
        else:
            self._update_noise_and_peak(power)
        threshold = self._threshold()
        is_on = power > threshold if threshold is not None else False
        return self._advance_state(is_on)

    def _update_noise_and_peak(self, power):
        # Simple adaptive envelope tracker: the noise floor slowly
        # follows the signal DOWN (so a burst of tone doesn't get
        # mistaken for a rising noise floor) and quickly follows it UP
        # when things get quieter; the peak does the opposite (quick
        # to follow a rising tone, slow to decay once it stops) -- the
        # combination adapts to varying signal strength without
        # needing a fixed, hand-tuned absolute threshold.
        if power < self._noise_floor:
            self._noise_floor = power
        else:
            self._noise_floor = self._noise_floor * 0.98 + power * 0.02
        if power > self._peak:
            self._peak = power
        else:
            self._peak = self._peak * 0.98 + power * 0.02

    def _threshold(self):
        if self._noise_floor is None or self._peak is None:
            return None
        spread = self._peak - self._noise_floor
        if spread <= 0:
            return None
        return self._noise_floor + spread * 0.4

    def _advance_state(self, is_on: bool) -> str:
        output = ""
        if is_on == self._tone_on:
            self._state_elapsed_ms += self._block_duration_ms
            return output
        # State just changed -- classify the run that just ended.
        duration_ms = self._state_elapsed_ms + self._block_duration_ms
        if self._tone_on:
            output += self._on_mark_ended(duration_ms)
        else:
            output += self._off_gap_ended(duration_ms)
        self._tone_on = is_on
        self._state_elapsed_ms = 0.0
        return output

    def _on_mark_ended(self, duration_ms) -> str:
        self._have_signal = True
        if self._classify_mark_is_dot(duration_ms):
            self._current_symbols += "."
            candidate_unit = duration_ms
        else:
            self._current_symbols += "-"
            # A dash is ~3 units -- back it out to a dot-length estimate
            # before folding it into the running average, so dashes
            # inform the speed estimate too (not just dots).
            candidate_unit = duration_ms / 3.0
        self._unit_ms = self._unit_ms * (1 - _UNIT_ADAPT_RATE) + candidate_unit * _UNIT_ADAPT_RATE
        self._mark_history.append(duration_ms)
        return ""

    def _classify_mark_is_dot(self, duration_ms) -> bool:
        """Returns True if `duration_ms` is a dot, False if a dash.
        With enough recent mark-duration history, uses simple 2-means
        clustering (short cluster = dots, long cluster = dashes)
        instead of a fixed multiple of the running unit_ms estimate.
        This matters because unit_ms starts from a fixed 20 WPM guess
        -- at a real speed far from that guess (e.g. 35 WPM), a fixed-
        multiple-of-unit_ms threshold can misclassify real dashes as
        dots, and since that wrong classification feeds right back
        into the unit_ms estimate, it can stay wrong far longer than
        it should. Clustering looks at the actual relative spread of
        recently observed marks instead, which separates dots from
        dashes correctly regardless of how far off the starting
        assumption was, and converges within the first character or
        two rather than staying stuck."""
        history = list(self._mark_history) + [duration_ms]
        if len(history) < 4 or max(history) - min(history) < 1e-6:
            return duration_ms < self._unit_ms * _DOT_DASH_THRESHOLD_UNITS
        short_center = min(history)
        long_center = max(history)
        for _ in range(4):
            short_cluster = [v for v in history if abs(v - short_center) <= abs(v - long_center)]
            long_cluster = [v for v in history if abs(v - short_center) > abs(v - long_center)]
            if short_cluster:
                short_center = sum(short_cluster) / len(short_cluster)
            if long_cluster:
                long_center = sum(long_cluster) / len(long_cluster)
        threshold = (short_center + long_center) / 2.0
        return duration_ms < threshold

    def _off_gap_ended(self, duration_ms) -> str:
        if duration_ms < self._unit_ms * _INTRA_INTER_GAP_THRESHOLD_UNITS:
            return ""  # still within the same character
        had_pending_symbols = bool(self._current_symbols)
        char = self._finalize_character()
        is_word_gap = duration_ms >= self._unit_ms * _INTER_WORD_GAP_THRESHOLD_UNITS
        if had_pending_symbols and not char:
            # _finalize_character suppressed a real, completed character
            # (implausible WPM -- see its own docstring), not just a
            # continued gap between already-flushed words -- skip the
            # trailing space too rather than emitting an orphan one.
            return ""
        output = char
        if is_word_gap:
            output += " "
        return output

    def _finalize_character(self) -> str:
        symbols = self._current_symbols
        self._current_symbols = ""
        if not symbols:
            return ""
        if not (MIN_ACCEPTED_WPM <= self.current_wpm <= MAX_ACCEPTED_WPM):
            # Implausible speed -- almost certainly noise/voice audio
            # producing on/off timing that isn't real human-sent CW,
            # not a genuine character. Discard rather than append
            # garbage to the transcript (see MIN_ACCEPTED_WPM's own
            # comment). The mark timing has already been folded into
            # _unit_ms by _on_mark_ended either way, so the estimate
            # keeps adapting/can recover even though this character is
            # dropped.
            return ""
        return _PATTERN_TO_CHAR.get(symbols, UNKNOWN_CHAR_PLACEHOLDER)
