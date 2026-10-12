"""FT8 / FT4 decoding for TORCA, built on kgoba/ft8_lib (MIT) -- the same
approach as the author's rtlsdrweb project.

Unlike TORCA's continuous decoders (CW, RTTY, PSK31, APRS), FT8 and FT4 are
sent in fixed UTC time slots (15 s for FT8, 7.5 s for FT4) and decoded as a
whole: a transmission starts about half a second into its slot and the
message is recovered by LDPC error correction over the full slot. So the shape
here is:

    Ft8SlotCapture  buffers the radio's audio and hands back one complete,
                    UTC-aligned slot at a time (with its true start time);
    decode_slot     resamples that slot to the 12 kHz mono 16-bit WAV the
                    decoder expects, runs the vendored `decode_ft8` binary on
                    it, and parses the lines it prints.

The decoder is C source vendored under vendor/ft8_lib/ and compiled locally
(`make decode_ft8`, which needs make and a C compiler -- install.sh does it,
and the FT8 tool offers to if it is missing); the binary is not in git.

Things worth knowing about the decoder's output, which this module corrects or
works around:
- its time column is the wall-clock time it ran, not the slot's, so each
  message here carries the slot start time computed by the capture instead;
- its SNR is a rough estimate (the library's own source marks it TODO), so it
  is indicative, not WSJT-X-grade;
- it only reads 12 kHz audio (at any other rate it would read a fraction of
  the slot), hence the resampling;
- each run is a fresh process, so callsigns sent as hashes (<...>) that were
  heard fully in an EARLIER slot cannot be resolved.
"""

import math
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass

import numpy as np

APP_DIR = pathlib.Path(__file__).resolve().parent
VENDOR_DIR = APP_DIR / "vendor" / "ft8_lib"

MODES = ("FT8", "FT4")
SLOT_SECONDS = {"FT8": 15.0, "FT4": 7.5, "WSPR": 120.0}   # WSPR: wspr.py reuses this capture
DECODER_RATE = 12000  # the only rate decode_ft8 reads correctly

# A slot captured less than this fraction cannot contain a decodable
# transmission (it would be missing its first symbols), so it is not decoded.
MIN_DECODABLE_FRACTION = 0.8

# A transmission is over this long after its slot starts (it begins 0.5 s in and
# lasts 12.64 s for FT8, 5.04 s for FT4), so the slot can be decoded this early
# -- the answer is then ready in time to reply in the very next slot.
EARLY_DECODE_SECONDS = {"FT8": 13.8, "FT4": 5.8, "WSPR": 114.0}
TX_START_OFFSET = 0.5          # seconds into its slot that a transmission begins
SYMBOL_PERIOD = {"FT8": 0.16, "FT4": 0.048}
SYMBOLS = {"FT8": 79, "FT4": 105}

# Matches decode_ft8's printf: "%02d%02d%02d %+05.1f %+4.2f %4.0f ~  %s\n"
#   HHMMSS  SNR(dB)  dt(s)  freq(Hz) ~  message
_LINE_RE = re.compile(
    r"^(?P<time>\d{6})\s+(?P<snr>[+-]?[\d.]+)\s+(?P<dt>[+-]?[\d.]+)\s+(?P<freq>[\d.]+)\s*~\s*(?P<message>.*)$"
)


# ---- the decoder program ------------------------------------------------------

def decoder_path():
    """Path of the decode_ft8 program: the one built from the vendored source,
    else one on PATH, else None."""
    local = VENDOR_DIR / "decode_ft8"
    if local.is_file() and os.access(local, os.X_OK):
        return local
    found = shutil.which("decode_ft8")
    return pathlib.Path(found) if found else None


def is_available():
    return decoder_path() is not None


def generator_path():
    """Path of the gen_ft8 program (turns a message into audio), or None."""
    local = VENDOR_DIR / "gen_ft8"
    if local.is_file() and os.access(local, os.X_OK):
        return local
    found = shutil.which("gen_ft8")
    return pathlib.Path(found) if found else None


def can_transmit():
    return generator_path() is not None


def can_build():
    return shutil.which("make") is not None and (shutil.which("cc") or shutil.which("gcc")) is not None


def build_decoder(timeout=300):
    """Compiles the vendored decoder. Returns (ok, message). Needs make and a
    C compiler (Debian: `sudo apt install build-essential`)."""
    if not (VENDOR_DIR / "Makefile").is_file():
        return False, "The FT8 decoder source (vendor/ft8_lib) is missing from this installation."
    if not can_build():
        return False, "Building the FT8 decoder needs make and a C compiler. On Debian/Ubuntu: sudo apt install build-essential"
    try:
        result = subprocess.run(["make", "decode_ft8", "gen_ft8"], cwd=VENDOR_DIR, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"Building the FT8 decoder failed: {exc}"
    if result.returncode != 0 or not is_available() or not can_transmit():
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        return False, "Building the FT8 decoder failed:\n" + "\n".join(tail)
    return True, "FT8 decoder built."


# ---- decoded messages ---------------------------------------------------------

@dataclass(frozen=True)
class Ft8Message:
    slot_start: float   # UTC epoch seconds of the slot it was heard in
    mode: str
    snr: float          # dB, approximate
    dt: float           # seconds from the slot's nominal start
    freq: float         # audio frequency, Hz
    text: str

    @property
    def utc(self):
        return time.strftime("%H:%M:%S", time.gmtime(self.slot_start))

    @property
    def is_cq(self):
        return self.text.upper().startswith("CQ")

    def involves(self, callsign):
        """True if `callsign` (e.g. the operator's own) is one of the message's
        callsigns, ignoring /P, /M etc."""
        base = (callsign or "").strip().upper().split("/")[0]
        if not base:
            return False
        return any(token.split("/")[0] == base for token in self.text.upper().split())


def parse_output(stdout, slot_start, mode="FT8"):
    messages = []
    for line in stdout.splitlines():
        match = _LINE_RE.match(line.strip())
        if match:
            messages.append(Ft8Message(
                slot_start=slot_start, mode=mode, snr=float(match.group("snr")), dt=float(match.group("dt")),
                freq=float(match.group("freq")), text=match.group("message").strip(),
            ))
    return messages


# ---- audio ---------------------------------------------------------------------

def to_decoder_rate(pcm, sample_rate):
    """Mono 16-bit little-endian PCM at `sample_rate` -> the same at 12 kHz
    (FFT resampling: exact for the band-limited audio of a voice channel)."""
    return resample(pcm, sample_rate, DECODER_RATE)


def resample(pcm, from_rate, to_rate):
    """Mono 16-bit little-endian PCM from one sample rate to another."""
    if from_rate == to_rate or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    n_out = int(round(len(samples) * to_rate / from_rate))
    spectrum = np.fft.rfft(samples)
    resized = np.zeros(n_out // 2 + 1, dtype=np.complex128)
    keep = min(len(spectrum), len(resized))
    resized[:keep] = spectrum[:keep]
    out = np.fft.irfft(resized, n_out) * (n_out / len(samples))
    return np.clip(np.rint(out), -32768, 32767).astype("<i2").tobytes()


@dataclass(frozen=True)
class CapturedSlot:
    slot_start: float   # UTC epoch seconds
    pcm: bytes          # exactly one slot of audio, at the capture's own sample rate
    fraction: float     # how much of the slot was really captured (1.0 = all of it)

    @property
    def decodable(self):
        return self.fraction >= MIN_DECODABLE_FRACTION


class Ft8SlotCapture:
    """Cuts the radio's audio into whole UTC-aligned slots.

    Alignment is anchored to the wall clock once (when audio starts, or after a
    gap) and from then on slots are cut by COUNTING SAMPLES, so the jitter of
    network audio chunks cannot move a slot boundary. If the buffered audio
    drifts more than RESYNC_SECONDS from the clock (audio dropped out, or
    arrived in a burst) it is re-anchored. A capture that starts mid-slot is
    padded at the start and flagged by its `fraction`.

    `clock` is injectable so this can be tested without waiting for real slots.
    The first slot after starting is almost always partial -- it comes back
    with a small `fraction` and is not worth decoding."""

    RESYNC_SECONDS = 1.5

    def __init__(self, mode, sample_rate, clock=time.time):
        if mode not in SLOT_SECONDS:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        self.sample_rate = sample_rate
        self.slot_seconds = SLOT_SECONDS[mode]
        self._clock = clock
        self._bytes_per_slot = int(round(self.slot_seconds * sample_rate)) * 2
        self._buffer = bytearray()
        self._leftover = b""          # an odd trailing byte of the previous chunk
        self._slot_start = None       # epoch of the slot currently being filled
        self._fraction = 1.0          # how much of that slot is real audio
        self._early_done = False      # the early snapshot of the current slot has been taken
        self._early = []              # CapturedSlot snapshots waiting for take_early()

    def _buffered_end(self):
        return self._slot_start + len(self._buffer) / 2 / self.sample_rate

    def _anchor(self, chunk_start):
        self._slot_start = math.floor(chunk_start / self.slot_seconds) * self.slot_seconds
        elapsed = chunk_start - self._slot_start
        pad_samples = int(round(elapsed * self.sample_rate))
        self._buffer = bytearray(pad_samples * 2)
        self._fraction = max(0.0, 1.0 - elapsed / self.slot_seconds)
        self._early_done = False

    def feed(self, pcm_bytes):
        """Adds audio; returns a list (usually empty) of CapturedSlot completed."""
        data = self._leftover + bytes(pcm_bytes)
        usable = len(data) - (len(data) % 2)
        self._leftover = data[usable:]
        data = data[:usable]
        if not data:
            return []
        chunk_start = self._clock() - len(data) / 2 / self.sample_rate  # when its first sample was heard
        if self._slot_start is None or abs(self._buffered_end() - chunk_start) > self.RESYNC_SECONDS:
            self._anchor(chunk_start)
        self._buffer.extend(data)
        self._snapshot_if_early()
        completed = []
        while len(self._buffer) >= self._bytes_per_slot:
            completed.append(CapturedSlot(self._slot_start, bytes(self._buffer[: self._bytes_per_slot]), self._fraction))
            del self._buffer[: self._bytes_per_slot]
            self._slot_start += self.slot_seconds
            self._fraction = 1.0
            self._early_done = False
            self._snapshot_if_early()
        return completed

    def _snapshot_if_early(self):
        """Once enough of the slot has arrived that its whole transmission is in
        (see EARLY_DECODE_SECONDS), keeps a copy of it, padded with silence to a
        full slot, for take_early(). The completed slot still comes back from feed()."""
        if self._early_done or self._fraction < MIN_DECODABLE_FRACTION:
            return
        if len(self._buffer) / 2 / self.sample_rate < EARLY_DECODE_SECONDS[self.mode]:
            return
        self._early_done = True
        padded = bytes(self._buffer[: self._bytes_per_slot]).ljust(self._bytes_per_slot, b"\x00")
        self._early.append(CapturedSlot(self._slot_start, padded, self._fraction))

    def take_early(self):
        """The early snapshots (usually none) taken since the last call."""
        early, self._early = self._early, []
        return early


def seconds_to_next_slot(mode, clock=time.time):
    """Seconds until the next slot boundary (for a countdown)."""
    slot = SLOT_SECONDS[mode]
    now = clock()
    return slot - (now % slot)


# ---- decoding ------------------------------------------------------------------

def decode_slot(slot, sample_rate, mode, timeout=30.0):
    """Decodes one CapturedSlot; returns a list of Ft8Message (empty if nothing
    was decodable, the slot was too incomplete, or the decoder is missing)."""
    binary = decoder_path()
    if binary is None or not slot.decodable:
        return []
    pcm = to_decoder_rate(slot.pcm, sample_rate)
    fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix=f"torca_{mode.lower()}_")
    try:
        os.close(fd)
        with wave.open(wav_path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(DECODER_RATE)
            wav.writeframes(pcm)
        command = [str(binary)] + (["-ft4"] if mode == "FT4" else []) + [wav_path]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return []
        return parse_output(result.stdout, slot.slot_start, mode)
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass


# ---- encoding (transmit) ----------------------------------------------------------

def encode_message(text, mode, audio_hz, timeout=15.0):
    """Turns a message ("CQ KF0UQR EM17", "W1AW KF0UQR -10", ...) into the audio of
    one transmission: mono 16-bit little-endian PCM at 12 kHz holding just the
    signal (12.64 s of FT8, 5.04 s of FT4), its lowest tone at `audio_hz`.
    Raises ValueError if the message cannot be encoded (not a valid FT8/FT4
    message, or too long) and RuntimeError if the generator is not built."""
    binary = generator_path()
    if binary is None:
        raise RuntimeError("The FT8 encoder has not been built (use Build Decoder in the FT8 Tool).")
    text = " ".join(text.upper().split())
    if not text:
        raise ValueError("Nothing to send.")
    fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix=f"torca_tx_{mode.lower()}_")
    try:
        os.close(fd)
        command = [str(binary), text, wav_path, f"{float(audio_hz):.1f}"] + (["-ft4"] if mode == "FT4" else [])
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0 or "Cannot parse" in result.stdout:
            raise ValueError(f"'{text}' is not a message FT8/FT4 can send.")
        with wave.open(wav_path, "rb") as wav:
            frames = wav.readframes(wav.getnframes())
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass
    # gen_ft8 centres the signal in a whole slot; keep only the signal itself,
    # so the caller decides exactly when it starts.
    signal_samples = int(0.5 + SYMBOLS[mode] * SYMBOL_PERIOD[mode] * DECODER_RATE)
    silence = int((SLOT_SECONDS[mode] * DECODER_RATE - signal_samples) / 2)
    return frames[silence * 2: (silence + signal_samples) * 2]
