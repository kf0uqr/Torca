"""Round-trip and robustness tests for psk31.py -- no radio/Qt/asyncio
involved. Synthesizes known-good BPSK31 PCM via build_psk31_pcm and
confirms Psk31Decoder recovers the original text back out, including
under conditions a real decode session will actually see: irregular
feed() chunk sizes, arbitrary sample-rate/timing misalignment between
where the decoder starts listening and where the transmitted symbols
actually landed, additive noise, and no signal at all (must not
produce spurious/garbage characters)."""

import numpy as np

from psk31 import (
    Psk31Decoder,
    build_psk31_pcm,
    text_to_varicode_bits,
    _VARICODE_DECODE,
    _VARICODE_ENCODE,
)

SAMPLE_RATE = 8000
TEST_TEXT = "CQ CQ DE KF0UQR K TEST MESSAGE 1234567890"


def _decode_all(pcm_bytes, sample_rate=SAMPLE_RATE, chunk=317):
    """Feeds pcm_bytes through a fresh Psk31Decoder in irregular-sized
    chunks (317 is not a multiple of 2, deliberately exercising the
    odd-byte-carry-over path every decoder in this project already has
    to handle) and returns the concatenated decoded text."""
    decoder = Psk31Decoder(sample_rate=sample_rate)
    out = []
    for i in range(0, len(pcm_bytes), chunk):
        out.append(decoder.feed(pcm_bytes[i:i + chunk]))
    return "".join(out)


def test_varicode_table_spot_checks():
    # Cross-checked against the ARRL's own PSK31 spec table -- see
    # psk31.py's module docstring for the exact source citations.
    assert _VARICODE_DECODE[0b1] == " "
    assert _VARICODE_DECODE[0b11] == "e"
    assert _VARICODE_DECODE[0b1011] == "a"
    assert _VARICODE_DECODE[0b1010101011] == "\x00"
    assert _VARICODE_ENCODE[" "] == "1"
    assert _VARICODE_ENCODE["e"] == "11"


def test_varicode_no_code_starts_or_ends_with_zero():
    # The property the whole self-synchronizing scheme depends on.
    for code_int in _VARICODE_DECODE:
        bits = bin(code_int)[2:]
        assert bits[0] == "1", bits
        assert bits[-1] == "1", bits
        assert "00" not in bits, bits


def test_basic_round_trip():
    pcm = build_psk31_pcm(TEST_TEXT, sample_rate=SAMPLE_RATE)
    assert _decode_all(pcm) == TEST_TEXT


def test_round_trip_at_multiple_sample_rates():
    for sample_rate in (8000, 12000, 44100, 48000):
        pcm = build_psk31_pcm(TEST_TEXT, sample_rate=sample_rate)
        assert _decode_all(pcm, sample_rate=sample_rate) == TEST_TEXT, sample_rate


def test_round_trip_all_varicode_printable_ascii():
    text = "".join(chr(c) for c in range(0x20, 0x7F))
    pcm = build_psk31_pcm(text, sample_rate=SAMPLE_RATE)
    assert _decode_all(pcm) == text


def test_decoder_syncs_despite_arbitrary_startup_offset():
    # Nothing guarantees the moment a decoder starts listening lines up
    # with the remote station's own symbol clock -- this is the whole
    # reason Psk31Decoder has a startup sync-acquisition step
    # (_try_acquire_sync) rather than just free-running from sample 0.
    pcm = build_psk31_pcm(TEST_TEXT, sample_rate=SAMPLE_RATE)
    for offset_samples in (1, 7, 50, 130, 255, 400, 1000):
        padded = (b"\x00" * (2 * offset_samples)) + pcm
        assert _decode_all(padded) == TEST_TEXT, offset_samples


def test_decoder_handles_leading_and_trailing_silence():
    text = "HELLO WORLD DE KF0UQR"
    pcm = build_psk31_pcm(text, sample_rate=SAMPLE_RATE)
    silence = np.zeros(SAMPLE_RATE * 2, dtype=np.int16).tobytes()
    assert _decode_all(silence + pcm + silence) == text


def test_pure_noise_produces_no_spurious_characters():
    rng = np.random.default_rng(7)
    for noise_std in (500, 2000, 5000):
        noise = rng.normal(0, noise_std, size=SAMPLE_RATE * 10).astype(np.int16)
        decoder = Psk31Decoder(sample_rate=SAMPLE_RATE)
        assert decoder.feed(noise.tobytes()) == ""


def test_real_signal_survives_moderate_noise():
    rng = np.random.default_rng(42)
    samples = np.frombuffer(build_psk31_pcm(TEST_TEXT, sample_rate=SAMPLE_RATE), dtype="<i2").astype(np.float64)
    for noise_std in (0, 500, 1500):
        noisy = np.clip(samples + rng.normal(0, noise_std, size=len(samples)), -32767, 32767).astype(np.int16)
        assert _decode_all(noisy.tobytes()) == TEST_TEXT, noise_std


def test_text_to_varicode_bits_has_preamble_and_postamble():
    bits = text_to_varicode_bits("e")
    assert bits.startswith("0" * 20)
    assert bits.endswith("1" * 20)
    # 'e' is 0b11, followed by the 2-zero-bit character separator.
    assert "1100" in bits


def test_empty_or_unsendable_text_raises():
    try:
        build_psk31_pcm("", sample_rate=SAMPLE_RATE)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty text")


if __name__ == "__main__":
    test_varicode_table_spot_checks()
    test_varicode_no_code_starts_or_ends_with_zero()
    test_basic_round_trip()
    test_round_trip_at_multiple_sample_rates()
    test_round_trip_all_varicode_printable_ascii()
    test_decoder_syncs_despite_arbitrary_startup_offset()
    test_decoder_handles_leading_and_trailing_silence()
    test_pure_noise_produces_no_spurious_characters()
    test_real_signal_survives_moderate_noise()
    test_text_to_varicode_bits_has_preamble_and_postamble()
    test_empty_or_unsendable_text_raises()
    print("All psk31 tests passed.")
