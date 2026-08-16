"""
Pure data/configuration constants for the Icom radio control app --
radio profiles, band tables, meter/control/level definitions, and audio
format constants. No dependencies on Qt, rigplane, or any other part of
this app, so every other module can safely import from here without risk
of circular imports.
"""

RADIO_PROFILES = {
    "IC-7300": {"addr_hex": "94", "default_connection": "usb"},
    "IC-9700": {"addr_hex": "A2", "default_connection": "network"},
    "IC-705": {"addr_hex": "A4", "default_connection": "network"},
}

# Which role a connected radio plays in satellite Doppler control, chosen
# per-connection in ConnectionDialog. "full_duplex" is a single dual-
# receiver radio (e.g. IC-9700) doing both uplink and downlink itself,
# exactly like this app's original single-radio design. "downlink"/
# "uplink" are for a "poor man's full duplex" pair of separate radios --
# one continuously Doppler-tuned to the downlink (never transmits), one
# PTT-driven onto the Doppler-tuned uplink (never continuously retuned
# outside of a transmission). "non_sat" opts a radio out of satellite
# tracking entirely -- a plain, standalone connection. See
# satellite_session.py for how each role's tick/PTT behavior differs.
RADIO_ROLES = [
    ("Satellite Full Duplex", "full_duplex"),
    ("Satellite Downlink", "downlink"),
    ("Satellite Uplink", "uplink"),
    ("Non-Sat", "non_sat"),
]

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

# Approximate occupied-bandwidth passband for the scope's mode overlay
# (widgets.py's SpectrumWidget) -- (hz_below_tuned_freq, hz_above_tuned_freq)
# per mode, matching the confirmed CONTROL_DEFINITIONS["mode"]["options"]
# values below. NOT read from the radio (there's no live "actual filter
# bandwidth in Hz" value available -- the "filter" control is a bare
# FIL1/2/3 slot number, not a Hz figure), so these are reasonable typical
# defaults for ham VHF/UHF/HF operation, not a precise per-radio reading.
# LSB/USB are asymmetric (passband is entirely below/above the tuned
# frequency, respectively, matching real SSB); everything else is
# centered on it.
MODE_BANDWIDTH_HZ = {
    "LSB": (2400, 0),
    "USB": (0, 2400),
    "AM": (3000, 3000),
    "CW": (250, 250),
    "CW_R": (250, 250),
    "RTTY": (350, 350),
    "RTTY_R": (350, 350),
    "FM": (6000, 6000),
    "WFM": (90000, 90000),
    "DV": (3000, 3000),
}

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
# slider loop). af_gain/squelch/rf_level's single candidates are
# confirmed, by reading rigplane's own source
# (core/radio_protocol.py's LevelsCapable Protocol), to be guaranteed
# present on any radio object that passes _setup_levels()'s own
# isinstance(self.radio, LevelsCapable) gate -- no need to keep
# candidate-list guessing (including af_gain's original get_af_gain/
# set_af_gain guess, confirmed by that same protocol to not exist at
# all) for names the protocol itself already guarantees. monitor/
# tx_level aren't part of that (or any other) formal rigplane
# Protocol, so those still probe multiple plausible names via
# find_method_name, same as before.
LEVEL_DEFINITIONS = {
    "af_gain": {
        "label": "AF Gain",
        "getter_candidates": ["get_af_level"],
        "setter_candidates": ["set_af_level"],
    },
    "squelch": {
        "label": "Squelch",
        "getter_candidates": ["get_squelch"],
        "setter_candidates": ["set_squelch"],
    },
    "monitor": {
        "label": "Monitor",
        # MONI: lets you hear your own transmitted audio through the
        # speaker, for checking audio quality/compression while
        # transmitting. get_monitor_gain/set_monitor_gain (the actual
        # 0-255 level) confirmed present via a dir(radio) scan -- no
        # get_monitor/set_monitor fallback: reading rigplane's own
        # source (core/radio_protocol.py) confirms those are a
        # DIFFERENT, boolean on/off toggle for the feature itself, not
        # a level at all, so falling back to them here would silently
        # misinterpret a True/False as a 0-255 gain value on any radio
        # lacking get_monitor_gain specifically.
        "getter_candidates": ["get_monitor_gain"],
        "setter_candidates": ["set_monitor_gain"],
    },
    "tx_level": {
        "label": "TX Level",
        # get_power/set_power confirmed (via the Power Output meter fix
        # earlier) to be the TX power OUTPUT LEVEL control itself --
        # NOT a meter (that's the separate get_power_meter, used for the
        # Power Output meter). Not part of any formal rigplane Protocol
        # (confirmed by reading core/radio_protocol.py), so this stays a
        # single best-known name rather than a guaranteed one. Same raw-
        # int/float scale ambiguity _call_level_setter already handles
        # for AF gain/squelch applies here too.
        "getter_candidates": ["get_power"],
        "setter_candidates": ["set_power"],
    },
    "rf_level": {
        "label": "RF Level",
        # RF Gain: receiver sensitivity/gain control (distinct from RF
        # power output above).
        "getter_candidates": ["get_rf_gain"],
        "setter_candidates": ["set_rf_gain"],
    },
}

# Which LEVEL_DEFINITIONS keys are genuinely independent per receiver on
# a dual-receiver radio. Their real getter/setter DOES accept a
# receiver= kwarg (confirmed via a full inspect.signature() sweep
# against rigplane's IcomRadio, same discovery that found "vfo"/"mode"
# needed it too -- see radio_worker.py's _receiver_kwargs) -- but
# confirmed live on a real 9700 that these three specifically don't
# actually respect it at all: AF gain/squelch/RF gain just follow
# whichever receiver is active (rigplane's select_receiver(), CI-V
# main_select/sub_select) regardless of what receiver= is passed. So
# there's only one slider per key (main_window.py), not a separate
# Main/Sub pair -- its label gets a live "(Sub)" suffix whenever the
# active receiver isn't Main, via active_receiver_button, so it's clear
# which receiver it's actually affecting at any given moment. "monitor"
# (TX sidetone level) and "tx_level" (TX power) aren't receiver RX
# settings at all -- only Main ever transmits (confirmed live: PTT
# always transmits from Main on a 9700/7610, a hardware limitation, not
# something these settings could route around either).
DUAL_RECEIVER_LEVEL_KEYS = {"af_gain", "squelch", "rf_level"}

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
        # get_alc_meter confirmed as the real name by reading rigplane's
        # own source (core/radio_protocol.py's MetersCapable Protocol:
        # "Get ALC meter reading (raw 0-255)"). _setup_meters() doesn't
        # gate on isinstance(radio, MetersCapable) though (some radios/
        # backends may expose meters outside that formal protocol), so
        # the other candidates stay as fallbacks -- just reordered so
        # the confirmed name wins first instead of risking an
        # unrelated-but-present earlier guess (find_method_name takes
        # the first match in list order).
        "getter": "get_alc_meter",
        "getter_candidates": ["get_alc_meter", "get_alc", "get_alc_level", "read_alc"],
        "kind": "linear",
        "unit": "",
        "raw_max": 255,
        "display_max": 100,
    },
    "voltage": {
        "label": "Voltage",
        # get_vd_meter confirmed as the real name (MetersCapable: "Get Vd
        # meter reading"). Reordered for the same reason as ALC above.
        "getter": "get_vd_meter",
        "getter_candidates": ["get_vd_meter", "get_voltage", "get_supply_voltage", "get_vd", "get_dc_voltage"],
        "kind": "linear",
        "unit": "V",
        "raw_max": 255,
        "display_max": 16.0,  # rough guess at supply-voltage full scale
    },
    "comp": {
        "label": "COMP",
        # Speech compressor meter -- confirmed as one of Icom's own 6
        # official TX meter parameters (PO/SWR/ALC/COMP/VD/ID, per the
        # IC-7300 manual). get_comp_meter confirmed as the real name
        # (MetersCapable: "Get compression meter reading"). Reordered
        # for the same reason as ALC above.
        "getter": "get_comp_meter",
        "getter_candidates": ["get_comp_meter", "get_comp", "get_compression"],
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
        # get_id_meter confirmed as the real name (MetersCapable: "Get
        # drive (Id) meter reading"). Reordered for the same reason as
        # ALC above.
        "getter": "get_id_meter",
        "getter_candidates": ["get_id_meter", "get_current", "get_id", "get_drain_current"],
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
        # rather than a dropdown.
        #
        # No enum_import: confirmed by reading rigplane's own source
        # (runtime/_dual_rx_runtime.py) that set_vfo_slot's real
        # signature is `(self, slot: str, receiver: int = 0)` -- plain
        # str, not an enum. There IS a VfoSlot class in rigplane (a
        # StrEnum), but it lives at
        # rigplane.core.state_pipeline_contracts, not rigplane.types --
        # an earlier guess at that path (before this app started
        # reading rigplane's source directly) always failed to import,
        # silently falling back to the plain "A"/"B" strings below on
        # every single connection. Since the setter wants a plain str
        # anyway, that fallback was already the objectively correct
        # behavior -- removed the dead import attempt instead of fixing
        # its path, rather than adding back a dependency that was never
        # actually needed.
        "type": "vfo_toggle",
        "getter_candidates": ["get_vfo_slot", "get_vfo", "get_active_vfo"],
        "setter_candidates": ["set_vfo_slot", "set_vfo", "select_vfo"],
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
    "split": {
        "label": "Split",
        # Plain bool toggle -- rigplane.SplitCapable (confirmed present:
        # `python -c "import rigplane; print(rigplane.SplitCapable)"`) is
        # a documented tier-1 protocol with get_split() -> bool and
        # set_split(bool), no enum involved. Its own docstring: "Split
        # applies to the whole transceiver, not a per-receiver toggle" --
        # confirmed rig-global (Icom CI-V 0x0F / Yaesu `ST;` under the
        # hood), so no radio-model-specific handling needed. Placed last
        # here (after "vfo"/"memory_mode") so it lands immediately after
        # the VFO A/B column in controls_row's left-to-right order --
        # same generic CONTROL_DEFINITIONS machinery as every other
        # toggle, not a hand-rolled one-off button.
        "type": "toggle",
        "getter_candidates": ["get_split"],
        "setter_candidates": ["set_split"],
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
