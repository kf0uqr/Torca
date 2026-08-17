"""
Round-trip test for sstv.py: a small test-only SSTV *encoder* (this
file only -- SSTV sending isn't a real product feature here any more
than CW sending goes through cw.py's decoder path) synthesizes known-
good SSTV audio (VIS header + a procedural test image, per mode, via
continuous-phase tone synthesis to avoid phase-click artifacts), feeds
it through the real SstvDecoder in irregular chunk sizes, and checks
the decoded image against the source.

No radio/Qt/asyncio involved at all -- pure DSP round-trip, the
fastest and most valuable layer of verification (see the approved
plan's Verification section for the reasoning; a separate, smaller-
scope test covers radio_worker.py's attach/detach wiring).

Run directly: ./bin/python3 test_sstv.py
"""

import sys
import time

import numpy as np

import sstv


def synthesize_tone_sequence(freq_ms_pairs, sample_rate: int) -> np.ndarray:
    """Continuous-phase sine synthesis from a list of (freq_hz,
    duration_ms) segments -- integrates frequency into phase (cumsum)
    rather than restarting phase at 0 per segment, so there are no
    audible/decodable clicks at segment boundaries. Same technique
    described in the approved plan."""
    pieces = []
    for freq_hz, duration_ms in freq_ms_pairs:
        n = max(1, int(round(duration_ms / 1000.0 * sample_rate)))
        pieces.append(np.full(n, freq_hz, dtype=np.float64))
    freq_per_sample = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float64)
    phase = 2.0 * np.pi * np.cumsum(freq_per_sample) / sample_rate
    return np.sin(phase)


def synthesize_vis_header(vis_code: int, sample_rate: int) -> np.ndarray:
    bits = [(vis_code >> i) & 1 for i in range(7)]
    parity = sum(bits) % 2
    pairs = [
        (sstv.FREQ_VIS_LEADER, sstv._VIS_LEADER_MS),
        (sstv.FREQ_SYNC, sstv._VIS_BREAK_MS),
        (sstv.FREQ_VIS_LEADER, sstv._VIS_LEADER_MS),
        (sstv.FREQ_SYNC, sstv._VIS_BIT_MS),  # start bit
    ]
    for bit in bits:
        pairs.append((sstv.FREQ_VIS_BIT_1 if bit else sstv.FREQ_VIS_BIT_0, sstv._VIS_BIT_MS))
    pairs.append((sstv.FREQ_VIS_BIT_1 if parity else sstv.FREQ_VIS_BIT_0, sstv._VIS_BIT_MS))
    pairs.append((sstv.FREQ_SYNC, sstv._VIS_BIT_MS))  # stop bit
    return synthesize_tone_sequence(pairs, sample_rate)


def make_test_image(width: int, height: int) -> np.ndarray:
    """Procedural test pattern: flat color bands (easy to verify exactly)
    plus a horizontal gradient band (exercises genuinely continuous
    frequency variation, not just constant-tone segments)."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    third = max(1, height // 3)
    img[0:third, :, :] = [200, 40, 40]        # red-ish band
    img[third:2 * third, :, :] = [40, 180, 60]  # green-ish band
    gradient = np.linspace(0, 255, width, dtype=np.uint8)
    img[2 * third:, :, 0] = gradient
    img[2 * third:, :, 1] = 255 - gradient
    img[2 * third:, :, 2] = 128
    return img


def _row_channel_values(mode: sstv.SstvModeSpec, image: np.ndarray, row: int, channel_index: int) -> np.ndarray:
    channel_name = mode.channel_order[channel_index]
    r = image[row, :, 0].astype(np.float64)
    g = image[row, :, 1].astype(np.float64)
    b = image[row, :, 2].astype(np.float64)
    if mode.color_space == "rgb":
        return {"R": r, "G": g, "B": b}[channel_name]
    # robot_yuv
    y = 0.299 * r + 0.587 * g + 0.114 * b
    if channel_name == "Y":
        return y
    is_cr_line = (row % 2) == 0
    if is_cr_line:
        return np.clip(128.0 + 0.713 * (r - y), 0, 255)
    return np.clip(128.0 + 0.564 * (b - y), 0, 255)


def synthesize_image_audio(mode: sstv.SstvModeSpec, image: np.ndarray, sample_rate: int) -> np.ndarray:
    total_ms = mode.line_ms * mode.height
    total_samples = int(round(total_ms / 1000.0 * sample_rate)) + sample_rate  # small safety margin
    out = np.zeros(total_samples, dtype=np.float64)
    pos = 0
    last_phase = 0.0
    for row in range(mode.height):
        for segment in mode.segments:
            n = max(1, int(round(segment.duration_ms / 1000.0 * sample_rate)))
            if segment.kind == "sync":
                freq_per_sample = np.full(n, sstv.FREQ_SYNC, dtype=np.float64)
            elif segment.kind == "porch":
                freq_per_sample = np.full(n, sstv.FREQ_BLACK, dtype=np.float64)
            else:
                values = _row_channel_values(mode, image, row, segment.channel_index)
                pixel_freqs = sstv.value_to_freq(values)
                width = mode.width
                bounds = np.round(np.arange(width + 1) * (n / float(width))).astype(np.int64)
                freq_per_sample = np.empty(n, dtype=np.float64)
                for i in range(width):
                    freq_per_sample[bounds[i]:bounds[i + 1]] = pixel_freqs[i]
            phase = last_phase + 2.0 * np.pi * np.cumsum(freq_per_sample) / sample_rate
            out[pos:pos + n] = np.sin(phase)
            last_phase = phase[-1]
            pos += n
    # Trailing padding, continuing the last tone in-phase (not silence --
    # a silent/zero-amplitude tail would feed the FmDemodulator an
    # unstable near-zero-magnitude phase and inject noise): the real
    # decoder never resyncs mid-transmission, so any front-boundary
    # slop between VIS-header consumption and the true header/image
    # split (the VIS tracking states' block granularity doesn't line
    # up exactly with the real 300/10/300ms leader/break timing, so a
    # handful of samples of image audio routinely get consumed as part
    # of the VIS header -- confirmed live, ~50 samples at 11025 Hz for
    # Robot 36) is a constant offset that persists for the rest of the
    # transmission, making the decoder want samples slightly past the
    # image's exact theoretical length. Real RX audio never runs out;
    # only this synthetic, exactly-sized buffer needs the slack.
    pad_n = max(1, int(round(0.25 * sample_rate)))
    freq_per_sample = np.full(pad_n, sstv.FREQ_SYNC, dtype=np.float64)
    phase = last_phase + 2.0 * np.pi * np.cumsum(freq_per_sample) / sample_rate
    out = np.concatenate([out[:pos], np.sin(phase)])
    return out


def to_pcm16_bytes(audio_float: np.ndarray) -> bytes:
    clipped = np.clip(audio_float, -1.0, 1.0)
    return (clipped * 32000).astype(np.int16).tobytes()


def feed_in_irregular_chunks(decoder: sstv.SstvDecoder, pcm_bytes: bytes, rng: np.random.Generator) -> None:
    pos = 0
    n = len(pcm_bytes)
    while pos < n:
        chunk = int(rng.integers(200, 2000)) * 2  # even byte count (int16 samples)
        chunk = min(chunk, n - pos)
        decoder.feed(pcm_bytes[pos:pos + chunk])
        pos += chunk


def test_mode_round_trip(mode: sstv.SstvModeSpec, sample_rate: int) -> None:
    print(f"--- {mode.name} @ {sample_rate} Hz ---")
    image = make_test_image(mode.width, mode.height)
    t0 = time.monotonic()
    header_audio = synthesize_vis_header(mode.vis_code, sample_rate)
    image_audio = synthesize_image_audio(mode, image, sample_rate)
    full_audio = np.concatenate([header_audio, image_audio])
    pcm = to_pcm16_bytes(full_audio)
    t1 = time.monotonic()
    print(f"  synthesized {len(pcm)} bytes ({len(full_audio) / sample_rate:.1f}s of audio) in {t1 - t0:.2f}s")

    decoder = sstv.SstvDecoder(sample_rate)
    rng = np.random.default_rng(42)
    feed_in_irregular_chunks(decoder, pcm, rng)
    t2 = time.monotonic()
    print(f"  decoded in {t2 - t1:.2f}s; status={decoder.status!r}")

    assert decoder.detected_mode_name == mode.name, (
        f"expected mode {mode.name!r}, got {decoder.detected_mode_name!r} (VIS detection failed)"
    )
    assert decoder.is_image_complete, f"image did not complete; status={decoder.status!r}"
    assert decoder.image is not None
    err = np.mean(np.abs(decoder.image.astype(np.int32) - image.astype(np.int32)))
    print(f"  mean abs pixel error: {err:.2f} / 255")
    assert err < 15.0, f"mean abs pixel error too high: {err:.2f}"
    print(f"  PASSED\n")


def test_noise_rejection(sample_rate: int) -> None:
    print("--- noise rejection ---")
    rng = np.random.default_rng(7)
    noise = rng.normal(0, 0.3, size=sample_rate * 3)  # 3s of noise
    pcm = to_pcm16_bytes(noise)
    decoder = sstv.SstvDecoder(sample_rate)
    decoder.feed(pcm)
    assert decoder.detected_mode_name is None, "false-positive VIS detection on pure noise"
    assert not decoder.is_image_complete
    print("  PASSED\n")


def test_back_to_back_images(sample_rate: int) -> None:
    print("--- back-to-back images (no manual reset) ---")
    mode_a = sstv.ROBOT36
    mode_b = sstv.MARTIN_M1
    small_a = _shrink_mode(mode_a, 24)
    small_b = _shrink_mode(mode_b, 24)
    image_a = make_test_image(small_a.width, small_a.height)
    image_b = make_test_image(small_b.width, small_b.height)

    audio_a = np.concatenate([
        synthesize_vis_header(small_a.vis_code, sample_rate),
        synthesize_image_audio(small_a, image_a, sample_rate),
    ])
    silence = np.zeros(int(sample_rate * 0.5), dtype=np.float64)
    audio_b = np.concatenate([
        synthesize_vis_header(small_b.vis_code, sample_rate),
        synthesize_image_audio(small_b, image_b, sample_rate),
    ])
    full = np.concatenate([audio_a, silence, audio_b])
    pcm = to_pcm16_bytes(full)

    decoder = sstv.SstvDecoder(sample_rate)
    orig_modes = dict(sstv.SSTV_MODES)
    sstv.SSTV_MODES[small_a.vis_code] = small_a
    sstv.SSTV_MODES[small_b.vis_code] = small_b
    try:
        rng = np.random.default_rng(99)
        completions = []
        pos = 0
        n = len(pcm)
        while pos < n:
            chunk = int(rng.integers(500, 3000)) * 2
            chunk = min(chunk, n - pos)
            decoder.feed(pcm[pos:pos + chunk])
            pos += chunk
            if decoder.is_image_complete and (not completions or completions[-1] != decoder.detected_mode_name):
                completions.append(decoder.detected_mode_name)
        print(f"  completions observed: {completions}")
        assert small_a.name in completions, "first image never completed"
        assert small_b.name in completions, "second image never completed (no auto-reset?)"
        print("  PASSED\n")
    finally:
        sstv.SSTV_MODES.clear()
        sstv.SSTV_MODES.update(orig_modes)


def _shrink_mode(mode: sstv.SstvModeSpec, height: int) -> sstv.SstvModeSpec:
    """Test-only: a copy of a real mode with fewer scan lines, for fast
    back-to-back testing (full-height back-to-back is exercised by
    test_mode_round_trip's per-mode runs, just not two consecutive
    full images in one test)."""
    from dataclasses import replace
    return replace(mode, height=height)


def main():
    ok = True
    sample_rate = 11025  # low enough to run fast, high enough for reasonable pixel-timing resolution
    try:
        for mode in (sstv.ROBOT36, sstv.MARTIN_M1, sstv.SCOTTIE_S1):
            test_mode_round_trip(mode, sample_rate)
        test_noise_rejection(sample_rate)
        test_back_to_back_images(sample_rate)
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        ok = False
    if ok:
        print("ALL SSTV TESTS PASSED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
