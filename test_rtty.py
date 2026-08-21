"""Round-trip test for rtty.py -- no radio/Qt/asyncio involved. Synthesizes
a known-good RTTY (Baudot/ITA2, 45.45 baud, mark/space AFSK) signal for a
test string, feeds it through RttyDecoder in irregular chunks, and checks
the decoded text comes back out."""

import math
import struct

from rtty import RttyDecoder, MARK_HZ, SPACE_HZ, BIT_MS, STOP_BITS, _LETTERS, _FIGURES

SAMPLE_RATE = 8000

_LETTERS_REV = {ch: i for i, ch in enumerate(_LETTERS) if ch and ch not in ("LTRS", "FIGS")}
_FIGURES_REV = {ch: i for i, ch in enumerate(_FIGURES) if ch and ch not in ("LTRS", "FIGS")}
_LTRS_CODE = _LETTERS.index("LTRS")
_FIGS_CODE = _LETTERS.index("FIGS")


def _text_to_bit_frames(text):
    """Yields (start_bit, [5 data bits LSB-first], stop_bits) tuples,
    inserting LTRS/FIGS shift frames as needed. Bits are 1=mark/0=space."""
    shift = "LTRS"
    for ch in text:
        if ch in _LETTERS_REV and (ch not in _FIGURES_REV or shift == "LTRS"):
            target_shift, code = "LTRS", _LETTERS_REV[ch]
        elif ch in _FIGURES_REV:
            target_shift, code = "FIGS", _FIGURES_REV[ch]
        else:
            raise ValueError(f"no Baudot code for {ch!r}")
        if target_shift != shift:
            shift_code = _LTRS_CODE if target_shift == "LTRS" else _FIGS_CODE
            yield _frame_for(shift_code)
            shift = target_shift
        yield _frame_for(code)


def _frame_for(code):
    data_bits = [(code >> i) & 1 for i in range(5)]
    return (0, data_bits, STOP_BITS)  # start bit = space(0), stop = mark, STOP_BITS long


def _synthesize_tone(duration_ms, freq_hz, phase):
    """Continuous-phase tone synthesis (integrates frequency to get
    phase) -- avoids clicks at bit boundaries, same reasoning the SSTV
    test encoder documents for itself."""
    n = round(SAMPLE_RATE * duration_ms / 1000.0)
    samples = []
    for _ in range(n):
        samples.append(int(round(16000 * math.sin(phase))))
        phase += 2 * math.pi * freq_hz / SAMPLE_RATE
    return samples, phase


def encode_rtty(text):
    """Synthesizes a full RTTY audio signal for `text`: some idle mark,
    then each character's start/data/stop bits, ending on idle mark."""
    phase = 0.0
    all_samples = []

    def add(duration_ms, is_mark):
        nonlocal phase
        samples, phase = _synthesize_tone(duration_ms, MARK_HZ if is_mark else SPACE_HZ, phase)
        all_samples.extend(samples)

    add(5 * BIT_MS, True)  # idle preamble
    for start_bit, data_bits, stop_bits in _text_to_bit_frames(text):
        add(BIT_MS, bool(start_bit))
        for bit in data_bits:
            add(BIT_MS, bool(bit))
        add(stop_bits * BIT_MS, True)
    add(5 * BIT_MS, True)  # idle postamble

    return struct.pack(f"<{len(all_samples)}h", *all_samples)


def _feed_in_chunks(decoder, pcm_bytes, chunk_sizes):
    out = []
    i = 0
    while i < len(pcm_bytes):
        size = chunk_sizes[i % len(chunk_sizes)]
        out.append(decoder.feed(pcm_bytes[i:i + size]))
        i += size
    return "".join(out)


def test_basic_letters():
    text = "HELLO WORLD"
    pcm = encode_rtty(text)
    decoder = RttyDecoder(SAMPLE_RATE)
    decoded = _feed_in_chunks(decoder, pcm, [37, 101, 250, 512])
    assert decoded == text, f"expected {text!r}, got {decoded!r}"


def test_figures_shift():
    text = "CQ CQ DE TEST 599 599"
    pcm = encode_rtty(text)
    decoder = RttyDecoder(SAMPLE_RATE)
    decoded = _feed_in_chunks(decoder, pcm, [64, 200])
    assert decoded == text, f"expected {text!r}, got {decoded!r}"


def test_noise_never_produces_garbage():
    import random
    rng = random.Random(42)
    noise = struct.pack(f"<{SAMPLE_RATE}h", *(rng.randint(-2000, 2000) for _ in range(SAMPLE_RATE)))
    decoder = RttyDecoder(SAMPLE_RATE)
    decoded = decoder.feed(noise)
    # Pure noise should very rarely pass both the start-bit AND stop-bit
    # framing checks -- a handful of false positives is acceptable, a
    # continuous stream of garbage is not.
    assert len(decoded) < 5, f"expected little/no output from noise, got {decoded!r}"


def test_back_to_back_messages():
    decoder = RttyDecoder(SAMPLE_RATE)
    pcm1 = encode_rtty("FIRST MSG")
    pcm2 = encode_rtty("SECOND MSG")
    decoded1 = _feed_in_chunks(decoder, pcm1, [128])
    decoded2 = _feed_in_chunks(decoder, pcm2, [128])
    assert decoded1 == "FIRST MSG"
    assert decoded2 == "SECOND MSG"


if __name__ == "__main__":
    test_basic_letters()
    test_figures_shift()
    test_noise_never_produces_garbage()
    test_back_to_back_messages()
    print("All rtty tests passed.")
