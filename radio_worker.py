"""
RadioWorker: a dedicated QThread that owns its own asyncio event loop and
does all actual rigplane radio I/O -- connecting, polling frequency/
meters/levels/controls, and handling commands from the GUI thread via
asyncio.run_coroutine_threadsafe(). Never touches a Qt widget directly;
only ever talks to the GUI thread by emitting signals.
"""

import asyncio
import copy
import importlib
import inspect

from PySide6.QtCore import QThread, Signal

from rigplane import create_radio, LanBackendConfig, CommandError, AudioCodec, Priority
try:
    # Confirmed by reading rigplane's own source
    # (backends/config.py's SerialBackendConfig.__init__): the real
    # signature is (device: str, baudrate: int, radio_addr: int | None,
    # ...) -- exactly what _build_config()'s serial branch below
    # already passes. (This class also exposes rx_device/tx_device for
    # rigplane's own built-in USB audio bridging, deliberately unused
    # here -- this app manages its own separate sounddevice/PortAudio
    # streams via AudioBridge instead, using whatever system audio
    # device the user picks in the connection dialog, including the
    # radio's own USB audio interface if the OS exposes it as a normal
    # soundcard.)
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
    # one install). See LEVEL_DEFINITIONS in constants.py and
    # RadioWorker._setup_levels() below, which probe the connected radio
    # object for the first matching name among several plausible ones
    # instead of betting on a single guess.
    from rigplane import LevelsCapable
except ImportError:
    LevelsCapable = None

try:
    # CAP_PBT: the profile-declared capability tag (rigplane's
    # radio.capabilities set), NOT a Protocol to isinstance-check against
    # -- DspControlCapable's get/set_pbt_inner/outer method names exist on
    # BOTH the Icom and Yaesu backends structurally, but Yaesu's just
    # unconditionally raise NotImplementedError (confirmed in
    # backends/yaesu_cat/radio.py). Only the IC-7610/IC-9700 rig profiles
    # actually declare "pbt" in their [capabilities] features list, so
    # that's the only reliable way to know Twin PBT is real on this
    # specific connected radio -- see is_pbt_capable below.
    from rigplane.core.capabilities import CAP_PBT
except ImportError:
    CAP_PBT = None

try:
    # CAP_FILTER_WIDTH: the profile-declared capability tag for
    # get_filter_width/set_filter_width (live DSP IF filter width in
    # Hz). Unlike PBT, these two ARE properly profile-gated inside
    # rigplane itself (they check profile.supports_cmd29(0x1A, 0x03)
    # before deciding whether to Command29-wrap, rather than hardcoding
    # it) -- confirmed by reading rigplane/runtime/radio.py's own
    # set_filter_width/get_filter_width, which explicitly branch on that
    # check and send a plain frame on IC-705/IC-9700. No raw-CI-V
    # workaround needed here, unlike PBT_DEFINITIONS -- see that
    # constant's own comment in constants.py for the contrast.
    from rigplane.core.capabilities import CAP_FILTER_WIDTH
except ImportError:
    CAP_FILTER_WIDTH = None

try:
    # CAP_FILTER_SHAPE: the profile-declared capability tag for SHARP/
    # SOFT filter shape. Unlike CAP_FILTER_WIDTH, get_filter_shape/
    # set_filter_shape have the SAME Command29-hardcoding bug as PBT
    # (confirmed by reading rigplane/commands/mode.py: both leaf command
    # functions hardcode command29=True unconditionally) -- so, like
    # PBT_DEFINITIONS, RadioWorker sends this via raw send_civ() rather
    # than calling those two methods. See FILTER_SHAPE_CIV_SUB's comment
    # in constants.py.
    from rigplane.core.capabilities import CAP_FILTER_SHAPE
except ImportError:
    CAP_FILTER_SHAPE = None

try:
    # Confirmed via rigplane's own type signatures: "Radio has two
    # independent receivers (e.g. IC-7610 Main/Sub)" -- the IC-9700 is
    # the other one, per its own docs. Confirmed live (a real 9700 vs.
    # 705 side by side): treating Main/Sub as if it were the same as a
    # single-receiver radio's VFO A/B split -- which is what this app
    # did before this was added -- produces a repeating switch-then-
    # revert on a 9700 but works fine on a 705, because they're genuinely
    # different mechanisms. IcomRadio.set_frequency()/get_frequency()
    # both take a `receiver` kwarg (0=Main, 1=Sub, matching rigplane's
    # own RECEIVER_MAIN/RECEIVER_SUB) regardless of whether a given radio
    # is actually dual-receiver -- isinstance-checking against this
    # protocol is what tells RadioWorker whether passing receiver=1
    # there means anything real, vs. classic single-receiver VFO A/B
    # split being the only thing that actually exists on that radio.
    from rigplane import DualReceiverCapable, RECEIVER_MAIN, RECEIVER_SUB
except ImportError:
    DualReceiverCapable = None
    RECEIVER_MAIN, RECEIVER_SUB = 0, 1

try:
    # Works around a confirmed path bug in the vendored rigplane's Yaesu
    # CAT backend (needed for the FTX-1 -- see constants.py's
    # RADIO_PROFILES entry): backends/yaesu_cat/radio.py computes its
    # default rig-profile directory as `Path(__file__).parents[4] /
    # "rigs"`, which lands one level ABOVE the actual installed
    # site-packages/rigplane/rigs/ directory (confirmed directly: that
    # computed path doesn't exist on disk at all, while `Path(rigplane.
    # __file__).parent / "rigs"` does and contains ftx1.toml). Left
    # unpatched, constructing a YaesuCatRadio with the default profile=
    # "ftx1" (i.e. any normal FTX-1 connection through this app, which
    # never passes an explicit profile path) raises RigLoadError before
    # ever reaching the radio. This patches the module's private _RIGS_DIR
    # constant to the correct location.
    #
    # DELIBERATELY fragile, same convention as torca_server.py's own
    # rigplane-internal patch: _RIGS_DIR is a private, underscore-
    # prefixed module attribute, not a stable API. If a future rigplane
    # release fixes the path itself (or renames/restructures this),
    # this either becomes a harmless no-op or fails the try/except below
    # -- caught, reported once, non-fatal either way. Direct consequence
    # of NOT applying this on a still-broken install: FTX-1 connections
    # fail immediately with a RigLoadError naming the wrong path.
    import pathlib as _pathlib
    import rigplane as _rigplane_pkg
    import rigplane.backends.yaesu_cat.radio as _yaesu_cat_radio_module

    _yaesu_cat_radio_module._RIGS_DIR = _pathlib.Path(_rigplane_pkg.__file__).parent / "rigs"
except Exception as _yaesu_rigs_dir_patch_exc:
    print(
        f"radio_worker: couldn't patch rigplane's Yaesu CAT rig-profile "
        f"directory ({_yaesu_rigs_dir_patch_exc}) -- FTX-1/other Yaesu CAT "
        f"connections may fail with a RigLoadError until this is fixed.",
    )

from constants import (
    RADIO_BANDS,
    BAND_STACKING_CODES,
    BAND_STACKING_REGISTER_LATEST,
    BAND_STACKING_EXCLUDED,
    LEVEL_DEBOUNCE_SECONDS,
    LEVEL_DEFINITIONS,
    PBT_DEFINITIONS,
    FILTER_SHAPE_CIV_SUB,
    PREAMP_CIV_SUB,
    ATT_CIV_COMMAND,
    SWR_CIV_SUB,
    SWR_CALIBRATION,
    SWR_PROTECTION_THRESHOLD,
    SCOPE_REF_CIV_SUB,
    SCOPE_REF_MIN_DB,
    SCOPE_REF_MAX_DB,
    SCOPE_REF_WIDE_RANGE_MODELS,
    POWER_CALIBRATION,
    COMP_CALIBRATION,
    VOLTAGE_CALIBRATION_IC9700,
    CURRENT_CALIBRATION_IC9700,
    METER_DEFINITIONS,
    CONTROL_DEFINITIONS,
    POLL_INTERVAL_SEC,
    SLOW_POLL_INTERVAL_SEC,
    AUDIO_DEVICE_SYSTEM_DEFAULT,
    AUDIO_TX_PCM_FRAME_BYTES,
)
from rig_discovery import find_method_name
from audio import AudioBridge
from remote_radio import RemoteWebRadio

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
        # Explicit receiver=RECEIVER_MAIN -- see RadioWorker._set_
        # frequency's own comment for why a bare call here isn't
        # reliable (it depends on Main genuinely being the radio's
        # currently-active receiver, which isn't guaranteed at this
        # point -- e.g. right after a band-conflict resolution moved
        # Sub out of the way).
        await radio.set_frequency(freq_hz, receiver=RECEIVER_MAIN)
    except Exception as exc:
        return None, f"set_frequency({freq_hz}) failed: {exc}"
    return freq_hz, None


def _bcd_encode_pbt_level(value: int) -> bytes:
    """0-255 int -> 2-byte BCD, matching the encoding rigplane's own
    (broken-for-PBT-on-a-single-receiver-radio, see PBT_DEFINITIONS'
    comment) get/set_pbt_inner/outer use internally -- reimplemented
    here, rather than importing rigplane's private _level_bcd_encode,
    so this app's raw-CI-V PBT workaround doesn't depend on rigplane's
    internal (underscore-prefixed, no compatibility guarantee) API.
    Each byte holds two BCD digits: 128 -> b'\\x01\\x28' (0,1 / 2,8)."""
    if not 0 <= value <= 255:
        raise ValueError(f"PBT level must be 0-255, got {value}")
    d = f"{value:04d}"
    return bytes([(int(d[0]) << 4) | int(d[1]), (int(d[2]) << 4) | int(d[3])])


def _bcd_decode_pbt_level(data: bytes) -> int:
    """Inverse of _bcd_encode_pbt_level."""
    d0, d1 = data[0], data[1]
    return (d0 >> 4) * 1000 + (d0 & 0x0F) * 100 + (d1 >> 4) * 10 + (d1 & 0x0F)


def _bcd_encode_byte(value: int) -> bytes:
    """0-99 int -> 1-byte BCD (high nibble = tens, low nibble = units),
    matching rigplane's own private _bcd_byte -- used for the IC-705
    Preamp/Attenuator raw-CI-V workaround (see PREAMP_CIV_SUB's comment
    in constants.py), same reimplemented-rather-than-imported reasoning
    as _bcd_encode_pbt_level."""
    if not 0 <= value <= 99:
        raise ValueError(f"Value must be 0-99, got {value}")
    return bytes([((value // 10) << 4) | (value % 10)])


def _bcd_decode_byte(data: bytes) -> int:
    """Inverse of _bcd_encode_byte."""
    b = data[0]
    return (b >> 4) * 10 + (b & 0x0F)


def _interpolate_swr(raw: int) -> float:
    """Raw 0-255 SWR meter byte -> calibrated ratio, via SWR_CALIBRATION
    (piecewise-linear, clamped at the endpoints) -- see that constant's
    own comment in constants.py for why this runs locally instead of
    through rigplane's own get_swr()/interpolate_swr()."""
    points = sorted(SWR_CALIBRATION)
    if raw <= points[0][0]:
        return points[0][1]
    if raw >= points[-1][0]:
        return points[-1][1]
    for (lo_raw, lo_val), (hi_raw, hi_val) in zip(points, points[1:]):
        if lo_raw <= raw <= hi_raw:
            frac = (raw - lo_raw) / (hi_raw - lo_raw)
            return lo_val + frac * (hi_val - lo_val)
    return points[-1][1]  # unreachable given the clamps above


def _interpolate_calibration(raw: int, calibration_points) -> float:
    """Generic version of _interpolate_swr's own piecewise-linear
    interpolation (raw byte -> real value, clamped at the endpoints),
    for meters other than SWR that also need it (Power/Comp -- see
    POWER_CALIBRATION/COMP_CALIBRATION's own comments in constants.py).
    Not used to refactor _interpolate_swr itself, to avoid touching
    already-confirmed-working code for no functional gain."""
    points = sorted(calibration_points)
    if raw <= points[0][0]:
        return points[0][1]
    if raw >= points[-1][0]:
        return points[-1][1]
    for (lo_raw, lo_val), (hi_raw, hi_val) in zip(points, points[1:]):
        if lo_raw <= raw <= hi_raw:
            frac = (raw - lo_raw) / (hi_raw - lo_raw)
            return lo_val + frac * (hi_val - lo_val)
    return points[-1][1]  # unreachable given the clamps above


def _encode_scope_ref(ref_db: float) -> bytes:
    """CI-V 0x27/0x19 scope reference level, same wire format as
    rigplane's own private _scope_ref_encode -- reimplemented locally
    WITHOUT that function's hardcoded -30.0/+10.0 range check (the
    IC-7610's own range, applied universally instead of per-model --
    see SCOPE_REF_MIN_DB/SCOPE_REF_MAX_DB's comment in constants.py for
    the full story). byte 0: high nibble = 10 dB digit, low nibble =
    1 dB digit. byte 1: high nibble = 0.1 dB digit, low nibble fixed 0.
    byte 2: 0x00 = positive, 0x01 = negative."""
    if not SCOPE_REF_MIN_DB <= ref_db <= SCOPE_REF_MAX_DB:
        raise ValueError(f"scope ref must be {SCOPE_REF_MIN_DB} to {SCOPE_REF_MAX_DB} dB, got {ref_db}")
    is_negative = ref_db < 0
    tenths = int(round(abs(ref_db) * 10))
    tens_db = tenths // 100
    ones_db = (tenths // 10) % 10
    frac_db = tenths % 10
    b0 = (tens_db << 4) | ones_db
    b1 = frac_db << 4
    sign = 0x01 if is_negative else 0x00
    return bytes([b0, b1, sign])


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
    # Emitted once _setup_meters() finishes building this connection's
    # own corrected METER_DEFINITIONS copy (dict), carrying that dict --
    # main_window.py pushes it into every MeterWidget via set_
    # definitions() so the UI actually uses the per-radio-corrected
    # scaling (e.g. the power meter's real display_max) instead of
    # quietly falling back to the unmodified module-level constant. See
    # widgets.py's MeterWidget class docstring for the bug this fixes.
    meters_ready = Signal(dict)
    control_updated = Signal(str, object)  # (control key, current value)
    scope_frame_received = Signal(object)  # rigplane.scope.ScopeFrame
    audio_status = Signal(str)           # informational, not an error
    level_updated = Signal(str, float)   # (level key, value 0.0-1.0)
    pbt_updated = Signal(str, int)       # (pbt key, raw level 0-255, 128=centered) -- see PBT_DEFINITIONS
    filter_width_updated = Signal(int)   # live DSP IF filter width in Hz, from get_filter_width() polling
    # Valid adjustable range for the CURRENT mode's filter width, or None
    # if this mode's width is fixed (can't be adjusted at all) -- a dict
    # {"min_hz", "max_hz", "segments": [(hz_min, hz_max, step_hz), ...]},
    # recomputed whenever the polled mode changes. See _refresh_filter_
    # width_range and main_window.py's _on_filter_width_range_updated.
    filter_width_range_updated = Signal(object)
    filter_shape_updated = Signal(int)   # 0=SHARP, 1=SOFT -- see FILTER_SHAPE_OPTIONS
    preamp_att_updated = Signal(int, int)  # (preamp_level 0/1/2, attenuator_db) -- IC-705 only, see is_ic705_preamp_att
    swr_protection_tripped = Signal(float)  # the SWR ratio that triggered an automatic PTT cutoff -- see SWR_PROTECTION_THRESHOLD
    active_receiver_changed = Signal(int)  # dual-receiver only: 0=MAIN, 1=SUB, from get_active_receiver() polling
    scope_span_changed = Signal(int)     # preset index 0-7, from get_scope_span() polling
    scope_ref_changed = Signal(float)    # dB, -30.0 to +10.0, from get_scope_ref() polling
    scope_speed_changed = Signal(int)    # 0=fast, 1=mid, 2=slow, from get_scope_speed() polling
    scope_ready = Signal()               # emitted once enable_scope() actually succeeds -- see is_scope_capable
    key_speed_changed = Signal(int)      # WPM, 6-48, from get_key_speed() polling
    cw_pitch_changed = Signal(int)       # Hz, 300-900, from get_cw_pitch() polling
    tuner_status_changed = Signal(int)   # 0=off, 1=on, 2=tuning, from get_tuner_status() polling
    memory_snapshot_captured = Signal(object)  # {"A": {...}, "B": {...}} or None on failure -- see capture_memory_snapshot
    error = Signal(str)

    def __init__(self, details, parent=None):
        super().__init__(parent)
        self._details = details
        self.loop = None       # asyncio event loop, created inside run()
        self._main_task = None  # the _main() asyncio Task, created inside run() -- see stop()
        self.radio = None      # rigplane Radio, set once connected
        self.is_dual_receiver = False  # set once connected -- see DualReceiverCapable import comment
        self.is_pbt_capable = False  # set once connected -- see CAP_PBT import comment
        self.is_filter_width_capable = False  # set once connected -- see CAP_FILTER_WIDTH import comment
        self._filter_width_hz = None  # live DSP filter width, polled -- see _poll_loop_slow
        self._filter_width_range_mode = None  # last mode a filter_width_range_updated was computed/emitted for -- avoids recomputing every poll cycle
        self._filter_width_debounce_task = None  # pending asyncio.Task, for set_filter_width_value()'s debounce
        self.is_filter_shape_capable = False  # set once connected -- see CAP_FILTER_SHAPE import comment
        self._filter_shape_available = False  # set in _setup_filter_shape() -- send_civ() usable for this radio
        self.is_ic705_preamp_att = False  # set once connected -- see PREAMP_CIV_SUB's comment in constants.py
        self.is_ic705_swr_workaround = False  # set once connected -- see SWR_CIV_SUB's comment in constants.py
        self.is_scope_ref_workaround = False  # set once connected -- see SCOPE_REF_WIDE_RANGE_MODELS' comment in constants.py
        self._scope_receiver = 0  # 0=MAIN, 1=SUB -- tracks set_scope_receiver() so the raw scope-ref bypass targets the right one
        self._power_is_raw_255 = False  # set in _setup_meters() -- Power needs POWER_CALIBRATION applied when True
        # SWR is only meaningful during TX -- confirmed live that
        # without this, the meter just kept showing whatever it last
        # read during the previous transmission (the radio's own SWR
        # register doesn't reset itself on RX), looking like it had
        # "frozen" instead of returning to a defined at-rest reading.
        # Set True/False by _start_ptt()/_stop_ptt() (both PTT entry
        # points funnel through these), read by _poll_loop_fast() to
        # skip polling SWR at all while not transmitting.
        self._ptt_active = False
        self._swr_reset_pending = False  # True right after PTT releases, until one reset value has been emitted
        # SWR protection -- see SWR_PROTECTION_THRESHOLD's own comment
        # in constants.py. _tuner_active gates the cutoff off during a
        # real tune sweep (naturally high SWR, not a fault) regardless
        # of whether tuning was started from this app's own Tune button
        # or the radio's front panel -- set optimistically by
        # _start_tuner() and authoritatively corrected by the tuner-
        # status polling in _poll_loop_slow either way. A plain
        # attribute, not asyncio-scheduled -- both are simple bool
        # flags read/written across the GUI/worker thread boundary,
        # safe under the GIL for this (a stale-by-one-cycle read is
        # harmless), same treatment as _ptt_active above.
        self._tuner_active = False
        self._swr_protection_enabled = True
        self.is_scope_capable = False  # set once connected, True only if enable_scope() (in _main) actually succeeds
        # Last-observed span/ref/speed, so _poll_loop_slow only emits
        # scope_*_changed when the value genuinely changes -- same
        # change-filtering as _last_observed_active_receiver above.
        self._last_observed_scope_span = None
        self._last_observed_scope_ref = None
        self._last_observed_scope_speed = None
        # Same change-filtered-polling idea, for the CW keyer's speed/
        # pitch settings (see set_key_speed/set_cw_pitch below).
        self._last_observed_key_speed = None
        self._last_observed_cw_pitch = None
        self._last_observed_tuner_status = None
        # Set by start_cw_decode(), cleared by stop_cw_decode() -- the
        # caller-supplied function _on_cw_decode_frame hands raw PCM
        # bytes to once audio starts arriving.
        self._cw_decode_callback = None
        # True while CW decode is attached via self.audio_bridge.set_
        # extra_rx_callback() (piggybacking on an already-active RX
        # stream) rather than its own direct radio.start_rx() tap --
        # see _attach_cw_decode/_detach_cw_decode.
        self._cw_decode_via_bridge = False
        # Set by start_sstv_decode()/cleared by stop_sstv_decode() --
        # structural clone of the CW decode fields above (same
        # AudioBridge-piggyback-or-direct-tap attach/detach logic, see
        # _attach_sstv_decode). AudioBridge's extra-RX-callback is a
        # single slot, so CW decode and SSTV decode can't both be
        # attached at once -- acceptable for v1, an operator isn't
        # running both simultaneously in practice.
        self._sstv_decode_callback = None
        self._sstv_decode_via_bridge = False
        # Set by start_rtty_decode()/cleared by stop_rtty_decode() --
        # structural clone of the CW/SSTV decode fields above (same
        # AudioBridge-piggyback-or-direct-tap attach/detach logic, see
        # _attach_rtty_decode). Same single-slot AudioBridge.extra_rx_
        # callback limitation -- only one of CW/SSTV/RTTY decode can be
        # attached at once.
        self._rtty_decode_callback = None
        self._rtty_decode_via_bridge = False
        # Set by start_aprs_decode()/cleared by stop_aprs_decode() --
        # structural clone of the CW/SSTV/RTTY decode fields above (same
        # AudioBridge-piggyback-or-direct-tap attach/detach logic, see
        # _attach_aprs_decode). Same single-slot AudioBridge.extra_rx_
        # callback limitation -- only one of CW/SSTV/RTTY/APRS decode can
        # be attached at once.
        self._aprs_decode_callback = None
        self._aprs_decode_via_bridge = False
        # Set by start_psk31_decode()/cleared by stop_psk31_decode() --
        # structural clone of the CW/SSTV/RTTY/APRS decode fields above
        # (same AudioBridge-piggyback-or-direct-tap attach/detach
        # logic, see _attach_psk31_decode). Same single-slot AudioBridge.
        # extra_rx_callback limitation -- only one of CW/SSTV/RTTY/APRS/
        # PSK31 decode can be attached at once.
        self._psk31_decode_callback = None
        self._psk31_decode_via_bridge = False
        # Dual-receiver only: which receiver every receiver-aware
        # getter/setter (see _receiver_kwargs) targets when none is
        # explicitly given. Starts at Main; kept in sync with the
        # radio's real active receiver by _select_receiver() (called by
        # both main_window.py's manual active_receiver_button and
        # satellite mode's automatic PTT-driven switching) -- so the
        # generic single-value controls (frequency readout, mode, etc.)
        # correctly follow along too, e.g. showing Sub's live downlink
        # while receiving and Main's live uplink while transmitting.
        self._active_receiver = RECEIVER_MAIN
        # Dual-receiver only: keys (CONTROL_DEFINITIONS/LEVEL_DEFINITIONS)
        # whose GETTER is confirmed, at runtime, to reject an explicit
        # receiver= for this radio/profile -- confirmed live on a real
        # 9700: AGC/Preamp/AF Gain/Squelch/RF Gain's own getters raise
        # CommandError ("receiver=1 is unsupported for profile IC-9700:
        # command 0x16/0x12 has no cmd29 route") for a non-Main
        # receiver, and (corrected -- see _receiver_unsupported_setters
        # right below) their SETTERS raise the exact same CommandError
        # for the exact same reason, not "silently ignore it" as
        # originally assumed here -- live logs showed set_squelch()/
        # set_rf_gain() failing outright with the identical cmd29-route
        # message, meaning those writes were never reaching the radio
        # at all while Sub was active, not just logging noise. Only
        # some commands are affected, not others (frequency/vfo_slot's
        # getters both confirmed working fine with an explicit
        # receiver=). Populated as each one is actually hit
        # (_poll_receiver_aware) rather than hand-listing them, since
        # there's no way to predict which commands a given radio
        # profile does or doesn't have this CI-V "cmd29" route for
        # without just trying it.
        self._receiver_unsupported_getters = set()
        # Write-side counterpart to the above -- see _call_receiver_aware.
        self._receiver_unsupported_setters = set()
        # Dual-receiver diagnostic: last value observed from
        # get_active_receiver() (a pure, instant read of rigplane's own
        # internal RadioState.active belief -- no extra CI-V traffic) --
        # _poll_loop_fast() emits an audio_status message (visible in the
        # console via [AUDIO], no special setup needed) every time this
        # changes, so an unexpected flip-flop is directly observable
        # instead of inferred from symptoms like the band-button
        # highlight or the frequency readout.
        self._last_observed_active_receiver = None
        self._radio_cm = None
        self._stop_requested = False
        self.audio_bridge = None  # set in _setup_audio() once connected, if applicable
        self._tx_audio_future = None  # concurrent.futures.Future for the in-flight send_tx_audio_pcm coroutine, if any -- see stop_tx_audio_send
        self._tx_stream_push_name = None  # resolved push_audio_tx_pcm/push_tx name while a start_tx_audio_stream()/stop_tx_audio_stream() session is open -- see those methods
        # Which receiver's audio the RX virtual cable carries ("mix"/
        # "main"/"sub", see audio._downmix_stereo_to_mono) -- a user
        # preference for feeding an external decoder (e.g. WSJT-X on a
        # satellite downlink) something other than the full Main+Sub
        # mix, kept separate from this app's own listening audio, which
        # always stays "mix" (see _setup_audio and
        # _set_virtual_cable_bridge's rx/tx=False branch, neither of
        # which reads this). Persisted here rather than on the
        # AudioBridge itself so the choice survives across virtual-
        # cable-bridge reconfiguration, which replaces the AudioBridge
        # instance entirely.
        self._rx_downmix_channel = "mix"
        # True while this worker's own audio bridge is pointed at
        # system-default devices for a virtual cable (RX and/or TX) --
        # see set_virtual_cable_bridge. The null-sink pair itself, and
        # PulseAudio's default sink/source, are no longer managed here
        # at all -- HamClockWindow owns that now (satellite_session.py's
        # sibling, ham_dashboard.py), since a virtual cable pairing can
        # span two DIFFERENT radios (one for RX, one for TX) and the
        # sinks must only ever be created once regardless of how many
        # radios' bridges point at them.
        self._virtual_cable_active = False
        self._meter_getters = {}  # meter_type -> resolved getter name, populated by _setup_meters()
        self._control_methods = {}  # control key -> (get_name, set_name), populated by _setup_controls()
        self._control_enums = {}    # control key -> resolved enum class, for controls needing enum_import
        self._level_methods = {}  # level key -> (get_name, set_name), populated by _setup_levels()
        # Some LevelsCapable setters want a 0.0-1.0 float; others (confirmed
        # via a runtime error on squelch) want a raw int on Icom's native
        # 0-255 CI-V level scale. Cache per level key which one worked so
        # _call_level_setter() only has to probe once per level.
        self._level_value_mode = {}
        # key -> pending asyncio.Task, for set_level_value()'s debounce.
        # A fast slider drag (or a remote connection's tuning knob) fires
        # many rapid calls for the same key; without this every single
        # intermediate value got sent as its own command, which either
        # floods a local radio with far more CI-V traffic than the drag
        # gesture itself needs, or -- confirmed live on a remote
        # connection -- hits the web server's 20/sec/command rate limit
        # and gets silently dropped, later corrected by the next real
        # poll and looking like the slider "bouncing back". See
        # set_level_value()/_debounce_level_value().
        self._level_debounce_tasks = {}
        self._pbt_methods = {}  # pbt key -> CI-V 0x14 sub-command byte, populated by _setup_pbt()
        self._pbt_debounce_tasks = {}  # same debounce idea as _level_debounce_tasks, separate dict/keyspace

    def run(self):
        """Thread entry point: create and own the asyncio loop here."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        # Dual-receiver only: serializes every coroutine that switches
        # which receiver is selected on the radio and then acts on it
        # (retune, mode write, PTT). Each such coroutine is already
        # internally atomic (see e.g. select_receiver_vfo_and_set_
        # frequency's docstring), but that guarantee only holds WITHIN
        # one coroutine -- it says nothing about two independently-
        # scheduled ones (e.g. the satellite tick's Sub retune, which
        # fires every 2s continuously for the whole tracking session,
        # and apply_satellite_mode's one-off Main mode write) racing
        # each other on the shared event loop. Mode in particular has
        # no receiver-specific CI-V write at all (confirmed by reading
        # rigplane's own runtime/radio.py: set_mode(receiver=0) is just
        # the bare "set mode on whichever receiver is CURRENTLY
        # selected" command, same "selected/unselected" semantics as
        # frequency) -- so if the Sub tick's own select_receiver(SUB)
        # lands on the radio in between this coroutine's own
        # select_receiver(MAIN) and its mode write, the write silently
        # lands on Sub instead, with Main's mode never actually
        # changing -- exactly what was reported live even after
        # removing all VFO-slot switching. This lock closes that
        # window.
        self._receiver_switch_lock = asyncio.Lock()
        # A distinct Task (not just run_until_complete(self._main())
        # directly) so stop() can cancel it from the GUI thread and
        # actually interrupt whatever _main() is doing right now --
        # confirmed necessary live: closing the window while a slow/
        # unresponsive CI-V round-trip was still in flight during
        # startup (real hardware over USB serial can take multiple
        # seconds to time out on some commands, per BAND_STACKING_
        # EXCLUDED's own docstring) left the old stop() -- which only
        # set self._stop_requested, a flag both poll loops' own while-
        # conditions check and nothing else does -- completely unable
        # to interrupt it. main_window.py's closeEvent only waits 2s
        # (worker.wait(2000)) before returning regardless, so Qt then
        # destroyed the still-running QThread out from under it:
        # "QThread: Destroyed while thread '' is still running" /
        # Aborted. Cancelling the task makes _main() unwind promptly
        # from wherever it actually is (any await point, not just
        # between poll iterations) instead.
        self._main_task = self.loop.create_task(self._main())
        try:
            self.loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass
        finally:
            self.loop.close()

    def _build_config(self, force_stereo=True):
        """force_stereo (network connections only): request a stereo RX
        codec explicitly instead of silently deferring to the radio
        profile's own codec_preference. Confirmed by reading rigs/
        ic9700.toml: rigplane pins the 9700 to mono ("no hardware has
        been available to validate stereo rx_codec negotiation; default
        to mono until proven") -- which is exactly why Sub's audio was
        never in the LAN stream at all (not a bug in this app's
        downmix, which never had anything to downmix).

        Passing audio_codec_explicit=True is the only way to bypass
        that profile default -- but doing so ALSO disarms rigplane's
        own automatic mono-retry-on-rejection safety net in
        _control_phase, which only fires for its own GLOBAL_DEFAULT/
        PROFILE_DEFAULT codec sources, not an EXPLICIT one. A radio
        that genuinely rejects a stereo conninfo request would then
        fail the WHOLE CONNECTION instead of falling back to mono.
        _main() re-implements that missing safety net at the app level
        instead: it calls this once with the default force_stereo=True,
        and if connecting with that config fails, retries once with
        force_stereo=False (the radio profile's own proven-safe
        default) before giving up for real. See the retry logic there."""
        d = self._details
        # model=d["radio_model"] matters a lot more than it looks: without
        # it, rigplane's backend factory defaults an unset serial model to
        # "IC-7610" (backends/factory.py's own fallback), which silently
        # routed EVERY serial connection -- IC-7300, IC-9700, IC-705 alike
        # -- through Icom7610SerialRadio's transport class regardless of
        # which radio was actually selected here (confirmed directly:
        # constructing SerialBackendConfig the way this method used to,
        # with no model=, always produced an Icom7610SerialRadio). Existing
        # Icom radios mostly got away with it because the real per-radio
        # command set is resolved from the CI-V address, not the transport
        # class -- but it silently substituted IC-7610-specific behavior
        # (its own audio-teardown/scope-guardrail overrides) for other
        # radios, and it's fatal for a genuinely different protocol family:
        # a Yaesu CAT radio (FTX-1) routed through the Icom-only factory
        # branch would speak the wrong wire protocol entirely. Passing the
        # real model here fixes both.
        if d["connection_type"] == "network":
            kwargs = dict(
                host=d["host"],
                port=d["port"],
                radio_addr=d["addr"],
                username=d["username"],
                password=d["password"],
                model=d.get("radio_model"),
            )
            if force_stereo:
                kwargs["audio_codec"] = AudioCodec.PCM_2CH_16BIT
                kwargs["audio_codec_explicit"] = True
            return LanBackendConfig(**kwargs)
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
            model=d.get("radio_model"),
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

    def set_rx_downmix_channel(self, channel: str):
        """Thread-safe: call from the GUI thread. channel is "mix"/
        "main"/"sub" -- see audio._downmix_stereo_to_mono. Only
        meaningful while Virtual Cables is active (this app's own
        listening audio always stays "mix", see _setup_audio); applies
        immediately to a running RX cable stream, no need to disable/
        re-enable Virtual Cables to change it. Safe to call before
        Virtual Cables has ever been enabled -- just remembers the
        preference for whenever it is."""
        if self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_rx_downmix_channel(channel), self.loop)

    async def _set_rx_downmix_channel(self, channel: str):
        self._rx_downmix_channel = channel
        if self._virtual_cable_active and self.audio_bridge is not None:
            self.audio_bridge.set_rx_downmix_channel(channel)

    def set_virtual_cable_bridge(self, rx: bool, tx: bool):
        """Thread-safe: call from the GUI thread. Points this radio's
        own audio bridge at system-default devices for RX and/or TX (or
        restores the connection-dialog devices if both are False) --
        the actual virtual-cable null sinks, PulseAudio default-sink/
        source redirection, and moving this process's own PortAudio
        streams onto them are no longer this worker's job at all.

        That responsibility moved to HamClockWindow (ham_dashboard.py)
        because a single virtual-cable pairing can now span TWO
        different radios -- one feeding the RX cable, a different one
        driven by the TX cable (e.g. decoding a downlink-only radio's
        audio while transmitting through a separate uplink-only radio).
        The null sinks must only ever be created ONCE regardless of how
        many radios' bridges end up pointed at them; doing that
        per-worker (the original design, back when only one radio was
        ever virtual-cable-enabled at a time) would try to create the
        same-named sink twice and collide.
        """
        if self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_virtual_cable_bridge(rx, tx), self.loop)

    async def _set_virtual_cable_bridge(self, rx: bool, tx: bool):
        # If CW decode is active, detach it BEFORE the bridge instance
        # it might currently be sharing (self._cw_decode_via_bridge) is
        # stopped/discarded below -- and re-attach once the swap is
        # done, so it picks up whatever the new situation actually is
        # (a new bridge's RX stream, a direct tap, or -- if genuinely
        # nothing to attach to -- start_rx() itself will fail and
        # report why, same as any other attach failure).
        cw_decode_was_active = self._cw_decode_callback is not None
        if cw_decode_was_active:
            await self._detach_cw_decode()
        sstv_decode_was_active = self._sstv_decode_callback is not None
        if sstv_decode_was_active:
            await self._detach_sstv_decode()

        if self.audio_bridge is not None:
            await self.audio_bridge.stop()

        if rx or tx:
            # device=None (AUDIO_DEVICE_SYSTEM_DEFAULT resolves to
            # PortAudio's "default" device) rather than trying to open
            # the null-sink/its monitor by name -- confirmed via earlier
            # live testing that this PortAudio build only exposes a
            # generic "default" device, not each individually-named
            # sink; HamClockWindow instead redirects PulseAudio's own
            # system default at the cables and moves this stream onto
            # them once opened.
            self.audio_bridge = AudioBridge(
                self.radio, self.loop,
                input_device=AUDIO_DEVICE_SYSTEM_DEFAULT if tx else None,
                output_device=AUDIO_DEVICE_SYSTEM_DEFAULT if rx else None,
                status_callback=self.audio_status.emit,
                rx_downmix_channel=self._rx_downmix_channel,
            )
            await self.audio_bridge.start()
            self._virtual_cable_active = True
        else:
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
            self._virtual_cable_active = False

        if cw_decode_was_active:
            await self._attach_cw_decode()
        if sstv_decode_was_active:
            await self._attach_sstv_decode()

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
            self._ptt_active = True
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
        # Set regardless of what set_ptt(False) below does -- the user
        # released PTT either way, and that's the signal the SWR meter
        # needs to stop showing a stale TX reading (see _ptt_active's
        # own comment), independent of whether the radio actually
        # unkeyed successfully.
        self._ptt_active = False
        self._swr_reset_pending = True
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

    def start_tuner(self):
        """Thread-safe: call from the GUI thread on the Tune button
        press. Fire-and-forget -- set_tuner_status(2) starts a tune
        cycle; get_tuner_status() polling in _poll_loop_slow (0=off, 1=on,
        2=tuning) is what reports it finishing, same "poll for the
        real result" split as start_ptt()/set_ptt() vs the radio's own
        PTT state."""
        if self.loop is None or self.radio is None:
            self.error.emit("Tuner: not connected yet -- start_tuner() ignored.")
            return
        asyncio.run_coroutine_threadsafe(self._start_tuner(), self.loop)

    async def _start_tuner(self):
        try:
            await self.radio.set_tuner_status(2)
            # Optimistic -- gates the SWR protection cutoff off
            # immediately rather than waiting for the next _poll_loop_
            # slow tick to notice tuner_status==2 (which, now that
            # settings poll every SLOW_POLL_INTERVAL_SEC, could
            # otherwise lag behind the tune sweep's own naturally-high
            # SWR by a couple seconds). That same polling still
            # authoritatively corrects/clears this once tuning actually
            # finishes, and also catches a front-panel-initiated tune
            # this optimistic set can't see.
            self._tuner_active = True
        except Exception as exc:
            self.error.emit(f"Tuner: radio.set_tuner_status(2) failed ({exc}).")

    def send_tx_audio_pcm(self, pcm_bytes: bytes):
        """Thread-safe: call from the GUI thread. Generic "key PTT,
        stream this pre-synthesized PCM out to the radio, unkey"
        primitive -- not specific to any one mode. First consumer:
        aprs_window.py's Send Packet (AFSK audio built by aprs.py's
        build_position_packet_pcm), but nothing here is APRS-specific;
        rtty.py/sstv.py could use this same method for their own send
        sides later.

        Deliberately does NOT go through self.audio_bridge (which only
        ever forwards audio actually captured from a real microphone,
        see audio.py's own docstring) -- resolves and calls the same
        underlying rigplane TX-audio methods AudioBridge does
        (start_audio_tx_pcm/push_audio_tx_pcm/stop_audio_tx_pcm, or the
        generic start_tx/push_tx/stop_tx fallback) directly on
        self.radio, confirmed via reading rigplane's own backends
        (Icom serial/USB, Yaesu CAT, LAN) that push_audio_tx_pcm()
        just writes raw PCM bytes to a TX audio stream/queue with no
        microphone involved anywhere in that path -- so a program can
        feed it synthesized audio exactly the same way. pcm_bytes must
        already be at AUDIO_TX_PCM_SAMPLE_RATE (48000 Hz, mono,
        s16le) -- confirmed via a real push_audio_tx_pcm() runtime
        error ("PCM frame size mismatch: expected 1920 bytes (20ms at
        48000Hz, 1ch s16le)") that this format is a hard requirement,
        not just this app's own RX convention -- callers (aprs.py's
        encoder) generate audio at that rate directly rather than
        resampling."""
        if self.loop is None or self.radio is None:
            self.error.emit("Send TX audio: not connected yet.")
            return
        self._tx_audio_future = asyncio.run_coroutine_threadsafe(self._send_tx_audio_pcm(pcm_bytes), self.loop)

    def stop_tx_audio_send(self):
        """Thread-safe: call from the GUI thread. Aborts an in-progress
        send_tx_audio_pcm() send (e.g. a long RTTY message the operator
        wants to cut short) -- cancels the underlying asyncio task via
        its concurrent.futures.Future (run_coroutine_threadsafe's
        documented cross-thread cancellation mechanism: .cancel() posts
        the cancel request onto the worker's own event loop rather than
        touching asyncio state from this thread directly). _send_tx_
        audio_pcm's try/finally still runs after cancellation (asyncio
        raises CancelledError at the next await point inside the try,
        which the bare `except Exception` does NOT catch -- CancelledError
        isn't an Exception subclass since Python 3.8 -- so it passes
        through untouched into finally), so PTT still gets unkeyed
        properly instead of being left stuck on. A no-op if nothing is
        currently sending."""
        if self._tx_audio_future is not None and not self._tx_audio_future.done():
            self._tx_audio_future.cancel()

    async def _send_tx_audio_pcm(self, pcm_bytes: bytes):
        push_name = find_method_name(self.radio, ["push_audio_tx_pcm", "push_tx"])
        if push_name is None:
            self.error.emit("Send TX audio: this radio/connection has no TX audio push method available.")
            return
        start_name = find_method_name(self.radio, ["start_audio_tx_pcm", "start_tx"])
        stop_name = find_method_name(self.radio, ["stop_audio_tx_pcm", "stop_tx"])

        try:
            await self.radio.set_ptt(True)
            self.audio_status.emit("Send TX audio: radio.set_ptt(True) succeeded.")
        except Exception as exc:
            self.error.emit(f"Send TX audio: PTT keying failed ({exc}) -- aborting send.")
            return

        try:
            if start_name:
                await getattr(self.radio, start_name)()
            # Pad to a whole number of AUDIO_TX_PCM_FRAME_BYTES-sized
            # frames with trailing silence -- push_audio_tx_pcm()
            # rejects any frame that isn't exactly that size (see this
            # method's own docstring).
            padded = pcm_bytes
            remainder = len(padded) % AUDIO_TX_PCM_FRAME_BYTES
            if remainder:
                padded += b"\x00" * (AUDIO_TX_PCM_FRAME_BYTES - remainder)
            for i in range(0, len(padded), AUDIO_TX_PCM_FRAME_BYTES):
                await getattr(self.radio, push_name)(padded[i:i + AUDIO_TX_PCM_FRAME_BYTES])
            if stop_name:
                await getattr(self.radio, stop_name)()
            self.audio_status.emit(f"Send TX audio: sent {len(pcm_bytes)} bytes via {push_name}().")
        except asyncio.CancelledError:
            self.audio_status.emit("Send TX audio: cancelled.")
            raise
        except Exception as exc:
            self.error.emit(f"Send TX audio: {exc}")
        finally:
            try:
                await self.radio.set_ptt(False)
                self.audio_status.emit("Send TX audio: radio.set_ptt(False) succeeded.")
            except Exception as exc:
                self.error.emit(
                    f"Send TX audio: radio.set_ptt(False) FAILED ({exc}) -- radio may still be keyed! "
                    "Check the radio directly."
                )

    def start_tx_audio_stream(self):
        """Thread-safe: call from any thread. Opens a TX-audio push
        session for a LIVE, ongoing stream of PCM chunks (Remote
        Access's browser-mic TX audio, web_remote/routes_audio.py) --
        the streaming counterpart to send_tx_audio_pcm() above, but
        deliberately does NOT touch PTT itself: unlike a synthesized
        one-shot send (APRS position packet, etc), a live mic stream's
        PTT is keyed/unkeyed by the operator holding the browser's own
        PTT button (request_ptt() -> start_ptt()/stop_ptt(), already
        wired), which may span many push_tx_audio_pcm() calls. Calling
        set_ptt() here too would double-key/unkey against whatever the
        PTT button is already doing."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._start_tx_audio_stream(), self.loop)

    async def _start_tx_audio_stream(self):
        self._tx_stream_push_name = find_method_name(self.radio, ["push_audio_tx_pcm", "push_tx"])
        if self._tx_stream_push_name is None:
            self.error.emit("TX audio stream: this radio/connection has no TX audio push method available.")
            return
        start_name = find_method_name(self.radio, ["start_audio_tx_pcm", "start_tx"])
        try:
            if start_name:
                await getattr(self.radio, start_name)()
        except Exception as exc:
            self.error.emit(f"TX audio stream: start failed ({exc}).")

    def push_tx_audio_pcm(self, pcm_bytes: bytes):
        """Thread-safe: call from any thread. Pushes one chunk of a
        LIVE stream already opened by start_tx_audio_stream() --
        pcm_bytes must be at AUDIO_TX_PCM_SAMPLE_RATE (48000 Hz, mono,
        s16le), same hard requirement as send_tx_audio_pcm(). A no-op
        if start_tx_audio_stream() hasn't resolved a push method yet
        (not connected, or this radio has no TX audio support)."""
        if self.loop is None or self.radio is None or self._tx_stream_push_name is None:
            return
        asyncio.run_coroutine_threadsafe(self._push_tx_audio_stream_pcm(pcm_bytes), self.loop)

    async def _push_tx_audio_stream_pcm(self, pcm_bytes: bytes):
        push_name = self._tx_stream_push_name
        if push_name is None:
            return
        padded = pcm_bytes
        remainder = len(padded) % AUDIO_TX_PCM_FRAME_BYTES
        if remainder:
            padded += b"\x00" * (AUDIO_TX_PCM_FRAME_BYTES - remainder)
        try:
            for i in range(0, len(padded), AUDIO_TX_PCM_FRAME_BYTES):
                await getattr(self.radio, push_name)(padded[i:i + AUDIO_TX_PCM_FRAME_BYTES])
        except Exception as exc:
            self.error.emit(f"TX audio stream: push failed ({exc}).")

    def stop_tx_audio_stream(self):
        """Thread-safe: call from any thread. Closes a session opened
        by start_tx_audio_stream() -- does NOT touch PTT, see that
        method's own docstring. Safe to call even if no session is
        open (e.g. the browser's TX audio websocket never successfully
        started one)."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._stop_tx_audio_stream(), self.loop)

    async def _stop_tx_audio_stream(self):
        stop_name = find_method_name(self.radio, ["stop_audio_tx_pcm", "stop_tx"])
        try:
            if stop_name:
                await getattr(self.radio, stop_name)()
        except Exception as exc:
            self.error.emit(f"TX audio stream: stop failed ({exc}).")
        self._tx_stream_push_name = None

    def send_cw_text(self, text: str):
        """Thread-safe: call from the GUI thread. Sends `text` via the
        radio's own built-in keyer (rigplane's send_cw_text() --
        splits into 30-char chunks, the radio converts it to real CW
        and keys the transmitter itself). No local audio/DSP on the
        send side at all -- unlike PTT/mic audio, this doesn't touch
        self.audio_bridge, so it works regardless of whether a CW
        decode tap or a Virtual Cable is active."""
        if self.loop is None or self.radio is None:
            self.error.emit("Send CW: not connected yet -- send_cw_text() ignored.")
            return
        asyncio.run_coroutine_threadsafe(self._send_cw_text(text), self.loop)

    async def _send_cw_text(self, text: str):
        try:
            await self.radio.send_cw_text(text)
        except Exception as exc:
            self.error.emit(f"Send CW text failed: {exc}")

    def stop_cw_text(self):
        """Thread-safe: call from the GUI thread -- aborts an in-
        progress CW send (rigplane's stop_cw_text())."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._stop_cw_text(), self.loop)

    async def _stop_cw_text(self):
        try:
            await self.radio.stop_cw_text()
        except Exception as exc:
            self.error.emit(f"Stop CW text failed: {exc}")

    def start_cw_decode(self, callback):
        """Thread-safe: call from the GUI thread. `callback` is
        invoked with raw PCM bytes (int16, mono) each time audio
        arrives -- on RadioWorker's own asyncio-loop thread, NOT the
        GUI thread, same cross-thread requirement as every other
        callback-driven signal in this app; the caller must hop back
        to the GUI thread itself (e.g. via a Qt signal) before
        touching any widget.

        Works alongside normal listening audio or a Virtual Cable --
        see _attach_cw_decode -- by sharing that AudioBridge's already-
        flowing RX stream instead of requiring exclusive access to
        rigplane's single start_rx() registration. Raises RuntimeError
        immediately (still on the calling/GUI thread) only if not
        connected yet."""
        if self.loop is None or self.radio is None:
            raise RuntimeError("start_cw_decode: not connected yet.")
        self._cw_decode_callback = callback
        asyncio.run_coroutine_threadsafe(self._start_cw_decode(), self.loop)

    async def _start_cw_decode(self):
        await self._attach_cw_decode()

    async def _attach_cw_decode(self):
        """Wires up wherever the current audio situation actually
        lets CW decode receive PCM. rigplane only allows ONE
        radio.start_rx() registration at a time -- if self.audio_bridge
        already holds it (has_rx_stream(): normal listening audio from
        the connection dialog, or an active Virtual Cable), CW decode
        piggybacks on that SAME stream via AudioBridge.
        add_extra_rx_callback() rather than stealing the registration
        out from under it (which would silently break whatever that
        bridge was already doing). Only registers CW decode's own
        direct start_rx() tap when nothing else currently holds it --
        e.g. no audio devices configured at all, or a TX-only bridge
        (mic only, no speaker: has_rx_stream() is False since it never
        calls start_rx in the first place)."""
        if self.audio_bridge is not None and self.audio_bridge.has_rx_stream():
            self._cw_decode_via_bridge = True
            self.audio_bridge.add_extra_rx_callback(self._on_cw_decode_frame_from_bridge)
            return
        self._cw_decode_via_bridge = False
        try:
            await self.radio.start_rx(self._on_cw_decode_frame)
        except Exception as exc:
            self._cw_decode_callback = None
            self.error.emit(f"start_cw_decode: radio.start_rx() failed ({exc}).")

    def _on_cw_decode_frame_from_bridge(self, pcm_bytes):
        """AudioBridge's extra-RX-callback path -- it already extracted
        and downmixed the PCM (the same bytes it queues for playback),
        so unlike _on_cw_decode_frame below, no unwrapping is needed
        here."""
        if self._cw_decode_callback is not None:
            self._cw_decode_callback(pcm_bytes)

    def _on_cw_decode_frame(self, packet=None):
        """Direct-tap path only (see _attach_cw_decode) -- called by
        rigplane on the asyncio-loop thread for each audio frame, same
        calling convention as AudioBridge._on_rx_audio (always exactly
        one positional arg, an AudioPacket or None; see its
        docstring). Reuses AudioBridge's own _extract_pcm_bytes rather
        than duplicating that unwrapping logic here."""
        pcm_bytes = AudioBridge._extract_pcm_bytes(packet)
        if pcm_bytes is not None and self._cw_decode_callback is not None:
            self._cw_decode_callback(pcm_bytes)

    def stop_cw_decode(self):
        """Thread-safe: call from the GUI thread."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._stop_cw_decode(), self.loop)

    async def _stop_cw_decode(self):
        self._cw_decode_callback = None
        await self._detach_cw_decode()

    async def _detach_cw_decode(self):
        """Tears down whichever mode _attach_cw_decode last used --
        clears the shared bridge's extra callback, or stops CW
        decode's own direct tap. Safe to call even if self.audio_bridge
        has since been replaced/stopped out from under a bridge-mode
        attachment (e.g. by _set_virtual_cable_bridge, which detaches
        before swapping the bridge instance)."""
        if self._cw_decode_via_bridge:
            if self.audio_bridge is not None:
                self.audio_bridge.remove_extra_rx_callback(self._on_cw_decode_frame_from_bridge)
            self._cw_decode_via_bridge = False
            return
        try:
            await self.radio.stop_rx()
        except Exception as exc:
            self.error.emit(f"stop_cw_decode: radio.stop_rx() failed ({exc}).")

    def start_sstv_decode(self, callback):
        """Thread-safe: call from the GUI thread. Structural clone of
        start_cw_decode -- `callback` is invoked with raw PCM bytes
        (int16, mono) each time audio arrives, on RadioWorker's own
        asyncio-loop thread, not the GUI thread. Raises RuntimeError
        immediately (still on the calling/GUI thread) only if not
        connected yet."""
        if self.loop is None or self.radio is None:
            raise RuntimeError("start_sstv_decode: not connected yet.")
        self._sstv_decode_callback = callback
        asyncio.run_coroutine_threadsafe(self._start_sstv_decode(), self.loop)

    async def _start_sstv_decode(self):
        await self._attach_sstv_decode()

    async def _attach_sstv_decode(self):
        """Structural clone of _attach_cw_decode -- piggybacks on
        self.audio_bridge's already-flowing RX stream via
        add_extra_rx_callback() when one exists (has_rx_stream()),
        otherwise registers its own direct radio.start_rx() tap. Each
        decoder registers its own distinct callback now (AudioBridge
        supports multiple simultaneous extra-RX consumers), so this can
        safely run alongside CW decode (or any other) without either
        clobbering the other's callback."""
        if self.audio_bridge is not None and self.audio_bridge.has_rx_stream():
            self._sstv_decode_via_bridge = True
            self.audio_bridge.add_extra_rx_callback(self._on_sstv_decode_frame_from_bridge)
            return
        self._sstv_decode_via_bridge = False
        try:
            await self.radio.start_rx(self._on_sstv_decode_frame)
        except Exception as exc:
            self._sstv_decode_callback = None
            self.error.emit(f"start_sstv_decode: radio.start_rx() failed ({exc}).")

    def _on_sstv_decode_frame_from_bridge(self, pcm_bytes):
        """AudioBridge's extra-RX-callback path -- already-extracted/
        downmixed PCM, no unwrapping needed (see
        _on_cw_decode_frame_from_bridge)."""
        if self._sstv_decode_callback is not None:
            self._sstv_decode_callback(pcm_bytes)

    def _on_sstv_decode_frame(self, packet=None):
        """Direct-tap path only (see _attach_sstv_decode) -- same
        calling convention as _on_cw_decode_frame."""
        pcm_bytes = AudioBridge._extract_pcm_bytes(packet)
        if pcm_bytes is not None and self._sstv_decode_callback is not None:
            self._sstv_decode_callback(pcm_bytes)

    def stop_sstv_decode(self):
        """Thread-safe: call from the GUI thread."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._stop_sstv_decode(), self.loop)

    async def _stop_sstv_decode(self):
        self._sstv_decode_callback = None
        await self._detach_sstv_decode()

    async def _detach_sstv_decode(self):
        """Structural clone of _detach_cw_decode -- tears down
        whichever mode _attach_sstv_decode last used. Safe to call even
        if self.audio_bridge has since been replaced/stopped out from
        under a bridge-mode attachment."""
        if self._sstv_decode_via_bridge:
            if self.audio_bridge is not None:
                self.audio_bridge.remove_extra_rx_callback(self._on_sstv_decode_frame_from_bridge)
            self._sstv_decode_via_bridge = False
            return
        try:
            await self.radio.stop_rx()
        except Exception as exc:
            self.error.emit(f"stop_sstv_decode: radio.stop_rx() failed ({exc}).")

    def start_rtty_decode(self, callback):
        """Thread-safe: call from the GUI thread. Structural clone of
        start_cw_decode/start_sstv_decode -- `callback` is invoked with
        raw PCM bytes (int16, mono) each time audio arrives, on
        RadioWorker's own asyncio-loop thread, not the GUI thread.
        Raises RuntimeError immediately (still on the calling/GUI
        thread) only if not connected yet."""
        if self.loop is None or self.radio is None:
            raise RuntimeError("start_rtty_decode: not connected yet.")
        self._rtty_decode_callback = callback
        asyncio.run_coroutine_threadsafe(self._start_rtty_decode(), self.loop)

    async def _start_rtty_decode(self):
        await self._attach_rtty_decode()

    async def _attach_rtty_decode(self):
        """Structural clone of _attach_cw_decode/_attach_sstv_decode --
        piggybacks on self.audio_bridge's already-flowing RX stream via
        add_extra_rx_callback() when one exists (has_rx_stream()),
        otherwise registers its own direct radio.start_rx() tap."""
        if self.audio_bridge is not None and self.audio_bridge.has_rx_stream():
            self._rtty_decode_via_bridge = True
            self.audio_bridge.add_extra_rx_callback(self._on_rtty_decode_frame_from_bridge)
            return
        self._rtty_decode_via_bridge = False
        try:
            await self.radio.start_rx(self._on_rtty_decode_frame)
        except Exception as exc:
            self._rtty_decode_callback = None
            self.error.emit(f"start_rtty_decode: radio.start_rx() failed ({exc}).")

    def _on_rtty_decode_frame_from_bridge(self, pcm_bytes):
        """AudioBridge's extra-RX-callback path -- already-extracted/
        downmixed PCM, no unwrapping needed (see
        _on_cw_decode_frame_from_bridge)."""
        if self._rtty_decode_callback is not None:
            self._rtty_decode_callback(pcm_bytes)

    def _on_rtty_decode_frame(self, packet=None):
        """Direct-tap path only (see _attach_rtty_decode) -- same
        calling convention as _on_cw_decode_frame."""
        pcm_bytes = AudioBridge._extract_pcm_bytes(packet)
        if pcm_bytes is not None and self._rtty_decode_callback is not None:
            self._rtty_decode_callback(pcm_bytes)

    def stop_rtty_decode(self):
        """Thread-safe: call from the GUI thread."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._stop_rtty_decode(), self.loop)

    async def _stop_rtty_decode(self):
        self._rtty_decode_callback = None
        await self._detach_rtty_decode()

    async def _detach_rtty_decode(self):
        """Structural clone of _detach_cw_decode/_detach_sstv_decode --
        tears down whichever mode _attach_rtty_decode last used."""
        if self._rtty_decode_via_bridge:
            if self.audio_bridge is not None:
                self.audio_bridge.remove_extra_rx_callback(self._on_rtty_decode_frame_from_bridge)
            self._rtty_decode_via_bridge = False
            return
        try:
            await self.radio.stop_rx()
        except Exception as exc:
            self.error.emit(f"stop_rtty_decode: radio.stop_rx() failed ({exc}).")

    def start_aprs_decode(self, callback):
        """Thread-safe: call from the GUI thread. Structural clone of
        start_cw_decode/start_sstv_decode/start_rtty_decode --
        `callback` is invoked with raw PCM bytes (int16, mono) each
        time audio arrives, on RadioWorker's own asyncio-loop thread,
        not the GUI thread. Raises RuntimeError immediately (still on
        the calling/GUI thread) only if not connected yet."""
        if self.loop is None or self.radio is None:
            raise RuntimeError("start_aprs_decode: not connected yet.")
        self._aprs_decode_callback = callback
        asyncio.run_coroutine_threadsafe(self._start_aprs_decode(), self.loop)

    async def _start_aprs_decode(self):
        await self._attach_aprs_decode()

    async def _attach_aprs_decode(self):
        """Structural clone of _attach_cw_decode/_attach_sstv_decode/
        _attach_rtty_decode -- piggybacks on self.audio_bridge's
        already-flowing RX stream via add_extra_rx_callback() when one
        exists (has_rx_stream()), otherwise registers its own direct
        radio.start_rx() tap."""
        if self.audio_bridge is not None and self.audio_bridge.has_rx_stream():
            self._aprs_decode_via_bridge = True
            self.audio_bridge.add_extra_rx_callback(self._on_aprs_decode_frame_from_bridge)
            return
        self._aprs_decode_via_bridge = False
        try:
            await self.radio.start_rx(self._on_aprs_decode_frame)
        except Exception as exc:
            self._aprs_decode_callback = None
            self.error.emit(f"start_aprs_decode: radio.start_rx() failed ({exc}).")

    def _on_aprs_decode_frame_from_bridge(self, pcm_bytes):
        """AudioBridge's extra-RX-callback path -- already-extracted/
        downmixed PCM, no unwrapping needed (see
        _on_cw_decode_frame_from_bridge)."""
        if self._aprs_decode_callback is not None:
            self._aprs_decode_callback(pcm_bytes)

    def _on_aprs_decode_frame(self, packet=None):
        """Direct-tap path only (see _attach_aprs_decode) -- same
        calling convention as _on_cw_decode_frame."""
        pcm_bytes = AudioBridge._extract_pcm_bytes(packet)
        if pcm_bytes is not None and self._aprs_decode_callback is not None:
            self._aprs_decode_callback(pcm_bytes)

    def stop_aprs_decode(self):
        """Thread-safe: call from the GUI thread."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._stop_aprs_decode(), self.loop)

    async def _stop_aprs_decode(self):
        self._aprs_decode_callback = None
        await self._detach_aprs_decode()

    async def _detach_aprs_decode(self):
        """Structural clone of _detach_cw_decode/_detach_sstv_decode/
        _detach_rtty_decode -- tears down whichever mode _attach_aprs_
        decode last used."""
        if self._aprs_decode_via_bridge:
            if self.audio_bridge is not None:
                self.audio_bridge.remove_extra_rx_callback(self._on_aprs_decode_frame_from_bridge)
            self._aprs_decode_via_bridge = False
            return
        try:
            await self.radio.stop_rx()
        except Exception as exc:
            self.error.emit(f"stop_aprs_decode: radio.stop_rx() failed ({exc}).")

    def start_psk31_decode(self, callback):
        """Thread-safe: call from the GUI thread. Structural clone of
        start_cw_decode/start_sstv_decode/start_rtty_decode/start_aprs_
        decode -- `callback` is invoked with raw PCM bytes (int16,
        mono) each time audio arrives, on RadioWorker's own asyncio-
        loop thread, not the GUI thread. Raises RuntimeError
        immediately (still on the calling/GUI thread) only if not
        connected yet."""
        if self.loop is None or self.radio is None:
            raise RuntimeError("start_psk31_decode: not connected yet.")
        self._psk31_decode_callback = callback
        asyncio.run_coroutine_threadsafe(self._start_psk31_decode(), self.loop)

    async def _start_psk31_decode(self):
        await self._attach_psk31_decode()

    async def _attach_psk31_decode(self):
        """Structural clone of _attach_cw_decode/_attach_sstv_decode/
        _attach_rtty_decode/_attach_aprs_decode -- piggybacks on self.
        audio_bridge's already-flowing RX stream via add_extra_rx_
        callback() when one exists (has_rx_stream()), otherwise
        registers its own direct radio.start_rx() tap."""
        if self.audio_bridge is not None and self.audio_bridge.has_rx_stream():
            self._psk31_decode_via_bridge = True
            self.audio_bridge.add_extra_rx_callback(self._on_psk31_decode_frame_from_bridge)
            return
        self._psk31_decode_via_bridge = False
        try:
            await self.radio.start_rx(self._on_psk31_decode_frame)
        except Exception as exc:
            self._psk31_decode_callback = None
            self.error.emit(f"start_psk31_decode: radio.start_rx() failed ({exc}).")

    def _on_psk31_decode_frame_from_bridge(self, pcm_bytes):
        """AudioBridge's extra-RX-callback path -- already-extracted/
        downmixed PCM, no unwrapping needed (see
        _on_cw_decode_frame_from_bridge)."""
        if self._psk31_decode_callback is not None:
            self._psk31_decode_callback(pcm_bytes)

    def _on_psk31_decode_frame(self, packet=None):
        """Direct-tap path only (see _attach_psk31_decode) -- same
        calling convention as _on_cw_decode_frame."""
        pcm_bytes = AudioBridge._extract_pcm_bytes(packet)
        if pcm_bytes is not None and self._psk31_decode_callback is not None:
            self._psk31_decode_callback(pcm_bytes)

    def stop_psk31_decode(self):
        """Thread-safe: call from the GUI thread."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._stop_psk31_decode(), self.loop)

    async def _stop_psk31_decode(self):
        self._psk31_decode_callback = None
        await self._detach_psk31_decode()

    async def _detach_psk31_decode(self):
        """Structural clone of _detach_cw_decode/_detach_sstv_decode/
        _detach_rtty_decode/_detach_aprs_decode -- tears down whichever
        mode _attach_psk31_decode last used."""
        if self._psk31_decode_via_bridge:
            if self.audio_bridge is not None:
                self.audio_bridge.remove_extra_rx_callback(self._on_psk31_decode_frame_from_bridge)
            self._psk31_decode_via_bridge = False
            return
        try:
            await self.radio.stop_rx()
        except Exception as exc:
            self.error.emit(f"stop_psk31_decode: radio.stop_rx() failed ({exc}).")

    async def _setup_levels(self):
        """Checks whether this radio supports rigplane's LevelsCapable
        protocol and, if so, discovers the real get/set method names for
        every entry in LEVEL_DEFINITIONS (same find_method_name
        discovery used elsewhere). Missing ones are reported once, with
        a filtered dir(radio) hint, instead of failing silently on first
        use. Live values are polled continuously in _poll_loop_slow() rather
        than only read once here, so a slider reflects changes made
        directly on the radio's own front panel too."""
        self._level_methods = {}
        if LevelsCapable is None or not isinstance(self.radio, LevelsCapable):
            return

        missing = []
        for key, definition in LEVEL_DEFINITIONS.items():
            get_name = find_method_name(self.radio, definition["getter_candidates"])
            set_name = find_method_name(self.radio, definition["setter_candidates"])
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

    async def _setup_pbt(self):
        """Populates self._pbt_methods (pbt key -> CI-V 0x14 sub-command
        byte) when is_pbt_capable is already True (set from the radio's
        own declared "pbt" capability). Deliberately does NOT use
        find_method_name to discover rigplane's own get_pbt_inner/
        set_pbt_inner/get_pbt_outer/set_pbt_outer -- those are confirmed
        broken on a real (single-receiver) IC-705, see PBT_DEFINITIONS'
        own comment in constants.py. _set_pbt_value/the poll loop instead
        send raw CI-V frames directly via rigplane's public send_civ(),
        which this radio object needs to actually expose -- an object
        that claims is_pbt_capable via CAP_PBT but has no send_civ (e.g.
        a future non-CI-V backend) just gets an empty _pbt_methods, same
        as the old "no working getter+setter found" case."""
        self._pbt_methods = {}
        if not self.is_pbt_capable or not hasattr(self.radio, "send_civ"):
            return
        for key, definition in PBT_DEFINITIONS.items():
            self._pbt_methods[key] = definition["civ_sub"]

    async def _setup_filter_shape(self):
        """Same idea as _setup_pbt() -- self._filter_shape_available is
        just "is_filter_shape_capable AND this radio object exposes
        send_civ()", since get_filter_shape/set_filter_shape are
        confirmed broken the same way PBT's were (see FILTER_SHAPE_
        CIV_SUB's comment in constants.py) and RadioWorker sends raw
        CI-V directly instead."""
        self._filter_shape_available = self.is_filter_shape_capable and hasattr(self.radio, "send_civ")

    def _resolve_filter_width_range(self, mode_name):
        """Looks up the adjustable filter-width range for `mode_name` via
        rigplane's public radio.profile.resolve_filter_rule() -- the same
        lookup set_filter_width()/get_filter_width() use internally
        (there via the private self._profile; .profile is the public
        equivalent, a plain property). Returns None if this mode's width
        can't be adjusted at all (no rule, or rule.fixed -- e.g. FM/WFM/
        DV), else a dict: {"min_hz", "max_hz", "segments": [(hz_min,
        hz_max, step_hz), ...]} -- segments (not just min/max) so the UI
        can snap to a valid step instead of guessing, since real Icom
        filter-width ranges mix step sizes (e.g. SSB: 50 Hz steps below
        600 Hz, 100 Hz steps from 600 Hz up)."""
        profile = getattr(self.radio, "profile", None)
        if profile is None:
            return None
        try:
            rule = profile.resolve_filter_rule(mode_name, data_mode=0)
        except Exception:
            return None
        if rule is None or rule.fixed or not rule.segments:
            return None
        segments = [(seg.hz_min, seg.hz_max, seg.step_hz) for seg in rule.segments]
        return {
            "min_hz": min(s[0] for s in segments),
            "max_hz": max(s[1] for s in segments),
            "segments": segments,
        }

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
    def _receiver_kwargs(method, receiver: int) -> dict:
        """{"receiver": receiver} if method's signature actually accepts
        a receiver kwarg, else {}. Confirmed via a full inspect.
        signature() sweep of every getter/setter name referenced in
        CONTROL_DEFINITIONS/LEVEL_DEFINITIONS that a lot more of them
        take one than just "vfo"/frequency (af_level, agc, filter,
        mode, preamp, rf_gain, squelch, vfo_slot, frequency -- all
        default to receiver=0/Main when omitted) -- confirmed live on a
        real 9700 that "mode" alone was enough to keep an
        uplink/downlink flip-flop going even after frequency and vfo
        were both already fixed individually. Rather than hand-listing
        (and inevitably missing more of) them one at a time, every
        getter/setter call in this file goes through this so whichever
        receiver is actually in use (self._active_receiver) is passed
        wherever the underlying method actually accepts it, and left
        alone (its own default, Main) everywhere it doesn't. Only
        meaningful for dual-receiver radios -- callers should only use
        this when self.is_dual_receiver."""
        try:
            if "receiver" in inspect.signature(method).parameters:
                return {"receiver": receiver}
        except (TypeError, ValueError):
            pass
        return {}

    async def _poll_receiver_aware(self, key: str, getter):
        """Calls getter(), passing receiver=self._active_receiver when
        self.is_dual_receiver and the method's signature accepts it
        (_receiver_kwargs) -- UNLESS key is already known
        (_receiver_unsupported_getters) to reject that at runtime for
        this radio/profile. On the first CommandError, remembers it
        (so every later poll cycle goes straight to the safe bare call
        instead of repeating -- and re-reporting -- the same failure
        forever) and retries once without the receiver kwarg. Only
        catches CommandError specifically -- anything else (a real
        connection problem, say) still propagates to the caller's own
        try/except, same as before this existed."""
        kwargs = {}
        if self.is_dual_receiver and key not in self._receiver_unsupported_getters:
            kwargs = self._receiver_kwargs(getter, self._active_receiver)
        if not kwargs:
            return await getter()
        try:
            return await getter(**kwargs)
        except CommandError as exc:
            self._receiver_unsupported_getters.add(key)
            self.error.emit(
                f"{key}: reading with an explicit receiver isn't supported on this "
                f"radio/profile ({exc}) -- reading the default receiver instead from now on."
            )
            return await getter()

    async def _call_receiver_aware(self, key: str, func, *args):
        """Write-side counterpart to _poll_receiver_aware: calls
        func(*args, receiver=self._active_receiver) when
        self.is_dual_receiver and func's signature accepts a receiver
        kwarg -- UNLESS key is already known
        (_receiver_unsupported_setters) to reject that at runtime for
        this radio/profile. On the first CommandError, remembers it
        (so later calls for this key go straight to the safe bare call
        instead of repeating -- and re-reporting -- the same failure
        forever, and so the value actually reaches the radio instead
        of being dropped) and retries once without the receiver kwarg
        -- confirmed live on a real 9700 that AF gain/squelch/RF gain
        all still land correctly on whichever receiver is actually
        active (select_receiver()) with no receiver kwarg at all, so
        the bare retry doesn't lose anything; it's the only form these
        setters actually accept for a non-Main receiver on this
        profile. Only catches CommandError specifically -- anything
        else still propagates to the caller's own try/except."""
        kwargs = {}
        if self.is_dual_receiver and key not in self._receiver_unsupported_setters:
            kwargs = self._receiver_kwargs(func, self._active_receiver)
        if not kwargs:
            return await func(*args)
        try:
            return await func(*args, **kwargs)
        except CommandError as exc:
            self._receiver_unsupported_setters.add(key)
            self.error.emit(
                f"{key}: writing with an explicit receiver isn't supported on this "
                f"radio/profile ({exc}) -- writing to the active receiver instead from now on."
            )
            return await func(*args)

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
        is cached per cache_key so later calls don't re-probe.

        Goes through _call_receiver_aware for the receiver= kwarg
        (dual-receiver only) -- confirmed live on a real 9700 that AF
        gain/squelch/RF gain's setters actually RAISE CommandError for
        a non-Main receiver (the same cmd29-route rejection their
        getters have), not silently ignore it as originally assumed
        here; _call_receiver_aware catches that once per key and falls
        back to a bare call (which does correctly land on whichever
        receiver is active via select_receiver()) from then on. See
        DUAL_RECEIVER_LEVEL_KEYS in constants.py and main_window.py's
        active_receiver_button for how the UI actually exposes control
        over these three."""
        setter = getattr(self.radio, setter_name)
        mode = self._level_value_mode.get(cache_key, "float")
        if mode == "float":
            try:
                await self._call_receiver_aware(cache_key, setter, value)
                self._level_value_mode[cache_key] = "float"
                return
            except (TypeError, ValueError):
                pass  # fall through and try the raw-int scale instead
        await self._call_receiver_aware(cache_key, setter, int(round(value * 255)))
        self._level_value_mode[cache_key] = "int255"

    def set_level_value(self, key: str, value: float):
        """Thread-safe: call from the GUI thread. value is 0.0-1.0.

        Debounced (LEVEL_DEBOUNCE_SECONDS): rather than sending a command
        for every single intermediate value during a drag, each call
        resets a short timer for that key -- only once the caller stops
        calling (the user stops moving the slider/knob) does the last
        value actually get sent. The widget's own on-screen position
        still follows the drag in real time regardless (that's native
        Qt behavior, untouched by this) -- only the underlying radio
        command is delayed/coalesced."""
        if self.loop is None or key not in self._level_methods:
            return
        asyncio.run_coroutine_threadsafe(self._debounce_level_value(key, value), self.loop)

    async def _debounce_level_value(self, key: str, value: float):
        # Runs on self.loop, same as every task it touches here, so
        # cancelling/replacing the pending task for this key is never
        # racing a concurrent call for the same key.
        existing = self._level_debounce_tasks.get(key)
        if existing is not None and not existing.done():
            existing.cancel()

        async def _wait_then_apply():
            try:
                await asyncio.sleep(LEVEL_DEBOUNCE_SECONDS)
            except asyncio.CancelledError:
                return
            await self._set_level_value(key, value)

        self._level_debounce_tasks[key] = asyncio.ensure_future(_wait_then_apply())

    async def _set_level_value(self, key: str, value: float):
        _get_name, set_name = self._level_methods[key]
        definition = LEVEL_DEFINITIONS[key]
        try:
            await self._call_level_setter(set_name, value, key)
        except Exception as exc:
            self.error.emit(f"{definition['label']}: {set_name}() failed ({exc}).")

    def set_pbt_value(self, key: str, value: int):
        """Thread-safe: call from the GUI thread. value is the raw 0-255
        PBT level (128=centered) -- see PBT_DEFINITIONS. Debounced the
        same way as set_level_value(), through a separate dict/keyspace
        (_pbt_debounce_tasks) so a PBT drag and a level-slider drag never
        cancel each other's pending task."""
        if self.loop is None or key not in self._pbt_methods:
            return
        asyncio.run_coroutine_threadsafe(self._debounce_pbt_value(key, value), self.loop)

    async def _debounce_pbt_value(self, key: str, value: int):
        existing = self._pbt_debounce_tasks.get(key)
        if existing is not None and not existing.done():
            existing.cancel()

        async def _wait_then_apply():
            try:
                await asyncio.sleep(LEVEL_DEBOUNCE_SECONDS)
            except asyncio.CancelledError:
                return
            await self._set_pbt_value(key, value)

        self._pbt_debounce_tasks[key] = asyncio.ensure_future(_wait_then_apply())

    async def _set_pbt_value(self, key: str, value: int):
        civ_sub = self._pbt_methods[key]
        definition = PBT_DEFINITIONS[key]
        try:
            # Raw CI-V 0x14/<civ_sub> write via send_civ(), NOT
            # rigplane's own set_pbt_inner/set_pbt_outer -- see
            # PBT_DEFINITIONS' comment in constants.py for why.
            await self.radio.send_civ(
                0x14, sub=civ_sub, data=_bcd_encode_pbt_level(value), wait_response=False
            )
        except Exception as exc:
            self.error.emit(f"{definition['label']}: send_civ(0x14, 0x{civ_sub:02x}) failed ({exc}).")

    def set_filter_width_value(self, width_hz: int):
        """Thread-safe: call from the GUI thread. width_hz should already
        be snapped to a valid step (see filter_width_range_updated's
        segments) -- rigplane's own set_filter_width() rejects anything
        else with a CommandError, caught below same as any other setter
        failure. Debounced the same way as set_pbt_value/set_level_value."""
        if self.loop is None or not self.is_filter_width_capable:
            return
        asyncio.run_coroutine_threadsafe(self._debounce_filter_width_value(width_hz), self.loop)

    async def _debounce_filter_width_value(self, width_hz: int):
        existing = self._filter_width_debounce_task
        if existing is not None and not existing.done():
            existing.cancel()

        async def _wait_then_apply():
            try:
                await asyncio.sleep(LEVEL_DEBOUNCE_SECONDS)
            except asyncio.CancelledError:
                return
            await self._set_filter_width_value(width_hz)

        self._filter_width_debounce_task = asyncio.ensure_future(_wait_then_apply())

    async def _set_filter_width_value(self, width_hz: int):
        try:
            await self.radio.set_filter_width(width_hz)
            self._filter_width_hz = width_hz
            self.filter_width_updated.emit(width_hz)
        except Exception as exc:
            self.error.emit(f"Filter width: set_filter_width({width_hz}) failed ({exc}).")

    def set_filter_shape_value(self, value: int):
        """Thread-safe: call from the GUI thread. value is 0 (SHARP) or
        1 (SOFT) -- see FILTER_SHAPE_OPTIONS. Not debounced, unlike the
        PBT/filter-width sliders -- this is a discrete combo selection,
        same immediate-dispatch treatment as the FIL1/2/3 combo
        (_on_control_combo_changed), not a dragged control."""
        if self.loop is None or not self._filter_shape_available:
            return
        asyncio.run_coroutine_threadsafe(self._set_filter_shape_value(value), self.loop)

    async def _set_filter_shape_value(self, value: int):
        try:
            # Raw CI-V 0x16/0x56 write via send_civ(), NOT rigplane's
            # own set_filter_shape() -- see FILTER_SHAPE_CIV_SUB's
            # comment in constants.py.
            await self.radio.send_civ(0x16, sub=FILTER_SHAPE_CIV_SUB, data=bytes([value]), wait_response=False)
            self.filter_shape_updated.emit(value)
        except Exception as exc:
            self.error.emit(f"Filter shape: send_civ(0x16, 0x56) failed ({exc}).")

    def set_swr_protection_enabled(self, enabled: bool):
        """Thread-safe: call from the GUI thread. Plain attribute set,
        no coroutine needed -- see _swr_protection_enabled's own comment."""
        self._swr_protection_enabled = enabled

    def set_preamp_att_value(self, preamp_level: int, atten_db):
        """Thread-safe: call from the GUI thread. preamp_level is 0/1/2;
        atten_db is 0/20 or None to leave the attenuator register alone
        (2m/70cm's OFF/ON options -- see PREAMP_ATT_OPTIONS_VHF_UHF in
        constants.py). Not debounced -- a discrete combo selection, same
        immediate-dispatch treatment as the Filter Shape combo."""
        if self.loop is None or not self.is_ic705_preamp_att:
            return
        asyncio.run_coroutine_threadsafe(self._set_preamp_att_value(preamp_level, atten_db), self.loop)

    async def _set_preamp_att_value(self, preamp_level: int, atten_db):
        try:
            # Raw CI-V writes via send_civ(), NOT rigplane's own
            # set_preamp()/set_attenuator_level() -- see PREAMP_CIV_SUB's
            # comment in constants.py.
            await self.radio.send_civ(
                0x16, sub=PREAMP_CIV_SUB, data=_bcd_encode_byte(preamp_level), wait_response=False
            )
            if atten_db is not None:
                await self.radio.send_civ(
                    ATT_CIV_COMMAND, data=_bcd_encode_byte(atten_db), wait_response=False
                )
                self.preamp_att_updated.emit(preamp_level, atten_db)
            else:
                # atten_db=None (VHF/UHF) -- only preamp changed; keep
                # whatever the last-known attenuator reading was rather
                # than emitting a made-up value for a register we didn't
                # touch.
                self.preamp_att_updated.emit(preamp_level, 0)
        except Exception as exc:
            self.error.emit(f"Preamp/Attenuator: send_civ() failed ({exc}).")

    async def _main(self):
        try:
            if self._details.get("connection_type") == "remote":
                # Bypasses rigplane's own create_radio()/BackendConfig
                # dispatch entirely -- RemoteWebRadio isn't one of
                # rigplane's own four backend types, it's this app's
                # own client for rigplane's separate `web` server (see
                # remote_radio.py's own docstring). Everything from
                # here on (__aenter__, isinstance capability checks,
                # _setup_audio/_setup_levels/_setup_meters/
                # _setup_controls, the scope-enable block, the poll loops)
                # is already fully backend-agnostic and needs no
                # remote-specific handling at all.
                self._radio_cm = RemoteWebRadio(
                    self._details["remote_host"],
                    self._details["remote_port"],
                    self._details.get("remote_token", ""),
                )
                self.radio = await self._radio_cm.__aenter__()
            else:
                config = self._build_config()
                try:
                    self._radio_cm = create_radio(config)
                    self.radio = await self._radio_cm.__aenter__()
                except Exception as exc:
                    if getattr(config, "audio_codec_explicit", False):
                        # See _build_config's docstring: an explicit stereo
                        # request disarms rigplane's own automatic mono-
                        # retry-on-rejection fallback, so re-implement it
                        # here -- one retry with the radio profile's own
                        # (proven-safe) codec default before actually giving
                        # up. Whatever failure this produces (if any) is
                        # what actually gets reported below, same as before
                        # this stereo attempt existed.
                        self.audio_status.emit(
                            f"Connect: stereo audio request failed ({exc}) -- retrying "
                            "with the radio profile's default (mono) codec."
                        )
                        config = self._build_config(force_stereo=False)
                        self._radio_cm = create_radio(config)
                        self.radio = await self._radio_cm.__aenter__()
                    else:
                        raise
        except Exception as exc:
            self.connection_failed.emit(str(exc))
            return

        # isinstance(radio, DualReceiverCapable) alone isn't enough --
        # confirmed live on an IC-705 (single-receiver): its methods
        # (swap_main_sub etc.) live on DualRxRuntimeMixin, which every
        # Icom radio class inherits UNCONDITIONALLY (rigplane/runtime/
        # radio.py: "class CoreRadio(ScopeRuntimeMixin, AudioRuntimeMixin,
        # DualRxRuntimeMixin)"), so the isinstance check is structurally
        # True even on a radio that only has one receiver -- the same
        # "method exists but isn't functionally real for this model"
        # trap already hit for PBT/filter shape/preamp elsewhere in this
        # app. The actual ground truth is the profile's own declared
        # receiver_count (IC-705's ic705.toml: "receiver_count = 1";
        # IC-7610/IC-9700: 2) -- both checks are required so a radio that
        # somehow reports receiver_count > 1 without implementing the
        # protocol at all still isn't treated as dual-receiver.
        profile = getattr(self.radio, "profile", None)
        self.is_dual_receiver = (
            DualReceiverCapable is not None
            and isinstance(self.radio, DualReceiverCapable)
            and getattr(profile, "receiver_count", 1) > 1
        )
        self.is_pbt_capable = CAP_PBT is not None and CAP_PBT in getattr(self.radio, "capabilities", set())
        self.is_filter_width_capable = (
            CAP_FILTER_WIDTH is not None and CAP_FILTER_WIDTH in getattr(self.radio, "capabilities", set())
        )
        self.is_filter_shape_capable = (
            CAP_FILTER_SHAPE is not None and CAP_FILTER_SHAPE in getattr(self.radio, "capabilities", set())
        )
        # Not a rigplane capability flag -- this is a model-specific
        # workaround (see PREAMP_CIV_SUB's comment in constants.py), so
        # it's keyed on radio_model directly, same scoping the app
        # already uses for CONTROL_OPTION_EXCLUDED.
        self.is_ic705_preamp_att = (
            self._details.get("radio_model") == "IC-705" and hasattr(self.radio, "send_civ")
        )
        self.is_ic705_swr_workaround = self.is_ic705_preamp_att  # same model+send_civ gate, see SWR_CIV_SUB's comment
        # Broader than the IC-705-only workarounds above -- confirmed
        # against BOTH the IC-705's and IC-9700's own CI-V Reference
        # Guides that the real range is -20.0/+20.0, not rigplane's
        # IC-7610-specific -30.0/+10.0 default -- see
        # SCOPE_REF_WIDE_RANGE_MODELS' comment in constants.py.
        self.is_scope_ref_workaround = (
            self._details.get("radio_model") in SCOPE_REF_WIDE_RANGE_MODELS and hasattr(self.radio, "send_civ")
        )
        self.connected.emit()
        # Everything from here through the poll loops is wrapped in one
        # try/finally so self._radio_cm.__aexit__() (closing the actual
        # serial/network connection) always runs -- including if stop()
        # cancels this task while still in setup (_setup_audio/_setup_
        # levels/etc, or enable_scope's own up-to-5s verification wait),
        # not just once the poll loops are reached. Without this, closing
        # the window during a slow real-hardware startup would cancel
        # cleanly (no more Qt abort -- see stop()'s docstring) but leak
        # the underlying connection, likely blocking a subsequent
        # reconnect attempt ("port busy") until the OS reclaims it.
        try:
            await self._setup_audio()
            await self._setup_levels()
            await self._setup_pbt()
            await self._setup_filter_shape()
            self._setup_meters()
            self.meters_ready.emit(self._meter_definitions)
            self._setup_controls()

            # Scope data arrives unsolicited over CI-V; register a
            # callback rather than polling for it. The callback fires
            # on this thread's event loop, so it must only emit a
            # signal -- never touch a widget.
            try:
                self.radio.on_scope_data(self._handle_scope_frame)
                await self.radio.enable_scope()
                self.is_scope_capable = True
                # is_scope_capable becomes true here, well AFTER connected.emit()
                # above -- main_window.py's _on_connected (which runs off that
                # signal) would almost always see it still False, since this
                # await completes on its own schedule long after the GUI thread
                # gets queued to enable other controls. Confirmed live: on a
                # real 9700, the scope itself loaded fine but the span/ref/
                # speed controls stayed greyed out forever, exactly this race.
                # A dedicated signal, fired only once this really is known true,
                # is what main_window.py's scope controls listen for instead.
                self.scope_ready.emit()
            except Exception as exc:
                self.error.emit(f"Scope unavailable: {exc}")

            # Two independent loops (own asyncio.sleep cadence each) --
            # see _poll_loop_slow's docstring for why. gather() so a
            # genuine failure in either one still propagates/cancels the
            # other, same all-or-nothing shutdown semantics a single
            # loop had.
            await asyncio.gather(self._poll_loop_fast(), self._poll_loop_slow())
        finally:
            # Cancel any pending debounced level-value sends -- without
            # this, a slider drag right before disconnect could leave a
            # task scheduled to call self.radio well after
            # _radio_cm.__aexit__() below has torn it down.
            for task in self._level_debounce_tasks.values():
                if not task.done():
                    task.cancel()
            self._level_debounce_tasks.clear()
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
        # Own, independent copy -- not the shared module-level
        # METER_DEFINITIONS dict. This app now supports multiple
        # simultaneously-connected radios (see satellite_session.py); two
        # different radio models can genuinely have different
        # native_power_unit scales, and mutating the shared dict in place
        # (as this used to do, back when only one radio was ever
        # connected at a time) would let whichever radio connected last
        # silently overwrite the power-meter scaling for every other
        # already-connected radio.
        self._meter_definitions = copy.deepcopy(METER_DEFINITIONS)
        missing = []
        self._meter_getters = {}  # meter_type -> resolved method name
        for meter_type, definition in self._meter_definitions.items():
            candidates = definition.get("getter_candidates", [definition["getter"]])
            resolved = find_method_name(self.radio, candidates)
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
        # scale its power reading uses instead of guessing.
        if "power" in self._meter_getters:
            native_unit = getattr(self.radio, "native_power_unit", None)
            if native_unit == "watts":
                self._meter_definitions["power"]["kind"] = "direct"
                self._power_is_raw_255 = False
                self.audio_status.emit("Power meter: radio reports native unit 'watts' -- reading directly.")
            elif native_unit == "raw_255":
                # "direct" here too, NOT "linear" -- _poll_loop_fast
                # applies POWER_CALIBRATION (a genuinely non-linear
                # piecewise curve, confirmed against the IC-705's own
                # CI-V Reference Guide -- see that constant's own
                # comment in constants.py) and converts straight to
                # watts before emitting, same as the native-watts case
                # above ends up with. The flat raw/255 linear ratio this
                # used to fall back to was root-caused to a real
                # miscalibration: the real 100% point is raw byte 213,
                # not 255, so even a plain raw_max fix alone wouldn't
                # have been enough -- the curve itself bulges away from
                # a straight line in between.
                self._meter_definitions["power"]["kind"] = "direct"
                self._power_is_raw_255 = True
                self.audio_status.emit("Power meter: radio reports native unit 'raw_255' -- scaling from raw.")
            else:
                self._power_is_raw_255 = False
                self.audio_status.emit(
                    f"Power meter: native_power_unit is {native_unit!r} (expected 'watts' or "
                    "'raw_255') -- defaulting to raw-scale math; watch the reading for sanity."
                )

            # Root-caused a real "shows ~55W, radio's actually at ~5W"
            # report to METER_DEFINITIONS' hardcoded display_max=100 --
            # correct for a 100W-class rig (IC-7300/7610/9700 all have
            # [power] max_watts=100 in their own rigplane profile TOML,
            # confirmed by reading rigs/*.toml directly) but wrong for a
            # QRP-max radio: the IC-705's own profile says max_watts=10
            # (rigs/ic705.toml, [power] section), so the SAME raw meter
            # byte that means "~50% of this radio's real max" was being
            # read against a scale ten times too wide. display_max is
            # still used the same way now (the 100%-of-max-watts
            # ceiling POWER_CALIBRATION's percent gets multiplied
            # against), just via _power_is_raw_255 instead of "kind".
            if self._power_is_raw_255:
                max_watts = getattr(getattr(self.radio, "profile", None), "max_watts", None)
                if max_watts:
                    self._meter_definitions["power"]["display_max"] = max_watts

        # Current (Id) display_max (the real max current a given radio
        # draws) is hardware-specific, not a shared CI-V protocol
        # constant like raw_max is -- see CURRENT_RAW_MAX's own comment
        # in constants.py. Confirmed against the IC-705's own CI-V
        # Reference Guide ("0000=0A, 0121=2A, 0241=4A") that its real
        # max is ~4A on internal battery/12V supply, vs. the ~25A
        # default sized for a 100W-class base/mobile rig -- same per-
        # radio-correction pattern as power's max_watts above.
        if "current" in self._meter_getters and self._details.get("radio_model") == "IC-705":
            self._meter_definitions["current"]["display_max"] = 4.0

        # IC-9700's Voltage/Current curves are genuinely non-linear
        # (unlike the IC-705's, a flat display_max fix isn't enough) --
        # see VOLTAGE_CALIBRATION_IC9700/CURRENT_CALIBRATION_IC9700's own
        # comment in constants.py. Switched to "direct" (piecewise
        # interpolation applied in _poll_loop_fast, same pattern as
        # "comp") instead of "linear"; every other radio keeps the
        # existing linear raw_max/display_max math unchanged.
        if self._details.get("radio_model") == "IC-9700":
            if "voltage" in self._meter_getters:
                self._meter_definitions["voltage"]["kind"] = "direct"
                self._meter_definitions["voltage"]["display_max"] = 16.0
            if "current" in self._meter_getters:
                self._meter_definitions["current"]["kind"] = "direct"
                self._meter_definitions["current"]["display_max"] = 20.0

        # SWR's METER_DEFINITIONS entry defaults to "direct" (get_swr's
        # already-calibrated ratio) -- but if this radio/backend only
        # exposes get_swr_meter (the raw, uncalibrated byte -- see that
        # entry's own comment), switch to "linear" with a display_max
        # matching the raw byte's real documented span (rigplane's
        # legacy fallback formula, 1.0 + raw/255*8.9 -- confirmed via
        # rigplane's own runtime/meter_cal.py) instead of the
        # "direct" kind's 1.0-3.0 display window, which would badly
        # under-represent a raw reading interpreted the wrong way.
        if self._meter_getters.get("swr") == "get_swr_meter":
            self._meter_definitions["swr"]["kind"] = "linear"
            self._meter_definitions["swr"]["display_max"] = 9.9
            self.audio_status.emit(
                "SWR meter: get_swr unavailable on this radio/backend -- falling back to "
                "the raw get_swr_meter byte (uncalibrated)."
            )

    def _setup_controls(self):
        """Resolves the real getter/setter method names for every entry
        in CONTROL_DEFINITIONS via find_method_name -- same discovery
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
            set_name = find_method_name(self.radio, definition["setter_candidates"])
            if definition.get("write_only"):
                get_name = None  # deliberately never resolved/polled -- see definition's comment
                if set_name:
                    self._control_methods[key] = (get_name, set_name)
                else:
                    missing.append(f"{definition['label']} (write-only, set=none)")
            else:
                get_name = find_method_name(self.radio, definition["getter_candidates"])
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
            setter = getattr(self.radio, set_name)
            await self._call_receiver_aware(key, setter, value)
        except Exception as exc:
            self.error.emit(f"{definition['label']}: {set_name}({value!r}) failed ({exc}).")

    def set_receiver_control_value(self, receiver: int, key: str, value, restore: bool = True, vfo_slot: str = None):
        """Thread-safe: call from the GUI thread. Like set_control_value,
        but sets `key` on a SPECIFIC receiver regardless of which one is
        currently active -- e.g. setting Main's mode while Sub is the
        active/listening receiver during satellite tracking, which
        set_control_value can't do (it always targets self._active_receiver,
        i.e. whichever one the operator is currently listening to/tuning).

        restore=False (main_window.py's apply_satellite_mode passes
        this for Main's uplink mode) skips switching back to whichever
        receiver was active before -- reported live on a real 9700 that
        Main's mode change wasn't reliably landing at all with the
        immediate switch-back in place, the same "temporarily switch to
        Main, do a thing, switch right back to Sub" shape that turned
        out to be unreliable for VFO/frequency writes too (see
        start_ptt_after_vfo's docstring). Sub naturally becomes active
        again on its own within a couple of seconds regardless, via the
        RX tick loop's own continuous re-selection -- so there's nothing
        left needing an explicit, immediate switch-back here.

        vfo_slot, when given, explicitly selects that VFO A/B slot on
        `receiver` before setting `key` -- confirmed live that mode is
        stored PER VFO SLOT, not per receiver, same two-independent-axes
        shape frequency already has (see select_receiver_vfo_and_set_
        frequency's docstring). apply_satellite_mode runs at transponder
        -selection time, before PTT has ever switched Main to VFO B, so
        without this the mode was landing on whichever VFO Main
        currently had selected (normally still A, its idle/RX VFO) --
        genuinely setting mode there, just not on VFO B, the one that
        actually transmits, which kept whatever it last happened to
        remember (confirmed live: showed CW, stale from unrelated
        earlier testing) regardless of what apply_satellite_mode
        computed."""
        if self.loop is None:
            return
        if key not in self._control_methods:
            self.error.emit(
                f"{CONTROL_DEFINITIONS[key]['label']}: not available on this radio/install "
                "(no working getter+setter was found on connect)."
            )
            return
        asyncio.run_coroutine_threadsafe(
            self._set_receiver_control_value(receiver, key, value, restore, vfo_slot), self.loop
        )

    async def _set_receiver_control_value(self, receiver: int, key: str, value, restore: bool = True, vfo_slot: str = None):
        """Temporarily selects `receiver` (if it isn't already active),
        optionally selects a specific VFO slot on it, sets `key` via the
        same path _set_control_value uses, then (if restore) restores
        whichever receiver was active before -- a single bundled
        coroutine, not several separately-dispatched ones, for the same
        reason every other multi-step dual-receiver operation in this
        file is bundled: independently-scheduled run_coroutine_
        threadsafe() calls don't guarantee real-world completion order
        (see _set_receiver_frequency's docstring for the self-
        perpetuating-oscillation bug this caused the one time it wasn't
        followed). Single-receiver radios have no "other receiver" to
        switch to or restore -- vfo_slot still applies for them (there's
        exactly one receiver, but still two independent VFO slots)."""
        if not self.is_dual_receiver:
            if vfo_slot is not None and "vfo" in self._control_methods:
                await self._set_control_value("vfo", vfo_slot)
            await self._set_control_value(key, value)
            return
        # Holds the lock for the ENTIRE select-then-write sequence,
        # including the final _set_control_value() call -- that call
        # reads self._active_receiver (via _call_receiver_aware) to
        # decide which receiver= kwarg to pass, and mode in particular
        # has no receiver-specific CI-V write at all on this radio (the
        # bare command just targets whichever receiver the radio
        # CURRENTLY has selected) -- so without the lock, an interleaved
        # coroutine (e.g. the satellite tick's continuous Sub retune)
        # could both reselect Sub on the actual hardware AND overwrite
        # self._active_receiver back to Sub in between this method's own
        # select_receiver(Main) and its mode write, silently landing the
        # write on Sub instead. Confirmed live: this was still happening
        # even after removing all VFO-slot switching.
        async with self._receiver_switch_lock:
            previously_active = self._active_receiver
            if receiver != previously_active:
                try:
                    await self.radio.select_receiver(receiver)
                    self._active_receiver = receiver
                except Exception as exc:
                    self.error.emit(f"Select receiver ({receiver}) for {key} failed: {exc}")
                    return
                # Reported live on a real 9700: Main's mode still wasn't
                # landing even with the correct receiver AND VFO slot
                # selected first -- consistent with the recurring theme
                # throughout this whole dual-receiver debugging effort, that
                # rapid back-to-back CI-V writes to this radio don't
                # reliably land in the order they're sent. A brief settle
                # delay after actually switching receivers is a new
                # mitigation, not yet tried for this specific call chain.
                await asyncio.sleep(0.15)
            if vfo_slot is not None:
                try:
                    await self.radio.set_vfo_slot(vfo_slot, receiver=receiver)
                except Exception as exc:
                    self.error.emit(f"VFO slot select (receiver {receiver}) for {key} failed: {exc}")
                else:
                    await asyncio.sleep(0.15)  # same settle-time reasoning as above, before the mode write itself
            self.audio_status.emit(
                f"[dual-rx] about to set {key}={value!r} on receiver {receiver} "
                f"(active={self._active_receiver}, vfo_slot={vfo_slot!r}, "
                f"receiver-unsupported-for-write={key in self._receiver_unsupported_setters})"
            )
            await self._set_control_value(key, value)
            if restore and receiver != previously_active:
                try:
                    await self.radio.select_receiver(previously_active)
                    self._active_receiver = previously_active
                except Exception as exc:
                    self.error.emit(f"Restoring active receiver ({previously_active}) after {key} failed: {exc}")

    async def _poll_loop_fast(self):
        """Frequency, active receiver, and every confirmed-supported
        meter (see _setup_meters()) -- polled every POLL_INTERVAL_SEC,
        runs CONCURRENTLY with _poll_loop_slow (see that method's own
        docstring for why the two are split). These are the only things
        that genuinely need sub-second freshness: meters reflect live
        signal/TX conditions, frequency drives the tuning display and
        satellite tracking, and active receiver gates which receiver's
        meter this loop's own next iteration reads (see
        _receiver_kwargs below) -- staleness there would misattribute a
        meter reading to the wrong receiver, unlike everything in the
        slow loop where a few seconds' staleness is harmless."""
        while not self._stop_requested:
            if self.is_dual_receiver:
                try:
                    observed = await self.radio.get_active_receiver()
                    if observed != self._last_observed_active_receiver:
                        previous = self._last_observed_active_receiver
                        previous_label = "unknown" if previous is None else ("SUB" if previous else "MAIN")
                        self.audio_status.emit(
                            f"Active receiver observed: {'SUB' if observed else 'MAIN'} (was {previous_label})"
                        )
                        self._last_observed_active_receiver = observed
                        # Also drives the UI (main_window.py's active_
                        # receiver_button etc) into sync -- confirmed
                        # reported: on connect, if the radio was already
                        # sitting on Sub (e.g. left there from a
                        # previous session), the button still showed
                        # "Active: MAIN" until manually clicked, since
                        # nothing was reflecting the radio's real
                        # starting state. This is real, polled ground
                        # truth, so it's safe to just always reflect it.
                        self.active_receiver_changed.emit(observed)
                except Exception as exc:
                    self.error.emit(f"get_active_receiver: {exc}")

            try:
                # get_frequency's receiver param is NOT a Main/Sub
                # identity selector -- confirmed by reading rigplane's
                # own source (runtime/_dual_rx_runtime.py): receiver=0
                # means "whichever receiver is currently SELECTED" (a
                # bare, no-cmd29 CI-V read that just returns whatever's
                # active), receiver=1 means "the UNSELECTED one"
                # specifically (CI-V 0x25 0x01, a dedicated "read the
                # other receiver" command). So once Sub is genuinely
                # selected (select_receiver), asking for receiver=1
                # actually reads Main -- the OPPOSITE of what
                # self._active_receiver was meant to express. Always
                # asking for receiver=0/"selected" is what correctly
                # follows whichever receiver select_receiver() last
                # made active, regardless of whether that's genuinely
                # Main or Sub.
                freq_hz = await self.radio.get_frequency(receiver=RECEIVER_MAIN)
                self.frequency_updated.emit(freq_hz)
            except Exception as exc:
                self.error.emit(str(exc))

            for meter_type, getter_name in self._meter_getters.items():
                definition = self._meter_definitions[meter_type]
                try:
                    if meter_type == "swr":
                        # SWR is only meaningful during TX -- see
                        # _ptt_active's own comment for why this skips
                        # polling entirely (rather than just displaying)
                        # while not transmitting, and emits exactly one
                        # defined at-rest reading right on PTT release.
                        if not self._ptt_active:
                            if self._swr_reset_pending:
                                self._swr_reset_pending = False
                                self.meter_updated.emit("swr", definition.get("display_min", 0.0))
                            continue
                        if self.is_ic705_swr_workaround:
                            # Raw CI-V 0x15/0x12 read via send_civ(), NOT
                            # rigplane's own get_swr() -- see SWR_CIV_SUB's
                            # comment in constants.py for why.
                            resp = await self.radio.send_civ(0x15, sub=SWR_CIV_SUB, wait_response=True)
                            if resp is None or not resp.data:
                                continue
                            value = _interpolate_swr(_bcd_decode_pbt_level(resp.data))
                            is_calibrated_reading = True
                        else:
                            getter = getattr(self.radio, getter_name)
                            kwargs = (
                                self._receiver_kwargs(getter, self._active_receiver)
                                if self.is_dual_receiver
                                else {}
                            )
                            value = await getter(**kwargs)
                            # Only rigplane's own get_swr() (kind
                            # "direct") returns a real calibrated ratio;
                            # the get_swr_meter() raw-byte fallback
                            # ("linear") is NOT safe to compare against
                            # SWR_PROTECTION_THRESHOLD -- 2.5 as a raw
                            # 0-255 byte is nowhere near a real fault.
                            is_calibrated_reading = definition["kind"] == "direct"
                        if (
                            is_calibrated_reading
                            and self._swr_protection_enabled
                            and not self._tuner_active
                            and value > SWR_PROTECTION_THRESHOLD
                        ):
                            try:
                                await self.radio.set_ptt(False)
                            except Exception as exc:
                                self.error.emit(
                                    f"SWR protection: failed to release PTT ({exc}) -- radio may still be keyed!"
                                )
                            self._ptt_active = False
                            self._swr_reset_pending = True
                            self.swr_protection_tripped.emit(value)
                    elif meter_type == "power" and self._power_is_raw_255:
                        # POWER_CALIBRATION (percent of max, piecewise
                        # non-linear) converted straight to watts using
                        # this radio's own max_watts -- see
                        # _setup_meters()'s own comment for why this
                        # isn't just a raw/255 ratio.
                        getter = getattr(self.radio, getter_name)
                        kwargs = self._receiver_kwargs(getter, self._active_receiver) if self.is_dual_receiver else {}
                        raw = await getter(**kwargs)
                        percent = _interpolate_calibration(raw, POWER_CALIBRATION)
                        value = percent / 100.0 * definition["display_max"]
                    elif meter_type == "comp":
                        # COMP_CALIBRATION -- a genuinely non-linear dB
                        # reading, not the 0-100% this app used to show
                        # it as -- see that constant's own comment in
                        # constants.py.
                        getter = getattr(self.radio, getter_name)
                        kwargs = self._receiver_kwargs(getter, self._active_receiver) if self.is_dual_receiver else {}
                        raw = await getter(**kwargs)
                        value = _interpolate_calibration(raw, COMP_CALIBRATION)
                    elif meter_type in ("voltage", "current") and definition["kind"] == "direct":
                        # IC-9700-only path (_setup_meters() only flips
                        # these to "direct" for that model) -- applies
                        # VOLTAGE_CALIBRATION_IC9700/CURRENT_CALIBRATION_
                        # IC9700 (genuinely non-linear, see their own
                        # comment in constants.py), same piecewise
                        # pattern as "comp" above.
                        getter = getattr(self.radio, getter_name)
                        kwargs = self._receiver_kwargs(getter, self._active_receiver) if self.is_dual_receiver else {}
                        raw = await getter(**kwargs)
                        table = VOLTAGE_CALIBRATION_IC9700 if meter_type == "voltage" else CURRENT_CALIBRATION_IC9700
                        value = _interpolate_calibration(raw, table)
                    else:
                        getter = getattr(self.radio, getter_name)
                        kwargs = self._receiver_kwargs(getter, self._active_receiver) if self.is_dual_receiver else {}
                        value = await getter(**kwargs)
                    self.meter_updated.emit(meter_type, value)
                except Exception as exc:
                    self.error.emit(f"{definition['label']}: {exc}")

            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def _poll_loop_slow(self):
        """Scope span/ref/speed, CW keyer speed/pitch, tuner status,
        every CONTROL_DEFINITIONS-based control (mode/AGC/filter/NR/NB/
        VFO/split/RIT/XIT/etc), every LEVEL_DEFINITIONS-based level,
        PBT, filter width, filter shape, and (IC-705 only) preamp/
        attenuator -- everything that only changes via a user action,
        either THROUGH THIS APP (where the UI already reflects the new
        value locally/immediately -- see _update_pbt_overlay et al.,
        _on_control_toggled/_on_control_combo_changed) or on the
        radio's own front panel (where a few seconds of latency before
        this app notices is a fine tradeoff) -- unlike frequency/meters,
        which genuinely need every-cycle freshness (see
        _poll_loop_fast).

        Runs as its own asyncio task, concurrently with _poll_loop_fast,
        on a separate SLOW_POLL_INTERVAL_SEC cadence -- confirmed live
        that folding all of this into ONE shared-cadence loop made the
        SWR meter (and others) sit behind a long sequential chain of
        ~20 settings reads every cycle and lag several seconds behind a
        real PTT press. Splitting the loops means the fast loop's own
        requests no longer have to wait for this loop's entire backlog
        to finish first. Our own raw send_civ() calls here also pass
        Priority.BACKGROUND, rigplane's own documented mechanism for
        exactly this ("background pollers pass Priority.BACKGROUND so
        polls yield to user commands") -- rigplane's higher-level
        convenience methods (get_mode, get_filter_width, etc.) don't
        expose a priority knob to pass through, so only our own bespoke
        raw-CI-V calls (PBT/filter shape/preamp/attenuator) get this."""
        while not self._stop_requested:
            if self.is_scope_capable:
                # Reflects the scope's real settings -- e.g. hand-adjusted
                # from the radio's own front panel -- same change-filtered
                # polling approach as active_receiver above. Cheap: three
                # more instant CI-V reads per cycle.
                try:
                    span = await self.radio.get_scope_span()
                    if span != self._last_observed_scope_span:
                        self._last_observed_scope_span = span
                        self.scope_span_changed.emit(span)
                except Exception as exc:
                    self.error.emit(f"get_scope_span: {exc}")
                try:
                    ref_db = await self.radio.get_scope_ref()
                    if ref_db != self._last_observed_scope_ref:
                        self._last_observed_scope_ref = ref_db
                        self.scope_ref_changed.emit(ref_db)
                except Exception as exc:
                    self.error.emit(f"get_scope_ref: {exc}")
                try:
                    speed = await self.radio.get_scope_speed()
                    if speed != self._last_observed_scope_speed:
                        self._last_observed_scope_speed = speed
                        self.scope_speed_changed.emit(speed)
                except Exception as exc:
                    self.error.emit(f"get_scope_speed: {exc}")

            # CW keyer speed/pitch -- same change-filtered polling as
            # scope span/ref/speed above, but unconditional (no
            # capability flag: get_key_speed/get_cw_pitch are plain
            # Radio methods, not gated behind a Capable protocol like
            # scope/audio are, and confirmed present for every radio
            # profile this app targets).
            try:
                key_speed = await self.radio.get_key_speed()
                if key_speed != self._last_observed_key_speed:
                    self._last_observed_key_speed = key_speed
                    self.key_speed_changed.emit(key_speed)
            except Exception as exc:
                self.error.emit(f"get_key_speed: {exc}")
            try:
                cw_pitch = await self.radio.get_cw_pitch()
                if cw_pitch != self._last_observed_cw_pitch:
                    self._last_observed_cw_pitch = cw_pitch
                    self.cw_pitch_changed.emit(cw_pitch)
            except Exception as exc:
                self.error.emit(f"get_cw_pitch: {exc}")

            # Antenna tuner status (0=off, 1=on, 2=tuning) -- same
            # change-filtered polling as key_speed/cw_pitch above.
            # Unlike those, not every profile actually declares the
            # "tuner" capability (FTX-1's is a different CAT command
            # rigplane doesn't map to get_tuner_status), so a bare
            # except here just means the button never lights up on
            # radios without one, rather than spamming the error log
            # every poll cycle.
            try:
                tuner_status = await self.radio.get_tuner_status()
                # Authoritative correction for _tuner_active (SWR
                # protection's tuning gate, see SWR_PROTECTION_
                # THRESHOLD's comment) -- catches a front-panel-
                # initiated tune _start_tuner()'s own optimistic set
                # can't see, and clears it once tuning genuinely
                # finishes (whether this app or the front panel started
                # it). Unconditional, not just on change, so it can't
                # get stuck stale if the very first poll after tuning
                # starts happens to already read status==2.
                self._tuner_active = tuner_status == 2
                if tuner_status != self._last_observed_tuner_status:
                    self._last_observed_tuner_status = tuner_status
                    self.tuner_status_changed.emit(tuner_status)
            except Exception:
                pass

            for key, (get_name, _set_name) in self._control_methods.items():
                if get_name is None:
                    continue  # write-only control (e.g. memory_mode) -- no GET variant exists, never poll
                if key == "split" and self.is_dual_receiver and self._active_receiver == RECEIVER_SUB:
                    # split is rig-global, not per-receiver (confirmed
                    # via rigplane's own SplitCapable docstring) -- but
                    # confirmed live on a real 9700 that get_split()
                    # returns an unstable/flickering value while Sub is
                    # the active receiver, making the split button
                    # visibly flip on and off every poll cycle on its
                    # own. Makes sense: Sub never transmits at all
                    # (confirmed hardware limitation, PTT always
                    # transmits from Main), so "is split on" isn't a
                    # question with a stable answer for it. Skip polling
                    # it entirely in that state -- the button just keeps
                    # showing whatever it last correctly reflected from
                    # when Main was active.
                    continue
                definition = CONTROL_DEFINITIONS[key]
                try:
                    getter = getattr(self.radio, get_name)
                    if key == "mode":
                        # get_mode's receiver param has the exact same
                        # "selected(0)/unselected(1)" semantic as
                        # get_frequency (confirmed by reading rigplane's
                        # source -- both route through _get_..._main /
                        # _get_unselected_... in the same way) -- NOT a
                        # Main/Sub identity selector. receiver=0 means
                        # "whichever receiver is currently selected",
                        # which is what should always be asked for here.
                        value = await getter(receiver=RECEIVER_MAIN)
                    else:
                        # See _receiver_kwargs's/_poll_receiver_aware's
                        # docstrings -- confirmed live on a real 9700
                        # that "vfo" alone wasn't the only control
                        # causing the flip-flop, and that AGC/Preamp's
                        # getters outright reject a non-Main receiver at
                        # runtime rather than just ignoring it.
                        value = await self._poll_receiver_aware(key, getter)
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
                    if key == "mode" and self.is_filter_width_capable and value != self._filter_width_range_mode:
                        self._filter_width_range_mode = value
                        self.filter_width_range_updated.emit(self._resolve_filter_width_range(value))
                except Exception as exc:
                    self.error.emit(f"{definition['label']}: {exc}")

            for key, (get_name, _set_name) in self._level_methods.items():
                definition = LEVEL_DEFINITIONS[key]
                getter = getattr(self.radio, get_name)
                try:
                    # AF gain/squelch/RF gain's SETTERS don't actually
                    # respect receiver= at all -- confirmed live on a
                    # real 9700 that setting always affects whichever
                    # receiver is active (select_receiver()), not
                    # self._active_receiver -- but their GETTERS are
                    # worse than just "ignored": confirmed live to raise
                    # CommandError for a non-Main receiver outright.
                    # _poll_receiver_aware handles that (see its
                    # docstring); see main_window.py's active_receiver_
                    # button/_level_receiver_suffix for how the UI
                    # reflects which receiver these end up affecting.
                    raw_value = await self._poll_receiver_aware(key, getter)
                    self.level_updated.emit(key, self._normalize_level_value(raw_value))
                except Exception as exc:
                    self.error.emit(f"{definition['label']}: {exc}")

            for key, civ_sub in self._pbt_methods.items():
                definition = PBT_DEFINITIONS[key]
                try:
                    # Raw CI-V 0x14/<civ_sub> read via send_civ(), NOT
                    # rigplane's own get_pbt_inner/get_pbt_outer -- see
                    # PBT_DEFINITIONS' comment in constants.py for why.
                    resp = await self.radio.send_civ(
                        0x14, sub=civ_sub, wait_response=True, priority=Priority.BACKGROUND
                    )
                    if resp is not None and len(resp.data) >= 2:
                        self.pbt_updated.emit(key, _bcd_decode_pbt_level(resp.data))
                except Exception as exc:
                    self.error.emit(f"{definition['label']}: {exc}")

            if self.is_filter_width_capable:
                try:
                    width_hz = await self.radio.get_filter_width()
                    self._filter_width_hz = width_hz
                    self.filter_width_updated.emit(width_hz)
                except Exception as exc:
                    self.error.emit(f"Filter width: {exc}")

            if self._filter_shape_available:
                try:
                    # Raw CI-V 0x16/0x56 read via send_civ(), NOT
                    # rigplane's own get_filter_shape() -- see
                    # FILTER_SHAPE_CIV_SUB's comment in constants.py.
                    resp = await self.radio.send_civ(
                        0x16, sub=FILTER_SHAPE_CIV_SUB, wait_response=True, priority=Priority.BACKGROUND
                    )
                    if resp is not None and resp.data:
                        self.filter_shape_updated.emit(resp.data[0])
                except Exception as exc:
                    self.error.emit(f"Filter shape: {exc}")

            if self.is_ic705_preamp_att:
                try:
                    # Raw CI-V reads via send_civ(), NOT rigplane's own
                    # get_preamp()/attenuator methods -- see
                    # PREAMP_CIV_SUB's comment in constants.py.
                    preamp_resp = await self.radio.send_civ(
                        0x16, sub=PREAMP_CIV_SUB, wait_response=True, priority=Priority.BACKGROUND
                    )
                    atten_resp = await self.radio.send_civ(
                        ATT_CIV_COMMAND, wait_response=True, priority=Priority.BACKGROUND
                    )
                    if preamp_resp is not None and preamp_resp.data and atten_resp is not None and atten_resp.data:
                        self.preamp_att_updated.emit(
                            _bcd_decode_byte(preamp_resp.data), _bcd_decode_byte(atten_resp.data)
                        )
                except Exception as exc:
                    self.error.emit(f"Preamp/Attenuator: {exc}")

            await asyncio.sleep(SLOW_POLL_INTERVAL_SEC)

    # ---- Thread-safe command entry points (call these from the GUI thread) ----

    def is_connected(self):
        """Thread-safe: call from the GUI thread. True once the radio
        connection is actually up, not just while it's being attempted."""
        return self.radio is not None

    def control_available(self, key: str) -> bool:
        """Thread-safe: call from the GUI thread. True once a working
        getter+setter for this CONTROL_DEFINITIONS key was actually found
        on this radio/install at connect time -- lets a caller check
        before relying on set_control_value()/select_vfo_and_set_frequency()
        actually doing anything, rather than only finding out from an
        error message after the fact (both controls this matters most
        for, "vfo" and "split", have setter names that were never fully
        confirmed against real hardware -- see their CONTROL_DEFINITIONS
        entries in constants.py)."""
        return key in self._control_methods

    def set_frequency(self, freq_hz: int, check_conflict: bool = True):
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_frequency(freq_hz, check_conflict), self.loop)

    async def _set_frequency(self, freq_hz: int, check_conflict: bool = True):
        # This method has no receiver parameter at all -- both of its
        # callers (the tuning knob's Main-active branch, and rigctld's
        # CAT frequency-set callback for external apps like WSJT-X) are
        # conceptually always about Main (or the sole VFO on a single-
        # receiver radio; Sub always goes through the receiver-aware
        # select_receiver_vfo_and_set_frequency path instead, which
        # already resolves this itself). Without this, switching Main to
        # a band Sub currently occupies was simply rejected outright by
        # the radio. No-op on a single-receiver radio
        # (_resolve_receiver_band_conflict's own guard).
        #
        # check_conflict=False (threaded through from select_vfo_and_
        # set_frequency/start_ptt_after_vfo, satellite PTT specifically)
        # skips it -- confirmed live this exact check, reached via THIS
        # path (start_ptt_after_vfo -> _select_vfo_and_set_frequency ->
        # here), was still corrupting Sub during satellite TX even after
        # the OTHER two call sites (the tick loop, and start_ptt_after_
        # vfo's own now-removed top-level call) were fixed -- this one
        # was the one still actually firing every PTT press all along.
        if check_conflict:
            await self._resolve_receiver_band_conflict(RECEIVER_MAIN, freq_hz)
        try:
            # Explicit receiver=RECEIVER_MAIN, not a bare call -- per
            # rigplane's own set_freq() source (runtime/radio.py),
            # receiver=RECEIVER_MAIN(0) always routes straight to its
            # internal _set_frequency_main(), a DIFFERENT, unconditional
            # code path from the receiver=SUB branch (which goes through
            # cmd29/fallback addressing instead) -- it does not depend
            # on which receiver the radio currently considers "active" at
            # all. A bare call (previously here) relies on select_
            # receiver_band_conflict having correctly restored Main as
            # active moments earlier, on real hardware, before this next
            # await even runs -- confirmed live on a real 9700 that this
            # was NOT reliable (a real "Main still doesn't reach the band
            # Sub was on" report persisted even after that restore was
            # added), the same "separately-dispatched receiver-state-
            # dependent commands aren't guaranteed to land in order"
            # class of bug already documented at length elsewhere in this
            # file (see select_vfo_and_set_frequency's own docstring).
            # Addressing Main explicitly sidesteps the ordering question
            # entirely instead of trying to win the race.
            await self.radio.set_frequency(freq_hz, receiver=RECEIVER_MAIN)
        except Exception as exc:
            self.error.emit(str(exc))

    def select_receiver(self, receiver: int):
        """Thread-safe: call from the GUI thread. Dual-receiver only --
        makes `receiver` the radio's active receiver via rigplane's
        select_receiver(), confirmed via its own docstring to issue the
        real main_select/sub_select CI-V opcode (0x07 0xD0/0xD1) and
        update RadioState.active. Genuinely different from addressing a
        specific receiver via receiver= on set_frequency/set_vfo_slot/
        etc elsewhere in this file -- those write into that receiver's
        own registers without necessarily making it "active" for
        anything else. Confirmed live on a real 9700 (manual toggle via
        active_receiver_button, main_window.py, then testing PTT by
        hand either way) plus independently confirmed by Icom's own
        documented behavior: PTT always transmits from Main regardless
        of which receiver is active -- a real hardware limitation, not
        something addressable from software at all. Kept as a manual
        control for whatever else "active receiver" turns out to affect
        (e.g. the scope -- see set_scope_receiver below).

        Also updates self._active_receiver to match -- now that sat
        mode only ever writes a frequency to whichever receiver is
        genuinely active (main_window.py's _on_satellite_tracking_tick,
        never both in the same tick any more), keeping this in sync
        means every other receiver-aware getter/setter (_receiver_
        kwargs) -- frequency, mode, etc -- correctly follows along too,
        e.g. so the frequency readout reflects Sub's live downlink
        while receiving and Main's live uplink while transmitting,
        instead of a stale value from whichever it stopped matching."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._select_receiver(receiver), self.loop)

    async def _select_receiver(self, receiver: int):
        try:
            await self.radio.select_receiver(receiver)
            self.audio_status.emit(f"[dual-rx] select_receiver({receiver}) OK")
        except Exception as exc:
            self.error.emit(f"Select active receiver ({receiver}) failed: {exc}")
        self._active_receiver = receiver

    def set_scope_receiver(self, receiver: int):
        """Thread-safe: call from the GUI thread. Dual-receiver only --
        rigplane's set_scope_receiver(), confirmed via its own docstring
        to select which receiver's spectrum/waterfall data the radio
        streams (0=MAIN, 1=SUB) -- independent of select_receiver()
        above; switching the active receiver doesn't also move the
        scope. Called alongside select_receiver() (main_window.py's
        _on_active_receiver_toggle_clicked) so the scope/waterfall
        follows whichever receiver the toggle switches to, e.g. showing
        Sub's downlink (to see your own signal) while Main handles TX."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_scope_receiver(receiver), self.loop)

    async def _set_scope_receiver(self, receiver: int):
        try:
            await self.radio.set_scope_receiver(receiver)
            # Tracked locally so is_scope_ref_workaround's raw CI-V bypass
            # (_set_scope_ref) can send the correct 00=Main/01=Sub prefix
            # byte instead of assuming Main -- rigplane's own ScopeControlsState
            # doesn't expose this back out for us to read.
            self._scope_receiver = receiver
        except Exception as exc:
            self.error.emit(f"Select scope receiver ({receiver}) failed: {exc}")

    def set_scope_span(self, span_index: int):
        """Thread-safe: call from the GUI thread. rigplane's
        set_scope_span() takes a PRESET INDEX, not a raw Hz value --
        confirmed via rigplane's own commands/scope.py:
        _SCOPE_SPAN_PRESETS_HZ = (2500, 5000, 10000, 25000, 50000,
        100000, 250000, 500000), i.e. index 0-7. Applies to whichever
        receiver is currently the scope receiver (set_scope_receiver),
        not something this call addresses separately -- confirmed via
        rigplane's runtime source, span/ref/speed all read
        self._scope_controls().receiver internally rather than taking
        their own receiver= kwarg."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_scope_span(span_index), self.loop)

    async def _set_scope_span(self, span_index: int):
        try:
            await self.radio.set_scope_span(span_index)
            self._last_observed_scope_span = span_index
        except Exception as exc:
            self.error.emit(f"Set scope span ({span_index}) failed: {exc}")

    def set_key_speed(self, wpm: int):
        """Thread-safe: call from the GUI thread. CW keyer speed in
        WPM -- confirmed range 6-48 (rigplane's own get_key_speed()/
        set_key_speed() BCD encoding, commands/levels.py)."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_key_speed(wpm), self.loop)

    async def _set_key_speed(self, wpm: int):
        try:
            await self.radio.set_key_speed(wpm)
            self._last_observed_key_speed = wpm
        except Exception as exc:
            self.error.emit(f"Set key speed ({wpm}) failed: {exc}")

    def set_cw_pitch(self, pitch_hz: int):
        """Thread-safe: call from the GUI thread. CW sidetone pitch in
        Hz -- confirmed range 300-900 (rigplane's own get_cw_pitch()/
        set_cw_pitch() BCD encoding, commands/levels.py). Also the
        frequency a CW decode session should listen at: it's both the
        operator's own sidetone AND, assuming the far station is
        roughly zero-beat, the frequency their signal appears at in
        the receiver's passband."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_cw_pitch(pitch_hz), self.loop)

    async def _set_cw_pitch(self, pitch_hz: int):
        try:
            await self.radio.set_cw_pitch(pitch_hz)
            self._last_observed_cw_pitch = pitch_hz
        except Exception as exc:
            self.error.emit(f"Set CW pitch ({pitch_hz}) failed: {exc}")

    def set_scope_ref(self, ref_db: float):
        """Thread-safe: call from the GUI thread. -30.0 to +10.0 dB in
        0.5 dB steps on most radios (rigplane's own set_scope_ref(),
        confirmed via commands/scope.py's _scope_ref_encode()) -- except
        the models in SCOPE_REF_WIDE_RANGE_MODELS (IC-705, IC-9700),
        whose real range is -20.0 to +20.0 dB and get bypassed with a
        local encoder instead (see is_scope_ref_workaround /
        SCOPE_REF_WIDE_RANGE_MODELS' comment in constants.py)."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_scope_ref(ref_db), self.loop)

    async def _set_scope_ref(self, ref_db: float):
        try:
            if self.is_scope_ref_workaround:
                # Raw CI-V 0x27/0x19 write via send_civ(), NOT
                # rigplane's own set_scope_ref() -- see
                # SCOPE_REF_WIDE_RANGE_MODELS' comment in constants.py for
                # why. The leading prefix byte is a required receiver-
                # index (0=MAIN, 1=SUB) that rigplane's own
                # set_scope_ref() always adds too -- confirmed by reading
                # its _scope_payload() helper: ScopeControlsState.receiver
                # defaults to 0 (a real int, never None) even for a
                # single-receiver radio, so that prefix byte is
                # unconditional. Confirmed live this was the actual bug
                # on IC-705: omitting it meant the write silently did
                # nothing (malformed frame, no error either) while reads
                # kept working fine through rigplane's own unmodified
                # get_scope_ref(), which includes the same prefix on its
                # own get path. self._scope_receiver (kept in sync by
                # set_scope_receiver()) picks the right byte for a real
                # dual-receiver radio like the IC-9700 instead of
                # hardcoding Main.
                await self.radio.send_civ(
                    0x27,
                    sub=SCOPE_REF_CIV_SUB,
                    data=bytes([self._scope_receiver]) + _encode_scope_ref(ref_db),
                    wait_response=False,
                )
            else:
                await self.radio.set_scope_ref(ref_db)
            self._last_observed_scope_ref = ref_db
        except Exception as exc:
            self.error.emit(f"Set scope ref ({ref_db}) failed: {exc}")

    def set_scope_speed(self, speed_index: int):
        """Thread-safe: call from the GUI thread. rigplane's
        set_scope_speed() -- confirmed via commands/scope.py's
        scope_set_speed(): 0=fast, 1=mid, 2=slow."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_scope_speed(speed_index), self.loop)

    async def _set_scope_speed(self, speed_index: int):
        try:
            await self.radio.set_scope_speed(speed_index)
            self._last_observed_scope_speed = speed_index
        except Exception as exc:
            self.error.emit(f"Set scope speed ({speed_index}) failed: {exc}")

    def set_dual_receiver_linking(self, on: bool):
        """Thread-safe: call from the GUI thread. Dual-receiver only --
        disables (or enables) two RADIO-SIDE features that automatically
        link/alternate Main and Sub on their own, independent of
        anything this app commands: dual watch (rigplane's
        set_dual_watch(), CI-V 0x07 0xC0/0xC1 -- alternates listening
        between Main and Sub on a schedule, a real Icom feature for
        casually monitoring two frequencies with one ear) and Main/Sub
        tracking (set_main_sub_tracking() -- links Sub's tuning to
        Main's). Confirmed live on a real 9700 that the active receiver
        kept oscillating between Main and Sub even after every
        software-side fix that could plausibly cause that (redundant
        vs. one-time select_receiver() calls, either way) -- strongly
        suggesting the radio itself, not this app's own commands, was
        doing the switching the whole time. Disabled when satellite
        mode starts (_start_satellite_tracking) since either feature
        would directly fight this app's own from-scratch Main=uplink/
        Sub=downlink management; failures are reported but don't block
        satellite mode from proceeding (the radio may not support one
        or both -- e.g. an IC-7610 profile might differ from the
        9700's)."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_dual_receiver_linking(on), self.loop)

    async def _set_dual_receiver_linking(self, on: bool):
        try:
            await self.radio.set_dual_watch(on)
            self.audio_status.emit(f"[dual-rx] set_dual_watch({on}) OK")
        except Exception as exc:
            self.error.emit(f"Dual watch ({'on' if on else 'off'}) failed: {exc}")
        try:
            await self.radio.set_main_sub_tracking(on)
            self.audio_status.emit(f"[dual-rx] set_main_sub_tracking({on}) OK")
        except Exception as exc:
            self.error.emit(f"Main/Sub tracking ({'on' if on else 'off'}) failed: {exc}")

    def quick_split(self):
        """Thread-safe: call from the GUI thread. One-shot split trigger
        (rigplane's quick_split(), CI-V 0x1A 0x05 0x00 0x33, fire-and-
        forget -- confirmed via its own docstring/source that it expects
        no ACK/NAK response). Used by split_dialog.py's Quick Split
        button."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._quick_split(), self.loop)

    async def _quick_split(self):
        try:
            await self.radio.quick_split()
            self.audio_status.emit("[split] quick_split() OK")
        except Exception as exc:
            self.error.emit(f"Quick split failed: {exc}")

    def equalize_main_sub(self):
        """Thread-safe: call from the GUI thread. Dual-receiver only --
        copies Main's VFO state (frequency/mode) onto Sub via rigplane's
        equalize_main_sub(). Used by split_dialog.py's "M=>S" button."""
        if self.loop is None or self.radio is None or not self.is_dual_receiver:
            return
        asyncio.run_coroutine_threadsafe(self._equalize_main_sub(), self.loop)

    async def _equalize_main_sub(self):
        try:
            await self.radio.equalize_main_sub()
            self.audio_status.emit("[dual-rx] equalize_main_sub() OK")
        except Exception as exc:
            self.error.emit(f"Equalize Main->Sub failed: {exc}")

    def swap_main_sub(self):
        """Thread-safe: call from the GUI thread. Dual-receiver only --
        swaps Main and Sub's VFO frequencies via rigplane's
        swap_main_sub(). Used by split_dialog.py's "Swap M/S" button."""
        if self.loop is None or self.radio is None or not self.is_dual_receiver:
            return
        asyncio.run_coroutine_threadsafe(self._swap_main_sub(), self.loop)

    async def _swap_main_sub(self):
        try:
            await self.radio.swap_main_sub()
            self.audio_status.emit("[dual-rx] swap_main_sub() OK")
        except Exception as exc:
            self.error.emit(f"Swap Main/Sub failed: {exc}")

    def set_repeater_tone_settings(self, mode: str, freq_hz: float):
        """Thread-safe: call from the GUI thread. mode is "none"/"tone"/
        "tsql" -- matches wfview's own repeater tone type model (None/
        Transmit Tone Only/Tone Squelch), the closest real-radio-
        accurate mapping onto rigplane's two independent bools
        (get/set_repeater_tone -- CI-V 0x16 0x42, transmit-tone-encode
        on/off -- and get/set_repeater_tsql -- CI-V 0x16 0x43, tone-
        squelch on/off): "tone" sets repeater_tone on/tsql off,
        "tsql" sets repeater_tone off/tsql on (wfview's own docs: "Tone
        Squelch: A tone is transmitted, and the same tone frequency is
        used as a tone squelch" -- TSQL already implies tone
        transmission, no need for both flags on at once), "none" clears
        both. Always targets Main (RECEIVER_MAIN) regardless of which
        receiver is currently active -- same reasoning as split itself
        only ever operating against Main's VFO A/B pair (PTT always
        transmits from Main, a confirmed real hardware limitation).

        Bundled into one coroutine (frequency writes, then both on/off
        flags) rather than several independently-dispatched calls --
        same non-guaranteed-ordering reasoning documented throughout
        this file for every other multi-step radio operation (see
        _set_receiver_control_value's docstring). freq_hz is written to
        BOTH set_tone_freq (0x1B 0x00, the transmit/TONE-mode
        frequency) and set_tsql_freq (0x1B 0x01, the TSQL-mode
        frequency) whenever mode != "none" -- confirmed via rigplane's
        own source that these are two independently addressable CI-V
        registers, not one shared value, and there's no way to know
        from here which one a given mode change will actually read from
        without live hardware to test against, so both are kept in sync
        rather than guessing only one matters."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_repeater_tone_settings(mode, freq_hz), self.loop)

    async def _set_repeater_tone_settings(self, mode: str, freq_hz: float):
        receiver = RECEIVER_MAIN
        if mode != "none":
            try:
                await self.radio.set_tone_freq(freq_hz, receiver=receiver)
                self.audio_status.emit(f"[tone] set_tone_freq({freq_hz}) OK")
            except Exception as exc:
                self.error.emit(f"Set tone frequency failed: {exc}")
            try:
                await self.radio.set_tsql_freq(freq_hz, receiver=receiver)
                self.audio_status.emit(f"[tone] set_tsql_freq({freq_hz}) OK")
            except Exception as exc:
                self.error.emit(f"Set TSQL frequency failed: {exc}")
        try:
            await self.radio.set_repeater_tone(mode == "tone", receiver=receiver)
            self.audio_status.emit(f"[tone] set_repeater_tone({mode == 'tone'}) OK")
        except Exception as exc:
            self.error.emit(f"Set repeater tone failed: {exc}")
        try:
            await self.radio.set_repeater_tsql(mode == "tsql", receiver=receiver)
            self.audio_status.emit(f"[tone] set_repeater_tsql({mode == 'tsql'}) OK")
        except Exception as exc:
            self.error.emit(f"Set repeater TSQL failed: {exc}")

    def set_rit_frequency(self, offset_hz: int):
        """Thread-safe: call from the GUI thread. Sets the RIT offset
        (rigplane's set_rit_frequency(), +-9999 Hz, fire-and-forget) --
        used by the RIT knob (main_window.py's rit_knob/_on_rit_knob_
        steps), which tracks the running offset itself (a relative
        encoder, same shape as the main tuning knob) and just sends the
        new absolute value here each step, rather than round-tripping a
        read first."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_rit_frequency(offset_hz), self.loop)

    async def _set_rit_frequency(self, offset_hz: int):
        try:
            await self.radio.set_rit_frequency(offset_hz)
        except Exception as exc:
            self.error.emit(f"Set RIT frequency ({offset_hz} Hz) failed: {exc}")

    def capture_memory_snapshot(self):
        """Thread-safe: call from the GUI thread. Used by memories_
        window.py's "Add Memory": reads VFO A's and VFO B's frequency
        and mode for whichever receiver is currently active (Main or
        Sub on a dual-receiver radio -- there's no separate "which
        receiver" question on a single-receiver radio) plus the
        current repeater tone settings, restores whichever VFO slot
        was originally selected when done, and emits memory_snapshot_
        captured with the result ({"A": {...}, "B": {...}, "tone":
        {...}}, "A"/"B" each a {"freq_hz", "mode", "filter"} dict and
        "tone" a {"mode": "none"/"tone"/"tsql", "freq_hz"} dict) or
        None on failure."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._capture_memory_snapshot(), self.loop)

    async def _capture_memory_snapshot(self):
        if "vfo" not in self._control_methods:
            self.error.emit("Memories: VFO A/B control not available on this radio/install -- can't capture a snapshot.")
            self.memory_snapshot_captured.emit(None)
            return
        get_name, set_name = self._control_methods["vfo"]
        getter = getattr(self.radio, get_name)
        setter = getattr(self.radio, set_name)
        try:
            previous_slot = await self._poll_receiver_aware("vfo", getter)
        except Exception as exc:
            self.error.emit(f"Memories: couldn't read the current VFO selection ({exc}) -- snapshot cancelled.")
            self.memory_snapshot_captured.emit(None)
            return

        snapshot = {}
        failed = False
        for slot in ("A", "B"):
            try:
                await self._call_receiver_aware("vfo", setter, slot)
                # Same settle delay used everywhere else in this file
                # between a VFO-slot select and a read/write that
                # depends on it having actually landed (see
                # _set_receiver_control_value's docstring).
                await asyncio.sleep(0.15)
                # get_frequency/get_mode's receiver=RECEIVER_MAIN(0)
                # means "whichever receiver is currently SELECTED", not
                # literally Main -- same quirk documented at length in
                # _poll_loop_fast above. So this always reads the genuinely
                # active receiver's VFO A/B, whether that's really Main
                # or Sub, with no extra receiver-identity bookkeeping
                # needed here.
                freq_hz = await self.radio.get_frequency(receiver=RECEIVER_MAIN)
                mode_name, filt = await self.radio.get_mode(receiver=RECEIVER_MAIN)
                snapshot[slot] = {"freq_hz": freq_hz, "mode": mode_name, "filter": filt}
            except Exception as exc:
                self.error.emit(f"Memories: failed to capture VFO {slot}: {exc}")
                failed = True
                break

        # Tone read failure is soft -- doesn't invalidate the VFO A/B
        # capture above, which is the more fundamental data and more
        # likely to be supported on any given radio/profile. Always
        # targets Main (same simplification split_dialog.py's own tone
        # settings already use -- see set_repeater_tone_settings),
        # since split/repeater-tone-relevant TX only ever happens on
        # Main on a dual-receiver radio.
        if not failed:
            tone = {"mode": "none", "freq_hz": None}
            try:
                tone_on = await self.radio.get_repeater_tone(receiver=RECEIVER_MAIN)
                tsql_on = await self.radio.get_repeater_tsql(receiver=RECEIVER_MAIN)
                if tsql_on:
                    tone["mode"] = "tsql"
                    tone["freq_hz"] = await self.radio.get_tsql_freq(receiver=RECEIVER_MAIN)
                elif tone_on:
                    tone["mode"] = "tone"
                    tone["freq_hz"] = await self.radio.get_tone_freq(receiver=RECEIVER_MAIN)
            except Exception as exc:
                self.error.emit(f"Memories: couldn't read repeater tone settings ({exc}) -- memory saved without tone info.")
            snapshot["tone"] = tone

        try:
            await self._call_receiver_aware("vfo", setter, previous_slot)
        except Exception as exc:
            self.error.emit(f"Memories: failed to restore the original VFO selection ({previous_slot}): {exc}")

        self.memory_snapshot_captured.emit(None if failed else snapshot)

    def recall_memory_snapshot(self, snapshot):
        """Thread-safe: call from the GUI thread. The inverse of
        capture_memory_snapshot() -- writes a previously-captured (or
        CSV-imported) {"A": {"freq_hz", "mode", "filter"}, "B": {...},
        "tone": {"mode", "freq_hz"}} snapshot back to the radio: VFO
        A's and VFO B's frequency/mode, then the repeater tone
        settings, then leaves VFO A selected (the normal operating
        VFO) when done. Used by memories_window.py's "Recall" button.

        Deliberately as simple/symmetric as capture_memory_snapshot
        itself -- same "whichever receiver is currently active" scope
        (RECEIVER_MAIN meaning "currently selected", not literally
        Main -- see that method's own docstring), no receiver-band-
        conflict resolution (capture doesn't need any either, since it
        only ever reads/writes the already-active receiver). A slot
        with no freq_hz at all (an entry that was never really
        captured, or a hand-edited memories.json) is skipped rather
        than writing a frequency of None -- for the same reason,
        VFO B for a manually-added non-repeater memory (no real split)
        is skipped too, if it was left blank."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._recall_memory_snapshot(snapshot), self.loop)

    async def _recall_memory_snapshot(self, snapshot):
        if "vfo" not in self._control_methods:
            self.error.emit("Memories: VFO A/B control not available on this radio/install -- can't recall.")
            return
        _get_name, set_name = self._control_methods["vfo"]
        setter = getattr(self.radio, set_name)

        for slot in ("A", "B"):
            data = snapshot.get(slot) or {}
            freq_hz = data.get("freq_hz")
            if freq_hz is None:
                continue
            try:
                await self._call_receiver_aware("vfo", setter, slot)
                # Same settle delay used everywhere else in this file
                # between a VFO-slot select and a write that depends on
                # it having actually landed (see capture_memory_
                # snapshot's own identical wait, and _set_receiver_
                # control_value's docstring).
                await asyncio.sleep(0.15)
                await self.radio.set_frequency(freq_hz, receiver=RECEIVER_MAIN)
                if data.get("mode"):
                    await self.radio.set_mode(data["mode"], data.get("filter"), receiver=RECEIVER_MAIN)
            except Exception as exc:
                self.error.emit(f"Memories: failed to recall VFO {slot}: {exc}")

        tone = snapshot.get("tone")
        if tone:
            try:
                await self._set_repeater_tone_settings(tone.get("mode") or "none", tone.get("freq_hz"))
            except Exception as exc:
                self.error.emit(f"Memories: failed to recall repeater tone settings ({exc}).")

        try:
            await self._call_receiver_aware("vfo", setter, "A")
        except Exception as exc:
            self.error.emit(f"Memories: failed to reselect VFO A after recall: {exc}")

    def set_receiver_frequency(self, receiver: int, freq_hz: int):
        """Thread-safe: call from the GUI thread. Only meaningful on a
        genuine dual-receiver radio (check self.is_dual_receiver first) --
        sets ONE of its two independent receivers' frequency directly
        (receiver=RECEIVER_MAIN or RECEIVER_SUB). Low-level: doesn't
        touch which VFO slot is selected within that receiver -- see
        select_receiver_vfo_and_set_frequency() below. PTT/split doesn't
        actually follow Sub on a real 9700 (confirmed live), so in
        practice this is only ever used to keep Sub locked to the
        continuous downlink for full-duplex RX (main_window.py's
        _on_satellite_tracking_tick) -- Main's own VFO A/B, via
        select_vfo_and_set_frequency() instead, is what actually
        matters for TX.

        Also selects `receiver` as active FIRST, in the same atomic
        coroutine -- confirmed live on a real 9700, via direct
        get_active_receiver() polling, that skipping this (an earlier
        version of this did) creates a genuinely SELF-PERPETUATING
        oscillation on this radio/profile: rigplane's own no-cmd29
        fallback (_run_with_receiver_vfo_fallback) for a bare
        receiver=1 write checks whether Sub is already the selected
        receiver, and if not, temporarily switches to it, writes, then
        switches BACK to whatever was selected before -- which, since
        every one of THESE calls always ends by switching back to Main,
        guarantees Main is "current" at the start of the very next
        call, triggering the exact same temporary-switch-and-revert
        every single time, forever, every satellite-tracking tick.
        Explicitly reselecting the receiver here first makes it already
        match every time, so the write lands and STAYS -- exactly the
        same reasoning already applied to select_receiver_vfo_and_set_
        frequency()."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_receiver_frequency(receiver, freq_hz), self.loop)

    async def _set_receiver_frequency(self, receiver: int, freq_hz: int):
        async with self._receiver_switch_lock:
            try:
                await self.radio.select_receiver(receiver)
                self.audio_status.emit(f"[dual-rx] select_receiver({receiver}) OK (bare freq write)")
            except Exception as exc:
                self.error.emit(f"Select active receiver ({receiver}) failed: {exc}")
            self._active_receiver = receiver
            try:
                await self.radio.set_frequency(freq_hz, receiver=receiver)
                self.audio_status.emit(f"[dual-rx] set_frequency({freq_hz}, receiver={receiver}) OK")
            except Exception as exc:
                self.error.emit(str(exc))

    def select_receiver_vfo_and_set_frequency(self, receiver: int, freq_hz: int, vfo_slot: str = "A", check_conflict: bool = True):
        """Thread-safe: call from the GUI thread. On a genuine dual-
        receiver radio, "which receiver" (Main/Sub) and "which VFO"
        (A/B) turned out to be two independent axes, not one -- each
        receiver has its OWN VFO A/B pair (rigplane's VfoSlotCapable,
        confirmed via its own docstring: "Radio exposes VFO A/B slots
        per receiver" -- get_vfo_slot()/set_vfo_slot() both take an
        explicit receiver= kwarg, not a guess). set_receiver_frequency()
        alone only ever writes into whatever VFO slot that receiver
        already happens to have selected; this also explicitly selects
        VFO A on that receiver first. Confirmed live (a real 9700):
        without this, the radio's own display never actually switches
        to the VFO context it labels "VFO A-2" for Sub -- receiver-only
        writes weren't reaching the VFO slot. Also selects `receiver`
        as the active receiver FIRST, in this same coroutine, rather
        than counting on a previously and separately dispatched
        select_receiver() call to still be in effect -- confirmed live
        on a real 9700 that it isn't reliable to assume that: bundling
        just the VFO-slot-select with the frequency write (an earlier
        version of this did, counting on active_receiver_button's own
        select_receiver() call from whenever Sub was last switched to,
        possibly long before) still didn't reliably reach Sub at all
        when triggered much later by the tuning knob, even though the
        exact same bundle IS confirmed reliable when it runs moments
        after its own select_receiver() call, back to back, in the same
        burst of activity (satellite mode's first tracking tick, right
        after _start_satellite_tracking's own select_receiver()).
        Sequential in one coroutine (active receiver, then VFO select,
        then frequency), same reasoning as select_vfo_and_set_
        frequency() above: separately-dispatched commands don't
        actually guarantee anything about their real-world ordering --
        or, it turns out, persistence -- on the radio's side.

        check_conflict=False skips _resolve_receiver_band_conflict --
        confirmed live on a real 9700 that repeating THAT specific step
        every 2-second tick throughout an ongoing transmission (Main's
        own mid-transmission Doppler correction, main_window.py's
        apply_satellite_tick) eventually misreads the other receiver's
        frequency and forcibly relocates it, even though everything
        else in this bundle (reselect receiver, reselect VFO slot, set
        frequency) genuinely needs to run every single tick -- a bare
        frequency-only write does NOT reliably keep VFO B selected on
        its own (confirmed live: Main drifted back to VFO A). Conflict
        checking is only genuinely needed once, at the moment of
        actually switching bands (e.g. start_ptt_after_vfo, at PTT-
        press time) -- every caller except that repeated-tick case
        should leave this at its default (True)."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._select_receiver_vfo_and_set_frequency(receiver, vfo_slot, freq_hz, check_conflict), self.loop
        )

    def _find_band(self, freq_hz: int):
        """Returns (label, low_hz, high_hz) for whichever band in this
        radio model's RADIO_BANDS freq_hz falls in, or None."""
        radio_model = self._details.get("radio_model")
        for label, low_hz, high_hz in RADIO_BANDS.get(radio_model, []):
            if low_hz <= freq_hz <= high_hz:
                return (label, low_hz, high_hz)
        return None

    def _find_safe_band(self, exclude_labels):
        """Returns (label, low_hz, high_hz) for the first band in this
        radio model's RADIO_BANDS whose label isn't in exclude_labels,
        or None if every band is excluded (e.g. a radio with only two
        bands total)."""
        radio_model = self._details.get("radio_model")
        for label, low_hz, high_hz in RADIO_BANDS.get(radio_model, []):
            if label not in exclude_labels:
                return (label, low_hz, high_hz)
        return None

    async def _get_receiver_frequency(self, receiver: int) -> int:
        """Reads `receiver`'s (RECEIVER_MAIN/RECEIVER_SUB identity)
        actual frequency, translated through rigplane's real GET
        addressing -- see the comment above the frequency poll in
        _poll_loop_fast. get_frequency(receiver=...) is NOT a Main/Sub
        identity selector: receiver=RECEIVER_MAIN(0) always means
        "whichever receiver is currently selected" and
        receiver=RECEIVER_SUB(1) always means "the unselected one",
        regardless of which one that genuinely is. So which literal
        value to pass depends on whether `receiver` IS the currently
        active receiver, not on `receiver` itself -- passing it
        straight through is wrong whenever Sub happens to be active
        (which it always is during satellite receive, right up until
        PTT switches to Main)."""
        addressing = RECEIVER_MAIN if receiver == self._active_receiver else RECEIVER_SUB
        return await self.radio.get_frequency(receiver=addressing)

    async def _resolve_receiver_band_conflict(self, receiver: int, freq_hz: int):
        """Dual-receiver only. Confirmed live on a real 9700: Main and
        Sub can't occupy the same band at the same time -- moving one
        onto a band the other already holds is flatly refused, you have
        to move one of them to a third, uninvolved band first to free
        up the target. This bites hardest right when satellite tracking
        starts: if Main and Sub happen to already be sitting on
        (respectively) the uplink and downlink bands a *newly selected*
        transponder needs -- reversed from what it actually wants (e.g.
        Main on 2m/Sub on 70cm, but this transponder's uplink is 70cm
        and downlink is 2m) -- neither can move to its target without
        the other vacating first, and nothing here was doing that
        automatically.

        Checks whether moving `receiver` to freq_hz's band would land
        on the band the OTHER receiver currently occupies (a fresh
        get_frequency() read, not cached), and if so, moves the OTHER
        receiver to any other available band first (VFO A, since it's
        just a temporary parking spot, not meant to be used) before
        returning -- callers should call this, then proceed with their
        own intended receiver/VFO/frequency write as normal. The safe
        band this picks also excludes `receiver`'s OWN current band,
        not just the target and conflict bands -- `receiver` hasn't
        moved off it yet at this point (that happens after this
        returns), so parking the other receiver there would just swap
        which pair is now colliding instead of resolving anything. No-
        op if there's no conflict, freq_hz doesn't land in any known
        band, or this radio model has fewer than 3 bands available
        (nowhere uninvolved to park the other receiver).

        Always leaves `receiver` (not `other_receiver`) selected as the
        real radio's active receiver before returning, whenever it
        actually moved other_receiver out of the way -- root-caused a
        real "tuning Main's knob flips the radio to Sub and starts
        tuning Sub instead" report to this NOT happening previously:
        parking the other receiver requires select_receiver(other_
        receiver) first (rigplane has no way to write another
        receiver's VFO/frequency without making it active), and every
        caller of this method (the knob's Main-frequency path, band-
        button clicks) then goes on to write its OWN intended frequency
        via a BARE set_frequency call with no receiver kwarg -- which
        always lands on whichever receiver is genuinely active on the
        real radio right then. Left on other_receiver, that bare write
        silently retuned Sub while Main's own target frequency was
        never actually written at all -- looking exactly like "the
        knob switched me to Sub", because that's precisely what
        happened. self._active_receiver itself is deliberately left
        untouched here (this method never changes it) -- restoring the
        real hardware to match what that Python-side state already
        believes is what fixes the mismatch, not changing the belief
        itself."""
        if not self.is_dual_receiver:
            return
        target_band = self._find_band(freq_hz)
        if target_band is None:
            return
        other_receiver = RECEIVER_MAIN if receiver == RECEIVER_SUB else RECEIVER_SUB
        try:
            other_freq_hz = await self._get_receiver_frequency(other_receiver)
        except Exception as exc:
            self.error.emit(f"Band-conflict check (receiver {other_receiver}) failed: {exc}")
            return
        other_band = self._find_band(other_freq_hz)
        if other_band is None or other_band[0] != target_band[0]:
            return  # no conflict
        exclude_labels = {target_band[0], other_band[0]}
        try:
            receiver_freq_hz = await self._get_receiver_frequency(receiver)
            receiver_band = self._find_band(receiver_freq_hz)
            if receiver_band is not None:
                exclude_labels.add(receiver_band[0])
        except Exception as exc:
            self.error.emit(f"Band-conflict check (receiver {receiver}) failed: {exc}")
            return
        safe_band = self._find_safe_band(exclude_labels)
        if safe_band is None:
            self.error.emit(
                f"Band conflict: receiver {other_receiver} is on {other_band[0]}, the same "
                f"band receiver {receiver} needs, and there's no third band available on this "
                "radio to move it out of the way first."
            )
            return
        safe_label, safe_low_hz, _safe_high_hz = safe_band
        self.audio_status.emit(
            f"[dual-rx] band conflict: receiver {other_receiver} is on {other_band[0]}, the "
            f"same band receiver {receiver} needs -- moving it to {safe_label} first."
        )
        # Whole parking sequence is now under self._receiver_switch_lock
        # (it wasn't before) -- root-caused a real "Main still can't
        # switch to Sub's band" report that SURVIVED the earlier fix
        # locking just the final write (radio_worker.py commit
        # 9dedae2): that fix only closed the race around the LAST write,
        # but this parking sequence itself -- select_receiver(other)/
        # set_vfo_slot/set_frequency, run BEFORE that lock is ever
        # acquired -- was just as exposed to the same concurrent-poll-
        # loop-flips-the-active-receiver race, for BOTH directions
        # equally. It happened to read as "Sub->Main always works, Main-
        # >Sub never does" only because Sub's own final write (already
        # locked, see _select_receiver_vfo_and_set_frequency) could
        # tolerate a botched parking attempt by coincidence far more
        # often than Main's -- not because the parking step itself was
        # actually reliable.
        async with self._receiver_switch_lock:
            try:
                await self.radio.select_receiver(other_receiver)
                await self.radio.set_vfo_slot("A", receiver=other_receiver)
                await self.radio.set_frequency(safe_low_hz, receiver=other_receiver)
                # The frequency write above is fire-and-forget (rigplane
                # never waits for an ACK on a plain frequency set) -- so
                # unlike the ACK-waited select_receiver/set_vfo_slot
                # calls just above it, nothing has actually confirmed
                # other_receiver truly left the conflicting band yet.
                # Read it back (with a couple of retries -- real
                # hardware over a serial link doesn't always reflect a
                # just-sent write on the very next poll) before trusting
                # it's safe to write the caller's OWN intended receiver/
                # band next; without this, "moved Sub out of the way" was
                # sometimes just an assumption, and the caller's
                # subsequent write into what the radio still considered
                # a same-band conflict was silently refused -- exactly
                # matching the "shows the new band for a moment, then
                # snaps back to the old one on the next poll" symptom
                # reported live.
                parked = False
                for _ in range(3):
                    await asyncio.sleep(0.15)
                    confirm_freq_hz = await self._get_receiver_frequency(other_receiver)
                    confirm_band = self._find_band(confirm_freq_hz)
                    if confirm_band is not None and confirm_band[0] == safe_label:
                        parked = True
                        break
                if not parked:
                    self.error.emit(
                        f"Band conflict: moved receiver {other_receiver} toward {safe_label} but "
                        f"it hasn't landed there yet -- the intended write may still be refused."
                    )
            except Exception as exc:
                self.error.emit(f"Moving receiver {other_receiver} to {safe_label} failed: {exc}")
            finally:
                # Restore `receiver` as the real radio's active receiver --
                # see this method's own docstring for the exact bug this
                # fixes (a bare frequency write, right after this returns,
                # otherwise silently lands on other_receiver instead).
                try:
                    await self.radio.select_receiver(receiver)
                except Exception as exc:
                    self.error.emit(f"Restoring active receiver ({receiver}) after band-conflict resolution failed: {exc}")

    async def _select_receiver_vfo_and_set_frequency(self, receiver: int, vfo_slot: str, freq_hz: int, check_conflict: bool = True):
        if check_conflict:
            await self._resolve_receiver_band_conflict(receiver, freq_hz)
        async with self._receiver_switch_lock:
            try:
                await self.radio.select_receiver(receiver)
                self.audio_status.emit(f"[dual-rx] select_receiver({receiver}) OK (vfo+freq bundle)")
            except Exception as exc:
                self.error.emit(f"Select active receiver ({receiver}) failed: {exc}")
            self._active_receiver = receiver
            try:
                await self.radio.set_vfo_slot(vfo_slot, receiver=receiver)
                self.audio_status.emit(f"[dual-rx] set_vfo_slot({vfo_slot!r}, receiver={receiver}) OK")
            except Exception as exc:
                self.error.emit(f"VFO slot select (receiver {receiver}): set_vfo_slot({vfo_slot!r}) failed ({exc}).")
            try:
                await self.radio.set_frequency(freq_hz, receiver=receiver)
                self.audio_status.emit(f"[dual-rx] set_frequency({freq_hz}, receiver={receiver}) OK (vfo+freq bundle)")
            except Exception as exc:
                self.error.emit(str(exc))

    def select_vfo_and_set_frequency(self, vfo_value, freq_hz: int, check_conflict: bool = True):
        """Thread-safe: call from the GUI thread. Selects VFO A/B and
        THEN sets its frequency, as one sequential coroutine rather than
        two independently-scheduled ones (set_control_value("vfo", ...)
        followed by set_frequency(...)). That pairing looks like it
        should just work in order since it's called that way from the
        GUI thread, but it doesn't actually guarantee anything: each
        becomes its own asyncio Task, and once the first yields to the
        event loop during its own real over-the-wire CI-V/serial round
        trip, the second is free to start running before the first has
        actually finished on the radio's side -- landing the frequency
        change on whatever VFO was PREVIOUSLY active. A single coroutine
        awaiting both in sequence has no such race: nothing about it
        yields control back to some OTHER task between those two awaits.
        Used for satellite split-mode PTT (main_window.py), where a
        frequency has to land on the VFO just switched to, not whichever
        one happened to still be selected.

        check_conflict is threaded straight through to _set_frequency --
        see its own docstring; start_ptt_after_vfo passes False here for
        the satellite-PTT case."""
        if self.loop is None or self.radio is None:
            return
        if "vfo" not in self._control_methods:
            self.error.emit(
                f"{CONTROL_DEFINITIONS['vfo']['label']}: not available on this radio/install "
                "(no working getter+setter was found on connect) -- frequency not changed."
            )
            return
        asyncio.run_coroutine_threadsafe(
            self._select_vfo_and_set_frequency(vfo_value, freq_hz, check_conflict), self.loop
        )

    async def _select_vfo_and_set_frequency(self, vfo_value, freq_hz: int, check_conflict: bool = True):
        await self._set_control_value("vfo", vfo_value)
        await self._set_frequency(freq_hz, check_conflict)

    def start_ptt_after_vfo(self, vfo_value, freq_hz: int, receiver: int = None, check_conflict: bool = True):
        """Thread-safe: call from the GUI thread. Single-receiver radios
        (7300/705): selects VFO A/B, sets its frequency, THEN keys PTT --
        all as ONE coroutine, not select_vfo_and_set_frequency() followed
        by a separately-dispatched start_ptt(). That pairing has the
        exact same non-guarantee select_vfo_and_set_frequency() itself
        exists to fix (see its docstring) -- each independently-scheduled
        command is its own asyncio Task with no real ordering guarantee
        relative to the other -- except here the stakes are worse: a
        real CI-V radio can outright refuse VFO/frequency changes once
        it's already transmitting. If start_ptt() ever won that race
        (confirmed live on a real 9700), the retune arrives after PTT
        already keyed up, the radio ignores it, and the whole
        transmission holds the RX frequency instead of switching to the
        uplink at all. Always keys PTT regardless of whether the VFO
        switch itself succeeds -- pressing PTT should always actually
        transmit, never silently do nothing because a retune failed.

        receiver, when given (dual-receiver satellite mode only --
        main_window.py's _on_ptt_toggled), also selects it as the
        active receiver first, in this same atomic coroutine -- e.g.
        switching to Main for TX so the tuning knob/AF/RF/squelch
        controls follow it, same reasoning as everything else bundled
        here. Deliberately does NOT touch the scope receiver, so it can
        keep showing Sub's downlink throughout the transmission.

        check_conflict=False (main_window.py's satellite PTT press
        always passes this) skips _resolve_receiver_band_conflict --
        confirmed live on a real 9700 that its own pre-check reads
        (get_frequency on both receivers, to look for a same-band
        collision before deciding whether to move anything) cause
        several rapid, visible receiver A/B oscillations right at the
        moment PTT is pressed -- BEFORE the radio even keys up -- and
        Sub can settle corrupted onto a free band's raw low edge by the
        time that resolves, exactly the "moved a receiver that was
        never supposed to move" failure the conflict check exists to
        prevent, just triggered by the check's own reads instead of an
        actual conflict. In practice Main's uplink band and Sub's
        downlink band are never the same band for any real full-duplex
        satellite transponder, and Sub is continuously verified to
        already be on its correct (different) band by the RX tick
        loop before any PTT press -- so this check has no genuine job
        left to do here by the time an operator actually presses PTT,
        only the demonstrated potential to corrupt Sub. This has to be
        threaded through explicitly (not just fixed at this method's
        own top level) -- the VFO+frequency step below goes through
        _select_vfo_and_set_frequency -> _set_frequency, which runs
        this exact same check on its own unless told not to; confirmed
        live that fixing only THIS method's own top-level call, while
        that inner one still ran unconditionally, did not actually stop
        the corruption at all."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._start_ptt_after_vfo(vfo_value, freq_hz, receiver, check_conflict), self.loop
        )

    async def _start_ptt_after_vfo(self, vfo_value, freq_hz: int, receiver: int = None, check_conflict: bool = True):
        async with self._receiver_switch_lock:
            if receiver is not None:
                try:
                    await self.radio.select_receiver(receiver)
                except Exception as exc:
                    self.error.emit(f"Select active receiver ({receiver}) failed: {exc}")
            if "vfo" in self._control_methods:
                await self._select_vfo_and_set_frequency(vfo_value, freq_hz, check_conflict)
            else:
                self.error.emit(
                    f"{CONTROL_DEFINITIONS['vfo']['label']}: not available on this radio/install "
                    "-- transmitting without switching to the uplink."
                )
            await self._start_ptt()

    def start_ptt_with_frequency(self, freq_hz: int, receiver: int = None):
        """Thread-safe: call from the GUI thread. Selects `receiver`
        (if given) as active, retunes its CURRENT VFO -- whichever one
        it already has selected, no VFO A/B switching at all -- THEN
        keys PTT, all as one atomic coroutine (same ordering-guarantee
        reasoning as start_ptt_after_vfo).

        Per explicit instruction, this replaces start_ptt_after_vfo for
        full-duplex satellite PTT (main_window.py's _on_ptt_toggled,
        dual-receiver branch only -- the standalone single-receiver
        "uplink" role still uses start_ptt_after_vfo/VFO A-B switching
        unchanged): after several rounds of live VFO-slot and mode
        corruption on a real 9700, traced to switching Main's VFO A<->B
        on every PTT press/release, the simplest fix is to just not do
        that at all -- Main (like Sub already does) stays on whichever
        VFO it started the session on and gets retuned in place. Main
        has no real "idle" state to preserve separately in this role
        anyway (it's TX-only in full_duplex -- Sub handles all
        reception), so nothing is lost by not having a distinct VFO
        "slot" for the uplink."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._start_ptt_with_frequency(freq_hz, receiver), self.loop)

    async def _start_ptt_with_frequency(self, freq_hz: int, receiver: int = None):
        async with self._receiver_switch_lock:
            if receiver is not None:
                try:
                    await self.radio.select_receiver(receiver)
                    self._active_receiver = receiver
                except Exception as exc:
                    self.error.emit(f"Select active receiver ({receiver}) failed: {exc}")
            await self._set_frequency(freq_hz, check_conflict=False)
            await self._start_ptt()

    def stop_ptt_then_vfo(self, vfo_value, freq_hz: int):
        """Thread-safe: call from the GUI thread. The release-side
        counterpart to start_ptt_after_vfo() -- stops transmitting
        FIRST, then (once that's actually finished, not just dispatched)
        selects VFO A/B and restores its frequency, all as one
        coroutine. Same reasoning as the press side: unkeying and
        retuning as two separately-dispatched commands doesn't guarantee
        the radio has actually stopped transmitting before the retune
        arrives, and a radio that refuses VFO changes while transmitting
        would just ignore it if it arrived too early.

        Single-receiver radios only -- dual-receiver satellite mode
        uses stop_ptt_and_select_receiver() below instead, since Main
        can't be retuned back to VFO A/downlink on release there: Sub
        is sitting on exactly that band continuously, and Main can't
        switch to a band Sub currently occupies (confirmed live on a
        real 9700)."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._stop_ptt_then_vfo(vfo_value, freq_hz), self.loop)

    async def _stop_ptt_then_vfo(self, vfo_value, freq_hz: int):
        await self._stop_ptt()
        if "vfo" in self._control_methods:
            await self._select_vfo_and_set_frequency(vfo_value, freq_hz)
        else:
            self.error.emit(
                f"{CONTROL_DEFINITIONS['vfo']['label']}: not available on this radio/install "
                "-- frequency not restored to the downlink."
            )

    def stop_ptt_and_select_receiver(self, receiver: int):
        """Thread-safe: call from the GUI thread. Dual-receiver
        satellite mode's PTT-release counterpart when Main's VFO
        shouldn't be retuned at all -- confirmed live on a real 9700
        that Main can't switch to a band Sub currently occupies (you
        have to move one of them to a third band first to free it up),
        and Sub is sitting on exactly that downlink band continuously
        throughout the transmission (_on_satellite_tracking_tick never
        touches it during TX) -- so stop_ptt_then_vfo's usual "restore
        Main to VFO A/downlink" on release would send Main straight
        into the band Sub's already on. Stops transmitting, then
        selects `receiver` (Sub) as active, both in one coroutine --
        no VFO/frequency command to Main at all; it just stays wherever
        the just-finished transmission left it (the uplink band, VFO B)
        until the next PTT press retunes it fresh -- nothing needs
        Main in between, since Sub is what's actually active/watched
        during RX."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._stop_ptt_and_select_receiver(receiver), self.loop)

    async def _stop_ptt_and_select_receiver(self, receiver: int):
        await self._stop_ptt()
        async with self._receiver_switch_lock:
            try:
                await self.radio.select_receiver(receiver)
            except Exception as exc:
                self.error.emit(f"Select active receiver ({receiver}) failed: {exc}")
            self._active_receiver = receiver

    def select_band(self, band_label: str, low_edge_hz: int, receiver: int = None):
        """Thread-safe. Tries Icom's confirmed get_bsr() band-stacking
        recall first -- reads whatever frequency was last used on that
        band and tunes to it, like pressing the real band button. Falls
        back to tuning to the band's low edge (low_edge_hz) for bands
        with no confirmed band-stacking code (currently VHF/UHF -- see
        BAND_STACKING_CODES) or if the attempt fails for any reason.

        receiver, when given (dual-receiver only, e.g. Sub while it's
        the active receiver -- main_window.py's _on_band_selected),
        targets the low-edge fallback at that receiver instead of
        unconditionally defaulting to Main. Band-stacking recall itself
        stays Main-only regardless -- confirmed via rigplane's own
        get_bsr()/get_band_stack() signatures, neither takes a receiver
        argument at all, so there's no way to ask for Sub's own
        band-stack register; it's skipped straight to the low-edge
        fallback whenever a non-Main receiver is targeted. Confirmed
        live on a real 9700 that leaving this Main-only entirely (as an
        earlier version of this did) actually changed Main's band while
        Sub was the active/displayed receiver, silently changing the
        wrong one -- the frequency readout would then show Main's
        just-selected band instead of Sub's real, unchanged one.

        The low-edge fallback for a non-Main receiver also selects VFO A
        on it first, in the same coroutine, rather than a bare
        set_frequency(receiver=...) -- confirmed live on a real 9700
        that a bare receiver-addressed write, with no VFO slot selected
        for that receiver in this same call, doesn't reliably land on
        it at all (see select_receiver_vfo_and_set_frequency's
        docstring; the tuning knob had the exact same problem)."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._select_band(band_label, low_edge_hz, receiver), self.loop)

    async def _select_band(self, band_label: str, low_edge_hz: int, receiver: int = None):
        radio_model = self._details["radio_model"]
        band_code = BAND_STACKING_CODES.get(band_label)
        if (radio_model, band_label) in BAND_STACKING_EXCLUDED:
            band_code = None  # confirmed to hang on this radio -- skip straight to the band edge

        if receiver is None:
            # Targeting Main (see this method's own docstring -- receiver
            # is only ever non-None for a non-Main receiver). Neither the
            # band-stacking-recall path nor the low-edge fallback below
            # had any band-conflict handling -- unlike the receiver-is-
            # not-None/Sub path just below, which routes through
            # _select_receiver_vfo_and_set_frequency and already resolves
            # this itself. Without it, switching Main to a band Sub
            # currently occupies (or vice versa, handled on Sub's own
            # path) was simply rejected outright by the radio -- the same
            # hardware constraint _resolve_receiver_band_conflict already
            # handles everywhere else (satellite tracking start, PTT).
            # Using low_edge_hz for this check even when band-stacking
            # recall ends up being used is safe: whatever frequency gets
            # recalled is guaranteed to be within the same band as
            # low_edge_hz (that's what makes it a band-stacking register
            # FOR this band). No-op on a single-receiver radio
            # (_resolve_receiver_band_conflict's own guard).
            await self._resolve_receiver_band_conflict(RECEIVER_MAIN, low_edge_hz)

        if band_code is not None and receiver is None:
            async with self._receiver_switch_lock:
                await self._reselect_main_before_write("band recall")
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

        if receiver is not None and self.is_dual_receiver:
            await self._select_receiver_vfo_and_set_frequency(receiver, "A", low_edge_hz)
            return

        # Root-caused a real "Main can't switch to a band Sub is on
        # (Sub->Main's band works fine though)" report to THIS write
        # being unprotected: _resolve_receiver_band_conflict() above
        # reselects Main (via a bare, unlocked select_receiver call) once
        # it's done parking Sub out of the way, but nothing then stopped
        # a concurrently-running poll-loop iteration (e.g. reading Sub's
        # own frequency/mode/meters, several of which temporarily flip
        # the radio's active receiver via rigplane's own
        # _run_with_receiver_vfo_fallback and flip it back afterward)
        # from interleaving in the gap between that reselect and this
        # write, landing this fire-and-forget set_frequency(receiver=
        # MAIN) call on whatever receiver the radio actually had selected
        # by the time it reached the wire -- not necessarily Main. The
        # Sub path just above doesn't have this problem because
        # _select_receiver_vfo_and_set_frequency already wraps its own
        # reselect+write in self._receiver_switch_lock. Same fix here:
        # hold the lock across an explicit Main reselect (with the same
        # confirmed-necessary settle delay used everywhere else this
        # exact race was root-caused, e.g. _set_control_value) and the
        # write itself, so no concurrent receiver-switching poll code can
        # interleave in between.
        async with self._receiver_switch_lock:
            await self._reselect_main_before_write("band tune")
            try:
                # Explicit receiver=RECEIVER_MAIN -- see _set_frequency's
                # own comment for why a bare call isn't reliable here.
                await self.radio.set_frequency(low_edge_hz, receiver=RECEIVER_MAIN)
            except Exception as exc:
                self.error.emit(f"Band: setting frequency failed ({exc}).")

    async def _reselect_main_before_write(self, context: str):
        """Explicitly reselect Main and let the radio settle before a
        Main-targeted write that immediately follows -- see the big
        comment in _select_band's low-edge-fallback branch for the exact
        race this closes. No-op on a single-receiver radio
        (select_receiver() is already a no-op there). Callers must hold
        self._receiver_switch_lock across this AND their own write."""
        if not self.is_dual_receiver:
            return
        try:
            await self.radio.select_receiver(RECEIVER_MAIN)
            self._active_receiver = RECEIVER_MAIN
            await asyncio.sleep(0.15)
        except Exception as exc:
            self.error.emit(f"Select active receiver (Main) before {context} failed: {exc}")

    def stop(self):
        """Request a clean shutdown of the polling loop and thread.
        Called from the GUI thread (main_window.py's closeEvent).

        Cancels the _main() task directly (via call_soon_threadsafe,
        the correct thread-safe way to touch another thread's asyncio
        loop) rather than only setting self._stop_requested -- that
        flag is exactly as before (still what cleanly ends both poll
        loops once they're actually running, checked at the top of each
        one's own while loop), but it did nothing at all to interrupt
        _main() during connection/startup, which can genuinely take several
        seconds on real hardware (slow/unresponsive CI-V round-trips,
        enable_scope's own up-to-5s verification wait). Cancellation
        unwinds _main() from wherever it currently is, not just
        between poll iterations, so closeEvent's worker.wait(2000)
        actually has something to succeed at instead of timing out and
        leaving Qt to destroy a still-running QThread (a hard abort)."""
        self._stop_requested = True
        if self.loop is not None and self._main_task is not None:
            self.loop.call_soon_threadsafe(self._main_task.cancel)
        # wait_for_thread (QThread.wait) is called by the GUI after this


