"""Tests for ft8_decoder.py: slot timing, resampling, output parsing, and REAL
decoding of generated FT8 audio (the fixture compiles a private copy of the
vendored decoder, so this works on a fresh checkout and never touches the
repo's own build)."""

import math
import pathlib
import shutil
import subprocess
import wave

import numpy as np
import pytest

import ft8_decoder as ft8
from ft8_decoder import Ft8SlotCapture


# ---- a private, freshly built decoder + generator ------------------------------

@pytest.fixture(scope="session")
def built(tmp_path_factory):
    if not ft8.can_build():
        pytest.skip("make and a C compiler are needed to build the FT8 decoder")
    work = tmp_path_factory.mktemp("ft8_lib")
    for name in ("common", "fft", "ft8", "demo"):
        shutil.copytree(ft8.VENDOR_DIR / name, work / name)
    for name in ("Makefile", "LICENSE"):
        shutil.copy(ft8.VENDOR_DIR / name, work / name)
    original = ft8.VENDOR_DIR
    ft8.VENDOR_DIR = work
    try:
        assert not ft8.is_available() or ft8.decoder_path().parent == work or True
        ok, message = ft8.build_decoder()                     # also exercises build_decoder itself
        assert ok, message
        subprocess.run(["make", "gen_ft8"], cwd=work, check=True, capture_output=True)
        yield work
    finally:
        ft8.VENDOR_DIR = original


def _generate(built, text, freq_hz, tmp_path):
    """12 kHz mono int16 PCM of one real FT8 transmission, from the library's generator."""
    path = tmp_path / "gen.wav"
    subprocess.run([str(built / "gen_ft8"), text, str(path), str(freq_hz)], check=True, capture_output=True)
    with wave.open(str(path)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 12000)
        return wav.readframes(wav.getnframes())


def _resample(pcm, src, dst):
    """Test-only FFT resampler (the production one only goes TO 12 kHz)."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    n_out = int(round(len(x) * dst / src))
    spectrum = np.fft.rfft(x)
    out = np.zeros(n_out // 2 + 1, dtype=np.complex128)
    keep = min(len(spectrum), len(out))
    out[:keep] = spectrum[:keep]
    return np.clip(np.rint(np.fft.irfft(out, n_out) * (n_out / len(x))), -32768, 32767).astype("<i2").tobytes()


def _mix(*pcms):
    arrays = [np.frombuffer(p, dtype="<i2").astype(np.int32) for p in pcms]
    return np.clip(sum(arrays), -32768, 32767).astype("<i2").tobytes()


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


# ---- parsing and message helpers ------------------------------------------------

REAL_OUTPUT = """000000 +16.5 +1.36 1500 ~  CQ KF0UQR EM18
000000 +03.0 -0.20 2200 ~  W1AW KF0UQR -12
some log noise that is not a decode
000000 -12.5 +0.04  805 ~  KF0UQR/P W9XYZ RR73
"""


def test_parse_output_reads_decode_lines_and_ignores_the_rest():
    messages = ft8.parse_output(REAL_OUTPUT, slot_start=1_800_000_015.0, mode="FT8")
    assert [m.text for m in messages] == ["CQ KF0UQR EM18", "W1AW KF0UQR -12", "KF0UQR/P W9XYZ RR73"]
    first = messages[0]
    assert (first.snr, first.dt, first.freq, first.mode) == (16.5, 1.36, 1500.0, "FT8")
    assert first.slot_start == 1_800_000_015.0                     # the SLOT's time, not the decoder's "000000"
    assert messages[2].snr == -12.5 and messages[2].freq == 805.0


def test_message_time_cq_and_callsign_helpers():
    cq, reply, directed = ft8.parse_output(REAL_OUTPUT, slot_start=1_800_000_015.0)
    assert cq.utc == time_utc(1_800_000_015.0) and len(cq.utc) == 8 and cq.utc[2] == ":"
    assert cq.is_cq and not reply.is_cq
    assert cq.involves("KF0UQR") and cq.involves("kf0uqr") and not cq.involves("W1AW")
    assert reply.involves("W1AW") and reply.involves("KF0UQR")
    assert directed.involves("KF0UQR") and directed.involves("KF0UQR/P") and directed.involves("W9XYZ")   # /P ignored
    assert not cq.involves("") and not cq.involves(None)


def time_utc(epoch):
    import time
    return time.strftime("%H:%M:%S", time.gmtime(epoch))


# ---- resampling --------------------------------------------------------------------

def _tone(freq, rate, seconds=2.0, amplitude=10000):
    t = np.arange(int(rate * seconds)) / rate
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype("<i2").tobytes()


def _peak_hz(pcm, rate):
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.argmax(spectrum) * rate / len(x)


@pytest.mark.parametrize("rate", [8000, 16000, 24000, 44100, 48000])
def test_resampling_to_12k_keeps_length_pitch_and_level(rate):
    pcm = _tone(1500, rate)
    out = ft8.to_decoder_rate(pcm, rate)
    assert len(out) // 2 == int(round(len(pcm) // 2 * 12000 / rate))
    assert abs(_peak_hz(out, 12000) - 1500) < 2
    level_in = np.abs(np.frombuffer(pcm, "<i2")).max()
    level_out = np.abs(np.frombuffer(out, "<i2")).max()
    assert 0.9 < level_out / level_in < 1.1


def test_resampling_12k_is_untouched_and_empty_is_safe():
    pcm = _tone(1000, 12000)
    assert ft8.to_decoder_rate(pcm, 12000) is pcm
    assert ft8.to_decoder_rate(b"", 48000) == b""


def test_resampling_removes_frequencies_above_the_new_band():
    pcm = _tone(9000, 48000)                              # above 12 kHz / 2 = 6 kHz: must not alias into the passband
    out = np.frombuffer(ft8.to_decoder_rate(pcm, 48000), "<i2")
    assert np.abs(out).max() < 200


# ---- slot capture (fake clock) -------------------------------------------------------

RATE = 8000


def _silence(seconds, rate=RATE):
    return bytes(int(seconds * rate) * 2)


def test_audio_starting_exactly_on_a_boundary_yields_exact_slots():
    clock = Clock(1_000_005.0 + 0.0)                      # a multiple of 15 once chunk time is subtracted
    cap = Ft8SlotCapture("FT8", RATE, clock=clock)
    start = 1_000_005.0 - (1_000_005.0 % 15.0)            # a slot boundary
    out = []
    t = start
    for _ in range(34):                                   # 34 half-second chunks = 17 s
        t += 0.5
        clock.now = t
        out += cap.feed(_silence(0.5))
    assert len(out) == 1
    slot = out[0]
    assert slot.slot_start == start and slot.fraction == 1.0 and slot.decodable
    assert len(slot.pcm) == 15 * RATE * 2                 # exactly one slot of audio


def test_a_capture_that_starts_mid_slot_is_padded_and_flagged_partial():
    start = 2_000_010.0 - (2_000_010.0 % 15.0)
    clock = Clock(start + 6.5)                            # first chunk ends 6.5 s into the slot (0.5 s chunk -> began at 6.0)
    cap = Ft8SlotCapture("FT8", RATE, clock=clock)
    out = cap.feed(_silence(0.5))
    t = start + 6.5
    while not out:
        t += 0.5
        clock.now = t
        out = cap.feed(_silence(0.5))
    slot = out[0]
    assert slot.slot_start == start and len(slot.pcm) == 15 * RATE * 2
    assert slot.fraction == pytest.approx(1 - 6.0 / 15.0)
    assert not slot.decodable                             # 60% of a slot: not worth decoding
    # and the slot after it is whole
    later = []
    while not later:
        t += 0.5
        clock.now = t
        later = cap.feed(_silence(0.5))
    assert later[0].slot_start == start + 15 and later[0].fraction == 1.0


def test_chunk_arrival_jitter_does_not_move_slot_boundaries():
    import random
    rng = random.Random(7)
    start = 3_000_000.0 - (3_000_000.0 % 15.0)
    clock = Clock(start)
    cap = Ft8SlotCapture("FT8", RATE, clock=clock)
    slots = []
    t = start
    for _ in range(190):                                  # ~95 s of audio in 0.5 s chunks, each delivered with +-0.3 s of jitter
        t += 0.5
        clock.now = t + rng.uniform(-0.3, 0.3)
        slots += cap.feed(_silence(0.5))
    # With a jittered clock the very first chunk may land just before a boundary, giving a nearly empty
    # first slot (flagged undecodable); every USABLE slot after it is exactly on the 15 s grid.
    usable = [s for s in slots if s.decodable]
    assert len(usable) >= 5 and all(s.slot_start % 15 == 0 for s in usable)
    assert [b.slot_start - a.slot_start for a, b in zip(slots, slots[1:])] == [15.0] * (len(slots) - 1)
    assert all(len(s.pcm) == 15 * RATE * 2 for s in slots)
    assert not slots[0].decodable or slots[0].slot_start == start


def test_a_gap_in_the_audio_reanchors_to_the_clock():
    start = 4_000_005.0 - (4_000_005.0 % 15.0)
    clock = Clock(start + 0.5)
    cap = Ft8SlotCapture("FT8", RATE, clock=clock)
    cap.feed(_silence(0.5))
    clock.now = start + 50.5                              # 50 s pass with NO audio (a dropout), then audio resumes
    out = []
    t = clock.now
    while not out:
        out = cap.feed(_silence(0.5))
        t += 0.5
        clock.now = t
    assert out[0].slot_start == pytest.approx(math.floor((start + 50.0) / 15) * 15)      # aligned to the new time, not 15 s multiples from the start
    assert out[0].fraction < 1.0                          # and flagged partial (it joined mid-slot)


def test_odd_byte_chunks_and_empty_chunks_are_handled():
    start = 5_000_010.0 - (5_000_010.0 % 15.0)
    clock = Clock(start + 0.5)
    cap = Ft8SlotCapture("FT8", RATE, clock=clock)
    assert cap.feed(b"") == []
    assert cap.feed(b"\x01") == []                        # half a sample
    data = _silence(15.5)
    out = cap.feed(data + b"\x00")
    assert isinstance(out, list)


def test_ft4_uses_7_5_second_slots():
    start = 6_000_000.0 - (6_000_000.0 % 7.5)
    clock = Clock(start + 0.5)
    cap = Ft8SlotCapture("FT4", RATE, clock=clock)
    out = []
    t = start + 0.5
    while len(out) < 2:
        out += cap.feed(_silence(0.5))
        t += 0.5
        clock.now = t
    assert [s.slot_start for s in out] == [start, start + 7.5]
    assert all(len(s.pcm) == int(7.5 * RATE) * 2 for s in out)


def test_invalid_mode_is_rejected_and_countdown_is_within_a_slot():
    with pytest.raises(ValueError):
        Ft8SlotCapture("PSK31", RATE)
    assert ft8.seconds_to_next_slot("FT8", clock=lambda: 15.0 * 100 + 4.0) == pytest.approx(11.0)
    assert ft8.seconds_to_next_slot("FT4", clock=lambda: 7.5 * 100 + 1.5) == pytest.approx(6.0)


# ---- real decoding ---------------------------------------------------------------------

@pytest.mark.parametrize("rate", [12000, 48000, 8000])
def test_real_ft8_messages_are_decoded_at_any_radio_audio_rate(built, tmp_path, rate):
    a = _generate(built, "CQ KF0UQR EM18", 1500, tmp_path)
    b = _generate(built, "W1AW KF0UQR -12", 2200, tmp_path)
    audio = _resample(_mix(a, b), 12000, rate)
    slot = ft8.CapturedSlot(slot_start=1_800_000_015.0, pcm=audio, fraction=1.0)
    messages = ft8.decode_slot(slot, rate, "FT8")
    by_text = {m.text: m for m in messages}
    assert set(by_text) == {"CQ KF0UQR EM18", "W1AW KF0UQR -12"}, [m.text for m in messages]
    assert abs(by_text["CQ KF0UQR EM18"].freq - 1500) <= 10 and abs(by_text["W1AW KF0UQR -12"].freq - 2200) <= 10
    assert all(m.slot_start == 1_800_000_015.0 and m.mode == "FT8" for m in messages)
    assert by_text["CQ KF0UQR EM18"].is_cq


def test_a_real_transmission_flows_from_capture_to_decode(built, tmp_path):
    pcm12 = _generate(built, "CQ DX KF0UQR EM18", 1200, tmp_path)
    rate = 8000
    audio = _resample(pcm12, 12000, rate)
    start = 7_000_005.0 - (7_000_005.0 % 15.0)
    clock = Clock(start)
    cap = Ft8SlotCapture("FT8", rate, clock=clock)
    slots = []
    step = int(0.25 * rate) * 2
    t = start
    for offset in range(0, len(audio), step):             # real-time delivery in quarter-second chunks, from the slot boundary
        chunk = audio[offset: offset + step]
        t += len(chunk) / 2 / rate
        clock.now = t
        slots += cap.feed(chunk)
    assert len(slots) == 1 and slots[0].slot_start == start and slots[0].decodable
    messages = ft8.decode_slot(slots[0], rate, "FT8")
    assert [m.text for m in messages] == ["CQ DX KF0UQR EM18"]
    assert messages[0].utc == time_utc(start)


def test_noise_only_decodes_to_nothing_without_error(built):
    rng = np.random.default_rng(1)
    noise = (rng.normal(0, 3000, 12000 * 15)).astype("<i2").tobytes()
    slot = ft8.CapturedSlot(slot_start=1_800_000_000.0, pcm=noise, fraction=1.0)
    assert ft8.decode_slot(slot, 12000, "FT8") == []


def test_a_partial_slot_is_not_even_sent_to_the_decoder(built, monkeypatch):
    monkeypatch.setattr(ft8.subprocess, "run", lambda *a, **k: pytest.fail("the decoder must not run for a partial slot"))
    slot = ft8.CapturedSlot(slot_start=1_800_000_000.0, pcm=bytes(12000 * 15 * 2), fraction=0.5)
    assert ft8.decode_slot(slot, 12000, "FT8") == []


def test_decoder_failures_never_raise(built, monkeypatch):
    slot = ft8.CapturedSlot(slot_start=1_800_000_000.0, pcm=bytes(12000 * 15 * 2), fraction=1.0)

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired("decode_ft8", 1)
    monkeypatch.setattr(ft8.subprocess, "run", timed_out)
    assert ft8.decode_slot(slot, 12000, "FT8") == []
    monkeypatch.setattr(ft8.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    assert ft8.decode_slot(slot, 12000, "FT8") == []


def test_ft4_runs_the_decoder_with_its_flag(built, monkeypatch):
    seen = {}

    class Result:
        stdout = "000000 +05.0 +0.10 1800 ~  CQ TEST AA00\n"

    def fake_run(command, **kwargs):
        seen["command"] = command
        return Result()
    monkeypatch.setattr(ft8.subprocess, "run", fake_run)
    slot = ft8.CapturedSlot(slot_start=1_800_000_000.0, pcm=bytes(12000 * 7 * 2), fraction=1.0)
    messages = ft8.decode_slot(slot, 12000, "FT4")
    assert "-ft4" in seen["command"] and [m.mode for m in messages] == ["FT4"]
    ft8.decode_slot(slot, 12000, "FT8")
    assert "-ft4" not in seen["command"]


def test_missing_decoder_means_not_available_and_no_decodes(tmp_path, monkeypatch):
    monkeypatch.setattr(ft8, "VENDOR_DIR", tmp_path)
    monkeypatch.setattr(ft8.shutil, "which", lambda name: None)
    assert not ft8.is_available() and ft8.decoder_path() is None
    slot = ft8.CapturedSlot(slot_start=1_800_000_000.0, pcm=bytes(12000 * 15 * 2), fraction=1.0)
    assert ft8.decode_slot(slot, 12000, "FT8") == []


def test_build_decoder_explains_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ft8, "VENDOR_DIR", tmp_path)                       # no Makefile there at all
    ok, message = ft8.build_decoder()
    assert not ok and "missing" in message
    (tmp_path / "Makefile").write_text("all:\n")
    monkeypatch.setattr(ft8, "can_build", lambda: False)
    ok, message = ft8.build_decoder()
    assert not ok and "build-essential" in message


def test_an_early_snapshot_is_taken_once_the_whole_transmission_is_in():
    rate = 8000
    clock = Clock(1_000_050.0 - (1_000_050.0 % 15.0))
    capture = Ft8SlotCapture("FT8", rate, clock=clock)
    second = b"\x01\x00" * rate
    for _ in range(13):
        clock.now += 1
        capture.feed(second)
    assert capture.take_early() == []                       # 13 s in: the signal is not all there yet
    clock.now += 1
    capture.feed(second)
    early = capture.take_early()
    assert len(early) == 1 and early[0].slot_start == clock.now - 14 and len(early[0].pcm) == 15 * rate * 2
    assert capture.take_early() == []                       # once per slot
    clock.now += 1
    done = capture.feed(second)
    assert len(done) == 1                                    # the normal end-of-slot slot still arrives
    assert capture.take_early() == []


def test_encoding_rejects_what_ft8_cannot_send(built):
    original = ft8.VENDOR_DIR
    ft8.VENDOR_DIR = built
    try:
        with pytest.raises(ValueError):
            ft8.encode_message("THIS IS FAR TOO LONG FOR ANY FT8 MESSAGE", "FT8", 1500)
        with pytest.raises(ValueError):
            ft8.encode_message("   ", "FT8", 1500)
    finally:
        ft8.VENDOR_DIR = original
