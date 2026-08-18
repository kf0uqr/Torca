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

# A dot length corresponding to 25 WPM (1200/wpm ms per dot -- the
# standard PARIS-word timing formula) -- the decoder's starting guess
# before it's seen any real marks to adapt from. Per explicit
# instruction: most real-world CW traffic this app's users actually
# encounter runs somewhere around 20-30 WPM, so starting the guess at
# 25 (rather than a generic 20) means the very first few marks -- the
# ones classified before there's enough history for the more reliable
# clustering approach below to kick in -- are compared against a
# threshold much closer to the true speed from the start, instead of
# needing several marks worth of adaptation to fix a bigger initial
# error. See _on_mark_ended/_cluster_marks for how the estimate then
# corrects toward however far off the true speed actually is.
_DEFAULT_UNIT_MS = 1200.0 / 25.0

# How much weight a newly-observed dot-like mark gets when updating the
# running unit-length estimate (exponential moving average) -- low
# enough that one anomalously short/long mark (noise, a misread dash)
# doesn't yank the estimate around. Only used before there's enough
# mark history for _cluster_marks to produce a real measurement (see
# _CLUSTER_UNIT_ADAPT_RATE below for the much faster rate used once it
# does) -- i.e. only for roughly the first character of a session.
_UNIT_ADAPT_RATE = 0.3

# Once _cluster_marks has enough history to measure the actual dot
# length directly (an average over several recent marks, not one raw
# sample), the estimate can be trusted with more weight per update
# than a single mark can -- confirmed live that the old flat 0.3 rate
# applied uniformly took many characters to fully correct a wrong
# starting guess, which (combined with the WPM-range output filter,
# see MIN_ACCEPTED_WPM) meant genuine code was getting silently
# dropped while the estimate was still converging. Originally 0.5,
# but confirmed live on real (not synthetic) radio audio that even a
# 50% blend let one bad/noisy cluster measurement swing the estimate
# wildly in a single step (WPM readings spiking to 100-200+) -- see
# _MAX_UNIT_MS_STEP_FRACTION below for the hard clamp that's now the
# main defense against that; this rate is deliberately more moderate
# too rather than relying on the clamp alone.
_CLUSTER_UNIT_ADAPT_RATE = 0.3

# Hard limit on how much _unit_ms can move in a single mark update,
# as a fraction of its current value -- applied on top of both
# _UNIT_ADAPT_RATE and _CLUSTER_UNIT_ADAPT_RATE's blending (see
# _adapt_unit_ms). Confirmed live: on real radio audio (noisier and
# messier than clean synthetic test tones), even a moderate blend
# rate wasn't enough -- a single sufficiently-bad measurement (a
# spurious cluster split that still happened to pass _MIN_CLUSTER_
# RATIO, or one badly-timed mark before clustering has enough
# history) could still be extreme enough in absolute terms to swing
# unit_ms drastically in one step. A blend rate alone can't bound
# that: it limits how much of the BAD measurement gets absorbed, not
# how far the bad measurement itself is from the truth. This clamps
# the actual step size directly, regardless of how far off any one
# measurement is -- the estimate can still fully correct a wrong
# guess, just gradually (over several marks) instead of in one
# possibly-wild jump.
_MAX_UNIT_MS_STEP_FRACTION = 0.15

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
# filter. Widened from an initial, too-tight 10-30 (per explicit
# follow-up instruction/research) to 5-60 -- comfortably covers
# everything from a slow beginner/QRS rag-chew up through real QRQ
# contest speeds without narrowly clipping legitimate traffic at
# either end.
MIN_ACCEPTED_WPM = 5.0
MAX_ACCEPTED_WPM = 60.0


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

    # Same idea as _MARK_HISTORY_SIZE, for recent GAP durations (see
    # _classify_gap) -- slightly larger since gap clustering there
    # needs to resolve three categories (intra-character/inter-
    # character/inter-word), not just two.
    _GAP_HISTORY_SIZE = 8

    # An on/off change has to persist for at least this many blocks
    # before it's accepted as a real mark/gap boundary -- otherwise
    # it's folded back into whatever state was already confirmed (see
    # _advance_state). Confirmed live (real radio audio, not clean
    # synthetic test tones): without this, ordinary noise/QRM briefly
    # dipping the signal mid-mark -- the same underlying class of
    # problem _update_noise_floor's gating already fixed for the
    # ADAPTIVE THRESHOLD, but this is a separate effect on the
    # DETECTOR itself -- could fragment one real mark into several
    # short bogus ones. Those fragments are shorter than the real
    # mark, so they pull the speed estimate toward a much higher WPM
    # every time it happens; since real noisy audio triggers this
    # fairly often (not just as a rare one-off), the estimate ends up
    # spending most of its time pinned high, only occasionally
    # reading correctly (and decoding) when a mark happens to come
    # through clean. Confirmed live that 2 blocks (8ms) wasn't quite
    # enough margin -- a brief noise dropout can straddle a block
    # boundary and still read as "off" across 2 separate blocks even
    # if the dropout itself is only ~5ms, since each block's Goertzel
    # power reflects whatever fraction of the notch actually falls
    # inside it. 3 blocks (~12ms at this decoder's ~4ms block size)
    # gives real margin against that while staying safely under the
    # shortest legitimate mark OR gap this decoder accepts (a dot, or
    # the intra-character gap between elements, is 20ms at
    # MAX_ACCEPTED_WPM=60).
    _MIN_TRANSITION_BLOCKS = 3

    def __init__(self, sample_rate: int, tone_hz: float):
        self.sample_rate = sample_rate
        self.tone_hz = tone_hz
        self._block_size = max(1, int(round(sample_rate * self._BLOCK_MS / 1000.0)))
        self._block_duration_ms = self._block_size * 1000.0 / sample_rate
        self._min_transition_ms = self._block_duration_ms * self._MIN_TRANSITION_BLOCKS
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
        self._gap_history = deque(maxlen=self._GAP_HISTORY_SIZE)
        self._tone_on = False
        self._state_elapsed_ms = 0.0   # time spent in the current CONFIRMED on/off state so far (includes any not-yet-confirmed candidate blocks -- see _advance_state)
        self._candidate_state = None   # an is_on value being tentatively tracked as a possible new state, or None
        self._candidate_ms = 0.0       # how long the candidate above has persisted so far
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
            # Peak always adapts (see _update_peak) -- including on the
            # very first block of a new mark, while self._tone_on is
            # still False (not toggled until _advance_state, below,
            # decides this block). That's deliberate: it's what lets
            # peak "bootstrap" onto a mark immediately even after
            # having decayed close to the noise floor over a long
            # preceding silence, rather than needing tone_on to already
            # be True before it's allowed to move. Noise floor is
            # gated instead (see _update_noise_floor) -- freezing IT
            # during an active mark is what actually fixes the bug,
            # and doesn't need the same bootstrap exception since
            # silence, unlike a mark, doesn't have a single instant it
            # "starts" that needs catching.
            self._update_peak(power)
            if not self._tone_on:
                self._update_noise_floor(power)
        threshold = self._threshold()
        is_on = power > threshold if threshold is not None else False
        return self._advance_state(is_on)

    def _update_noise_floor(self, power):
        # Only adapts while NOT currently in a mark (self._tone_on is
        # False -- see _process_block). Confirmed live that adapting
        # this unconditionally on every block, including ones where a
        # mark is actively playing, let a long sustained mark's own
        # power slowly drag the "noise" floor up toward it (each block
        # nudges it 2% closer) -- for a long enough mark (a dash, or
        # any mark at a slower WPM -- e.g. a 12 WPM dash is ~75 blocks
        # long at this decoder's ~4ms block size) that compounds far
        # enough (over 75 blocks, ~80% of the way toward the tone's own
        # power) that ordinary block-to-block Goertzel power jitter
        # started registering as spurious false "off" gaps in the
        # MIDDLE of a still-transmitting mark, fragmenting one real
        # mark into several bogus short ones. Only following the floor
        # while genuinely in silence avoids this entirely -- slowly
        # follows the signal DOWN (so a burst of noise doesn't get
        # mistaken for a rising floor), quickly follows it UP when
        # things get quieter.
        if power < self._noise_floor:
            self._noise_floor = power
        else:
            self._noise_floor = self._noise_floor * 0.98 + power * 0.02

    def _update_peak(self, power):
        # Mirror of _update_noise_floor -- only adapts while currently
        # IN a mark, so a long silence can't drag the peak down toward
        # it for the same underlying reason. Quick to follow a rising
        # tone, slow to decay once one ends.
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
        if is_on == self._tone_on:
            # Matches the confirmed state -- also cancels any
            # in-progress candidate flip in the other direction (it
            # was a transient blip that didn't persist, not a real
            # transition -- see _MIN_TRANSITION_BLOCKS).
            self._candidate_state = None
            self._candidate_ms = 0.0
            self._state_elapsed_ms += self._block_duration_ms
            return ""

        if self._candidate_state == is_on:
            self._candidate_ms += self._block_duration_ms
        else:
            self._candidate_state = is_on
            self._candidate_ms = self._block_duration_ms
        self._state_elapsed_ms += self._block_duration_ms

        if self._candidate_ms < self._min_transition_ms:
            return ""  # hasn't persisted long enough yet -- still provisionally the confirmed state

        # The candidate has persisted long enough to accept as a real
        # transition. Exactly one block is attributed to the OLD
        # state's finished duration (matching the pre-debounce
        # convention of attributing the first differing block to the
        # state that just ended, since we can't resolve sub-block
        # timing); the rest of the persisted candidate period belongs
        # to the NEW state that's starting now.
        old_state_duration_ms = self._state_elapsed_ms - self._candidate_ms + self._block_duration_ms
        output = ""
        if self._tone_on:
            output += self._on_mark_ended(old_state_duration_ms)
        else:
            output += self._off_gap_ended(old_state_duration_ms)
        self._tone_on = is_on
        self._state_elapsed_ms = self._candidate_ms - self._block_duration_ms
        self._candidate_state = None
        self._candidate_ms = 0.0
        return output

    def _on_mark_ended(self, duration_ms) -> str:
        self._have_signal = True
        history = list(self._mark_history) + [duration_ms]
        cluster = self._cluster_marks(history)
        is_dot = self._classify_mark_is_dot(duration_ms, cluster)
        if is_dot:
            self._current_symbols += "."
        else:
            self._current_symbols += "-"
        if cluster is not None:
            # Enough recent marks to measure the real dot length
            # directly (an average over several marks, not one raw
            # sample) -- move unit_ms toward it at _CLUSTER_UNIT_ADAPT_
            # RATE (faster than the single-mark EMA below) rather than
            # continuing to crawl there one mark at a time.
            short_center, _long_center = cluster
            self._adapt_unit_ms(short_center, _CLUSTER_UNIT_ADAPT_RATE)
        else:
            if is_dot:
                candidate_unit = duration_ms
            else:
                # A dash is ~3 units -- back it out to a dot-length
                # estimate before folding it into the running average,
                # so dashes inform the speed estimate too (not just
                # dots).
                candidate_unit = duration_ms / 3.0
            self._adapt_unit_ms(candidate_unit, _UNIT_ADAPT_RATE)
        self._mark_history.append(duration_ms)
        return ""

    def _adapt_unit_ms(self, target, rate):
        """Moves self._unit_ms toward `target` by the given EMA
        `rate`, then hard-clamps the resulting step to at most
        _MAX_UNIT_MS_STEP_FRACTION of the current value (see that
        constant's own comment for why the rate alone isn't a
        sufficient safeguard against one badly-off measurement)."""
        blended = self._unit_ms * (1 - rate) + target * rate
        max_step = self._unit_ms * _MAX_UNIT_MS_STEP_FRACTION
        delta = max(-max_step, min(max_step, blended - self._unit_ms))
        self._unit_ms += delta

    # A real dot/dash split should be close to the standard 3:1 PARIS
    # ratio -- this is deliberately well below that (allowing for real
    # operators not sending perfectly precise timing) but well above
    # the natural jitter within a run of same-length marks. Confirmed
    # live: with realistic (not mathematically perfect) mark timing, a
    # run of several dots in a row (e.g. "H"/"S"/"5") naturally varies
    # by a few percent mark-to-mark -- 2-means clustering, which always
    # forces a 2-way split as long as there's ANY nonzero spread at
    # all, was splitting that jitter into a fake "short"/"long" pair
    # instead of recognizing it as one real cluster, misclassifying
    # dots as dashes and (combined with _CLUSTER_UNIT_ADAPT_RATE's fast
    # snap) corrupting the unit_ms/current_wpm estimate -- this is what
    # was producing wildly inflated WPM readings (200+) on genuine,
    # much slower code. Requiring the two candidate centers to actually
    # be this far apart before trusting the split as real fixes it.
    _MIN_CLUSTER_RATIO = 1.8

    @classmethod
    def _cluster_marks(cls, history):
        """Simple 2-means clustering over recent mark durations (short
        cluster = dots, long cluster = dashes). Returns (short_center,
        long_center), or None if there isn't yet enough history, no
        real spread in it, or the resulting two centers aren't
        separated enough to plausibly be a genuine dot/dash split (see
        _MIN_CLUSTER_RATIO) -- callers fall back to a fixed multiple of
        the running unit_ms estimate in any of those cases (see
        _classify_mark_is_dot/_on_mark_ended)."""
        if len(history) < 4 or max(history) - min(history) < 1e-6:
            return None
        short_center = min(history)
        long_center = max(history)
        for _ in range(4):
            short_cluster = [v for v in history if abs(v - short_center) <= abs(v - long_center)]
            long_cluster = [v for v in history if abs(v - short_center) > abs(v - long_center)]
            if short_cluster:
                short_center = sum(short_cluster) / len(short_cluster)
            if long_cluster:
                long_center = sum(long_cluster) / len(long_cluster)
        if short_center <= 0 or long_center / short_center < cls._MIN_CLUSTER_RATIO:
            return None
        return short_center, long_center

    def _classify_mark_is_dot(self, duration_ms, cluster) -> bool:
        """Returns True if `duration_ms` is a dot, False if a dash.
        `cluster` is _cluster_marks' result (already computed once by
        the caller, shared rather than recomputed here) -- with real
        clustering data, splits on the midpoint between the short/long
        cluster centers instead of a fixed multiple of the running
        unit_ms estimate. This matters because unit_ms starts from a
        fixed guess (see _DEFAULT_UNIT_MS) -- at a real speed far from
        that guess, a fixed-multiple-of-unit_ms threshold can
        misclassify real dashes as dots, and since that wrong
        classification feeds right back into the unit_ms estimate, it
        can stay wrong far longer than it should. Clustering looks at
        the actual relative spread of recently observed marks instead,
        which separates dots from dashes correctly regardless of how
        far off the starting assumption was."""
        if cluster is None:
            return duration_ms < self._unit_ms * _DOT_DASH_THRESHOLD_UNITS
        short_center, long_center = cluster
        threshold = (short_center + long_center) / 2.0
        return duration_ms < threshold

    # Need at least this many recent gaps before trusting their
    # minimum as a real measurement (see _classify_gap) rather than
    # falling back to a fixed multiple of unit_ms.
    _MIN_GAPS_FOR_ESTIMATE = 3

    def _classify_gap(self, duration_ms) -> str:
        """Returns "intra" (still the same character), "inter_char"
        (character boundary), or "inter_word" (word boundary -- emits
        a space).

        With enough recent gap history, anchors the classification
        thresholds to the MINIMUM of recently observed gaps rather
        than a fixed multiple of the running unit_ms (a MARK-based
        measurement) estimate -- confirmed live on real (noisy) radio
        audio that unit_ms being even slightly off from the true GAP
        timing (real keying isn't perfectly symmetric between mark and
        gap duration, and this decoder's own mark/gap detection paths
        aren't perfectly symmetric either) could misjudge a real
        intra-character gap as a character boundary, cutting a
        character short right after its first element -- which is
        exactly what a spurious extra "E" or "T" (the shortest
        possible dot/dash) in the transcript looks like. The minimum
        recent gap is a good, low-noise proxy specifically for the
        true intra-character gap length: that category is both the
        shortest of the three AND, in ordinary text, by far the most
        common, so it reliably dominates any reasonably-sized recent
        window.

        An EARLIER version of this tried reusing _cluster_marks' 2-
        means clustering here too (which works well for dot/dash,
        genuinely only 2 categories) -- confirmed live that this
        breaks down for gaps because there are 3 real categories
        mixed in one window, and an unweighted 2-way split doesn't
        reliably separate them the same way (it can just as easily
        end up splitting off only the rare word gaps and lumping
        intra- and inter-character gaps together, systematically
        misclassifying every inter-character gap as intra and
        breaking character segmentation entirely)."""
        recent = list(self._gap_history)
        if len(recent) >= self._MIN_GAPS_FOR_ESTIMATE:
            intra_estimate = min(recent)
        else:
            intra_estimate = self._unit_ms
        is_intra = duration_ms < intra_estimate * _INTRA_INTER_GAP_THRESHOLD_UNITS
        is_word = duration_ms >= intra_estimate * _INTER_WORD_GAP_THRESHOLD_UNITS
        if is_intra:
            return "intra"
        return "inter_word" if is_word else "inter_char"

    def _off_gap_ended(self, duration_ms) -> str:
        gap_kind = self._classify_gap(duration_ms)
        self._gap_history.append(duration_ms)
        if gap_kind == "intra":
            return ""  # still within the same character
        had_pending_symbols = bool(self._current_symbols)
        char = self._finalize_character()
        if had_pending_symbols and not char:
            # _finalize_character suppressed a real, completed character
            # (implausible WPM -- see its own docstring), not just a
            # continued gap between already-flushed words -- skip the
            # trailing space too rather than emitting an orphan one.
            return ""
        output = char
        if gap_kind == "inter_word":
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
