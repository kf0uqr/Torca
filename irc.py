"""
PySide6 GUI for controlling an Icom radio via rigplane, using a
dedicated worker thread for all async radio I/O.

Install:
    pip install rigplane PySide6

Why a worker thread instead of qasync:
    All rigplane calls are asyncio coroutines. RadioWorker (a QThread)
    owns its own asyncio event loop and never touches a single Qt
    widget directly -- it only ever talks to the GUI thread by
    emitting signals. Commands go the other way via
    asyncio.run_coroutine_threadsafe(), so calling worker.set_frequency(...)
    from a button click is safe and non-blocking.

    This shape scales cleanly to live telemetry: add a polling task
    in RadioWorker._poll_loop() for S-meter, add a signal for it, and
    later swap the polling loop for a push/subscribe pattern if
    rigplane exposes one -- the GUI code doesn't have to change.

Edit RADIO_HOST / USERNAME / PASSWORD below to match your radio's
network remote-control settings (Icom's "Network" menu -- these
credentials are set on the radio itself, not an Icom account).
"""

import asyncio
import importlib
import math
import os
import platform
import queue
import re
import shutil
import subprocess
import sys

from PySide6.QtCore import QThread, Signal, Slot, Qt, QRectF, QPointF, QSettings
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPainterPath, QImage, QRadialGradient, QPalette
from PySide6.QtNetwork import QHostAddress, QTcpServer
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QDialog,
    QDialogButtonBox,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QSlider,
    QComboBox,
    QPushButton,
    QMessageBox,
    QMenu,
    QFileDialog,
)

try:
    # pip install sounddevice (wraps PortAudio). Used both to list real
    # input/output devices in the connection dialog AND to actually open
    # the capture/playback streams -- using the same library for both
    # means the device the user picks is guaranteed to be openable, no
    # cross-library name matching involved.
    import sounddevice as sd
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    sd = None
    SOUNDDEVICE_AVAILABLE = False

from rigplane import create_radio, LanBackendConfig
try:
    # NOTE: rigplane's docs/PyPI page only show LanBackendConfig in examples --
    # a serial/USB config class isn't documented anywhere we could find. This
    # name and its kwargs (port, baud_rate, radio_addr) are inferred by
    # symmetry with LanBackendConfig, not confirmed. Verify with
    # `python -c "import rigplane; help(rigplane)"` before trusting the USB
    # path below.
    from rigplane import SerialBackendConfig
except ImportError:
    SerialBackendConfig = None

try:
    # Confirmed tier-1 protocol (rigplane's public-api-surface docs):
    # AudioTransport declares start_rx/stop_rx/start_tx/push_tx/stop_tx plus
    # format descriptors (audio_codec, audio_sample_rate, ...). What is NOT
    # documented anywhere we could find is how a *host* input/output device
    # (the thing this dialog lets you pick) binds to it -- there's no
    # confirmed "pass this device name/id in" call. So this import is used
    # below only to confirm the protocol exists and report its real method
    # names, not to pretend we've wired up working audio streaming.
    from rigplane import AudioTransport
except ImportError:
    AudioTransport = None

try:
    # Confirmed tier-1 protocol (rigplane's public-api-surface docs):
    # "Protocol for setting receiver levels: AF, RF gain, squelch." The
    # protocol's existence and purpose are confirmed; the exact method
    # names are NOT documented anywhere we could find (get_af_gain/
    # set_af_gain, our first guess, turned out to be wrong on at least
    # one install). See AF_GAIN_*/SQUELCH_*_CANDIDATES and
    # RadioWorker._setup_levels() below, which probe the connected radio
    # object for the first matching name among several plausible ones
    # instead of betting on a single guess.
    from rigplane import LevelsCapable
except ImportError:
    LevelsCapable = None

# Known radios and their factory-default CI-V address (hex, no "0x" prefix).
# Per rigplane's own supported-radios table: IC-7300 is USB CI-V only (no
# LAN); IC-9700 and IC-705 support both LAN and USB. These are just sane
# defaults for the dialog -- both fields stay editable regardless of model.
RADIO_PROFILES = {
    "IC-7300": {"addr_hex": "94", "default_connection": "usb"},
    "IC-9700": {"addr_hex": "A2", "default_connection": "network"},
    "IC-705": {"addr_hex": "A4", "default_connection": "network"},
}

# Amateur band edges (US band plan) each radio actually covers, as
# (label, low_hz, high_hz). IC-7300/IC-705 are HF+6m; IC-9700 is VHF/UHF
# (its 23cm/1.2GHz entry requires the optional UX-9100 module -- included
# here but may not apply to every unit). 60m is channelized in the US
# rather than a continuous band -- the range below spans the channel
# allocation for quick-tune purposes only; the operator is responsible for
# actually using a valid channel, this app doesn't enforce that.
HF_6M_BANDS = [
    ("160m", 1_800_000, 2_000_000),
    ("80m", 3_500_000, 4_000_000),
    ("60m", 5_330_000, 5_406_000),
    ("40m", 7_000_000, 7_300_000),
    ("30m", 10_100_000, 10_150_000),
    ("20m", 14_000_000, 14_350_000),
    ("17m", 18_068_000, 18_168_000),
    ("15m", 21_000_000, 21_450_000),
    ("12m", 24_890_000, 24_990_000),
    ("10m", 28_000_000, 29_700_000),
    ("6m", 50_000_000, 54_000_000),
]
VHF_UHF_BANDS = [
    ("2m", 144_000_000, 148_000_000),
    # 420 MHz is the true US/Canada band edge, but that's a North-America-
    # only allocation -- most of the rest of the world's 70cm band starts
    # at 430 MHz (420-430 MHz is reserved for other services, like
    # radiolocation, almost everywhere else). Jumping to 420.000 MHz was
    # confirmed to fail on an IC-9700 where 2m and 23cm worked fine, which
    # points at the radio's actual allowed range not including 420-430
    # regardless of region. 430 MHz is valid everywhere 70cm is allocated,
    # including the full US 420-450 range, so it's used here as the safer
    # "jump to" edge even though it understates the true US band edge.
    ("70cm", 430_000_000, 450_000_000),
    ("23cm", 1_240_000_000, 1_300_000_000),
]

RADIO_BANDS = {
    "IC-7300": HF_6M_BANDS,
    "IC-9700": VHF_UHF_BANDS,
    "IC-705": HF_6M_BANDS + VHF_UHF_BANDS[:2],  # HF+6m+2m+70cm; no 23cm on the 705
}

# Confirmed two ways: (1) Icom's own IC-7300 CI-V reference manual lists
# these exact band codes for the 1A 01 command, and (2) a live probe
# against an IC-705 -- radio.get_bsr(3, 1) returned frequency_hz=7074000
# (the standard 40m FT8 frequency), confirming band code 3 = 40m on that
# radio too. So this table is now used for every radio, not just the
# IC-7300. 60m has no code (channelized, excluded from Icom's own table).
# VHF/UHF bands (2m/70cm/23cm) aren't included -- their band-stack codes,
# if any, aren't confirmed, so those still fall back to tuning to the
# band's low edge (see RadioWorker._select_band).
BAND_STACKING_CODES = {
    "160m": 1, "80m": 2, "40m": 3, "30m": 4, "20m": 5,
    "17m": 6, "15m": 7, "12m": 8, "10m": 9, "6m": 10,
}
BAND_STACKING_REGISTER_LATEST = 1  # most recently used -- matches a single press of the real band button

# Some (radio_model, band_label) combinations time out entirely rather than
# raising a clean error when queried for a band-stacking register --
# confirmed via a multi-second CI-V timeout on get_bsr(10, 1) [6m] on an
# IC-705, even though band code 10 = 6m is correct and works fine on the
# IC-7300 per Icom's own manual. Skip the attempt entirely for entries
# here rather than eating that timeout on every band switch; other radios
# aren't affected since this is keyed per (radio_model, band_label).
BAND_STACKING_EXCLUDED = {
    ("IC-705", "6m"),
}


async def _try_recall_band_stack(radio, band_code, register=BAND_STACKING_REGISTER_LATEST):
    """Confirmed working: radio.get_bsr(band, register) (get_band_stack is
    just an alias for the same method) returns a BandStackRegister with a
    .frequency_hz field -- read that, then tune Main to it. This mirrors
    what the real band button does (recall the band's last-used
    frequency) without needing to construct any raw CI-V bytes.

    Deliberately does NOT also apply the returned .mode -- the int->mode
    mapping rigplane uses internally isn't confirmed, and guessing wrong
    risks silently switching to an unintended mode, which is worse than
    just leaving mode alone.

    Returns (frequency_hz, None) on success, or (None, reason) on
    failure -- callers should fall back to tuning to the band's low edge
    when frequency_hz is None, and can surface `reason` for diagnosis
    (e.g. an empty/never-used register on this band, vs. a band code
    that doesn't exist, look different and are worth telling apart)."""
    method = getattr(radio, "get_bsr", None) or getattr(radio, "get_band_stack", None)
    if method is None:
        return None, "get_bsr/get_band_stack not found on this radio/install"
    try:
        result = await method(band_code, register)
    except Exception as exc:
        return None, f"get_bsr({band_code}, {register}) raised {type(exc).__name__}: {exc}"
    freq_hz = getattr(result, "frequency_hz", None)
    if freq_hz is None:
        return None, f"get_bsr returned no frequency_hz (result={result!r})"
    try:
        await radio.set_frequency(freq_hz)
    except Exception as exc:
        return None, f"set_frequency({freq_hz}) failed: {exc}"
    return freq_hz, None

# Defaults shown in the connection dialog -- edit these if you want
# your own values pre-filled, or just leave them and enter values
# in the dialog each run.
DEFAULT_HOST = "192.168.1.133"
DEFAULT_PORT = 50011
DEFAULT_USERNAME = "user"
DEFAULT_PASSWORD = ""
DEFAULT_SERIAL_PORT = "/dev/ttyUSB0"  # or e.g. COM3 on Windows
DEFAULT_BAUD_RATE = 19200

POLL_INTERVAL_SEC = 0.5  # how often to read frequency/s-meter from the radio
WATERFALL_ROWS = 200     # how many past scope frames the waterfall keeps on screen

# Step sizes offered next to the tuning knob, and how many degrees of knob
# rotation correspond to one step (one "detent" of a real rotary encoder).
TUNING_STEPS = [("10 Hz", 10), ("100 Hz", 100), ("1 kHz", 1_000), ("10 kHz", 10_000)]
DEGREES_PER_KNOB_STEP = 15

# Audio format used for the local capture/playback streams. Only raw PCM16
# mono is handled -- if the radio reports a different codec via its
# audio_codec/audio_tx_codec attributes, AudioBridge disables streaming
# rather than sending bytes the radio won't understand (see AudioBridge).
AUDIO_SAMPLE_WIDTH = 2   # bytes per sample, int16
# A real device index is always >= 0, and None means "this direction
# wasn't configured/selected at all -- don't open a stream for it" (set
# by _setup_audio() when the connection dialog's device combo was left
# on "None"). This sentinel is kept deliberately distinct from that None
# so "please use whatever the system's current default device is" (used
# by Virtual Cables, via PulseAudio's own default-sink/source redirection)
# doesn't get treated as "don't open a stream at all" -- a real bug this
# app hit and fixed: AudioBridge.start() gates on `is not None`, and
# plain None is falsy in Python, so passing None to mean "use default"
# would have silently skipped opening the stream entirely.
AUDIO_DEVICE_SYSTEM_DEFAULT = -1
AUDIO_CHANNELS = 1
AUDIO_DEFAULT_SAMPLE_RATE = 8000  # fallback only, used if the radio doesn't expose audio_sample_rate
# Confirmed via a runtime error, straight from push_audio_tx_pcm() itself:
# "PCM frame size mismatch: expected 1920 bytes (20ms at 48000Hz, 1ch
# s16le), got 834." -- this is the TX-specific mic capture format;
# NOT necessarily the same as RX's sample rate (self.sample_rate below),
# which is confirmed working separately at whatever the radio reports for
# RX, so this is applied only to the input/capture stream, not output.
AUDIO_TX_PCM_SAMPLE_RATE = 48000
AUDIO_TX_PCM_FRAME_MS = 20
AUDIO_TX_PCM_FRAME_SAMPLES = int(AUDIO_TX_PCM_SAMPLE_RATE * AUDIO_TX_PCM_FRAME_MS / 1000)  # 960
AUDIO_TX_PCM_FRAME_BYTES = AUDIO_TX_PCM_FRAME_SAMPLES * AUDIO_SAMPLE_WIDTH * AUDIO_CHANNELS  # 1920
# How much RX audio to buffer before starting playback, to absorb jitter in
# when rigplane's audio chunks actually arrive (network/USB timing isn't
# perfectly regular). Bigger = smoother but more latency between "radio
# receives it" and "you hear it".
AUDIO_JITTER_BUFFER_MS = 150
# Fixed PortAudio callback size for the output stream. A fixed size (rather
# than PortAudio's variable default) makes the RX buffering logic's
# behavior more predictable across platforms.
AUDIO_OUTPUT_BLOCK_MS = 20

# rigplane's LevelsCapable protocol is confirmed to exist and to cover
# "AF, RF gain, squelch" -- but not the exact method names (get_af_gain/
# set_af_gain turned out to be wrong on at least one install). Rather than
# guess a second name blindly, RadioWorker._setup_levels() searches the
# connected radio object for the first name in each list below that
# actually exists via hasattr(), and reports exactly what it found (or a
# filtered dir(radio) listing if nothing matched) so a wrong guess here is
# immediately visible and fixable rather than silently wrong.
#
# Every entry gets a slider built generically (see RadioWindow's level
# slider loop). getter_candidates/setter_candidates confirmed present via
# a dir(radio) scan except AF gain/Squelch's first guesses (see above).
LEVEL_DEFINITIONS = {
    "af_gain": {
        "label": "AF Gain",
        "getter_candidates": ["get_af_gain", "get_af_level", "get_af", "get_volume", "get_audio_gain"],
        "setter_candidates": ["set_af_gain", "set_af_level", "set_af", "set_volume", "set_audio_gain"],
    },
    "squelch": {
        "label": "Squelch",
        "getter_candidates": ["get_squelch", "get_squelch_level", "get_sql"],
        "setter_candidates": ["set_squelch", "set_squelch_level", "set_sql"],
    },
    "monitor": {
        "label": "Monitor",
        # MONI: lets you hear your own transmitted audio through the
        # speaker, for checking audio quality/compression while
        # transmitting. get_monitor_gain/get_monitor both confirmed
        # present via a dir(radio) scan; the _gain-suffixed one is tried
        # first since it's more likely to be the actual level (vs. an
        # on/off toggle for the feature itself).
        "getter_candidates": ["get_monitor_gain", "get_monitor"],
        "setter_candidates": ["set_monitor_gain", "set_monitor"],
    },
    "tx_level": {
        "label": "TX Level",
        # get_power/set_power confirmed (via the Power Output meter fix
        # earlier) to be the TX power OUTPUT LEVEL control itself --
        # NOT a meter (that's the separate get_power_meter, used for the
        # Power Output meter). Same raw-int/float scale ambiguity
        # _call_level_setter already handles for AF gain/squelch applies
        # here too.
        "getter_candidates": ["get_power"],
        "setter_candidates": ["set_power"],
    },
    "rf_level": {
        "label": "RF Level",
        # RF Gain: receiver sensitivity/gain control (distinct from RF
        # power output above). get_rf_gain/set_rf_gain confirmed present
        # via a dir(radio) scan.
        "getter_candidates": ["get_rf_gain"],
        "setter_candidates": ["set_rf_gain"],
    },
}


def _find_method_name(obj, candidates):
    """Returns the first candidate name that exists as an attribute on
    obj, or None if none of them do."""
    for name in candidates:
        if hasattr(obj, name):
            return name
    return None

# get_s_meter() returns a raw 0-255 value over CI-V (confirmed against
# Hamlib's IC-7300 backend, which reports RAWSTR range 0..255). The
# raw-to-S-unit mapping below (S9 ~= 120, full-scale/S9+60dB ~= 241) is the
# calibration convention used across most Icom CAT tools -- it is NOT
# something we could confirm rigplane itself documents or that your specific
# radio is calibrated to. If the meter reads visibly high or low against
# your radio's own front-panel meter, adjust these two numbers.
S_METER_RAW_S9 = 120
S_METER_RAW_MAX = 241
# Fraction of the meter bar's width devoted to S0-S9 vs. S9-S9+60dB,
# matching the compressed look of a real Icom meter face.
S_METER_S9_FRACTION = 0.65

# Registry of swappable meters. Each entry names the async Radio method
# rigplane exposes for that reading, plus how to scale/label it.
#
# "s_meter" is the only kind with its own S/dB rendering (see
# MeterWidget._paint_s_meter). Everything else uses a plain linear scale
# from raw_max down to 0, shown as a segmented bar with numeric ticks.
#
# Confirmed against rigplane's own IC-7300 feature page: power, S-meter,
# SWR, and ALC are all listed as "verified" features, which is why they're
# included here. Voltage is NOT listed anywhere in rigplane's docs -- it's
# included as a common Icom meter reading, but `get_voltage` is a guessed
# method name. If selecting it errors out, that confirms it isn't there;
# check `dir(radio)` on your install to find the real name if one exists.
# Keywords for the one-time dir(radio) diagnostic dump printed on connect
# (see RadioWorker._print_radio_attribute_diagnostic). Edit this list to
# search for something else -- e.g. add "sub" while chasing a SUB-receiver
# method name, or "af"/"squelch" for level-control names. Set to None (or
# an empty tuple) to skip keyword filtering entirely and just dump every
# get_/send_ attribute instead -- see DIAGNOSTIC_DIR_PREFIXES below.
DIAGNOSTIC_DIR_KEYWORDS = ("power", "watt", "temp", "bsr", "band_stack", "ptt", "tx", "key")
# Prefixes for a broader, unfiltered dump -- every attribute starting with
# one of these prints regardless of DIAGNOSTIC_DIR_KEYWORDS, for scanning
# by eye when a keyword search comes up empty (e.g. temperature wasn't
# found under "temp" -- it might be named something unexpected). Includes
# set_ now too -- PTT is fundamentally a setter action with no obvious
# getter to have caught it via the get_/send_-only dump used so far.
DIAGNOSTIC_DIR_PREFIXES = ("get_", "send_", "set_")

METER_DEFINITIONS = {
    "s_meter": {
        "label": "S-Meter",
        "getter": "get_s_meter",
        "kind": "s_meter",
    },
    "power": {
        "label": "Power Output",
        # get_power/set_power turned out to be the TX power LEVEL CONTROL
        # (PowerControlCapable: "power on/off and TX power level control"),
        # not a live reading -- confirmed via a dir(radio) scan on a real
        # IC-705, which also surfaced get_power_meter (matching
        # MetersCapable's confirmed "S-meter, SWR, TX power" description)
        # as the actual meter. "kind" defaults to "linear" (raw 0-255) but
        # is overridden to "direct" at connect time if the radio's own
        # native_power_unit attribute (confirmed to exist by rigplane's
        # docs, a Literal["raw_255", "watts"]) says the reading is already
        # in watts -- see RadioWorker._setup_meters().
        "getter": "get_power_meter",
        "getter_candidates": ["get_power_meter", "get_power", "get_rf_power"],
        "kind": "linear",
        "unit": "W",
        "raw_max": 255,
        "display_max": 100,  # typical full-scale for Icom HF/VHF rigs
    },
    "swr": {
        "label": "SWR",
        # get_swr vs get_swr_meter is the exact same control-vs-meter split
        # confirmed on "power" (get_power was the level-control setting,
        # get_power_meter the actual live reading) -- a dir(radio) dump
        # showed BOTH exist here too, so get_swr may well have been the
        # wrong one this whole time. Preferring the _meter name now.
        "getter": "get_swr_meter",
        "getter_candidates": ["get_swr_meter", "get_swr"],
        "kind": "linear",
        "unit": ":1",
        "raw_max": 255,
        "display_min": 1.0,
        "display_max": 3.0,  # most SWR meters top out around here in practice
    },
    "alc": {
        "label": "ALC",
        # get_alc() confirmed NOT to exist on at least one install. ALC is
        # always a live, read-only TX-drive indicator on real Icom radios
        # (never an adjustable setting -- what's adjustable is things like
        # RF power/mic gain, which affect ALC, not ALC itself), so every
        # candidate here is a read-style name; _setup_meters() below picks
        # whichever one actually exists rather than betting on one guess.
        "getter": "get_alc",
        "getter_candidates": ["get_alc", "get_alc_meter", "get_alc_level", "read_alc"],
        "kind": "linear",
        "unit": "",
        "raw_max": 255,
        "display_max": 100,
    },
    "voltage": {
        "label": "Voltage",  # confirmed working via discovery
        "getter": "get_voltage",
        "getter_candidates": ["get_voltage", "get_supply_voltage", "get_vd", "get_vd_meter", "get_dc_voltage"],
        "kind": "linear",
        "unit": "V",
        "raw_max": 255,
        "display_max": 16.0,  # rough guess at supply-voltage full scale
    },
    "comp": {
        "label": "COMP",
        # Speech compressor meter -- confirmed as one of Icom's own 6
        # official TX meter parameters (PO/SWR/ALC/COMP/VD/ID, per the
        # IC-7300 manual). Same discovery approach as ALC/Voltage since
        # the exact method name isn't documented.
        "getter": "get_comp",
        "getter_candidates": ["get_comp", "get_comp_meter", "get_compression"],
        "kind": "linear",
        "unit": "",
        "raw_max": 255,
        "display_max": 100,
    },
    "current": {
        "label": "Current (ID)",
        # Drain current meter -- the other of Icom's 6 official TX meters
        # not yet covered. IC-7300/IC-9700 draw up to ~20-25A on TX;
        # IC-705 much less on internal battery -- display_max is a rough
        # full-scale guess either way, adjust if it looks off in practice.
        "getter": "get_current",
        "getter_candidates": ["get_current", "get_id", "get_id_meter", "get_drain_current"],
        "kind": "linear",
        "unit": "A",
        "raw_max": 255,
        "display_max": 25.0,
    },
    "temperature": {
        "label": "Temperature (unsupported)",
        # Confirmed absent: a full dump of every get_/send_ attribute on a
        # connected IC-705 (~140 names) contained nothing matching "temp"
        # at all. This radio's rigplane backend simply doesn't expose a
        # temperature reading -- kept here (rather than removed) in case
        # a future rigplane version or a different radio adds it; the
        # getter_candidates below just won't resolve to anything on
        # installs like the one this was confirmed against.
        "getter": "get_temperature",
        "getter_candidates": [
            "get_temperature", "get_temp", "get_temp_meter", "get_pa_temperature", "get_pa_temp",
        ],
        "kind": "linear",
        "unit": "\u00b0C",
        "raw_max": 255,
        "display_max": 100.0,
    },
}

# Radio controls beyond frequency/levels/meters. Each entry is either
# "toggle" (a simple on/off, shown as a checkable button) or "combo" (a
# fixed set of options, shown as a dropdown). Getter names for all of
# these were confirmed present via a dir(radio) scan on a real IC-705;
# setter names were NOT in that scan (it only covered get_/send_
# prefixes), so setter_candidates lists are best-effort guesses following
# the get_x/set_x naming convention confirmed everywhere else in this
# API, resolved at runtime via _find_method_name the same way AF gain/
# squelch/meters were. "options" values for AGC/Preamp/Filter
# are reasonable guesses at what these radios actually use, NOT
# confirmed -- if a set call fails, the exact exception will show in the
# console the same way every other guess in this app has been refined.
CONTROL_DEFINITIONS = {
    "mode": {
        "label": "Mode",
        "type": "combo",
        # Confirmed via rigplane's own quickstart example:
        # `await radio.set_mode("USB")`, `mode, _ = await radio.get_mode()`
        # -- get_mode() returns (mode, filter), hence tuple_result below.
        # Exact supported values confirmed via a runtime error message
        # from set_mode() itself: LSB, USB, AM, CW, RTTY, FM, WFM, CW_R,
        # RTTY_R, DV -- underscores, not hyphens, and WFM was missing
        # from the first guess. Display labels keep the hyphen for
        # readability; only the underlying value sent to set_mode() uses
        # the confirmed underscore form.
        "getter_candidates": ["get_mode"],
        "setter_candidates": ["set_mode"],
        "tuple_result": True,
        "options": [
            ("LSB", "LSB"), ("USB", "USB"), ("AM", "AM"), ("CW", "CW"),
            ("RTTY", "RTTY"), ("FM", "FM"), ("WFM", "WFM"),
            ("CW-R", "CW_R"), ("RTTY-R", "RTTY_R"), ("DV", "DV"),
        ],
    },
    "data_mode": {
        "label": "Digital Mode",
        "type": "toggle",
        "getter_candidates": ["get_data_mode"],
        "setter_candidates": ["set_data_mode", "set_digital_mode"],
    },
    "nr": {
        "label": "NR",
        "type": "toggle",
        "getter_candidates": ["get_nr"],
        "setter_candidates": ["set_nr"],
    },
    "nb": {
        "label": "NB",
        "type": "toggle",
        "getter_candidates": ["get_nb"],
        "setter_candidates": ["set_nb"],
    },
    "agc": {
        "label": "AGC",
        "type": "combo",
        "getter_candidates": ["get_agc"],
        "setter_candidates": ["set_agc"],
        # Confirmed via a runtime error: set_agc() rejects a plain string
        # ("'FAST' is not a valid AgcMode") -- it wants an actual AgcMode
        # enum member. enum_import names where to find that class;
        # RadioWorker._setup_controls() imports it once and looks up each
        # option value by name (AgcMode["FAST"], etc.) before every set
        # call. rigplane.types is the first guess since that's confirmed
        # to be where AudioCodec (used elsewhere in this app) lives.
        "enum_import": ("rigplane.types", "AgcMode"),
        "options": [("OFF", "OFF"), ("FAST", "FAST"), ("MID", "MID"), ("SLOW", "SLOW")],
    },
    "preamp": {
        "label": "Preamp",
        "type": "combo",
        "getter_candidates": ["get_preamp"],
        "setter_candidates": ["set_preamp"],
        "options": [("OFF", 0), ("P.AMP1", 1), ("P.AMP2", 2)],
    },
    "filter": {
        "label": "Filter",
        "type": "combo",
        "getter_candidates": ["get_filter"],
        "setter_candidates": ["set_filter"],
        # Confirmed via a runtime error: set_filter() does int(value)
        # internally, so it wants the numeric filter slot (1/2/3), not
        # the string "FIL2" -- matches the FIL1/FIL2/FIL3 numbering seen
        # in Icom's own CI-V manual (subcommand data 01/02/03) earlier
        # in this app's development. Labels stay as "FIL1" etc.; values
        # are the plain ints the setter actually wants.
        "options": [("FIL1", 1), ("FIL2", 2), ("FIL3", 3)],
    },
    "vfo": {
        "label": "VFO",
        # Not "combo"/"toggle" -- a dedicated single button that swaps
        # A<->B on click, matching the real A/B button on these radios,
        # rather than a dropdown. get_vfo_slot was seen in a dir(radio)
        # scan; the setter name isn't confirmed. rigplane's confirmed
        # tier-1 protocol list includes "VfoSlotCapable", which suggests
        # a dedicated VfoSlot enum may exist the same way AgcMode did for
        # AGC -- tried first via enum_import, falling back to plain
        # "A"/"B" strings if that import fails.
        "type": "vfo_toggle",
        "getter_candidates": ["get_vfo_slot", "get_vfo", "get_active_vfo"],
        "setter_candidates": ["set_vfo_slot", "set_vfo", "select_vfo"],
        "enum_import": ("rigplane.types", "VfoSlot"),
        "options": [("VFO A", "A"), ("VFO B", "B")],
    },
    "memory_mode": {
        "label": "VFO/MEM",
        # Same click-to-swap button style as "vfo" above -- matches the
        # real V/M button on these radios (toggles between VFO tuning
        # and memory-channel recall). get_memory_mode/set_memory_mode
        # confirmed present via a dir(radio) scan; values are a plain
        # bool guess (True = memory mode) following the same convention
        # that's worked for the other simple boolean toggles (NR/NB/
        # Digital Mode) -- no enum_import, since there's no equivalent
        # confirmed evidence of a dedicated enum here the way there was
        # for AGC.
        #
        # write_only=True: confirmed via a runtime error straight from
        # rigplane -- "Command 0x08 is SELECT-only (no GET variant)" per
        # Icom's own CI-V reference manual. There is NO way to read the
        # current VFO/Memory state over CI-V at all, only to select into
        # one or the other, so this is never polled (it would just fail
        # forever) -- the button's label reflects the last command sent,
        # not a live-confirmed radio state.
        "type": "vfo_toggle",
        "write_only": True,
        "getter_candidates": ["get_memory_mode"],
        "setter_candidates": ["set_memory_mode"],
        "options": [("VFO", False), ("MEMORY", True)],
    },
}

# Radio-specific control-option exclusions, keyed (radio_model, control
# key) -> set of option labels to leave out of that combo entirely.
# Confirmed bug, not a wrong value guess: on the IC-705, set_preamp() with
# ANY non-zero value (both P.AMP1=1 and P.AMP2=2) fails with the identical
# error -- "get_digisel is unsupported by profile IC-705: no cmd29 route
# for command 0x16/0x4E" -- while 0 (OFF) works fine, and the same control
# works normally on the IC-9700. This is a rigplane IC-705 profile routing
# bug; excluding both broken options here rather than offering choices
# that reliably error, while leaving OFF (the one that works) in place.
CONTROL_OPTION_EXCLUDED = {
    ("IC-705", "preamp"): {"P.AMP1", "P.AMP2"},
}

# Amplitude (0-160, per ScopeFrame.pixels) -> color, modeled on rigplane's
# own "classic" scope theme: dark blue (noise floor) through cyan and
# yellow to red (strong signal).
_COLOR_ANCHORS = [
    (0.0, (0, 0, 40)),
    (0.25, (0, 0, 180)),
    (0.5, (0, 180, 180)),
    (0.75, (255, 255, 0)),
    (1.0, (255, 0, 0)),
]


def amplitude_to_color(amp, max_amp=160):
    frac = max(0.0, min(1.0, amp / max_amp))
    for (f0, c0), (f1, c1) in zip(_COLOR_ANCHORS, _COLOR_ANCHORS[1:]):
        if f0 <= frac <= f1:
            t = (frac - f0) / (f1 - f0)
            r = int(c0[0] + (c1[0] - c0[0]) * t)
            g = int(c0[1] + (c1[1] - c0[1]) * t)
            b = int(c0[2] + (c1[2] - c0[2]) * t)
            return QColor(r, g, b)
    return QColor(*_COLOR_ANCHORS[-1][1])


# ==================== Virtual audio cables (Linux only) ====================
#
# There's no cross-platform way to create a virtual audio device at
# runtime -- Windows/macOS need a separately-installed driver (VB-CABLE,
# BlackHole) that this app can't install itself (kernel driver signing/
# approval, admin privileges). On Linux, PulseAudio and PipeWire's pulse
# compatibility layer both support creating "null sinks" at runtime via
# `pactl`, with no driver installation needed -- that's what this uses.
#
# Two sinks are created:
#   RX cable: this app's own AudioBridge plays received radio audio INTO
#     it (as its output device); an external app (WSJT-X, etc.) selects
#     the sink's auto-created ".monitor" source as ITS input device to
#     "hear" what the radio is receiving.
#   TX cable: an external app plays its generated audio (e.g. FT8 tones)
#     INTO it (selecting the sink as ITS output device); this app's
#     AudioBridge captures from the sink's monitor (as its input device)
#     and sends that to the radio via push_tx().
# This is the same routing pattern real hardware virtual-cable setups use
# for FT8 -- just built from a null-sink instead of a purchased/installed
# virtual cable driver.
VIRTUAL_CABLE_RX_NAME = "RadioApp_RX_Cable"
VIRTUAL_CABLE_RX_DESC = "RadioApp RX Cable"
VIRTUAL_CABLE_TX_NAME = "RadioApp_TX_Cable"
VIRTUAL_CABLE_TX_DESC = "RadioApp TX Cable"


def pactl_available():
    return shutil.which("pactl") is not None


def create_null_sink(sink_name, description):
    """Creates a PulseAudio/PipeWire-pulse null-sink via `pactl
    load-module module-null-sink`. Returns the loaded module's ID (needed
    to unload it later cleanly) -- raises RuntimeError with pactl's own
    error text on failure."""
    result = subprocess.run(
        ["pactl", "load-module", "module-null-sink",
         f"sink_name={sink_name}", f"sink_properties=device.description={description}"],
        capture_output=True, text=True, timeout=5,
    )
    output = result.stdout.strip()
    if result.returncode != 0 or not output.isdigit():
        raise RuntimeError(result.stderr.strip() or output or "pactl load-module failed")
    return int(output)


def unload_pactl_module(module_id):
    """Best-effort cleanup -- doesn't raise on failure, since this is
    normally called during teardown where there's nothing useful to do
    with an error other than leave a stray sink behind."""
    try:
        subprocess.run(["pactl", "unload-module", str(module_id)], capture_output=True, text=True, timeout=5)
    except Exception:
        pass


def get_default_sink_name():
    try:
        result = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def get_default_source_name():
    try:
        result = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def set_default_sink(name):
    """Best-effort -- doesn't raise; a failure here just means the
    virtual RX cable won't actually receive audio, which the caller
    finds out about naturally when nothing comes through."""
    try:
        subprocess.run(["pactl", "set-default-sink", name], capture_output=True, text=True, timeout=5)
    except Exception:
        pass


def set_default_source(name):
    try:
        subprocess.run(["pactl", "set-default-source", name], capture_output=True, text=True, timeout=5)
    except Exception:
        pass


def _find_own_stream_ids(list_command, header_pattern, pid):
    """Parses `pactl list sink-inputs`/`list source-outputs`'s classic
    text output (more portable across pactl versions than relying on the
    newer `-f json` mode) for entries belonging to this process (matched
    by application.process.id against our own PID). Returns a list of
    integer stream IDs."""
    try:
        result = subprocess.run(list_command, capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    if result.returncode != 0:
        return []
    ids = []
    current_id = None
    for line in result.stdout.splitlines():
        header = re.match(header_pattern, line.strip())
        if header:
            current_id = int(header.group(1))
            continue
        if current_id is not None and "application.process.id" in line:
            match = re.search(r'"(\d+)"', line)
            if match and int(match.group(1)) == pid:
                ids.append(current_id)
                current_id = None  # already matched -- no need to keep scanning this block
    return ids


def find_own_sink_input_ids(pid):
    return _find_own_stream_ids(["pactl", "list", "sink-inputs"], r"Sink Input #(\d+)", pid)


def find_own_source_output_ids(pid):
    return _find_own_stream_ids(["pactl", "list", "source-outputs"], r"Source Output #(\d+)", pid)


def move_sink_input(sink_input_id, sink_name):
    try:
        subprocess.run(["pactl", "move-sink-input", str(sink_input_id), sink_name], capture_output=True, text=True, timeout=5)
    except Exception:
        pass


def move_source_output(source_output_id, source_name):
    try:
        subprocess.run(["pactl", "move-source-output", str(source_output_id), source_name], capture_output=True, text=True, timeout=5)
    except Exception:
        pass


class AudioBridge:
    """Bridges a selected mic/speaker to rigplane's AudioTransport
    protocol. start_rx/stop_rx/start_tx/push_tx/stop_tx are confirmed
    real method names (rigplane's public API surface docs); the exact
    callback-registration signature for start_rx() is NOT documented
    anywhere we could find, so this tries the pattern used elsewhere in
    the API (on_scope_data(cb) + enable) first, then a plain
    callback-argument call, and reports clearly via status_callback if
    neither matches your installed rigplane version.

    Only raw PCM16 mono is handled. If the radio's audio_codec /
    audio_tx_codec attributes report anything else, streaming is
    disabled rather than sending bytes the radio can't decode.

    Safety: RX (radio audio -> speaker) starts automatically, since
    receiving is inherently safe. TX (mic -> radio) is NOT continuous --
    push_tx() is only ever called while set_tx_active(True) has been
    called, which RadioWorker does when PTT is held (see RadioWorker.
    start_ptt/stop_ptt, which key the radio directly and independently
    of this bridge -- PTT still works with no mic configured at all,
    just without any TX audio). Audio is never sent to the radio without
    the operator actively holding PTT.

    Threading: sounddevice's callbacks run on PortAudio's own internal
    thread -- separate from both the Qt GUI thread and this bridge's
    asyncio loop (the RadioWorker's loop, passed in as `loop`). Data
    crosses those boundaries only via thread-safe queues, never by
    calling asyncio or Qt APIs directly from a PortAudio callback.
    """

    def __init__(self, radio, loop, input_device, output_device, status_callback):
        self.radio = radio
        self.loop = loop
        self._input_device = input_device
        self._output_device = output_device
        self._status = status_callback  # str -> None; must be safe to call from this thread

        self.sample_rate = getattr(radio, "audio_sample_rate", None) or AUDIO_DEFAULT_SAMPLE_RATE
        self._pcm_ok = self._check_pcm_codec()

        self._in_stream = None
        self._out_stream = None
        self._rx_queue = queue.Queue(maxsize=200)    # drained by the output stream's callback
        self._tx_queue = asyncio.Queue(maxsize=50)   # drained by _tx_pump on the asyncio loop
        self._tx_pump_task = None
        self._tx_active = False
        self._rx_active = False
        self._rx_chunk_count = 0
        self._rx_unrecognized_reported = False
        self._rx_watchdog_task = None
        self._tx_captured_count = 0  # chunks actually captured from the mic while active
        self._tx_sent_count = 0      # chunks actually handed to radio.push_tx()
        self._tx_watchdog_task = None
        # Resolved once in start() -- see the comment there for why the
        # PCM-suffixed variants are preferred over the generic ones.
        self._tx_start_name = None
        self._tx_push_name = None
        self._tx_stop_name = None
        # Byte-level playback buffer, separate from the chunk queue above --
        # rigplane's audio chunk size has no reason to match what PortAudio
        # asks for per callback, so incoming chunks get concatenated here
        # and sliced to exactly the requested size, instead of assuming a
        # 1-chunk-per-callback correspondence (which caused the choppiness).
        self._rx_playback_buf = bytearray()
        self._rx_primed = False  # True once buffered audio reaches the jitter-buffer threshold
        self._rx_prebuffer_bytes = int(
            self.sample_rate * (AUDIO_JITTER_BUFFER_MS / 1000.0) * AUDIO_SAMPLE_WIDTH * AUDIO_CHANNELS
        )
        self._rx_underrun_count = 0

    @staticmethod
    def _codec_label(value):
        """A readable label for an audio_codec/audio_tx_codec value.
        rigplane's AudioCodec (from rigplane.types) is very likely an
        IntEnum -- and on Python 3.11+, IntEnum.__str__() was changed to
        print the bare integer (e.g. "4") instead of "AudioCodec.PCM16".
        .name is unaffected by that change and always gives the readable
        member name, so prefer it over str() whenever it's available."""
        return getattr(value, "name", None) or str(value)

    @staticmethod
    def _device_label(index):
        """A readable label for a PortAudio device index, for status
        messages. Falls back to the raw index if the name lookup fails."""
        if index is None:
            return "(none)"
        if index == AUDIO_DEVICE_SYSTEM_DEFAULT:
            return "system default"
        try:
            return f"'{sd.query_devices(index)['name']}' (index {index})"
        except Exception:
            return f"index {index}"

    @staticmethod
    def _resolve_device(device):
        """Translates our AUDIO_DEVICE_SYSTEM_DEFAULT sentinel into the
        actual None value sounddevice/PortAudio itself expects to mean
        "use the current system default device". Kept as a distinct
        sentinel internally (see the constant's comment) so it isn't
        confused with our own None, which means "not configured, don't
        open a stream" -- this is the one place that sentinel gets
        translated to what the underlying library actually wants."""
        return None if device == AUDIO_DEVICE_SYSTEM_DEFAULT else device

    def _check_pcm_codec(self):
        codec = getattr(self.radio, "audio_codec", None)
        tx_codec = getattr(self.radio, "audio_tx_codec", codec)
        for label, value in (("RX", codec), ("TX", tx_codec)):
            if value is None:
                continue
            name = self._codec_label(value)
            if "pcm" not in name.lower():
                self._status(
                    f"Radio reports {label} codec '{name}' -- only raw PCM "
                    "is handled here, so audio streaming is disabled."
                )
                return False
        return True

    async def start(self):
        if not SOUNDDEVICE_AVAILABLE:
            self._status("sounddevice isn't installed -- run `pip install sounddevice` for audio.")
            return
        if not self._pcm_ok:
            return
        if self._output_device is not None:
            await self._start_rx()
        if self._input_device is not None:
            self._open_input_stream()
            if self._in_stream:
                # Confirmed via a dir(radio) scan: THREE separate families
                # of TX audio methods exist -- generic (start_tx/push_tx/
                # stop_tx, the newer codec-neutral AudioTransport
                # protocol) and legacy PCM/Opus-suffixed ones
                # (start_audio_tx_pcm/push_audio_tx_pcm/stop_audio_tx_pcm,
                # rigplane's own docs call these "permanent back-compat
                # shims" under the older AudioCapable protocol). A live
                # test confirmed the generic push_tx() accepts calls
                # without error but produces total silence on this radio
                # -- consistent with the newer protocol being only
                # partially wired up for this backend. Preferring the PCM
                # triplet here since it's more likely to be the one that
                # actually works end-to-end.
                self._tx_start_name = _find_method_name(
                    self.radio, ["start_audio_tx_pcm", "start_tx"]
                )
                self._tx_push_name = _find_method_name(
                    self.radio, ["push_audio_tx_pcm", "push_tx"]
                )
                self._tx_stop_name = _find_method_name(
                    self.radio, ["stop_audio_tx_pcm", "stop_tx"]
                )
                self._status(
                    f"TX audio methods resolved: start={self._tx_start_name}, "
                    f"push={self._tx_push_name}, stop={self._tx_stop_name} "
                    f"(hasattr push_audio_tx_pcm={hasattr(self.radio, 'push_audio_tx_pcm')})."
                )
                self._tx_pump_task = asyncio.ensure_future(self._tx_pump())

    async def stop(self):
        self.set_tx_active(False)
        await self._stop_rx()
        if self._tx_pump_task:
            self._tx_pump_task.cancel()
            self._tx_pump_task = None
        if self._in_stream:
            self._in_stream.close()
            self._in_stream = None
        if self._out_stream:
            self._out_stream.close()
            self._out_stream = None

    # ---- RX: radio audio -> speaker ----

    async def _start_rx(self):
        self._rx_playback_buf.clear()
        self._rx_primed = False
        self._rx_underrun_count = 0
        blocksize = max(1, int(self.sample_rate * (AUDIO_OUTPUT_BLOCK_MS / 1000.0)))
        try:
            self._out_stream = sd.RawOutputStream(
                device=self._resolve_device(self._output_device),
                samplerate=self.sample_rate,
                channels=AUDIO_CHANNELS,
                dtype="int16",
                blocksize=blocksize,
                callback=self._on_output_needed,
            )
            self._out_stream.start()
        except Exception as exc:
            self._status(f"Couldn't open output device {self._device_label(self._output_device)}: {exc}")
            self._out_stream = None
            return

        try:
            if hasattr(self.radio, "on_audio_rx"):
                self.radio.on_audio_rx(self._on_rx_audio)
                await self.radio.start_rx()
            else:
                await self.radio.start_rx(self._on_rx_audio)
            self._rx_active = True
            self._status(
                f"RX audio streaming started: output "
                f"{self._device_label(self._output_device)}, {self.sample_rate} Hz."
            )
            self._rx_watchdog_task = asyncio.ensure_future(self._rx_watchdog())
        except Exception as exc:
            self._status(
                f"RX audio: start_rx() didn't accept the calling convention "
                f"tried here ({exc}). Check the real signature with "
                "`import inspect; inspect.signature(radio.start_rx)`."
            )

    async def _rx_watchdog(self):
        """Speaks up if start_rx() reported success but no audio ever
        actually arrived -- distinguishes "wrong callback convention,
        silently accepted" from "genuinely no signal/PTT active"."""
        await asyncio.sleep(3.0)
        if self._rx_active and self._rx_chunk_count == 0:
            self._status(
                "RX audio: start_rx() registered without error, but no "
                "audio has arrived after 3s. Likely causes: the radio needs "
                "a separate enable step beyond start_rx(), or delivers audio "
                "through something other than this callback (an async "
                "iterator/queue, for instance). Try "
                "`import inspect; inspect.signature(radio.start_rx)` and "
                "look for anything like 'audio_rx_queue' or 'iter' on "
                "`dir(radio)`."
            )

    async def _stop_rx(self):
        if self._rx_watchdog_task:
            self._rx_watchdog_task.cancel()
            self._rx_watchdog_task = None
        if self._rx_active:
            try:
                await self.radio.stop_rx()
            except Exception:
                pass
            self._rx_active = False
        if self._out_stream:
            self._out_stream.stop()

    def _on_rx_audio(self, *args, **kwargs):
        """Called by rigplane when audio arrives from the radio -- runs
        on the asyncio loop thread. Only touches the thread-safe
        queue.Queue, never a Qt widget.

        Signature is deliberately *args/**kwargs: the real calling
        convention isn't documented, so this extracts raw PCM bytes from
        whatever was actually passed (see _extract_pcm_bytes) rather
        than assuming a single positional bytes argument and potentially
        raising inside rigplane's own dispatch code -- a TypeError there
        could easily be swallowed silently, which looks identical to "no
        audio arriving" from the outside."""
        data = self._extract_pcm_bytes(args, kwargs)
        if data is None:
            # A lone None argument (and possibly other shapes) shows up
            # periodically from rigplane -- most likely a benign
            # keepalive/end-of-burst marker rather than a real error, and
            # reporting it every single occurrence was confirmed to flood
            # the console in a live test. Only the first unrecognized
            # shape gets reported, in case it's worth investigating.
            if not self._rx_unrecognized_reported:
                self._rx_unrecognized_reported = True
                self._status(
                    "RX audio: callback fired with an unrecognized argument "
                    f"shape (args={[type(a).__name__ for a in args]} "
                    f"kwargs={list(kwargs)}) -- couldn't find raw PCM bytes "
                    "in it. Only reporting this once; likely benign."
                )
            return
        if self._rx_chunk_count == 0:
            self._status(f"RX audio: first chunk received ({len(data)} bytes).")
        self._rx_chunk_count += 1
        try:
            self._rx_queue.put_nowait(data)
        except queue.Full:
            # Drop the OLDEST buffered chunk rather than the newest, so a
            # momentary backlog doesn't let latency creep upward -- always
            # keep the freshest audio available.
            try:
                self._rx_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._rx_queue.put_nowait(data)
            except queue.Full:
                pass

    @staticmethod
    def _extract_pcm_bytes(args, kwargs):
        """Best-effort: find a bytes-like payload among the callback's
        arguments, checking raw positional/keyword values first, then a
        few common attribute names in case it's wrapped in a frame
        object (mirroring rigplane's own ScopeFrame.pixels pattern)."""
        candidates = list(args) + list(kwargs.values())
        for value in candidates:
            if isinstance(value, (bytes, bytearray, memoryview)):
                return bytes(value)
        for value in candidates:
            for attr in ("data", "pcm", "payload", "audio", "samples", "raw"):
                inner = getattr(value, attr, None)
                if isinstance(inner, (bytes, bytearray, memoryview)):
                    return bytes(inner)
        return None

    def _on_output_needed(self, outdata, frames, time_info, status):
        """sounddevice callback -- runs on PortAudio's own thread.

        Rewritten from the original 1-chunk-per-callback version, which
        assumed each queued chunk's size happened to match `needed` --
        there's no reason for that to hold, and the mismatch was the
        actual cause of the choppiness (truncating chunks that were too
        big, padding silence into chunks that were too small). Instead:
        drain whatever's queued into a persistent byte buffer, then slice
        off exactly `needed` bytes each callback, carrying any remainder
        to the next one. A jitter buffer (_rx_prebuffer_bytes) delays the
        start of playback until enough audio has accumulated, and
        re-primes after any underrun, so a burst of network/USB jitter
        causes one clean re-buffering pause instead of repeated stutter."""
        needed = frames * AUDIO_SAMPLE_WIDTH * AUDIO_CHANNELS
        buf = self._rx_playback_buf

        while True:
            try:
                buf.extend(self._rx_queue.get_nowait())
            except queue.Empty:
                break

        if not self._rx_primed:
            if len(buf) >= self._rx_prebuffer_bytes:
                self._rx_primed = True
            else:
                outdata[:needed] = b"\x00" * needed
                return

        if len(buf) >= needed:
            outdata[:needed] = bytes(buf[:needed])
            del buf[:needed]
        else:
            # Ran dry: play what little is left, pad the rest with
            # silence, and require re-priming before resuming so a
            # single straggler chunk doesn't just underrun again
            # immediately.
            outdata[:len(buf)] = bytes(buf)
            outdata[len(buf):needed] = b"\x00" * (needed - len(buf))
            buf.clear()
            self._rx_primed = False
            self._rx_underrun_count += 1
            if self._rx_underrun_count == 1:
                # One-time, not per-occurrence -- underruns during normal
                # jitter are expected sometimes; only the first one is
                # worth surfacing, to avoid flooding the status line.
                self._status(
                    "RX audio: buffer underrun -- re-priming. Occasional "
                    "underruns are normal; if this repeats constantly, try "
                    "raising AUDIO_JITTER_BUFFER_MS for more cushion."
                )

    # ---- TX: mic -> radio, gated by PTT ----

    def _open_input_stream(self):
        try:
            self._in_stream = sd.RawInputStream(
                device=self._resolve_device(self._input_device),
                # Confirmed exact requirement (see AUDIO_TX_PCM_* comment
                # above): 48000 Hz with a fixed 960-sample (1920-byte)
                # blocksize, not self.sample_rate/PortAudio's default
                # blocksize -- the earlier 834-byte chunks were neither
                # the right rate nor a fixed size at all.
                samplerate=AUDIO_TX_PCM_SAMPLE_RATE,
                blocksize=AUDIO_TX_PCM_FRAME_SAMPLES,
                channels=AUDIO_CHANNELS,
                dtype="int16",
                callback=self._on_input_captured,
            )
            self._in_stream.start()
        except Exception as exc:
            self._status(f"Couldn't open input device {self._device_label(self._input_device)}: {exc}")
            self._in_stream = None

    def _on_input_captured(self, indata, frames, time_info, status):
        """sounddevice callback -- runs on PortAudio's own thread. Only
        hands data to the asyncio loop via call_soon_threadsafe; the
        actual radio.push_tx() call happens in _tx_pump()."""
        if not self._tx_active:
            return
        data = bytes(indata)
        if self._tx_captured_count == 0:
            self._status(f"TX audio: first chunk captured from mic ({len(data)} bytes).")
        self._tx_captured_count += 1
        self.loop.call_soon_threadsafe(self._enqueue_tx, data)

    def _enqueue_tx(self, data):
        try:
            self._tx_queue.put_nowait(data)
        except asyncio.QueueFull:
            pass

    async def _tx_pump(self):
        while True:
            data = await self._tx_queue.get()
            if not self._tx_active:
                continue
            push_name = self._tx_push_name or "push_tx"
            try:
                await getattr(self.radio, push_name)(data)
                if self._tx_sent_count == 0:
                    self._status(f"TX audio: first chunk sent via {push_name}() ({len(data)} bytes).")
                self._tx_sent_count += 1
            except Exception as exc:
                self._status(f"TX audio: {push_name}() failed ({exc}); releasing PTT.")
                self._tx_active = False

    async def _tx_watchdog(self):
        """Speaks up if PTT has been held for a few seconds with zero mic
        data captured -- distinguishes "wrong/muted input device" from
        "capturing fine, but push_tx() silently isn't reaching the radio"."""
        await asyncio.sleep(3.0)
        if self._tx_active and self._tx_captured_count == 0:
            self._status(
                "TX audio: PTT has been held for 3s with no mic data captured at all -- "
                "check the selected input device is actually the right one and isn't muted "
                "at the OS level."
            )
        elif self._tx_active and self._tx_sent_count == 0:
            self._status(
                f"TX audio: mic is capturing ({self._tx_captured_count} chunks) but none have "
                "been confirmed sent via push_tx() -- check for a push_tx() error above, or "
                "this may indicate push_tx() accepts the call but the radio doesn't use the "
                "audio (wrong data format/mode-dependent behavior)."
            )

    async def start_tx_session(self):
        """Called by RadioWorker._start_ptt() when a mic is configured --
        brackets the audio session using whichever start method was
        resolved in start() (PCM-specific preferred). Separate from
        set_ptt(), which actually keys the radio and is confirmed to
        work independent of this."""
        start_name = self._tx_start_name or "start_tx"
        try:
            await getattr(self.radio, start_name)()
        except Exception as exc:
            self._status(f"TX audio: {start_name}() failed ({exc}).")

    async def stop_tx_session(self):
        """Called by RadioWorker._stop_ptt() when a mic is configured."""
        stop_name = self._tx_stop_name or "stop_tx"
        try:
            await getattr(self.radio, stop_name)()
        except Exception as exc:
            self._status(f"TX audio: {stop_name}() failed ({exc}).")

    def set_tx_active(self, active: bool):
        """Called by RadioWorker when PTT is pressed/released -- gates
        whether captured mic audio gets streamed (see _on_input_captured/
        _tx_pump above). Does NOT key the radio itself; that's
        RadioWorker's job now, independent of whether a mic is even
        configured (a mic-less connection can still key PTT, it just
        won't send any TX audio while doing so)."""
        self._tx_active = active
        if active:
            self._tx_captured_count = 0
            self._tx_sent_count = 0
            if self._tx_watchdog_task:
                self._tx_watchdog_task.cancel()
            self._tx_watchdog_task = asyncio.ensure_future(self._tx_watchdog())
        elif self._tx_watchdog_task:
            self._tx_watchdog_task.cancel()
            self._tx_watchdog_task = None

    def has_mic(self):
        """Whether a capture stream is actually open. Lets PTT keying
        report clearly when there's no mic to send audio from, instead
        of silently keying with no TX audio and leaving that to be
        discovered by ear."""
        return self._in_stream is not None


class RadioWorker(QThread):
    """Owns the asyncio loop and the rigplane radio connection.

    Runs entirely on its own thread. Communicates outward only via
    signals; accepts commands only via thread-safe coroutine
    scheduling (see set_frequency()).
    """

    connected = Signal()
    connection_failed = Signal(str)
    frequency_updated = Signal(int)      # Hz
    meter_updated = Signal(str, int)     # (meter_type key, raw value)
    control_updated = Signal(str, object)  # (control key, current value)
    scope_frame_received = Signal(object)  # rigplane.scope.ScopeFrame
    audio_status = Signal(str)           # informational, not an error
    level_updated = Signal(str, float)   # (level key, value 0.0-1.0)
    error = Signal(str)

    def __init__(self, details, parent=None):
        super().__init__(parent)
        self._details = details
        self.loop = None       # asyncio event loop, created inside run()
        self.radio = None      # rigplane Radio, set once connected
        self._radio_cm = None
        self._stop_requested = False
        self.audio_bridge = None  # set in _setup_audio() once connected, if applicable
        self._virtual_cable_modules = None  # (rx_module_id, tx_module_id) while virtual cables are active
        self._virtual_cable_previous_sink = None    # PulseAudio's default sink before enabling virtual cables
        self._virtual_cable_previous_source = None  # PulseAudio's default source before enabling virtual cables
        self._meter_getters = {}  # meter_type -> resolved getter name, populated by _setup_meters()
        self._control_methods = {}  # control key -> (get_name, set_name), populated by _setup_controls()
        self._control_enums = {}    # control key -> resolved enum class, for controls needing enum_import
        self._level_methods = {}  # level key -> (get_name, set_name), populated by _setup_levels()
        # Some LevelsCapable setters want a 0.0-1.0 float; others (confirmed
        # via a runtime error on squelch) want a raw int on Icom's native
        # 0-255 CI-V level scale. Cache per level key which one worked so
        # _call_level_setter() only has to probe once per level.
        self._level_value_mode = {}

    def run(self):
        """Thread entry point: create and own the asyncio loop here."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        finally:
            self.loop.close()

    def _build_config(self):
        d = self._details
        if d["connection_type"] == "network":
            return LanBackendConfig(
                host=d["host"],
                port=d["port"],
                radio_addr=d["addr"],
                username=d["username"],
                password=d["password"],
            )
        if SerialBackendConfig is None:
            raise RuntimeError(
                "rigplane has no SerialBackendConfig in this install -- "
                "check `python -c \"import rigplane; help(rigplane)\"` "
                "for the correct USB/serial config class name."
            )
        return SerialBackendConfig(
            device=d["serial_port"],
            baudrate=d["baud_rate"],
            radio_addr=d["addr"],
        )

    async def _setup_audio(self):
        """Builds and starts an AudioBridge if the user selected any
        audio devices and this radio supports rigplane's AudioTransport
        protocol. See AudioBridge's docstring for what it does and does
        not guarantee."""
        input_device = self._details.get("audio_input_device")
        output_device = self._details.get("audio_output_device")
        if not input_device and not output_device:
            return  # user left both as "None" -- nothing to set up

        if AudioTransport is None or not isinstance(self.radio, AudioTransport):
            self.audio_status.emit(
                "Audio devices selected, but this radio/backend doesn't "
                "implement rigplane's AudioTransport protocol."
            )
            return

        self.audio_bridge = AudioBridge(
            self.radio, self.loop, input_device, output_device,
            status_callback=self.audio_status.emit,
        )
        await self.audio_bridge.start()

    def enable_virtual_cables(self):
        """Thread-safe: call from the GUI thread. Creates two PulseAudio/
        PipeWire null-sinks (see the module-level comment above
        AudioBridge) and switches this app's own audio bridge to use
        them instead of whatever devices were chosen in the connection
        dialog, so an external app can send/receive audio through this
        app's radio connection. Linux/PulseAudio-or-PipeWire only."""
        if self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._enable_virtual_cables(), self.loop)

    async def _enable_virtual_cables(self):
        if not pactl_available():
            self.error.emit(
                "Virtual Cables: requires Linux with PulseAudio or PipeWire "
                "(pactl not found) -- on Windows/macOS, install a virtual "
                "audio cable driver like VB-CABLE or BlackHole instead, "
                "then select it directly in the connection dialog."
            )
            return

        try:
            rx_module = create_null_sink(VIRTUAL_CABLE_RX_NAME, VIRTUAL_CABLE_RX_DESC)
            tx_module = create_null_sink(VIRTUAL_CABLE_TX_NAME, VIRTUAL_CABLE_TX_DESC)
        except Exception as exc:
            self.error.emit(f"Virtual Cables: couldn't create sinks ({exc}).")
            return
        self._virtual_cable_modules = (rx_module, tx_module)

        # Confirmed via two live tests: trying to get PortAudio to
        # enumerate each new sink as its own separately-named device
        # doesn't work on this system -- its PortAudio build appears to
        # only expose a generic "default" device (the same one the
        # normal, non-virtual RX stream already uses successfully). So
        # instead of fighting that, redirect PulseAudio's own DEFAULT
        # sink/source at the new cables, and just use PortAudio's
        # "default" device (device=None, which sounddevice/PortAudio
        # resolves to whatever the system default currently is) -- a
        # mechanism already proven to work, rather than one that isn't.
        self._virtual_cable_previous_sink = get_default_sink_name()
        self._virtual_cable_previous_source = get_default_source_name()
        set_default_sink(VIRTUAL_CABLE_RX_NAME)
        set_default_source(f"{VIRTUAL_CABLE_TX_NAME}.monitor")

        if self.audio_bridge is not None:
            await self.audio_bridge.stop()

        self.audio_bridge = AudioBridge(
            self.radio, self.loop,
            input_device=AUDIO_DEVICE_SYSTEM_DEFAULT,
            output_device=AUDIO_DEVICE_SYSTEM_DEFAULT,
            status_callback=self.audio_status.emit,
        )
        await self.audio_bridge.start()

        # Confirmed by direct testing: changing PulseAudio's default
        # sink/source doesn't retroactively (or even prospectively, for
        # this app's stream specifically) redirect where our own already-
        # open stream connects -- what actually worked, done manually via
        # pavucontrol, was MOVING the already-open stream to the new
        # sink/source. Doing that automatically here instead of relying
        # on the default-redirection alone. Retries briefly since the
        # stream may take a moment to register with PulseAudio after
        # opening.
        my_pid = os.getpid()
        sink_input_ids = []
        source_output_ids = []
        for _attempt in range(6):
            if not sink_input_ids:
                sink_input_ids = find_own_sink_input_ids(my_pid)
            if not source_output_ids:
                source_output_ids = find_own_source_output_ids(my_pid)
            if sink_input_ids and source_output_ids:
                break
            await asyncio.sleep(0.3)

        for sink_input_id in sink_input_ids:
            move_sink_input(sink_input_id, VIRTUAL_CABLE_RX_NAME)
        for source_output_id in source_output_ids:
            move_source_output(source_output_id, f"{VIRTUAL_CABLE_TX_NAME}.monitor")

        if not sink_input_ids or not source_output_ids:
            missing = []
            if not sink_input_ids:
                missing.append("playback")
            if not source_output_ids:
                missing.append("recording")
            self.audio_status.emit(
                f"Virtual Cables: couldn't automatically find/move this app's "
                f"own {' and '.join(missing)} stream in PulseAudio -- you may "
                "need to reassign it manually in pavucontrol (Playback/"
                "Recording tabs), same as before."
            )

        self.audio_status.emit(
            f"Virtual Cables active. In WSJT-X (or similar), set its input "
            f"device to \"Monitor of {VIRTUAL_CABLE_RX_DESC}\" and its "
            f"output device to \"{VIRTUAL_CABLE_TX_DESC}\"."
        )

    def disable_virtual_cables(self):
        """Thread-safe: call from the GUI thread. Restores the audio
        devices originally chosen in the connection dialog and tears
        down the virtual sinks."""
        if self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._disable_virtual_cables(), self.loop)

    async def _disable_virtual_cables(self):
        if self.audio_bridge is not None:
            await self.audio_bridge.stop()

        if self._virtual_cable_previous_sink:
            set_default_sink(self._virtual_cable_previous_sink)
        if self._virtual_cable_previous_source:
            set_default_source(self._virtual_cable_previous_source)
        self._virtual_cable_previous_sink = None
        self._virtual_cable_previous_source = None

        if self._virtual_cable_modules:
            for module_id in self._virtual_cable_modules:
                unload_pactl_module(module_id)
            self._virtual_cable_modules = None

        input_device = self._details.get("audio_input_device")
        output_device = self._details.get("audio_output_device")
        if input_device or output_device:
            self.audio_bridge = AudioBridge(
                self.radio, self.loop, input_device, output_device,
                status_callback=self.audio_status.emit,
            )
            await self.audio_bridge.start()
        else:
            self.audio_bridge = None
        self.audio_status.emit("Virtual Cables disabled -- restored original audio devices.")

    def start_ptt(self):
        """Thread-safe: call from the GUI thread on PTT button press.
        Keys the radio directly -- previously this only worked if an
        AudioBridge with a configured mic existed, which meant PTT did
        nothing at all on a connection with no audio devices selected.
        Keying and mic-audio-streaming are independent now: this radio
        gets keyed regardless, and if a mic IS configured, the bridge
        also gets told TX is active so it starts sending audio."""
        if self.loop is None or self.radio is None:
            self.error.emit("PTT: not connected yet -- start_ptt() ignored.")
            return
        asyncio.run_coroutine_threadsafe(self._start_ptt(), self.loop)

    async def _start_ptt(self):
        # set_ptt() confirmed as a real, separate method via a dir(radio)
        # scan -- it's what actually keys the radio; the audio-session
        # start/push/stop calls (via AudioBridge) are only relevant if a
        # mic is configured. Keying happens first regardless of audio.
        try:
            await self.radio.set_ptt(True)
            self.audio_status.emit("PTT: radio.set_ptt(True) succeeded.")
        except Exception as exc:
            self.error.emit(f"PTT: radio.set_ptt(True) failed ({exc}).")
            return
        if self.audio_bridge is None:
            self.audio_status.emit(
                "PTT: no audio devices were configured for this connection -- "
                "keying the radio, but sending no TX audio."
            )
        elif not self.audio_bridge.has_mic():
            self.audio_status.emit(
                "PTT: no microphone/input device is open for this connection -- "
                "keying the radio, but sending no TX audio."
            )
        else:
            await self.audio_bridge.start_tx_session()
            self.audio_bridge.set_tx_active(True)

    def stop_ptt(self):
        """Thread-safe: call from the GUI thread on PTT button release."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._stop_ptt(), self.loop)

    async def _stop_ptt(self):
        if self.audio_bridge is not None and self.audio_bridge.has_mic():
            self.audio_bridge.set_tx_active(False)
            await self.audio_bridge.stop_tx_session()
        # Always attempt to unkey, regardless of audio-session teardown --
        # leaving the radio keyed indefinitely is worse than a slightly
        # out-of-order shutdown.
        try:
            await self.radio.set_ptt(False)
            self.audio_status.emit("PTT: radio.set_ptt(False) succeeded.")
        except Exception as exc:
            self.error.emit(
                f"PTT: radio.set_ptt(False) FAILED ({exc}) -- radio may still be keyed! "
                "Check the radio directly."
            )

    async def _setup_levels(self):
        """Checks whether this radio supports rigplane's LevelsCapable
        protocol and, if so, discovers the real get/set method names for
        every entry in LEVEL_DEFINITIONS (same _find_method_name
        discovery used elsewhere). Missing ones are reported once, with
        a filtered dir(radio) hint, instead of failing silently on first
        use. Live values are polled continuously in _poll_loop() rather
        than only read once here, so a slider reflects changes made
        directly on the radio's own front panel too."""
        self._level_methods = {}
        if LevelsCapable is None or not isinstance(self.radio, LevelsCapable):
            return

        missing = []
        for key, definition in LEVEL_DEFINITIONS.items():
            get_name = _find_method_name(self.radio, definition["getter_candidates"])
            set_name = _find_method_name(self.radio, definition["setter_candidates"])
            if get_name and set_name:
                self._level_methods[key] = (get_name, set_name)
            else:
                missing.append(f"{definition['label']} (get={get_name or 'none'}, set={set_name or 'none'})")
        if missing:
            # Show exactly which candidates matched (or didn't), plus a
            # filtered dir(radio) listing of anything that looks related,
            # right in the app -- no need to go inspect it separately.
            hints = sorted(
                n for n in dir(self.radio)
                if any(k in n.lower() for k in ("af", "squelch", "sql", "volume", "gain", "monitor", "power", "rf"))
                and not n.startswith("_")
            )
            self.error.emit(
                f"Levels unavailable on this radio/install: {', '.join(missing)}. "
                f"Related attributes found on radio: {', '.join(hints) or '(none)'}"
            )

        if "af_gain" in self._level_methods:
            self._hint_usb_audio_level_attrs()

    def _hint_usb_audio_level_attrs(self):
        """The AF gain slider controls the radio's own physical speaker
        output -- Hamlib (rigplane's nearest cousin) defines a SEPARATE
        level, RIG_LEVEL_USB_AF, specifically for the digital audio
        stream's output level. Scan for anything that looks like it on
        this radio and surface it, rather than making the person go dig
        through dir(radio) themselves after finding this out the hard way."""
        hints = sorted(
            n for n in dir(self.radio)
            if "usb" in n.lower()
            and any(k in n.lower() for k in ("af", "audio", "gain", "level", "output"))
            and not n.startswith("_")
        )
        if hints:
            self.audio_status.emit(
                "Note: AF Gain controls the radio's own speaker, not "
                "necessarily the audio streamed to this app. Found possible "
                f"USB/computer-audio level attributes: {', '.join(hints)} -- "
                "one of these may be the real control for what you hear here."
            )

    @staticmethod
    def _normalize_level_value(raw):
        """Getter values might come back as a 0.0-1.0 float or, like the
        squelch setter turned out to want, a raw int on Icom's 0-255
        CI-V level scale. Treat anything above 1.0 as the latter."""
        value = float(raw)
        if value > 1.0:
            value = value / 255.0
        return max(0.0, min(1.0, value))

    async def _call_level_setter(self, setter_name, value, cache_key):
        """Calls a discovered LevelsCapable setter with a 0.0-1.0 value.
        Tries a plain float first; if the setter rejects that with a
        TypeError/ValueError (as set_squelch() did -- it formats the
        value with Python's `:d` spec, which only accepts ints), retries
        as int(round(value * 255)), the raw Icom CI-V level scale used
        elsewhere in this app (e.g. the S-meter). Whichever form works
        is cached per cache_key so later calls don't re-probe."""
        setter = getattr(self.radio, setter_name)
        mode = self._level_value_mode.get(cache_key, "float")
        if mode == "float":
            try:
                await setter(value)
                self._level_value_mode[cache_key] = "float"
                return
            except (TypeError, ValueError):
                pass  # fall through and try the raw-int scale instead
        await setter(int(round(value * 255)))
        self._level_value_mode[cache_key] = "int255"

    def set_level_value(self, key: str, value: float):
        """Thread-safe: call from the GUI thread. value is 0.0-1.0."""
        if self.loop is None or key not in self._level_methods:
            return
        asyncio.run_coroutine_threadsafe(self._set_level_value(key, value), self.loop)

    async def _set_level_value(self, key: str, value: float):
        _get_name, set_name = self._level_methods[key]
        definition = LEVEL_DEFINITIONS[key]
        try:
            await self._call_level_setter(set_name, value, key)
        except Exception as exc:
            self.error.emit(f"{definition['label']}: {set_name}() failed ({exc}).")

    def _print_radio_attribute_diagnostic(self):
        """Prints two things on connect, to help find real getter/setter
        names for something rigplane doesn't document, without writing a
        separate throwaway script:
        1. Every attribute matching DIAGNOSTIC_DIR_KEYWORDS (quick, targeted).
        2. Every attribute starting with DIAGNOSTIC_DIR_PREFIXES (comprehensive
           -- for scanning by eye when the keyword search comes up empty,
           in case the real name doesn't contain the keyword you'd expect)."""
        if DIAGNOSTIC_DIR_KEYWORDS:
            keyword_matches = sorted(
                n for n in dir(self.radio)
                if any(k in n.lower() for k in DIAGNOSTIC_DIR_KEYWORDS) and not n.startswith("_")
            )
            print(f"[DIAGNOSTIC] dir(radio) matching {DIAGNOSTIC_DIR_KEYWORDS}:")
            for name in keyword_matches:
                print(f"  {name}")
            if not keyword_matches:
                print("  (no matches)")

        prefix_matches = sorted(
            n for n in dir(self.radio)
            if n.startswith(DIAGNOSTIC_DIR_PREFIXES)
        )
        print(f"[DIAGNOSTIC] dir(radio) starting with {DIAGNOSTIC_DIR_PREFIXES}:")
        for name in prefix_matches:
            print(f"  {name}")
        if not prefix_matches:
            print("  (no matches)")

    async def _probe_band_stack_methods(self):
        """Safe, read-only probing of get_bsr/get_band_stack (found via a
        dir(radio) scan on a real IC-705) to learn their actual call
        signature by observation. Since both are getters, calling them
        with a few plausible argument shapes can't change radio state --
        unlike guessing at a setter, which is why this is safe to try
        automatically rather than needing the person to write a script.
        Prints whatever each call returns, or the exception if the args
        were wrong, so the real signature is visible in the console."""
        candidate_args = [
            (),
            ("40m",), ("2m",),
            (3,), (7,),
            ("40m", 1), (3, 1),
            (0x03, 0x01),
        ]
        for name in ("get_bsr", "get_band_stack"):
            method = getattr(self.radio, name, None)
            if method is None:
                continue
            print(f"[DIAGNOSTIC] probing {name}():")
            for args in candidate_args:
                try:
                    result = await method(*args)
                    arg_str = ", ".join(repr(a) for a in args)
                    print(f"  {name}({arg_str}) -> {result!r}")
                except TypeError as exc:
                    arg_str = ", ".join(repr(a) for a in args)
                    print(f"  {name}({arg_str}) -> TypeError: {exc}")
                except Exception as exc:
                    arg_str = ", ".join(repr(a) for a in args)
                    print(f"  {name}({arg_str}) -> {type(exc).__name__}: {exc}")

    async def _main(self):
        try:
            config = self._build_config()
            self._radio_cm = create_radio(config)
            self.radio = await self._radio_cm.__aenter__()
        except Exception as exc:
            self.connection_failed.emit(str(exc))
            return

        self.connected.emit()
        self._print_radio_attribute_diagnostic()
        await self._probe_band_stack_methods()
        await self._setup_audio()
        await self._setup_levels()
        self._setup_meters()
        self._setup_controls()

        # Scope data arrives unsolicited over CI-V; register a callback
        # rather than polling for it. The callback fires on this thread's
        # event loop, so it must only emit a signal -- never touch a widget.
        try:
            self.radio.on_scope_data(self._handle_scope_frame)
            await self.radio.enable_scope()
        except Exception as exc:
            self.error.emit(f"Scope unavailable: {exc}")

        try:
            await self._poll_loop()
        finally:
            if self.audio_bridge:
                await self.audio_bridge.stop()
            try:
                await self.radio.disable_scope()
                self.radio.on_scope_data(None)
            except Exception:
                pass
            await self._radio_cm.__aexit__(None, None, None)

    def _handle_scope_frame(self, frame):
        """Runs on the worker thread (called from rigplane's CI-V receive
        loop). Only ever emits a signal -- no Qt widget access here."""
        self.scope_frame_received.emit(frame)

    def _setup_meters(self):
        """For each meter type, resolves the real getter name -- either
        the single hardcoded one, or (for types with a getter_candidates
        list, like ALC/Voltage where the first guess was confirmed wrong)
        the first candidate that actually exists via hasattr. Done once
        here rather than discovering a missing getter by hitting
        AttributeError on every single poll cycle forever. Meter widgets
        set to a type with no working getter simply won't update; missing
        ones are reported once here instead of repeatedly."""
        missing = []
        self._meter_getters = {}  # meter_type -> resolved method name
        for meter_type, definition in METER_DEFINITIONS.items():
            candidates = definition.get("getter_candidates", [definition["getter"]])
            resolved = _find_method_name(self.radio, candidates)
            if resolved:
                self._meter_getters[meter_type] = resolved
            else:
                missing.append(definition["label"])
        if missing:
            self.error.emit(
                f"Meters unavailable on this radio/install: {', '.join(missing)}."
            )

        # native_power_unit is confirmed by rigplane's own docs to be a
        # Literal["raw_255", "watts"] attribute -- ask the radio which
        # scale its power reading uses instead of guessing. Mutating
        # METER_DEFINITIONS in place is deliberate: this app only ever
        # connects to one radio at a time, so there's no risk of one
        # radio's scale leaking into another connection.
        if "power" in self._meter_getters:
            native_unit = getattr(self.radio, "native_power_unit", None)
            if native_unit == "watts":
                METER_DEFINITIONS["power"]["kind"] = "direct"
                self.audio_status.emit("Power meter: radio reports native unit 'watts' -- reading directly.")
            elif native_unit == "raw_255":
                METER_DEFINITIONS["power"]["kind"] = "linear"
                self.audio_status.emit("Power meter: radio reports native unit 'raw_255' -- scaling from raw.")
            else:
                self.audio_status.emit(
                    f"Power meter: native_power_unit is {native_unit!r} (expected 'watts' or "
                    "'raw_255') -- defaulting to raw-scale math; watch the reading for sanity."
                )

    def _setup_controls(self):
        """Resolves the real getter/setter method names for every entry
        in CONTROL_DEFINITIONS via _find_method_name -- same discovery
        pattern as _setup_meters()/_setup_levels(). Getters were
        confirmed present via a dir(radio) scan; setters are best-effort
        guesses, so this is where a wrong guess gets surfaced (once,
        clearly) rather than failing silently on first use.

        Also resolves "enum_import" entries (confirmed needed for AGC:
        set_agc() rejected a plain string, wanting a real AgcMode enum
        member instead) -- imports the named class once and stores it in
        self._control_enums for _set_control_value() to look values up
        against by name.

        Entries marked "write_only" (confirmed needed for memory_mode:
        Icom's own CI-V reference documents command 0x08 as SELECT-only,
        no GET variant at all) only need a setter to resolve -- the poll
        loop skips these entirely rather than polling something that is
        guaranteed to fail every single cycle forever."""
        missing = []
        self._control_methods = {}
        self._control_enums = {}
        for key, definition in CONTROL_DEFINITIONS.items():
            set_name = _find_method_name(self.radio, definition["setter_candidates"])
            if definition.get("write_only"):
                get_name = None  # deliberately never resolved/polled -- see definition's comment
                if set_name:
                    self._control_methods[key] = (get_name, set_name)
                else:
                    missing.append(f"{definition['label']} (write-only, set=none)")
            else:
                get_name = _find_method_name(self.radio, definition["getter_candidates"])
                if get_name and set_name:
                    self._control_methods[key] = (get_name, set_name)
                else:
                    missing.append(
                        f"{definition['label']} (get={get_name or 'none'}, set={set_name or 'none'})"
                    )

            enum_import = definition.get("enum_import")
            if enum_import and key in self._control_methods:
                module_name, class_name = enum_import
                try:
                    module = importlib.import_module(module_name)
                    self._control_enums[key] = getattr(module, class_name)
                except Exception as exc:
                    self.audio_status.emit(
                        f"{definition['label']}: couldn't import {class_name} from "
                        f"{module_name} ({exc}) -- will try plain values instead."
                    )
        if missing:
            self.error.emit(f"Controls unavailable on this radio/install: {', '.join(missing)}.")

    def set_control_value(self, key: str, value):
        """Thread-safe: call from the GUI thread."""
        if self.loop is None:
            return
        if key not in self._control_methods:
            self.error.emit(
                f"{CONTROL_DEFINITIONS[key]['label']}: not available on this radio/install "
                "(no working getter+setter was found on connect)."
            )
            return
        asyncio.run_coroutine_threadsafe(self._set_control_value(key, value), self.loop)

    async def _set_control_value(self, key: str, value):
        _get_name, set_name = self._control_methods[key]
        definition = CONTROL_DEFINITIONS[key]
        enum_cls = self._control_enums.get(key)
        if enum_cls is not None and isinstance(value, str):
            try:
                value = enum_cls[value]
            except KeyError:
                pass  # fall through and try the raw value anyway
        try:
            await getattr(self.radio, set_name)(value)
        except Exception as exc:
            self.error.emit(f"{definition['label']}: {set_name}({value!r}) failed ({exc}).")

    async def _poll_loop(self):
        """Periodically reads live values: frequency, plus every
        confirmed-supported meter (see _setup_meters()). There are only a
        handful, so polling all of them each cycle is negligible overhead
        -- this lets any number of independently-selectable MeterWidgets
        share this one poll loop, each just filtering meter_updated for
        whichever type it's currently showing, rather than the worker
        needing to track which widget wants which type."""
        while not self._stop_requested:
            try:
                freq_hz = await self.radio.get_frequency()
                self.frequency_updated.emit(freq_hz)
            except Exception as exc:
                self.error.emit(str(exc))

            for meter_type, getter_name in self._meter_getters.items():
                definition = METER_DEFINITIONS[meter_type]
                try:
                    getter = getattr(self.radio, getter_name)
                    value = await getter()
                    self.meter_updated.emit(meter_type, value)
                except Exception as exc:
                    self.error.emit(f"{definition['label']}: {exc}")

            for key, (get_name, _set_name) in self._control_methods.items():
                if get_name is None:
                    continue  # write-only control (e.g. memory_mode) -- no GET variant exists, never poll
                definition = CONTROL_DEFINITIONS[key]
                try:
                    getter = getattr(self.radio, get_name)
                    value = await getter()
                    if definition.get("tuple_result") and isinstance(value, tuple):
                        value = value[0]
                    # Generic enum unwrap: any Enum/IntEnum member has a
                    # .name (plain str/int/bool don't), so this covers
                    # every control that turns out to return an enum, not
                    # just AGC (which is where this was actually confirmed
                    # needed) -- .name is what matches our option values.
                    if hasattr(value, "name"):
                        value = value.name
                    self.control_updated.emit(key, value)
                except Exception as exc:
                    self.error.emit(f"{definition['label']}: {exc}")

            for key, (get_name, _set_name) in self._level_methods.items():
                definition = LEVEL_DEFINITIONS[key]
                try:
                    getter = getattr(self.radio, get_name)
                    raw_value = await getter()
                    self.level_updated.emit(key, self._normalize_level_value(raw_value))
                except Exception as exc:
                    self.error.emit(f"{definition['label']}: {exc}")

            await asyncio.sleep(POLL_INTERVAL_SEC)

    # ---- Thread-safe command entry points (call these from the GUI thread) ----

    def set_frequency(self, freq_hz: int):
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_frequency(freq_hz), self.loop)

    async def _set_frequency(self, freq_hz: int):
        try:
            await self.radio.set_frequency(freq_hz)
        except Exception as exc:
            self.error.emit(str(exc))

    def select_band(self, band_label: str, low_edge_hz: int):
        """Thread-safe. Tries Icom's confirmed get_bsr() band-stacking
        recall first -- reads whatever frequency was last used on that
        band and tunes to it, like pressing the real band button. Falls
        back to tuning to the band's low edge (low_edge_hz) for bands
        with no confirmed band-stacking code (currently VHF/UHF -- see
        BAND_STACKING_CODES) or if the attempt fails for any reason."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._select_band(band_label, low_edge_hz), self.loop)

    async def _select_band(self, band_label: str, low_edge_hz: int):
        radio_model = self._details["radio_model"]
        band_code = BAND_STACKING_CODES.get(band_label)
        if (radio_model, band_label) in BAND_STACKING_EXCLUDED:
            band_code = None  # confirmed to hang on this radio -- skip straight to the band edge

        if band_code is not None:
            freq_hz, reason = await _try_recall_band_stack(self.radio, band_code)
            if freq_hz is not None:
                self.audio_status.emit(
                    f"Band: recalled {band_label} via band-stacking register ({freq_hz / 1e6:.6f} MHz)."
                )
                return
            self.audio_status.emit(
                f"Band: band-stacking register unavailable/failed for {band_label} "
                f"({reason}) -- tuning to the band edge instead."
            )

        try:
            await self.radio.set_frequency(low_edge_hz)
        except Exception as exc:
            self.error.emit(f"Band: setting frequency failed ({exc}).")

    def stop(self):
        """Request a clean shutdown of the polling loop and thread."""
        self._stop_requested = True
        # wait_for_thread (QThread.wait) is called by the GUI after this


class ConnectionDialog(QDialog):
    """Collects radio connection details before the main window opens.

    Call get_details() (a static helper below) rather than
    instantiating this directly -- it handles exec() and returns
    either a details dict or None if the user cancelled.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to Radio")

        self.radio_combo = QComboBox()
        self.radio_combo.addItems(RADIO_PROFILES.keys())
        self.radio_combo.currentTextChanged.connect(self._on_radio_changed)

        self.connection_combo = QComboBox()
        self.connection_combo.addItem("Network (LAN)", "network")
        self.connection_combo.addItem("USB (Serial)", "usb")
        self.connection_combo.currentIndexChanged.connect(self._on_connection_type_changed)

        self.addr_input = QLineEdit()
        self.addr_input.setPlaceholderText("A2")

        # --- Network-specific fields ---
        self.host_input = QLineEdit(DEFAULT_HOST)
        self.host_input.setPlaceholderText("192.168.1.100")

        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(DEFAULT_PORT)

        self.username_input = QLineEdit(DEFAULT_USERNAME)

        self.password_input = QLineEdit(DEFAULT_PASSWORD)
        self.password_input.setEchoMode(QLineEdit.Password)

        # --- USB-specific fields ---
        self.serial_port_input = QLineEdit(DEFAULT_SERIAL_PORT)
        self.serial_port_input.setPlaceholderText("/dev/ttyUSB0 or COM3")

        self.baud_rate_input = QSpinBox()
        self.baud_rate_input.setRange(300, 921_600)
        self.baud_rate_input.setValue(DEFAULT_BAUD_RATE)

        # --- Audio device selection (independent of CI-V connection type) ---
        self.audio_input_combo = QComboBox()
        self.audio_output_combo = QComboBox()
        self._populate_audio_devices()

        form = QFormLayout()
        form.addRow("Radio:", self.radio_combo)
        form.addRow("Connection:", self.connection_combo)
        form.addRow("CI-V Address (hex):", self.addr_input)

        self.network_rows = [
            ("Host / IP:", self.host_input),
            ("Port:", self.port_input),
            ("Username:", self.username_input),
            ("Password:", self.password_input),
        ]
        self.usb_rows = [
            ("Serial Port:", self.serial_port_input),
            ("Baud Rate:", self.baud_rate_input),
        ]
        for label, widget in self.network_rows + self.usb_rows:
            form.addRow(label, widget)

        form.addRow("Audio Input:", self.audio_input_combo)
        form.addRow("Audio Output:", self.audio_output_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

        self.details = None  # populated on successful accept

        # Apply initial defaults for whichever radio is selected first.
        self._on_radio_changed(self.radio_combo.currentText())

    def _populate_audio_devices(self):
        """Fill the audio combos by querying sounddevice/PortAudio
        directly -- the same library AudioBridge uses to actually open
        the streams, so whatever's picked here is guaranteed openable.
        Stores each device's numeric PortAudio index as the combo's
        item data (not a name string), since numeric indices don't have
        the cross-library name-mismatch problem a name string would."""
        self.audio_input_combo.addItem("None (no RX audio)", None)
        self.audio_output_combo.addItem("None (no TX audio)", None)

        if not SOUNDDEVICE_AVAILABLE:
            self.audio_input_combo.addItem("sounddevice not installed", None)
            self.audio_output_combo.addItem("sounddevice not installed", None)
            self.audio_input_combo.setEnabled(False)
            self.audio_output_combo.setEnabled(False)
            return

        try:
            devices = sd.query_devices()
        except Exception as exc:
            self.audio_input_combo.addItem(f"Couldn't query devices: {exc}", None)
            self.audio_output_combo.addItem(f"Couldn't query devices: {exc}", None)
            self.audio_input_combo.setEnabled(False)
            self.audio_output_combo.setEnabled(False)
            return

        for index, device in enumerate(devices):
            if device.get("max_input_channels", 0) > 0:
                self.audio_input_combo.addItem(device["name"], index)
            if device.get("max_output_channels", 0) > 0:
                self.audio_output_combo.addItem(device["name"], index)

    def _on_radio_changed(self, radio_model):
        profile = RADIO_PROFILES[radio_model]
        self.addr_input.setText(profile["addr_hex"])
        index = self.connection_combo.findData(profile["default_connection"])
        if index != -1:
            self.connection_combo.setCurrentIndex(index)
        else:
            self._on_connection_type_changed(self.connection_combo.currentIndex())

    def _on_connection_type_changed(self, _index):
        is_network = self.connection_combo.currentData() == "network"
        for _label, widget in self.network_rows:
            widget.setEnabled(is_network)
            widget.setVisible(is_network)
        for _label, widget in self.usb_rows:
            widget.setEnabled(not is_network)
            widget.setVisible(not is_network)
        # Also hide/show the row labels themselves.
        form = self.layout().itemAt(0).layout()
        for label, widget in self.network_rows:
            row_label = form.labelForField(widget)
            if row_label is not None:
                row_label.setVisible(is_network)
        for label, widget in self.usb_rows:
            row_label = form.labelForField(widget)
            if row_label is not None:
                row_label.setVisible(not is_network)

    def _on_accept(self):
        addr_text = self.addr_input.text().strip()
        try:
            addr = int(addr_text, 16)
        except ValueError:
            QMessageBox.warning(
                self, "Invalid address", "CI-V address must be hex, e.g. A2."
            )
            return

        connection_type = self.connection_combo.currentData()
        details = {
            "radio_model": self.radio_combo.currentText(),
            "connection_type": connection_type,
            "addr": addr,
            "audio_input_device": self.audio_input_combo.currentData(),
            "audio_output_device": self.audio_output_combo.currentData(),
        }

        if connection_type == "network":
            host = self.host_input.text().strip()
            if not host:
                QMessageBox.warning(self, "Missing host", "Enter the radio's IP address.")
                return
            details.update({
                "host": host,
                "port": self.port_input.value(),
                "username": self.username_input.text().strip(),
                "password": self.password_input.text(),
            })
        else:
            serial_port = self.serial_port_input.text().strip()
            if not serial_port:
                QMessageBox.warning(self, "Missing serial port", "Enter the serial device path.")
                return
            details.update({
                "serial_port": serial_port,
                "baud_rate": self.baud_rate_input.value(),
            })

        self.details = details
        self.accept()

    @staticmethod
    def get_details(parent=None):
        """Show the dialog modally. Returns a details dict, or None
        if the user cancelled."""
        dialog = ConnectionDialog(parent)
        if dialog.exec() == QDialog.Accepted:
            return dialog.details
        return None


class SpectrumWidget(QWidget):
    """Amplitude-vs-frequency plot of the most recent ScopeFrame."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self._frame = None
        self._overlay_widget = None
        self._overlay_margin = 8

    def set_overlay_widget(self, widget, margin=8):
        """Reparents `widget` onto this scope as a fixed top-right
        overlay (e.g. the main frequency readout) -- Qt composites child
        widgets on top of a parent's own paintEvent automatically, so no
        z-order trick is needed beyond normal parenting. The widget keeps
        its own size (give it setFixedWidth()/setFixedHeight() beforehand
        so its box doesn't jump around as its text changes); only its
        position is recalculated here, on every resize of the scope."""
        self._overlay_widget = widget
        self._overlay_margin = margin
        widget.setParent(self)
        self._position_overlay()

    def _position_overlay(self):
        if self._overlay_widget is None:
            return
        x = self.width() - self._overlay_widget.width() - self._overlay_margin
        y = self._overlay_margin
        self._overlay_widget.move(max(0, x), y)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_overlay()

    def set_frame(self, frame):
        self._frame = frame
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(10, 10, 20))

        if self._frame is None or not self._frame.pixels:
            painter.setPen(QColor(120, 120, 120))
            painter.drawText(self.rect(), Qt.AlignCenter, "Waiting for scope data...")
            return

        pixels = self._frame.pixels
        n = len(pixels)
        w, h = self.width(), self.height()

        painter.setPen(QPen(QColor(0, 220, 120), 1.5))
        path = QPainterPath()
        for i, amp in enumerate(pixels):
            x = (i / (n - 1)) * w if n > 1 else 0
            y = h - (amp / 160.0) * h
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.drawPath(path)

        painter.setPen(QColor(150, 150, 150))
        start_text = f"{self._frame.start_freq_hz / 1e6:.4f} MHz"
        end_text = f"{self._frame.end_freq_hz / 1e6:.4f} MHz"
        painter.drawText(5, h - 5, start_text)
        painter.drawText(w - painter.fontMetrics().horizontalAdvance(end_text) - 5, h - 5, end_text)


class WaterfallWidget(QWidget):
    """Scrolling history of ScopeFrames, newest at the top."""

    def __init__(self, max_rows=WATERFALL_ROWS, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self._max_rows = max_rows
        self._image = None  # QImage buffer: width = pixels/frame, height = max_rows

    def set_frame(self, frame):
        pixels = frame.pixels
        n = len(pixels)
        if n == 0:
            return

        if self._image is None or self._image.width() != n:
            self._image = QImage(n, self._max_rows, QImage.Format_RGB32)
            self._image.fill(QColor(10, 10, 20))

        # Shift the existing image down by one row, dropping the oldest.
        scrolled = QImage(n, self._max_rows, QImage.Format_RGB32)
        painter = QPainter(scrolled)
        painter.drawImage(0, 1, self._image, 0, 0, n, self._max_rows - 1)
        painter.end()
        self._image = scrolled

        for x, amp in enumerate(pixels):
            self._image.setPixelColor(x, 0, amplitude_to_color(amp))

        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._image is None:
            painter.fillRect(self.rect(), QColor(10, 10, 20))
            painter.setPen(QColor(120, 120, 120))
            painter.drawText(self.rect(), Qt.AlignCenter, "Waiting for scope data...")
            return
        painter.drawImage(self.rect(), self._image, self._image.rect())


class MeterWidget(QWidget):
    """Segmented bargraph meter, styled after the LCD meter dock on the
    IC-7300 / IC-9700 / IC-705 touchscreen. Double-click to switch which
    reading it displays (see METER_DEFINITIONS)."""

    NUM_SEGMENTS = 32
    S_TICKS = [("S1", 0.072), ("S3", 0.217), ("S5", 0.361), ("S7", 0.506), ("S9", S_METER_S9_FRACTION)]
    DB_TICKS = [("+20", 0.767), ("+40", 0.884), ("+60", 1.0)]

    meter_type_changed = Signal(str)

    def __init__(self, meter_type="s_meter", parent=None):
        super().__init__(parent)
        self.setMinimumHeight(70)
        self.setMinimumWidth(300)
        self.setToolTip("Double-click to change meter")
        self._raw_value = 0
        self._meter_type = meter_type

    @property
    def meter_type(self):
        return self._meter_type

    def set_meter_type(self, meter_type):
        if meter_type not in METER_DEFINITIONS:
            return
        self._meter_type = meter_type
        self._raw_value = 0
        self.update()

    def set_value(self, raw_value):
        self._raw_value = raw_value
        self.update()

    def mouseDoubleClickEvent(self, event):
        menu = QMenu(self)
        for key, definition in METER_DEFINITIONS.items():
            action = menu.addAction(definition["label"])
            action.setCheckable(True)
            action.setChecked(key == self._meter_type)
            action.triggered.connect(lambda checked=False, k=key: self._select_meter_type(k))
        menu.exec(event.globalPosition().toPoint())

    def _select_meter_type(self, key):
        if key == self._meter_type:
            return
        self.set_meter_type(key)
        self.meter_type_changed.emit(key)

    # ---- S-meter calibration (raw 0-255 -> S0..S9+60dB) ----

    def _s_meter_fraction(self):
        raw = self._raw_value
        if raw <= S_METER_RAW_S9:
            return max(0.0, raw / S_METER_RAW_S9) * S_METER_S9_FRACTION
        span = S_METER_RAW_MAX - S_METER_RAW_S9
        over = (raw - S_METER_RAW_S9) / span if span else 0
        return S_METER_S9_FRACTION + min(1.0, over) * (1.0 - S_METER_S9_FRACTION)

    def _s_meter_label(self):
        raw = self._raw_value
        if raw <= S_METER_RAW_S9:
            s_units = (raw / S_METER_RAW_S9) * 9 if S_METER_RAW_S9 else 0
            return f"S{round(s_units)}"
        span = S_METER_RAW_MAX - S_METER_RAW_S9
        db_over = ((raw - S_METER_RAW_S9) / span) * 60 if span else 0
        return f"S9+{round(db_over)}dB"

    # ---- generic linear meter (raw 0-raw_max -> display_min-display_max) ----

    def _linear_fraction(self, definition):
        raw_max = definition["raw_max"]
        return max(0.0, min(1.0, self._raw_value / raw_max)) if raw_max else 0.0

    def _linear_label(self, definition):
        frac = self._linear_fraction(definition)
        display_min = definition.get("display_min", 0.0)
        display_max = definition["display_max"]
        value = display_min + frac * (display_max - display_min)
        return f"{value:.1f}{definition['unit']}"

    # ---- direct-value meter (reading is already in real units, e.g.
    # watts -- no raw/255 normalization, unlike "linear" above) ----

    def _direct_fraction(self, definition):
        display_min = definition.get("display_min", 0.0)
        display_max = definition["display_max"]
        span = display_max - display_min
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (self._raw_value - display_min) / span))

    def _direct_label(self, definition):
        return f"{self._raw_value:.1f}{definition['unit']}"

    def paintEvent(self, event):
        definition = METER_DEFINITIONS[self._meter_type]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        painter.fillRect(self.rect(), QColor(15, 15, 20))

        margin = 10
        bar_top = 20
        bar_height = h - 40
        bar_left = margin
        bar_width = w - 2 * margin

        if definition["kind"] == "s_meter":
            fraction = self._s_meter_fraction()
            redline_segment = int(S_METER_S9_FRACTION * self.NUM_SEGMENTS)
            label_text = self._s_meter_label()
        elif definition["kind"] == "direct":
            fraction = self._direct_fraction(definition)
            redline_segment = self.NUM_SEGMENTS + 1  # never red for a plain direct-value meter
            label_text = self._direct_label(definition)
        else:
            fraction = self._linear_fraction(definition)
            redline_segment = self.NUM_SEGMENTS + 1  # never red for a plain linear meter
            label_text = self._linear_label(definition)

        lit_segments = int(fraction * self.NUM_SEGMENTS)

        seg_gap = 2
        seg_width = (bar_width - seg_gap * (self.NUM_SEGMENTS - 1)) / self.NUM_SEGMENTS

        for i in range(self.NUM_SEGMENTS):
            x = bar_left + i * (seg_width + seg_gap)
            if i < lit_segments:
                color = QColor(255, 90, 60) if i >= redline_segment else QColor(80, 220, 140)
            else:
                color = QColor(40, 40, 48)
            painter.fillRect(QRectF(x, bar_top, seg_width, bar_height), color)

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        label_y = bar_top + bar_height + 2

        if definition["kind"] == "s_meter":
            painter.setPen(QColor(190, 190, 190))
            for label, frac in self.S_TICKS:
                x = bar_left + frac * bar_width
                painter.drawText(QRectF(x - 15, label_y, 30, 14), Qt.AlignCenter, label)
            painter.setPen(QColor(255, 130, 110))
            for label, frac in self.DB_TICKS:
                x = bar_left + frac * bar_width
                painter.drawText(QRectF(x - 15, label_y, 30, 14), Qt.AlignCenter, label)
        else:
            painter.setPen(QColor(190, 190, 190))
            display_min = definition.get("display_min", 0.0)
            display_max = definition["display_max"]
            for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
                value = display_min + frac * (display_max - display_min)
                x = bar_left + frac * bar_width
                painter.drawText(QRectF(x - 20, label_y, 40, 14), Qt.AlignCenter, f"{value:.0f}")

        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(230, 230, 230))
        painter.drawText(QRectF(bar_left, 0, bar_width * 0.6, bar_top - 2), Qt.AlignLeft | Qt.AlignVCenter, label_text)
        painter.setPen(QColor(150, 150, 150))
        painter.drawText(QRectF(bar_left + bar_width * 0.6, 0, bar_width * 0.4, bar_top - 2), Qt.AlignRight | Qt.AlignVCenter, definition["label"])


class TuningKnobWidget(QWidget):
    """A rotary tuning knob styled after an Icom main-dial knob: a
    knurled metal ring around a raised center cap with a white
    position mark.

    Unlike a slider, a real tuning knob has no fixed endpoints -- it's
    a rotary encoder that spins freely and reports relative clicks
    (detents), not an absolute position. This widget works the same
    way: dragging in a circle emits steps_changed(+1) or (-1) each time
    the drag crosses DEGREES_PER_KNOB_STEP of rotation, and the knob's
    own visual rotation is purely cosmetic (it just keeps spinning).
    Mouse-wheel scrolling over the knob also emits one step per notch.
    """

    steps_changed = Signal(int)  # +1 = clockwise/increase, -1 = counter-clockwise/decrease

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(130, 130)
        self.setCursor(Qt.OpenHandCursor)
        self._rotation = 0.0       # cumulative visual rotation, degrees, wraps mod 360
        self._dragging = False
        self._last_angle = None
        self._angle_accum = 0.0    # rotation since the last emitted step

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self._dragging = True
            self._last_angle = self._angle_at(event.position())
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        angle = self._angle_at(event.position())
        delta = self._shortest_delta(self._last_angle, angle)
        self._last_angle = angle
        self._rotation = (self._rotation + delta) % 360
        self._angle_accum += delta
        self.update()
        while abs(self._angle_accum) >= DEGREES_PER_KNOB_STEP:
            step = 1 if self._angle_accum > 0 else -1
            self._angle_accum -= step * DEGREES_PER_KNOB_STEP
            self.steps_changed.emit(step)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = False
            self._angle_accum = 0.0
            self.setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, event):
        if not self.isEnabled():
            return
        step = 1 if event.angleDelta().y() > 0 else -1
        self._rotation = (self._rotation + step * DEGREES_PER_KNOB_STEP) % 360
        self.update()
        self.steps_changed.emit(step)

    def _angle_at(self, pos):
        cx, cy = self.width() / 2, self.height() / 2
        return math.degrees(math.atan2(pos.y() - cy, pos.x() - cx))

    @staticmethod
    def _shortest_delta(a0, a1):
        """Signed angular difference a1-a0, wrapped to [-180, 180] so a
        drag crossing the -180/180 seam doesn't register as a huge jump."""
        return (a1 - a0 + 180) % 360 - 180

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        radius = min(w, h) / 2 - 4

        # Outer knurled ring, brushed-metal look via a radial gradient.
        outer_gradient = QRadialGradient(cx - radius * 0.3, cy - radius * 0.3, radius * 1.4)
        outer_gradient.setColorAt(0.0, QColor(150, 150, 155))
        outer_gradient.setColorAt(0.55, QColor(90, 90, 95))
        outer_gradient.setColorAt(1.0, QColor(35, 35, 38))
        painter.setBrush(QBrush(outer_gradient))
        painter.setPen(QPen(QColor(15, 15, 15), 2))
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # Knurled grooves around the rim, rotating with the knob.
        painter.save()
        painter.translate(cx, cy)
        painter.rotate(self._rotation)
        for i in range(40):
            painter.save()
            painter.rotate(i * (360 / 40))
            painter.setPen(QPen(QColor(15, 15, 15), 1.4))
            painter.drawLine(QPointF(radius * 0.86, 0), QPointF(radius * 0.98, 0))
            painter.restore()
        painter.restore()

        # Raised center cap.
        inner_radius = radius * 0.70
        inner_gradient = QRadialGradient(cx - inner_radius * 0.3, cy - inner_radius * 0.3, inner_radius * 1.3)
        inner_gradient.setColorAt(0.0, QColor(75, 75, 80))
        inner_gradient.setColorAt(1.0, QColor(25, 25, 28))
        painter.setBrush(QBrush(inner_gradient))
        painter.setPen(QPen(QColor(10, 10, 10), 1))
        painter.drawEllipse(QPointF(cx, cy), inner_radius, inner_radius)

        # White position mark, like the paint dab on an Icom VFO knob.
        painter.save()
        painter.translate(cx, cy)
        painter.rotate(self._rotation)
        pen = QPen(QColor(235, 235, 235), 3)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(0, -inner_radius * 0.28), QPointF(0, -inner_radius * 0.82))
        painter.restore()

        if not self.isEnabled():
            painter.setBrush(QColor(0, 0, 0, 120))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(cx, cy), radius, radius)


# WSJT-X's UDP Server settings (host/port/enable) live in its own .ini
# config file, but the exact key names for that file aren't documented or
# confirmed anywhere we could find -- writing them blind risks corrupting
# a file we don't own (unlike a wrong guess in our own CONTROL_DEFINITIONS,
# which just shows an error). Instead: launch WSJT-X into an entirely
# separate, isolated profile using its confirmed --rig-name=NAME option
# (documented in WSJT-X's own man page). First launch is a blank profile
# needing one-time setup; every launch after that reuses the same profile
# with those settings already in place -- this never touches the user's
# main/default WSJT-X profile at all.
WSJTX_RIG_NAME = "RadioApp"

# Common install locations to check before asking the user to browse.
# Not exhaustive -- package managers/custom installs vary -- just a
# starting guess per platform.
_WSJTX_CANDIDATE_PATHS = {
    "Windows": [
        r"C:\WSJT-X\bin\wsjtx.exe",
        r"C:\Program Files\WSJT-X\wsjtx.exe",
        r"C:\Program Files (x86)\WSJT-X\wsjtx.exe",
    ],
    "Darwin": [
        "/Applications/WSJT-X.app/Contents/MacOS/wsjtx",
    ],
    "Linux": [
        "/usr/bin/wsjtx",
        "/usr/local/bin/wsjtx",
    ],
}


def find_wsjtx_executable():
    """Best-effort auto-detection: checks the PATH, then common per-OS
    install locations. Returns a path string or None if nothing was found
    -- callers should fall back to asking the user to browse for it."""
    on_path = shutil.which("wsjtx")
    if on_path:
        return on_path
    for candidate in _WSJTX_CANDIDATE_PATHS.get(platform.system(), []):
        if os.path.isfile(candidate):
            return candidate
    return None


def launch_wsjtx(executable_path):
    """Launches WSJT-X into its own isolated --rig-name profile (see
    WSJTX_RIG_NAME above). Raises OSError/subprocess errors on failure --
    callers should catch and report those."""
    subprocess.Popen([executable_path, f"--rig-name={WSJTX_RIG_NAME}"])


# ==================== rigctld ("NET rigctl") server ====================
#
# A minimal Hamlib rigctld-compatible TCP server, so other CAT-aware apps
# (WSJT-X, JTDX, fldigi, ...) can drive THIS app's already-open radio
# connection -- selecting rig model 2 ("Hamlib NET rigctl") pointed at
# 127.0.0.1:4532 -- instead of needing their own separate CAT connection
# to the radio (which the radio's CI-V/serial port often can't share
# with two programs at once anyway).
#
# Protocol confirmed against Hamlib's own rigctld documentation: plain
# ASCII, newline-terminated commands; lowercase = get, uppercase = set;
# "RPRT x\n" (x=0 for success) replies to set commands; get commands
# return one value per line. Default port 4532 is Hamlib's documented
# default for "NET rigctl".
#
# The \dump_state handshake (WSJT-X's Hamlib backend sends this
# immediately on connect and requires a correctly-shaped response before
# it will proceed -- a real bug in another rigctld-compatible server,
# AetherSDR GitHub issue #63, shows what a malformed one does: 9-55s
# hangs or outright "IO error" connection failures) is now built by
# tracing Hamlib's actual netrigctl_open() source line-by-line (see
# RigctldServer._dump_state_response()'s docstring for the confirmed
# field sequence) rather than reconstructed from documentation alone. An
# earlier version of this got two things wrong that source-tracing
# caught: it omitted a required ITU-region line (shifting every
# subsequent field by one position) and sent trailing lines that were
# never supposed to be there for protocol version 0. What's still
# inferred rather than directly confirmed: the exact end-of-list
# sentinel values (RIG_IS_FRNG_END etc.) -- based on long-established
# Hamlib convention, not the literal macro source.
RIGCTLD_DEFAULT_PORT = 4532


class RigctldServer(QTcpServer):
    """Runs on the GUI thread -- QTcpServer/QTcpSocket are event-driven
    via Qt signals, so no separate thread is needed. Talks to the actual
    radio only through the callback functions passed in, reusing
    RadioWindow's existing worker/widget plumbing rather than
    duplicating radio-control logic."""

    def __init__(self, get_freq, set_freq, get_mode, set_mode, get_ptt, set_ptt,
                 port=RIGCTLD_DEFAULT_PORT, parent=None):
        super().__init__(parent)
        self._get_freq = get_freq
        self._set_freq = set_freq
        self._get_mode = get_mode
        self._set_mode = set_mode
        self._get_ptt = get_ptt
        self._set_ptt = set_ptt
        self._port = port
        self._buffers = {}  # QTcpSocket -> bytearray, for partial-line buffering
        self.newConnection.connect(self._on_new_connection)

    def start(self):
        if not self.listen(QHostAddress.AnyIPv4, self._port):
            raise RuntimeError(f"Couldn't listen on TCP port {self._port}: {self.errorString()}")

    def stop(self):
        for sock in list(self._buffers):
            sock.disconnectFromHost()
        self._buffers.clear()
        self.close()

    def _on_new_connection(self):
        while self.hasPendingConnections():
            sock = self.nextPendingConnection()
            self._buffers[sock] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(lambda s=sock: self._on_disconnected(s))

    def _on_disconnected(self, sock):
        self._buffers.pop(sock, None)
        sock.deleteLater()

    def _on_ready_read(self, sock):
        if sock not in self._buffers:
            return
        self._buffers[sock] += bytes(sock.readAll())
        while b"\n" in self._buffers[sock]:
            line, _sep, rest = self._buffers[sock].partition(b"\n")
            self._buffers[sock] = bytearray(rest)
            self._handle_command(sock, line.decode("utf-8", errors="replace").strip())

    def _handle_command(self, sock, line):
        if not line:
            return
        long_form = line.startswith("\\")
        body = line[1:] if long_form else line
        cmd = body.split(None, 1)[0] if long_form else body[:1]
        args_text = body[len(cmd):].strip() if long_form else body[1:].strip()
        args = args_text.split()

        try:
            if cmd in ("f", "get_freq"):
                self._respond(sock, f"{int(self._get_freq())}\n")
            elif cmd in ("F", "set_freq"):
                self._set_freq(float(args[0]))
                self._respond(sock, "RPRT 0\n")
            elif cmd in ("m", "get_mode"):
                mode, passband = self._get_mode()
                self._respond(sock, f"{mode}\n{passband}\n")
            elif cmd in ("M", "set_mode"):
                self._set_mode(args[0])
                self._respond(sock, "RPRT 0\n")
            elif cmd in ("t", "get_ptt"):
                self._respond(sock, f"{1 if self._get_ptt() else 0}\n")
            elif cmd in ("T", "set_ptt"):
                self._set_ptt(args[0] not in ("0", ""))
                self._respond(sock, "RPRT 0\n")
            elif cmd in ("v", "get_vfo"):
                self._respond(sock, "VFOA\n")
            elif cmd in ("V", "set_vfo"):
                self._respond(sock, "RPRT 0\n")  # accepted, no-op -- single-VFO from rigctld's perspective
            elif cmd == "chk_vfo":
                self._respond(sock, "CHKVFO 0\n")
            elif cmd == "dump_state":
                self._respond(sock, self._dump_state_response())
            else:
                self._respond(sock, "RPRT -11\n")  # -RIG_ENAVAIL: not implemented
        except Exception:
            self._respond(sock, "RPRT -1\n")  # -RIG_EINVAL: malformed args or callback failure

    @staticmethod
    def _respond(sock, text):
        sock.write(text.encode("utf-8"))

    @staticmethod
    def _dump_state_response():
        """Field sequence traced directly from Hamlib's own netrigctl_open()
        source (rigs/dummy/netrigctl.c) -- this is what WSJT-X's "Hamlib
        NET rigctl" backend actually parses, line by line, in this exact
        order:
          1. protocol version (int) -- 0 here
          2. one line that netrigctl_open() reads but never uses for
             anything (confirmed in the source: a second read_string()
             call whose result is simply overwritten by the next one)
          3. ITU region (int)
          4+. rx_range_list entries: "startf endf modes(hex) low_power
             high_power vfo(hex) ant(hex)", terminated by an all-zero line
          N+. tx_range_list entries, same shape, same all-zero terminator
          N+. tuning_steps entries: "modes(hex) step_hz", terminated by a
             "0 0" line
          N+. filters entries: "modes(hex) width_hz" (width must be
             nonzero or it reads as the terminator), terminated by "0 0"
          then six more single-value lines in this exact order: max_rit,
          max_xit, max_ifshift, announces, preamp list, attenuator list,
          has_get_func, has_set_func, has_get_level, has_set_level,
          has_get_parm, has_set_parm.

        Critically: since protocol version is 0, netrigctl_open() returns
        successfully immediately after has_set_parm and reads NOTHING
        further -- any extra trailing lines would sit unread in the
        socket and corrupt the next command's response. An earlier
        version of this method got this wrong two ways: it omitted the
        ITU region line entirely (shifting every field after it by one
        position) and included four extra trailing lines that were never
        supposed to be sent for protocol version 0.

        One thing NOT directly confirmed from the source (it's inferred
        from long-established, widely-documented Hamlib convention): the
        exact end-of-list sentinel checks (RIG_IS_FRNG_END/_TS_END/
        _FLT_END) -- believed to be startf==0&&endf==0 for ranges, step==0
        for tuning steps, and width==0 for filters, which is why the
        filter entry below uses a nonzero width."""
        return (
            "0\n"                                                             # protocol version
            "2\n"                                                             # unused line (confirmed discarded by the client)
            "0\n"                                                             # ITU region
            "150000.000000 1500000000.000000 0x1ff -1 -1 0x10000003 0x3\n"    # rx range
            "0 0 0 0 0 0 0\n"                                                 # rx range list terminator
            "150000.000000 1500000000.000000 0x1ff -1 -1 0x10000003 0x3\n"    # tx range
            "0 0 0 0 0 0 0\n"                                                 # tx range list terminator
            "0x1ff 1\n"                                                       # tuning step
            "0 0\n"                                                           # tuning step list terminator
            "0x1ff 2400\n"                                                    # filter (2400 Hz -- nonzero, so not read as the terminator)
            "0 0\n"                                                           # filter list terminator
            "0\n"                                                             # max_rit
            "0\n"                                                             # max_xit
            "0\n"                                                             # max_ifshift
            "0\n"                                                             # announces
            "0\n"                                                             # preamp list
            "0\n"                                                             # attenuator list
            "0\n"                                                             # has_get_func
            "0\n"                                                             # has_set_func
            "0\n"                                                             # has_get_level
            "0\n"                                                             # has_set_level
            "0\n"                                                             # has_get_parm
            "0\n"                                                             # has_set_parm -- last line read when protocol version is 0
        )



class RadioWindow(QWidget):
    def __init__(self, details):
        super().__init__()
        self._details = details
        if details["connection_type"] == "network":
            self._connection_label = f"{details['host']} (LAN)"
        else:
            self._connection_label = f"{details['serial_port']} (USB)"
        self.setWindowTitle(f"Icom Radio Control -- {details['radio_model']}")

        self.freq_display = QLabel("-- MHz")
        self.freq_display.setStyleSheet(
            "font-size: 28px; font-weight: bold; color: white; "
            # Matches SpectrumWidget's own background fill (QColor(10, 10,
            # 20)) exactly, rather than a semi-transparent black, since
            # this label sits directly on top of that scope.
            "background-color: rgb(10, 10, 20); padding: 4px 10px; border-radius: 4px;"
        )
        self.freq_display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # Fixed width sized for the longest realistic reading ("999.999999
        # MHz") so the overlay box doesn't resize/jump as digits change --
        # only its position (top-right of the scope) needs recalculating,
        # and only on resize, not on every frequency update. 230px was too
        # narrow at this font size/padding -- right-aligned text overflowing
        # a too-narrow fixed-width label clips off the LEFT edge (the
        # visible symptom), so this needs headroom rather than an exact fit.
        self.freq_display.setFixedWidth(280)

        # Three independently double-click-switchable meters side by side,
        # like a radio's multi-function meter that can show several
        # readings at once. Different defaults so they're not all showing
        # S-Meter out of the box; each is free to be switched to any of
        # METER_DEFINITIONS regardless of what the others show.
        self.meter_widget = MeterWidget(meter_type="s_meter")
        self.meter_widget_2 = MeterWidget(meter_type="power")
        self.meter_widget_3 = MeterWidget(meter_type="swr")
        self.meter_widgets = [self.meter_widget, self.meter_widget_2, self.meter_widget_3]
        self.meters_row = QHBoxLayout()
        for widget in self.meter_widgets:
            self.meters_row.addWidget(widget)

        # One button per band this radio supports, rather than a dropdown --
        # built now since the radio model (and therefore band list) is
        # already known from the connection dialog, before the radio itself
        # connects. Buttons stay disabled until _on_connected() enables them.
        self.band_buttons = []
        self.band_button_ranges = []  # parallel list: (button, low_hz, high_hz), for highlighting the active band
        self.band_buttons_row = QHBoxLayout()
        for label, low_hz, high_hz in RADIO_BANDS.get(details["radio_model"], []):
            button = QPushButton(label)
            button.setEnabled(False)
            button.setToolTip(f"{low_hz / 1e6:.3f}\u2013{high_hz / 1e6:.3f} MHz")
            button.clicked.connect(
                lambda checked=False, lbl=label, f=low_hz: self._on_band_selected(lbl, f)
            )
            self.band_buttons.append(button)
            self.band_button_ranges.append((button, low_hz, high_hz))
            self.band_buttons_row.addWidget(button)

        self.spectrum_widget = SpectrumWidget()
        self.spectrum_widget.set_overlay_widget(self.freq_display)
        self.waterfall_widget = WaterfallWidget()

        self.tuning_knob = TuningKnobWidget()
        self.tuning_knob.setEnabled(False)
        self.tuning_knob.steps_changed.connect(self._on_knob_steps)

        self.step_combo = QComboBox()
        for label, step_hz in TUNING_STEPS:
            self.step_combo.addItem(label, step_hz)
        self.step_combo.setCurrentIndex(2)  # default to 1 kHz steps

        self._current_freq_hz = None  # last known frequency, used as the knob's baseline

        self.ptt_button = QPushButton("PTT")
        self.ptt_button.setFixedHeight(40)
        self.ptt_button.setCheckable(True)
        self.ptt_button.setStyleSheet(
            "QPushButton { background-color: #a33; color: white; font-weight: bold; border-radius: 4px; }"
            "QPushButton:checked { background-color: #f22; }"
            "QPushButton:disabled { background-color: #555; color: #999; }"
        )
        self.ptt_button.setEnabled(False)
        self.ptt_button.setToolTip("Click to transmit, click again to release")
        self.ptt_button.toggled.connect(self._on_ptt_toggled)

        self.wsjtx_button = QPushButton("Launch WSJT-X")
        self.wsjtx_button.setToolTip(
            f"Launches WSJT-X in its own isolated profile (--rig-name={WSJTX_RIG_NAME}), "
            "separate from your main WSJT-X settings. Use the Rigctld button below to "
            "let it control this radio."
        )
        self.wsjtx_button.clicked.connect(self._on_wsjtx_button_clicked)

        self.rigctld_port_input = QSpinBox()
        self.rigctld_port_input.setRange(1, 65535)
        self.rigctld_port_input.setValue(RIGCTLD_DEFAULT_PORT)
        self.rigctld_port_input.setToolTip("TCP port for the rigctld server to listen on.")

        self.rigctld_button = QPushButton(f"Rigctld: OFF (port {RIGCTLD_DEFAULT_PORT})")
        self.rigctld_button.setCheckable(True)
        self.rigctld_button.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.rigctld_button.setToolTip(
            "Lets other CAT-aware apps (WSJT-X, JTDX, fldigi, ...) control this "
            "radio through this app's connection -- select \"Hamlib NET rigctl\" "
            "(rig model 2) and 127.0.0.1:<port above> in that app."
        )
        self.rigctld_button.toggled.connect(self._on_rigctld_toggled)
        self.rigctld_server = None  # created lazily on first enable

        self.virtual_cable_button = QPushButton("Virtual Cables: OFF")
        self.virtual_cable_button.setCheckable(True)
        self.virtual_cable_button.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.virtual_cable_button.setEnabled(False)
        self.virtual_cable_button.setToolTip(
            "Creates two virtual audio devices (Linux/PulseAudio or PipeWire "
            "only) so an external app (WSJT-X, etc.) can send/receive audio "
            "through this app's radio connection, in place of the physical "
            "devices chosen in the connection dialog. Click again to switch "
            "back to those original devices."
        )
        self.virtual_cable_button.toggled.connect(self._on_virtual_cable_toggled)

        self.status_label = QLabel("Connecting...")

        # AF Gain/Squelch/Monitor/TX Level/RF Level -- built generically
        # from LEVEL_DEFINITIONS, all disabled until _on_connected().
        self.level_sliders = {}
        self.level_labels = {}
        levels_row = QVBoxLayout()
        for key, definition in LEVEL_DEFINITIONS.items():
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 100)
            slider.setEnabled(False)
            slider.valueChanged.connect(lambda value, k=key: self._on_level_changed(k, value))
            label = QLabel(f"{definition['label']}: --")
            row = QHBoxLayout()
            row.addWidget(label)
            row.addWidget(slider)
            levels_row.addLayout(row)
            self.level_sliders[key] = slider
            self.level_labels[key] = label

        # Mode/Digital/NR/NB/AGC/Preamp/Filter/VFO -- built generically
        # from CONTROL_DEFINITIONS: a combo box ("combo" type), a
        # checkable toggle button ("toggle" type), or a plain click-to-
        # swap button ("vfo_toggle" type). All disabled until
        # _on_connected() enables them.
        self.control_widgets = {}
        controls_row = QHBoxLayout()
        vfo_toggle_column = None  # lazily created; all vfo_toggle-type buttons stack in this one column
        for key, definition in CONTROL_DEFINITIONS.items():
            if definition["type"] == "combo":
                widget = QComboBox()
                excluded_labels = CONTROL_OPTION_EXCLUDED.get((details["radio_model"], key), set())
                for option_label, option_value in definition["options"]:
                    if option_label in excluded_labels:
                        continue
                    widget.addItem(option_label, option_value)
                widget.setEnabled(False)
                widget.currentIndexChanged.connect(
                    lambda index, k=key, w=widget: self._on_control_combo_changed(k, w)
                )
                label = QLabel(definition["label"])
                col = QVBoxLayout()
                col.addWidget(label, alignment=Qt.AlignHCenter)
                col.addWidget(widget)
                # controls_row gets stretched to match the much taller knob
                # column next to it in tuning_row -- without this stretch,
                # Qt distributes that extra height by pushing the combo
                # down away from its label instead of leaving it flush.
                col.addStretch()
                controls_row.addLayout(col)
                controls_row.setAlignment(col, Qt.AlignTop)
            elif definition["type"] == "vfo_toggle":
                # A single button showing the current state, like the real
                # A/B or V/M button -- click swaps to the other option,
                # rather than a dropdown to pick from. Starts on the first
                # option's label; _on_control_updated() corrects it to
                # the radio's actual state once connected (except for
                # write_only entries, which never get corrected -- see
                # the tooltip below). All buttons of this type share one
                # vertical column (e.g. VFO A/B with VFO/MEM stacked
                # directly under it), rather than each getting its own
                # flat slot in the row.
                widget = QPushButton(definition["options"][0][0])
                widget.setEnabled(False)
                if definition.get("write_only"):
                    widget.setToolTip(
                        "This radio can't report its current state for this control "
                        "(CI-V read-only limitation) -- this button shows the last "
                        "command sent, not a live-confirmed value."
                    )
                widget.clicked.connect(lambda checked=False, k=key: self._on_vfo_toggle_clicked(k))
                if vfo_toggle_column is None:
                    vfo_toggle_column = QVBoxLayout()
                    controls_row.addLayout(vfo_toggle_column)
                    controls_row.setAlignment(vfo_toggle_column, Qt.AlignTop)
                vfo_toggle_column.addWidget(widget)
            else:  # "toggle"
                widget = QPushButton(definition["label"])
                widget.setCheckable(True)
                widget.setEnabled(False)
                widget.setStyleSheet(
                    "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
                )
                widget.toggled.connect(lambda checked, k=key: self._on_control_toggled(k, checked))
                controls_row.addWidget(widget)
                controls_row.setAlignment(widget, Qt.AlignTop)
            self.control_widgets[key] = widget

        # Extra action buttons (WSJT-X, Rigctld, and whatever gets added
        # later) -- a horizontal row of its own, kept as an attribute so
        # future buttons can just be appended to it directly.
        rigctld_column = QVBoxLayout()
        rigctld_column.addWidget(self.rigctld_port_input)
        rigctld_column.addWidget(self.rigctld_button)

        self.extra_buttons_row = QHBoxLayout()
        self.extra_buttons_row.addWidget(self.wsjtx_button)
        self.extra_buttons_row.addLayout(rigctld_column)
        self.extra_buttons_row.addWidget(self.virtual_cable_button)
        self.extra_buttons_row.addStretch()

        # controls_row (Mode/Digital/NR/NB/AGC/Preamp/Filter/VFO) and
        # extra_buttons_row stack vertically together, to the left of the
        # tuning knob -- not mixed into knob_row, which is the knob's own
        # column on the right.
        left_column = QVBoxLayout()
        left_column.addLayout(controls_row)
        left_column.addLayout(self.extra_buttons_row)

        knob_row = QVBoxLayout()
        knob_row.addWidget(self.tuning_knob, alignment=Qt.AlignHCenter)
        knob_row.addWidget(self.step_combo, alignment=Qt.AlignHCenter)
        knob_row.addWidget(self.ptt_button)

        tuning_row = QHBoxLayout()
        tuning_row.addLayout(left_column)
        tuning_row.addStretch()
        tuning_row.addLayout(knob_row)

        layout = QVBoxLayout()
        layout.addWidget(self.spectrum_widget)
        layout.addWidget(self.waterfall_widget)
        layout.addLayout(self.meters_row)
        layout.addLayout(self.band_buttons_row)
        layout.addLayout(tuning_row)
        layout.addLayout(levels_row)
        layout.addWidget(self.status_label)
        self.setLayout(layout)

        self.worker = RadioWorker(details)
        self.worker.connected.connect(self._on_connected)
        self.worker.connection_failed.connect(self._on_connection_failed)
        self.worker.frequency_updated.connect(self._on_frequency_updated)
        self.worker.meter_updated.connect(self._on_meter_updated)
        self.worker.scope_frame_received.connect(self._on_scope_frame)
        self.worker.error.connect(self._on_error)
        self.worker.audio_status.connect(self._on_audio_status)
        self.worker.level_updated.connect(self._on_level_updated)
        self.worker.control_updated.connect(self._on_control_updated)
        self.worker.start()

    @Slot()
    def _on_connected(self):
        self.status_label.setText(f"Connected to {self._connection_label}")
        self.tuning_knob.setEnabled(True)
        self.ptt_button.setEnabled(True)
        for button in self.band_buttons:
            button.setEnabled(True)
        for slider in self.level_sliders.values():
            slider.setEnabled(True)
        for widget in self.control_widgets.values():
            widget.setEnabled(True)
        self.virtual_cable_button.setEnabled(True)

    @Slot(str)
    def _on_connection_failed(self, message):
        self.status_label.setText("Connection failed")
        QMessageBox.critical(self, "Connection Error", message)

    @Slot(int)
    def _on_frequency_updated(self, freq_hz):
        self._current_freq_hz = freq_hz
        self.freq_display.setText(f"{freq_hz / 1e6:.6f} MHz")
        self._update_band_button_highlight()

    @Slot(str, int)
    def _on_meter_updated(self, meter_type, level):
        # Each meter widget independently filters for whichever type it's
        # currently showing -- guards against a stale reading landing
        # just after the user double-clicked to a different meter type
        # mid-poll-cycle, same as before, just applied per-widget now.
        for widget in self.meter_widgets:
            if widget.meter_type == meter_type:
                widget.set_value(level)

    @Slot(object)
    def _on_scope_frame(self, frame):
        self.spectrum_widget.set_frame(frame)
        self.waterfall_widget.set_frame(frame)

    @Slot(str)
    def _on_error(self, message):
        print(f"[ERROR] {message}", file=sys.stderr)

    @Slot(str)
    def _on_audio_status(self, message):
        print(f"[AUDIO] {message}")

    @Slot(str, float)
    def _on_level_updated(self, key, value):
        slider = self.level_sliders.get(key)
        label = self.level_labels.get(key)
        if slider is None:
            return
        percent = round(value * 100)
        # Block signals while reflecting the radio's actual value so this
        # doesn't immediately fire _on_level_changed and write it straight
        # back to the radio.
        if slider.value() != percent:
            slider.blockSignals(True)
            slider.setValue(percent)
            slider.blockSignals(False)
        label.setText(f"{LEVEL_DEFINITIONS[key]['label']}: {percent}%")

    def _on_level_changed(self, key, value):
        self.level_labels[key].setText(f"{LEVEL_DEFINITIONS[key]['label']}: {value}%")
        self.worker.set_level_value(key, value / 100.0)

    def _on_control_combo_changed(self, key, widget):
        value = widget.currentData()
        self.worker.set_control_value(key, value)

    def _on_control_toggled(self, key, checked):
        self.worker.set_control_value(key, checked)

    def _on_ptt_toggled(self, checked):
        self.ptt_button.setText("TRANSMITTING" if checked else "PTT")
        if checked:
            self.worker.start_ptt()
        else:
            self.worker.stop_ptt()

    def _on_wsjtx_button_clicked(self):
        settings = QSettings("IcomRadioApp", "RadioControl")
        path = settings.value("wsjtx_executable_path", "")

        if not path or not os.path.isfile(path):
            path = find_wsjtx_executable()

        if not path:
            path, _filter = QFileDialog.getOpenFileName(
                self, "Locate the WSJT-X executable",
                "", "Executable (*.exe);;All files (*)" if platform.system() == "Windows" else "All files (*)",
            )
            if not path:
                return  # user cancelled the browse dialog

        try:
            launch_wsjtx(path)
        except OSError as exc:
            QMessageBox.critical(self, "WSJT-X", f"Couldn't launch WSJT-X at:\n{path}\n\n{exc}")
            return

        # Only remember the path once it's actually confirmed to work
        # (subprocess.Popen not raising means the OS accepted it).
        settings.setValue("wsjtx_executable_path", path)

    def _on_rigctld_toggled(self, checked):
        if checked:
            port = self.rigctld_port_input.value()
            self.rigctld_server = RigctldServer(
                get_freq=lambda: self._current_freq_hz or 0,
                set_freq=lambda hz: self.worker.set_frequency(int(hz)),
                get_mode=lambda: (self.control_widgets["mode"].currentData() or "USB", 0),
                set_mode=lambda mode: self.worker.set_control_value("mode", mode),
                get_ptt=lambda: self.ptt_button.isChecked(),
                set_ptt=lambda on: self.ptt_button.setChecked(on),  # reuses the normal PTT path via its toggled signal
                port=port,
            )
            try:
                self.rigctld_server.start()
            except RuntimeError as exc:
                QMessageBox.critical(self, "Rigctld", str(exc))
                self.rigctld_server = None
                self.rigctld_button.setChecked(False)
                return
            self.rigctld_button.setText(f"Rigctld: ON (port {port})")
            self.rigctld_port_input.setEnabled(False)  # changing port on a live server needs a stop/restart first
        else:
            port = self.rigctld_port_input.value()
            if self.rigctld_server is not None:
                self.rigctld_server.stop()
                self.rigctld_server = None
            self.rigctld_button.setText(f"Rigctld: OFF (port {port})")
            self.rigctld_port_input.setEnabled(True)

    def _on_virtual_cable_toggled(self, checked):
        if checked:
            self.virtual_cable_button.setText("Virtual Cables: ON")
            self.worker.enable_virtual_cables()
        else:
            self.virtual_cable_button.setText("Virtual Cables: OFF")
            self.worker.disable_virtual_cables()

    def _on_vfo_toggle_clicked(self, key):
        definition = CONTROL_DEFINITIONS[key]
        options = definition["options"]
        current_label = self.control_widgets[key].text()
        labels = [label for label, _value in options]
        try:
            index = labels.index(current_label)
        except ValueError:
            index = 0
        # Swap to the OTHER option (cycles through all if there were more
        # than two, but VFO A/B only ever has two).
        target_label, target_value = options[(index + 1) % len(options)]
        self.worker.set_control_value(key, target_value)

    @Slot(str, object)
    def _on_control_updated(self, key, value):
        widget = self.control_widgets.get(key)
        if widget is None:
            return
        definition = CONTROL_DEFINITIONS[key]
        # blockSignals while reflecting the radio's actual state so this
        # doesn't immediately fire _on_control_combo_changed/_on_control_
        # toggled and write the value straight back to the radio.
        if definition["type"] == "combo":
            index = widget.findData(value)
            if index != -1 and widget.currentIndex() != index:
                widget.blockSignals(True)
                widget.setCurrentIndex(index)
                widget.blockSignals(False)
        elif definition["type"] == "vfo_toggle":
            label = next((lbl for lbl, val in definition["options"] if val == value), None)
            if label and widget.text() != label:
                widget.setText(label)
        else:  # "toggle"
            checked = bool(value)
            if widget.isChecked() != checked:
                widget.blockSignals(True)
                widget.setChecked(checked)
                widget.blockSignals(False)

    def _on_band_selected(self, band_label, low_edge_hz):
        self.worker.select_band(band_label, low_edge_hz)
        # Update optimistically using the band's low edge -- if the IC-7300
        # band-stacking register recalls a different frequency within the
        # band, the next poll cycle corrects this to the real value.
        self._current_freq_hz = low_edge_hz
        self.freq_display.setText(f"{low_edge_hz / 1e6:.6f} MHz")
        self._update_band_button_highlight()

    def _on_knob_steps(self, steps):
        if self._current_freq_hz is None:
            return  # haven't heard a frequency from the radio yet
        step_hz = self.step_combo.currentData()
        new_freq_hz = max(0, self._current_freq_hz + steps * step_hz)
        self.worker.set_frequency(new_freq_hz)
        # Update optimistically so the readout feels responsive while
        # spinning the knob; the next poll cycle will confirm/correct it.
        self._current_freq_hz = new_freq_hz
        self.freq_display.setText(f"{new_freq_hz / 1e6:.6f} MHz")
        self._update_band_button_highlight()

    def _update_band_button_highlight(self):
        """Turns the button for whichever band the current frequency
        falls in green, matching the toggle-button convention used
        elsewhere in this app -- and clears it for every other band."""
        freq = self._current_freq_hz
        for button, low_hz, high_hz in self.band_button_ranges:
            active = freq is not None and low_hz <= freq <= high_hz
            button.setStyleSheet(
                "QPushButton { background-color: #2a6; color: white; font-weight: bold; }" if active else ""
            )

    def closeEvent(self, event):
        if self.rigctld_server is not None:
            self.rigctld_server.stop()
        if self.virtual_cable_button.isChecked():
            self.worker.disable_virtual_cables()  # best-effort; worker.stop() below may cut this off before it finishes
        self.worker.stop()
        self.worker.wait(2000)  # give the polling loop a moment to exit cleanly
        event.accept()


def apply_dark_theme(app):
    """Applies an overall dark theme to the whole app -- the Fusion style
    plus a matching QPalette, which QDialog/QWidget/etc all pick up
    automatically (so this covers the connection dialog too, not just the
    main window). The custom-painted widgets (scope, waterfall, meters,
    tuning knob) already draw their own dark backgrounds regardless of
    this; this is specifically for the standard Qt widgets -- buttons,
    labels, combo boxes, sliders, dialogs -- that would otherwise use
    whatever light/native theme the OS provides and look mismatched next
    to those."""
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(45, 45, 48))
    palette.setColor(QPalette.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.Base, QColor(30, 30, 32))
    palette.setColor(QPalette.AlternateBase, QColor(45, 45, 48))
    palette.setColor(QPalette.ToolTipBase, QColor(45, 45, 48))
    palette.setColor(QPalette.ToolTipText, QColor(220, 220, 220))
    palette.setColor(QPalette.Text, QColor(220, 220, 220))
    palette.setColor(QPalette.Button, QColor(55, 55, 58))
    palette.setColor(QPalette.ButtonText, QColor(220, 220, 220))
    palette.setColor(QPalette.BrightText, QColor(255, 80, 80))
    palette.setColor(QPalette.Link, QColor(90, 160, 255))
    palette.setColor(QPalette.Highlight, QColor(60, 120, 200))
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))

    # Disabled states need their own explicit entries -- otherwise
    # disabled widgets (very common in this app before a radio connects)
    # can end up nearly invisible against a dark background.
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(120, 120, 120))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(120, 120, 120))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(120, 120, 120))
    palette.setColor(QPalette.Disabled, QPalette.Base, QColor(40, 40, 42))

    app.setPalette(palette)

    # A couple of things Fusion+QPalette alone doesn't fully cover.
    app.setStyleSheet("""
        QToolTip {
            color: #dcdcdc;
            background-color: #2d2d30;
            border: 1px solid #555555;
        }
    """)


def main():
    app = QApplication(sys.argv)
    apply_dark_theme(app)

    details = ConnectionDialog.get_details()
    if details is None:
        sys.exit(0)  # user cancelled -- exit quietly, nothing to clean up yet

    window = RadioWindow(details)
    window.resize(700, 620)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()