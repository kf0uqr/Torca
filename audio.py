"""
Everything audio-related: the AudioBridge class that streams mic/speaker
audio to and from the radio via rigplane's AudioTransport protocol, and
the Linux/PulseAudio "virtual audio cable" helpers used by the Ham
Dashboard-adjacent Virtual Cables feature in the main window.
"""

import asyncio
import queue
import re
import shutil
import subprocess

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

from constants import (
    AUDIO_SAMPLE_WIDTH,
    AUDIO_DEVICE_SYSTEM_DEFAULT,
    AUDIO_CHANNELS,
    AUDIO_DEFAULT_SAMPLE_RATE,
    AUDIO_TX_PCM_SAMPLE_RATE,
    AUDIO_TX_PCM_FRAME_SAMPLES,
    AUDIO_JITTER_BUFFER_MS,
    AUDIO_OUTPUT_BLOCK_MS,
)
from rig_discovery import find_method_name

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
                self._tx_start_name = find_method_name(
                    self.radio, ["start_audio_tx_pcm", "start_tx"]
                )
                self._tx_push_name = find_method_name(
                    self.radio, ["push_audio_tx_pcm", "push_tx"]
                )
                self._tx_stop_name = find_method_name(
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

