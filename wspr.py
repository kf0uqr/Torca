"""WSPR (Weak Signal Propagation Reporter) reception for TORCA.

WSPR stations send a tiny beacon -- callsign, 4-character grid square and power
-- every so often, in two-minute slots starting on EVEN minutes; the signal begins
a second into the slot and lasts 110.6 s. Listening is a way to see, with no
operator at the other end, who can hear whom on a band right now.

The signal decoding is done by `wsprd`, the decoder that ships with WSJT-X (install
the `wsjtx` package; on Debian/Ubuntu `sudo apt install wsjtx`). TORCA runs it as a
separate program on a 12 kHz recording of each slot and parses what it prints, the
same way the FT8 tool uses its decoder -- nothing of WSJT-X's code is copied into
TORCA. Everything here that is not that run (slot timing, parsing, distances) is
plain Python so it can be tested without it.

Its time stamps and the hashed-callsign memory live in a small folder of its own
(see DATA_DIR), so a callsign sent in full in one slot can fill in a "<...>" in a later one.
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
from typing import Optional

import adif
import ft8_decoder

SLOT_SECONDS = 120.0
SIGNAL_START = 1.0               # seconds into the slot
SIGNAL_SECONDS = 110.592         # 162 symbols of 8192 samples at 12 kHz
SYMBOLS = 162
SYMBOL_SAMPLES = 8192
TONE_SPACING_HZ = 12000 / 8192   # 1.4648 Hz
DECODER_RATE = ft8_decoder.DECODER_RATE
DATA_DIR = pathlib.Path.home() / ".icom_radio_app_cache" / "wspr"

# The standard WSPR dial frequencies (USB), in MHz: the signal sits 1400-1600 Hz above them.
BAND_DIAL_MHZ = {
    "160m": 1.8366, "80m": 3.5686, "60m": 5.2872, "40m": 7.0386, "30m": 10.1387, "20m": 14.0956,
    "17m": 18.1046, "15m": 21.0946, "12m": 24.9246, "10m": 28.1246, "6m": 50.2930, "2m": 144.4890,
}


def decoder_path():
    found = shutil.which("wsprd")
    return pathlib.Path(found) if found else None


def is_available():
    return decoder_path() is not None


def nearest_band(dial_hz):
    """(band name, dial MHz) of the WSPR band a radio frequency is on or beside, or None."""
    mhz = dial_hz / 1e6
    for band, dial in BAND_DIAL_MHZ.items():
        if dial - 0.002 <= mhz <= dial + 0.0035:
            return band, dial
    return None


@dataclass(frozen=True)
class WsprSpot:
    slot_start: float       # UTC epoch of the slot it was heard in
    snr: int                # dB in 2500 Hz
    dt: float               # seconds
    frequency_mhz: float    # absolute frequency of the signal
    drift: int              # Hz per minute
    callsign: str
    grid: str               # "" when the message did not carry one
    power_dbm: Optional[int]
    raw: str

    @property
    def utc(self):
        return time.strftime("%H:%M", time.gmtime(self.slot_start))

    @property
    def power_watts(self):
        return None if self.power_dbm is None else 10 ** (self.power_dbm / 10.0) / 1000.0


# ---- parsing what wsprd prints ----------------------------------------------------

# "0802  -23  0.3  14.097103  0  K1ABC FN42 37"  (time, snr, dt, MHz, drift, then the message)
_LINE = re.compile(r"^\s*(?P<time>\d+)\s+(?P<snr>-?\d+)\s+(?P<dt>-?[\d.]+)\s+(?P<freq>[\d.]+)\s+(?P<drift>-?\d+)\s+(?P<message>.+?)\s*$")
_GRID = re.compile(r"^[A-R]{2}\d{2}([A-X]{2})?$", re.I)


def parse_message(message):
    """(callsign, grid, power dBm) of a WSPR message: "K1ABC FN42 37", "PJ4/K1ABC 37"
    (no grid), "<PJ4/K1ABC> FK52UD 37" (a hashed callsign shown in brackets)."""
    tokens = message.split()
    if not tokens:
        return None
    callsign = tokens[0].strip("<>")
    grid, power = "", None
    for token in tokens[1:]:
        if _GRID.match(token):
            grid = token.upper()
        elif re.fullmatch(r"\d{1,2}", token):
            power = int(token)
    return callsign, grid, power


def parse_output(stdout, slot_start):
    spots = []
    for line in stdout.splitlines():
        match = _LINE.match(line)
        if not match or "<DecodeFinished>" in line:
            continue
        parsed = parse_message(match.group("message"))
        if parsed is None or not parsed[0] or parsed[0] == "...":
            continue
        callsign, grid, power = parsed
        spots.append(WsprSpot(
            slot_start=slot_start, snr=int(match.group("snr")), dt=float(match.group("dt")),
            frequency_mhz=float(match.group("freq")), drift=int(match.group("drift")),
            callsign=callsign, grid=grid, power_dbm=power, raw=line.strip(),
        ))
    return spots


# ---- decoding a slot -------------------------------------------------------------

def decode_slot(slot, sample_rate, dial_mhz, timeout=120.0, data_dir=None):
    """Decodes one captured two-minute slot (an ft8_decoder.CapturedSlot) with wsprd.
    Returns a list of WsprSpot (empty if the slot was too incomplete or wsprd is missing)."""
    binary = decoder_path()
    if binary is None or not slot.decodable:
        return []
    pcm = ft8_decoder.to_decoder_rate(slot.pcm, sample_rate)
    data = pathlib.Path(data_dir or DATA_DIR)
    data.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="torca_wspr_") as work:
        # wsprd reads the slot's date and time from the file NAME: YYMMDD_HHMM.wav
        path = os.path.join(work, time.strftime("%y%m%d_%H%M", time.gmtime(slot.slot_start)) + ".wav")
        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(DECODER_RATE)
            wav.writeframes(pcm)
        command = [str(binary), "-a", str(data), "-f", f"{dial_mhz:.6f}", "-w", path]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=work)
        except (subprocess.TimeoutExpired, OSError):
            return []
    return parse_output(result.stdout, slot.slot_start)


# ---- distance --------------------------------------------------------------------

def distance_km(my_grid, grid):
    """Great-circle distance between two grid squares in km, or None if either is unknown."""
    a, b = adif.grid_square_to_latlon(my_grid), adif.grid_square_to_latlon(grid)
    if a is None or b is None:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


# ---- transmitting: the beacon -----------------------------------------------------

# The power levels a WSPR message can carry, dBm (the message holds a number from this list, not any value).
VALID_POWERS_DBM = (0, 3, 7, 10, 13, 17, 20, 23, 27, 30, 33, 37, 40, 43, 47, 50, 53, 57, 60)
TX_AUDIO_RANGE_HZ = (1410, 1590)     # the 200 Hz WSPR sub-band is 1400-1600 Hz of audio; the signal itself is 6 Hz wide
_STANDARD_CALL = re.compile(r"^[A-Z0-9]{1,2}[0-9][A-Z]{1,3}$")     # no prefix/suffix forms: those need WSPR's other message types


def can_encode():
    """Transmitting needs wsprsim (it comes with WSJT-X) to turn a message into WSPR's channel symbols."""
    return shutil.which("wsprsim") is not None


def power_label(dbm):
    watts = 10 ** (dbm / 10.0) / 1000.0
    return f"{dbm} dBm  ({watts:.3g} W)" if watts >= 1 else f"{dbm} dBm  ({watts * 1000:.3g} mW)"


def beacon_message(callsign, grid, dbm):
    """The WSPR message text "KF0UQR EM18 37", or raises ValueError saying what is wrong with it."""
    callsign = (callsign or "").strip().upper()
    grid = (grid or "").strip().upper()[:4]
    if not _STANDARD_CALL.match(callsign):
        raise ValueError(f"'{callsign or 'no callsign'}' can't be sent as a standard WSPR message "
                         "(set your plain callsign in Profile; callsigns with a prefix or suffix aren't supported yet).")
    if not re.fullmatch(r"[A-R]{2}\d{2}", grid):
        raise ValueError("Your location isn't set (Profile), so there is no grid square to send.")
    if dbm not in VALID_POWERS_DBM:
        raise ValueError(f"{dbm} dBm is not a power WSPR can send.")
    return f"{callsign} {grid} {dbm}"


def beacon_audio(callsign, grid, dbm, audio_hz, level=0.3, sample_rate=48000):
    """One WSPR transmission (110.6 s) as mono 16-bit PCM at `sample_rate`: (message text, pcm bytes)."""
    message = beacon_message(callsign, grid, dbm)
    symbols = channel_symbols(message)
    return message, synthesize(symbols, audio_hz, amplitude=level, sample_rate=sample_rate, signal_only=True)


# ---- for tests: a WSPR signal -----------------------------------------------------

def channel_symbols(message):
    """The 162 four-level channel symbols for a message, from WSJT-X's own `wsprsim`
    (used by tests to make real signals)."""
    binary = shutil.which("wsprsim")
    if binary is None:
        raise RuntimeError("wsprsim is not installed")
    out = subprocess.run([binary, "-c", "-o", os.devnull, message], capture_output=True, text=True, timeout=30).stdout
    block = out.split("Channel symbols:")[1]
    symbols = [int(t) for t in block.split() if t.isdigit()][:SYMBOLS]
    if len(symbols) != SYMBOLS:
        raise ValueError(f"cannot encode {message!r}")
    return symbols


def synthesize(symbols, audio_hz=1500.0, amplitude=0.1, sample_rate=DECODER_RATE, signal_only=False):
    """One slot of audio (mono 16-bit PCM, two minutes) carrying `symbols` from 1 s in -- or, with
    signal_only, just the 110.6 s of signal itself."""
    import numpy as np
    per_symbol = int(round(SYMBOL_SAMPLES * sample_rate / DECODER_RATE))
    tones = audio_hz + np.repeat(np.array(symbols, dtype=float), per_symbol) * TONE_SPACING_HZ
    phase = 2 * math.pi * np.cumsum(tones) / sample_rate
    if signal_only:
        return (np.clip(np.sin(phase) * amplitude, -1, 1) * 32767).astype("<i2").tobytes()
    signal = np.zeros(int(SLOT_SECONDS * sample_rate))
    start = int(SIGNAL_START * sample_rate)
    signal[start:start + len(phase)] = np.sin(phase) * amplitude
    return (np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes()
