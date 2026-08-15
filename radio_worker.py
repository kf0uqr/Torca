"""
RadioWorker: a dedicated QThread that owns its own asyncio event loop and
does all actual rigplane radio I/O -- connecting, polling frequency/
meters/levels/controls, and handling commands from the GUI thread via
asyncio.run_coroutine_threadsafe(). Never touches a Qt widget directly;
only ever talks to the GUI thread by emitting signals.
"""

import asyncio
import importlib
import inspect
import os

from PySide6.QtCore import QThread, Signal

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
    # one install). See LEVEL_DEFINITIONS in constants.py and
    # RadioWorker._setup_levels() below, which probe the connected radio
    # object for the first matching name among several plausible ones
    # instead of betting on a single guess.
    from rigplane import LevelsCapable
except ImportError:
    LevelsCapable = None

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

from constants import (
    BAND_STACKING_CODES,
    BAND_STACKING_REGISTER_LATEST,
    BAND_STACKING_EXCLUDED,
    LEVEL_DEFINITIONS,
    DUAL_RECEIVER_LEVEL_KEYS,
    SUB_LEVEL_KEY_SUFFIX,
    METER_DEFINITIONS,
    CONTROL_DEFINITIONS,
    DIAGNOSTIC_DIR_KEYWORDS,
    DIAGNOSTIC_DIR_PREFIXES,
    POLL_INTERVAL_SEC,
    AUDIO_DEVICE_SYSTEM_DEFAULT,
)
from rig_discovery import find_method_name
from audio import (
    AudioBridge,
    pactl_available,
    create_null_sink,
    unload_pactl_module,
    get_default_sink_name,
    get_default_source_name,
    set_default_sink,
    set_default_source,
    find_own_sink_input_ids,
    find_own_source_output_ids,
    move_sink_input,
    move_source_output,
    VIRTUAL_CABLE_RX_NAME,
    VIRTUAL_CABLE_RX_DESC,
    VIRTUAL_CABLE_TX_NAME,
    VIRTUAL_CABLE_TX_DESC,
)

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
        self.is_dual_receiver = False  # set once connected -- see DualReceiverCapable import comment
        # Dual-receiver only: which receiver every receiver-aware
        # getter/setter (see _receiver_kwargs) targets when none is
        # explicitly given -- fixed at RECEIVER_MAIN and never changed.
        # Confirmed live on a real 9700 (a controlled test giving each
        # VFO a distinct mode to tell them apart) that PTT/split-driven
        # transmission always follows Main's own VFO A/B context, never
        # Sub, regardless of what's written to Sub -- so Main is the
        # right target for every generic single-value control (mode,
        # frequency readout, squelch, etc.) unconditionally. Sub is only
        # ever touched directly, by receiver number, for its own
        # continuous downlink tracking (main_window.py's
        # _on_satellite_tracking_tick) -- that never needs to change
        # what this defaults to.
        self._active_receiver = RECEIVER_MAIN
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
        every entry in LEVEL_DEFINITIONS (same find_method_name
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

    @staticmethod
    def _normalize_level_value(raw):
        """Getter values might come back as a 0.0-1.0 float or, like the
        squelch setter turned out to want, a raw int on Icom's 0-255
        CI-V level scale. Treat anything above 1.0 as the latter."""
        value = float(raw)
        if value > 1.0:
            value = value / 255.0
        return max(0.0, min(1.0, value))

    async def _call_level_setter(self, setter_name, value, cache_key, receiver=None):
        """Calls a discovered LevelsCapable setter with a 0.0-1.0 value.
        Tries a plain float first; if the setter rejects that with a
        TypeError/ValueError (as set_squelch() did -- it formats the
        value with Python's `:d` spec, which only accepts ints), retries
        as int(round(value * 255)), the raw Icom CI-V level scale used
        elsewhere in this app (e.g. the S-meter). Whichever form works
        is cached per cache_key so later calls don't re-probe.

        receiver overrides self._active_receiver when given -- lets a
        dedicated per-receiver slider (Sub's AF/RF/Squelch,
        set_level_value_for_receiver below) stay independent of
        whichever receiver the generic single-value controls currently
        target, without needing its own separate cache_key (the
        float-vs-int255 probe result is a property of the setter
        function itself, not of which receiver it's called for)."""
        setter = getattr(self.radio, setter_name)
        target_receiver = self._active_receiver if receiver is None else receiver
        kwargs = self._receiver_kwargs(setter, target_receiver) if self.is_dual_receiver else {}
        mode = self._level_value_mode.get(cache_key, "float")
        if mode == "float":
            try:
                await setter(value, **kwargs)
                self._level_value_mode[cache_key] = "float"
                return
            except (TypeError, ValueError):
                pass  # fall through and try the raw-int scale instead
        await setter(int(round(value * 255)), **kwargs)
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

    def set_level_value_for_receiver(self, key: str, value: float, receiver: int):
        """Thread-safe: call from the GUI thread. Same as
        set_level_value() but targets an explicit receiver rather than
        self._active_receiver -- confirmed live on a real 9700 that AF
        gain, squelch, and RF gain are all controlled independently per
        receiver, so Sub needs its own slider (main_window.py) that
        doesn't move Main's. value is 0.0-1.0."""
        if self.loop is None or key not in self._level_methods:
            return
        asyncio.run_coroutine_threadsafe(self._set_level_value_for_receiver(key, value, receiver), self.loop)

    async def _set_level_value_for_receiver(self, key: str, value: float, receiver: int):
        _get_name, set_name = self._level_methods[key]
        definition = LEVEL_DEFINITIONS[key]
        try:
            await self._call_level_setter(set_name, value, key, receiver=receiver)
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

        self.is_dual_receiver = DualReceiverCapable is not None and isinstance(self.radio, DualReceiverCapable)
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
            kwargs = self._receiver_kwargs(setter, self._active_receiver) if self.is_dual_receiver else {}
            await setter(value, **kwargs)
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
                getter = self.radio.get_frequency
                # Dual-receiver: read whichever receiver is actually in
                # use right now (see _active_receiver's definition in
                # __init__) rather than unconditionally defaulting to
                # Main -- reading a receiver turned out, same as writing
                # one, to also visibly focus/select it on a real 9700.
                kwargs = self._receiver_kwargs(getter, self._active_receiver) if self.is_dual_receiver else {}
                freq_hz = await getter(**kwargs)
                self.frequency_updated.emit(freq_hz)
            except Exception as exc:
                self.error.emit(str(exc))

            for meter_type, getter_name in self._meter_getters.items():
                definition = METER_DEFINITIONS[meter_type]
                try:
                    getter = getattr(self.radio, getter_name)
                    kwargs = self._receiver_kwargs(getter, self._active_receiver) if self.is_dual_receiver else {}
                    value = await getter(**kwargs)
                    self.meter_updated.emit(meter_type, value)
                except Exception as exc:
                    self.error.emit(f"{definition['label']}: {exc}")

            for key, (get_name, _set_name) in self._control_methods.items():
                if get_name is None:
                    continue  # write-only control (e.g. memory_mode) -- no GET variant exists, never poll
                definition = CONTROL_DEFINITIONS[key]
                try:
                    getter = getattr(self.radio, get_name)
                    # See _receiver_kwargs's docstring -- confirmed live
                    # on a real 9700 that "vfo" alone wasn't the only
                    # control causing the flip-flop; "mode" was too, and
                    # there was no reason to expect it'd stop there.
                    kwargs = self._receiver_kwargs(getter, self._active_receiver) if self.is_dual_receiver else {}
                    value = await getter(**kwargs)
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
                getter = getattr(self.radio, get_name)
                try:
                    kwargs = self._receiver_kwargs(getter, self._active_receiver) if self.is_dual_receiver else {}
                    raw_value = await getter(**kwargs)
                    self.level_updated.emit(key, self._normalize_level_value(raw_value))
                except Exception as exc:
                    self.error.emit(f"{definition['label']}: {exc}")

                # AF gain/squelch/RF gain are controlled independently
                # per receiver on a real 9700 (confirmed live) -- also
                # poll Sub's own value for these, under a synthetic
                # "<key>_sub" signal key, so main_window.py's dedicated
                # Sub sliders can reflect it without a second entry in
                # LEVEL_DEFINITIONS.
                if self.is_dual_receiver and key in DUAL_RECEIVER_LEVEL_KEYS:
                    try:
                        sub_kwargs = self._receiver_kwargs(getter, RECEIVER_SUB)
                        raw_value = await getter(**sub_kwargs)
                        self.level_updated.emit(f"{key}{SUB_LEVEL_KEY_SUFFIX}", self._normalize_level_value(raw_value))
                    except Exception as exc:
                        self.error.emit(f"{definition['label']} (Sub): {exc}")

            await asyncio.sleep(POLL_INTERVAL_SEC)

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

    def set_frequency(self, freq_hz: int):
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_frequency(freq_hz), self.loop)

    async def _set_frequency(self, freq_hz: int):
        try:
            await self.radio.set_frequency(freq_hz)
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
        (e.g. the scope -- see set_scope_receiver below)."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._select_receiver(receiver), self.loop)

    async def _select_receiver(self, receiver: int):
        try:
            await self.radio.select_receiver(receiver)
        except Exception as exc:
            self.error.emit(f"Select active receiver ({receiver}) failed: {exc}")

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
        except Exception as exc:
            self.error.emit(f"Select scope receiver ({receiver}) failed: {exc}")

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
        matters for TX."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_receiver_frequency(receiver, freq_hz), self.loop)

    async def _set_receiver_frequency(self, receiver: int, freq_hz: int):
        try:
            await self.radio.set_frequency(freq_hz, receiver=receiver)
        except Exception as exc:
            self.error.emit(str(exc))

    def select_receiver_vfo_and_set_frequency(self, receiver: int, freq_hz: int, vfo_slot: str = "A"):
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
        writes weren't reaching the VFO slot. Sequential in one
        coroutine (VFO select, then frequency), same reasoning as
        select_vfo_and_set_frequency() above: two separately-dispatched
        commands don't actually guarantee anything about their
        real-world ordering on the radio's side."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._select_receiver_vfo_and_set_frequency(receiver, vfo_slot, freq_hz), self.loop
        )

    async def _select_receiver_vfo_and_set_frequency(self, receiver: int, vfo_slot: str, freq_hz: int):
        try:
            await self.radio.set_vfo_slot(vfo_slot, receiver=receiver)
        except Exception as exc:
            self.error.emit(f"VFO slot select (receiver {receiver}): set_vfo_slot({vfo_slot!r}) failed ({exc}).")
        try:
            await self.radio.set_frequency(freq_hz, receiver=receiver)
        except Exception as exc:
            self.error.emit(str(exc))

    def select_vfo_and_set_frequency(self, vfo_value, freq_hz: int):
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
        one happened to still be selected."""
        if self.loop is None or self.radio is None:
            return
        if "vfo" not in self._control_methods:
            self.error.emit(
                f"{CONTROL_DEFINITIONS['vfo']['label']}: not available on this radio/install "
                "(no working getter+setter was found on connect) -- frequency not changed."
            )
            return
        asyncio.run_coroutine_threadsafe(self._select_vfo_and_set_frequency(vfo_value, freq_hz), self.loop)

    async def _select_vfo_and_set_frequency(self, vfo_value, freq_hz: int):
        await self._set_control_value("vfo", vfo_value)
        await self._set_frequency(freq_hz)

    def start_ptt_after_vfo(self, vfo_value, freq_hz: int):
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
        transmit, never silently do nothing because a retune failed."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._start_ptt_after_vfo(vfo_value, freq_hz), self.loop)

    async def _start_ptt_after_vfo(self, vfo_value, freq_hz: int):
        if "vfo" in self._control_methods:
            await self._select_vfo_and_set_frequency(vfo_value, freq_hz)
        else:
            self.error.emit(
                f"{CONTROL_DEFINITIONS['vfo']['label']}: not available on this radio/install "
                "-- transmitting without switching to the uplink."
            )
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
        would just ignore it if it arrived too early."""
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


