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

from rigplane import create_radio, LanBackendConfig, CommandError, AudioCodec
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
    RADIO_BANDS,
    BAND_STACKING_CODES,
    BAND_STACKING_REGISTER_LATEST,
    BAND_STACKING_EXCLUDED,
    LEVEL_DEFINITIONS,
    METER_DEFINITIONS,
    CONTROL_DEFINITIONS,
    DIAGNOSTIC_DIR_KEYWORDS,
    DIAGNOSTIC_DIR_PREFIXES,
    POLL_INTERVAL_SEC,
    AUDIO_DEVICE_SYSTEM_DEFAULT,
)
from rig_discovery import find_method_name
from audio import AudioBridge

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
    active_receiver_changed = Signal(int)  # dual-receiver only: 0=MAIN, 1=SUB, from get_active_receiver() polling
    scope_span_changed = Signal(int)     # preset index 0-7, from get_scope_span() polling
    scope_ref_changed = Signal(float)    # dB, -30.0 to +10.0, from get_scope_ref() polling
    scope_speed_changed = Signal(int)    # 0=fast, 1=mid, 2=slow, from get_scope_speed() polling
    scope_ready = Signal()               # emitted once enable_scope() actually succeeds -- see is_scope_capable
    error = Signal(str)

    def __init__(self, details, parent=None):
        super().__init__(parent)
        self._details = details
        self.loop = None       # asyncio event loop, created inside run()
        self.radio = None      # rigplane Radio, set once connected
        self.is_dual_receiver = False  # set once connected -- see DualReceiverCapable import comment
        self.is_scope_capable = False  # set once connected, True only if enable_scope() (in _main) actually succeeds
        # Last-observed span/ref/speed, so _poll_loop only emits
        # scope_*_changed when the value genuinely changes -- same
        # change-filtering as _last_observed_active_receiver above.
        self._last_observed_scope_span = None
        self._last_observed_scope_ref = None
        self._last_observed_scope_speed = None
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
        # _poll_loop() emits an audio_status message (visible in the
        # console via [AUDIO], no special setup needed) every time this
        # changes, so an unexpected flip-flop is directly observable
        # instead of inferred from symptoms like the band-button
        # highlight or the frequency readout.
        self._last_observed_active_receiver = None
        self._radio_cm = None
        self._stop_requested = False
        self.audio_bridge = None  # set in _setup_audio() once connected, if applicable
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

    def run(self):
        """Thread entry point: create and own the asyncio loop here."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
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
        if d["connection_type"] == "network":
            kwargs = dict(
                host=d["host"],
                port=d["port"],
                radio_addr=d["addr"],
                username=d["username"],
                password=d["password"],
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
                self.audio_status.emit("Power meter: radio reports native unit 'watts' -- reading directly.")
            elif native_unit == "raw_255":
                self._meter_definitions["power"]["kind"] = "linear"
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
            await self._call_receiver_aware(key, setter, value)
        except Exception as exc:
            self.error.emit(f"{definition['label']}: {set_name}({value!r}) failed ({exc}).")

    def set_receiver_control_value(self, receiver: int, key: str, value):
        """Thread-safe: call from the GUI thread. Like set_control_value,
        but sets `key` on a SPECIFIC receiver regardless of which one is
        currently active -- e.g. setting Main's mode while Sub is the
        active/listening receiver during satellite tracking, which
        set_control_value can't do (it always targets self._active_receiver,
        i.e. whichever one the operator is currently listening to/tuning)."""
        if self.loop is None:
            return
        if key not in self._control_methods:
            self.error.emit(
                f"{CONTROL_DEFINITIONS[key]['label']}: not available on this radio/install "
                "(no working getter+setter was found on connect)."
            )
            return
        asyncio.run_coroutine_threadsafe(self._set_receiver_control_value(receiver, key, value), self.loop)

    async def _set_receiver_control_value(self, receiver: int, key: str, value):
        """Temporarily selects `receiver` (if it isn't already active),
        sets `key` on it via the same path _set_control_value uses, then
        restores whichever receiver was active before -- a single bundled
        coroutine, not three separately-dispatched calls, for the same
        reason every other multi-step dual-receiver operation in this
        file is bundled: two independently-scheduled
        run_coroutine_threadsafe() calls don't guarantee real-world
        completion order (see _set_receiver_frequency's docstring for the
        self-perpetuating-oscillation bug this caused the one time it
        wasn't followed). Single-receiver radios have no "other receiver"
        to switch to or restore, so this is a no-op wrapper around the
        normal _set_control_value path for them."""
        if not self.is_dual_receiver:
            await self._set_control_value(key, value)
            return
        previously_active = self._active_receiver
        if receiver != previously_active:
            try:
                await self.radio.select_receiver(receiver)
                self._active_receiver = receiver
            except Exception as exc:
                self.error.emit(f"Select receiver ({receiver}) for {key} failed: {exc}")
                return
        await self._set_control_value(key, value)
        if receiver != previously_active:
            try:
                await self.radio.select_receiver(previously_active)
                self._active_receiver = previously_active
            except Exception as exc:
                self.error.emit(f"Restoring active receiver ({previously_active}) after {key} failed: {exc}")

    async def _poll_loop(self):
        """Periodically reads live values: frequency, plus every
        confirmed-supported meter (see _setup_meters()). There are only a
        handful, so polling all of them each cycle is negligible overhead
        -- this lets any number of independently-selectable MeterWidgets
        share this one poll loop, each just filtering meter_updated for
        whichever type it's currently showing, rather than the worker
        needing to track which widget wants which type."""
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
                    getter = getattr(self.radio, getter_name)
                    kwargs = self._receiver_kwargs(getter, self._active_receiver) if self.is_dual_receiver else {}
                    value = await getter(**kwargs)
                    self.meter_updated.emit(meter_type, value)
                except Exception as exc:
                    self.error.emit(f"{definition['label']}: {exc}")

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
        await self._resolve_receiver_band_conflict(RECEIVER_MAIN, freq_hz)
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

    def set_scope_ref(self, ref_db: float):
        """Thread-safe: call from the GUI thread. rigplane's
        set_scope_ref() -- confirmed via commands/scope.py's
        _scope_ref_encode(): -30.0 to +10.0 dB in 0.5 dB steps."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._set_scope_ref(ref_db), self.loop)

    async def _set_scope_ref(self, ref_db: float):
        try:
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
        _poll_loop. get_frequency(receiver=...) is NOT a Main/Sub
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
        (nowhere uninvolved to park the other receiver)."""
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
        try:
            await self.radio.select_receiver(other_receiver)
            await self.radio.set_vfo_slot("A", receiver=other_receiver)
            await self.radio.set_frequency(safe_low_hz, receiver=other_receiver)
        except Exception as exc:
            self.error.emit(f"Moving receiver {other_receiver} to {safe_label} failed: {exc}")

    async def _select_receiver_vfo_and_set_frequency(self, receiver: int, vfo_slot: str, freq_hz: int, check_conflict: bool = True):
        if check_conflict:
            await self._resolve_receiver_band_conflict(receiver, freq_hz)
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

    def start_ptt_after_vfo(self, vfo_value, freq_hz: int, receiver: int = None):
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

        Deliberately does NOT run _resolve_receiver_band_conflict --
        confirmed live on a real 9700 that this call's own pre-check
        reads (get_frequency on both receivers, to look for a same-
        band collision before deciding whether to move anything) cause
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
        only the demonstrated potential to corrupt Sub."""
        if self.loop is None or self.radio is None:
            return
        asyncio.run_coroutine_threadsafe(self._start_ptt_after_vfo(vfo_value, freq_hz, receiver), self.loop)

    async def _start_ptt_after_vfo(self, vfo_value, freq_hz: int, receiver: int = None):
        if receiver is not None:
            try:
                await self.radio.select_receiver(receiver)
            except Exception as exc:
                self.error.emit(f"Select active receiver ({receiver}) failed: {exc}")
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

        try:
            await self.radio.set_frequency(low_edge_hz)
        except Exception as exc:
            self.error.emit(f"Band: setting frequency failed ({exc}).")

    def stop(self):
        """Request a clean shutdown of the polling loop and thread."""
        self._stop_requested = True
        # wait_for_thread (QThread.wait) is called by the GUI after this


