"""
RemoteWebRadio: a client for rigplane's own `web` server
(`rigplane.web.server.WebServer` / the `rigplane web` CLI command),
letting this app connect to a radio being shared from a *different*
machine -- typically a USB-only radio (e.g. IC-7300) with no native
LAN capability of its own -- with the same feature set as connecting
to it directly: frequency/mode/PTT/levels/meters/scope/audio.

Why this exists: rigplane already ships a complete, backend-agnostic
server for exactly this (confirmed by reading its source and running
a real instance during development) -- `rigplane web --backend serial
--serial-port /dev/ttyUSB0 ...` exposes full state, scope, and audio
over HTTP/WebSocket for ANY connected radio. But its only existing
client is its own bundled browser app; there's no Python client. This
module is that missing client, built specifically against what
radio_worker.py actually needs (not the full protocol surface).

Confirmed protocol facts (read from rigplane's own source, then
verified live against a real `WebServer` wrapping
`rigplane.backends.icom7610.drivers.serial_stub.SerialMockRadio` --
see the plan's verification section for why no physical hardware is
needed to test this):

- Control channel is `/api/v1/ws`. On connect, the server sends a
  `hello` message (carries `capabilities`), then a full `state_update`
  snapshot -- always in that order, always exactly those two messages
  first.
- Commands: `{"type":"cmd","id":"...","name":"set_freq","params":{...}}`
  -> always ACKed on the same connection:
  `{"type":"response","id":"...","ok":true,"result":{...}}` (or
  `ok:false` with `message`/`error`).
- State updates arrive as `{"type":"state_update","data":{"type":"full"
  |"delta", ...}}` -- `rigplane.web._delta_encoder.apply_delta` (reused
  directly below, not reimplemented) turns either shape into an
  updated flat state dict.
- State field names are camelCase and NOT the same as this app's own
  method names -- e.g. frequency lives at `state["main"]["freqHz"]` /
  `state["sub"]["freqHz"]`, mode at `state["main"]["mode"]`, PTT at the
  top-level `state["ptt"]`. Command *names*, by contrast, closely
  mirror rigplane's own internal radio method names (`set_freq`,
  `set_af_level`, `set_nb`, ...) -- confirmed directly against
  `SerialMockRadio`'s own method names, which the server's command
  whitelist wraps almost 1:1.
- Level values (AF gain, RF gain, squelch) are already 0.0-1.0 floats
  in state, matching EXACTLY what a local rigplane radio's own
  get_af_level()/etc already return (confirmed live) -- no unit
  conversion needed between a remote and a local connection.

Scope (`/api/v1/scope`) and audio (`/api/v1/audio`, PCM16 RX+TX) are
both implemented. Opus and WebRTC are explicitly out of scope (LAN-
first use case; PCM16 already gives full parity). Audio IS gated by
`isinstance(remote_radio, AudioTransport)` (radio_worker.py:364) --
a `@runtime_checkable` Protocol (confirmed by reading
rigplane/core/radio_protocol.py), purely structural, satisfied by
which methods/properties a class defines -- all 5 properties and 5
methods it requires are implemented below. (Scope, by contrast, isn't
gated by an isinstance() check at all -- radio_worker.py just calls
enable_scope() in a try/except.)
"""

import asyncio
import base64
import json
import os
import struct

from rigplane import AudioCodec
from rigplane.audio.lan_stream import AudioPacket
from rigplane.scope import ScopeFrame
from rigplane.web._delta_encoder import apply_delta
from rigplane.web.protocol import (
    AUDIO_CODEC_PCM16,
    AUDIO_HEADER_SIZE,
    MSG_TYPE_AUDIO_RX,
    MSG_TYPE_AUDIO_TX,
    encode_audio_frame,
)

_WS_OP_TEXT = 0x1
_WS_OP_BINARY = 0x2
_WS_OP_CLOSE = 0x8
_WS_OP_PING = 0x9
_WS_OP_PONG = 0xA

_DEFAULT_COMMAND_TIMEOUT = 5.0

# Scope binary frame header, confirmed from rigplane.web.protocol.encode_scope_frame
# (there's no decode counterpart there -- server only ever encodes -- so this
# mirrors that function's layout by hand): msg_type(1) + receiver(1) + mode(1)
# + start_freq_hz(u32 LE) + end_freq_hz(u32 LE) + sequence(u16 LE) + flags(1)
# + pixel_count(u16 LE), then pixel_count pixel bytes.
_SCOPE_MSG_TYPE = 0x01
_SCOPE_HEADER_SIZE = 16


class RemoteRadioError(RuntimeError):
    """Raised for any remote-connection-specific failure (handshake,
    auth, command rejection, connection loss). Callers already handle
    a bare Exception from any radio.* call the same way regardless of
    connection type (radio_worker.py wraps essentially every radio
    call in its own try/except), so this doesn't need special
    handling anywhere else in the app -- just a clear message."""


class _WebSocketClient:
    """Minimal RFC 6455 WebSocket client -- hand-rolled rather than
    reusing rigplane.web.websocket (that module implements the SERVER
    side of the handshake/framing; per RFC 6455 only CLIENT frames are
    masked, so a server-oriented module doesn't directly serve a
    client's needs without real adaptation anyway). Deliberately
    minimal: no fragmentation support (rigplane's own server never
    fragments its messages -- confirmed by reading web/websocket.py --
    and nothing this client sends needs to either), no permessage-
    deflate (never required by the server unless explicitly
    negotiated, which this client doesn't offer). Verified directly
    against a real, locally-running rigplane.web.server.WebServer
    during development."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._closed = False

    @classmethod
    async def connect(cls, host: str, port: int, path: str, *, token: str = "", timeout: float = 10.0) -> "_WebSocketClient":
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        query = f"?token={token}" if token else ""
        lines = [
            f"GET {path}{query} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if token:
            lines.append(f"Authorization: Bearer {token}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        try:
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        except Exception as exc:
            writer.close()
            raise RemoteRadioError(f"Couldn't reach {host}:{port}{path}: {exc}") from exc
        status_line = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            writer.close()
            raise RemoteRadioError(
                f"WebSocket upgrade to {path} was refused: {status_line.decode(errors='replace').strip()}"
            )
        return cls(reader, writer)

    async def send_text(self, text: str) -> None:
        await self._send_frame(_WS_OP_TEXT, text.encode("utf-8"))

    async def send_binary(self, payload: bytes) -> None:
        await self._send_frame(_WS_OP_BINARY, payload)

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask_key = os.urandom(4)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        self._writer.write(header + mask_key + masked)
        await self._writer.drain()

    async def recv(self) -> tuple:
        """Returns (opcode, payload) for the next TEXT/BINARY frame --
        transparently answers pings and skips pongs, matching the
        server's own keepalive expectations (WebConfig.keepalive_interval)."""
        while True:
            header = await self._reader.readexactly(2)
            b0, b1 = header
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = int.from_bytes(await self._reader.readexactly(2), "big")
            elif length == 127:
                length = int.from_bytes(await self._reader.readexactly(8), "big")
            payload = await self._reader.readexactly(length) if length else b""
            if opcode == _WS_OP_PING:
                await self._send_frame(_WS_OP_PONG, payload)
                continue
            if opcode == _WS_OP_PONG:
                continue
            if opcode == _WS_OP_CLOSE:
                raise RemoteRadioError("Remote server closed the connection.")
            return opcode, payload

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._send_frame(_WS_OP_CLOSE, b"")
        except Exception:
            pass
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            pass


def _level_property(command_name: str, state_path: tuple, *, param_name: str = "level"):
    """Generates a (getter, setter) coroutine pair for a simple
    Main/Sub-aware level or toggle -- one of these per LEVEL_
    DEFINITIONS/CONTROL_DEFINITIONS entry, built from a table instead
    of hand-writing ~20 nearly-identical methods. state_path is the
    field name under state["main"]/state["sub"] (e.g. ("afLevel",))."""

    async def getter(self, receiver: int = 0):
        branch = self._receiver_branch(receiver)
        node = self._state.get(branch, {})
        for key in state_path:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        return node if node != {} else None

    async def setter(self, value, receiver: int = 0) -> None:
        result = await self._send_command(command_name, {param_name: value, "receiver": receiver})
        if result.get("throttled"):
            # Confirmed live: the server's rate limiter (20/sec/command/
            # client -- web/handlers/control.py) ACKs a dropped command
            # as ok:true with result={"throttled": True} rather than an
            # error, so a fast slider drag firing many set_* calls in a
            # row silently drops the excess ones WITHOUT this ever
            # raising. Applying the optimistic write below unconditionally
            # made the slider jump straight to wherever the drag ended,
            # even on commands the radio never actually received -- then
            # a few seconds later the next real state sync corrected it
            # back to the radio's true value, looking like a random
            # partial snap-back. Skipping the write on a throttled ack
            # leaves the last command that actually landed as the
            # optimistic value until that real sync arrives, instead of
            # lying about one that didn't.
            return
        branch = self._receiver_branch(receiver)
        self._state.setdefault(branch, {})[state_path[-1]] = value

    return getter, setter


class RemoteWebRadio:
    """Async context manager -- same shape radio_worker.py already
    uses for rigplane's own create_radio(config) result:

        self._radio_cm = RemoteWebRadio(host, port, token)
        self.radio = await self._radio_cm.__aenter__()
        ...
        await self._radio_cm.__aexit__(None, None, None)

    Implements the SPECIFIC method names radio_worker.py actually
    calls -- confirmed by reading radio_worker.py directly, e.g.
    get_frequency()/set_frequency() (radio_worker.py's own names,
    which differ from the core Radio protocol's get_freq()/set_freq()
    -- rigplane's own IcomRadio class exposes both; radio_worker.py
    was written against the friendlier pair)."""

    def __init__(self, host: str, port: int, token: str = ""):
        self._host = host
        self._port = port
        self._token = token
        self._control_ws = None
        self._control_reader_task = None
        self._state: dict = {}
        self._capabilities: set = set()
        self._radio_model = "Remote Radio"
        self._pending_commands: dict = {}
        self._next_command_id = 0
        self._scope_ws = None
        self._scope_reader_task = None
        self._scope_callback = None
        self._audio_ws = None
        self._audio_reader_task = None
        self._rx_callback = None
        self._audio_sample_rate = 48000
        self._audio_channels = 1
        self._audio_tx_seq = 0

    # ---- Async context manager (Radio protocol lifecycle) ------------

    async def __aenter__(self) -> "RemoteWebRadio":
        self._control_ws = await _WebSocketClient.connect(self._host, self._port, "/api/v1/ws", token=self._token)
        # The server always sends exactly these two messages, in this
        # order, immediately after the WS upgrade -- confirmed live.
        # Consumed synchronously here (before the background reader
        # loop starts) so __aenter__ doesn't return until there's
        # real state to read from.
        _, payload = await self._control_ws.recv()
        hello = json.loads(payload)
        if hello.get("type") != "hello":
            raise RemoteRadioError(f"Expected the server's 'hello' message first, got {hello.get('type')!r}.")
        self._capabilities = set(hello.get("capabilities", []))
        self._radio_model = hello.get("radio") or self._radio_model

        _, payload = await self._control_ws.recv()
        initial = json.loads(payload)
        if initial.get("type") == "state_update":
            self._state = apply_delta(self._state, initial["data"])

        self._control_reader_task = asyncio.ensure_future(self._control_reader_loop())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        if self._control_ws is None:
            await self.__aenter__()

    async def disconnect(self) -> None:
        await self.disable_scope()
        await self._close_audio_ws()
        if self._control_reader_task is not None:
            self._control_reader_task.cancel()
            try:
                await self._control_reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._control_reader_task = None
        if self._control_ws is not None:
            await self._control_ws.close()
            self._control_ws = None
        # Any command still awaiting a reply when the connection drops
        # would otherwise hang its caller forever.
        for future in self._pending_commands.values():
            if not future.done():
                future.set_exception(RemoteRadioError("Disconnected while waiting for a command reply."))
        self._pending_commands.clear()

    @property
    def connected(self) -> bool:
        return self._control_ws is not None

    @property
    def radio_ready(self) -> bool:
        return self.connected and bool(self._state)

    @property
    def model(self) -> str:
        return self._radio_model

    @property
    def backend_id(self) -> str:
        return "torca_remote_web"

    @property
    def capabilities(self) -> set:
        return set(self._capabilities)

    def supports_command(self, command: str) -> bool:
        # The server enforces its own command whitelist and reports a
        # clear "unsupported_command" error if it's ever wrong -- see
        # _send_command -- so this is intentionally permissive rather
        # than duplicating that whitelist here to maintain in lockstep.
        return True

    # ---- Internal: control channel plumbing ---------------------------

    async def _control_reader_loop(self) -> None:
        try:
            while True:
                _, payload = await self._control_ws.recv()
                try:
                    msg = json.loads(payload)
                except ValueError:
                    continue
                msg_type = msg.get("type")
                if msg_type == "state_update":
                    self._state = apply_delta(self._state, msg["data"])
                elif msg_type == "response":
                    future = self._pending_commands.pop(msg.get("id"), None)
                    if future is not None and not future.done():
                        future.set_result(msg)
                # subscribe acks / dx_spots / notifications: not needed
                # by radio_worker.py yet, ignored rather than erroring.
        except asyncio.CancelledError:
            raise
        except Exception:
            # Connection genuinely died. Leave self._control_ws as-is
            # (not None) so `connected` doesn't lie about a clean
            # disconnect -- the next real call will fail naturally and
            # get reported through radio_worker.py's own per-call
            # try/except, same as a local radio dropping mid-session.
            pass

    async def _send_command(self, name: str, params: dict = None, timeout: float = _DEFAULT_COMMAND_TIMEOUT) -> dict:
        if self._control_ws is None:
            raise RemoteRadioError(f"{name}: not connected.")
        command_id = f"torca-{self._next_command_id}"
        self._next_command_id += 1
        future = asyncio.get_event_loop().create_future()
        self._pending_commands[command_id] = future
        try:
            await self._control_ws.send_text(json.dumps({
                "type": "cmd", "id": command_id, "name": name, "params": params or {},
            }))
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_commands.pop(command_id, None)
        if not response.get("ok", False):
            raise RemoteRadioError(f"{name} failed: {response.get('message') or response.get('error') or 'unknown error'}")
        return response.get("result") or {}

    @staticmethod
    def _receiver_branch(receiver: int) -> str:
        return "sub" if receiver else "main"

    # ---- Frequency -----------------------------------------------------

    async def get_frequency(self, receiver: int = 0) -> int:
        branch = self._receiver_branch(receiver)
        return int(self._state.get(branch, {}).get("freqHz", 0))

    async def set_frequency(self, freq_hz: int, receiver: int = 0) -> None:
        # Confirmed live: rapid tuning-knob turns hit the server's 20/sec
        # set_* rate limit constantly -- a throttled command ACKs ok:true
        # with result={"throttled": True} rather than an error (see
        # _level_property's setter for the full explanation), so this
        # must skip the optimistic write on a throttled ack or the
        # displayed frequency lies about a turn the radio never received.
        result = await self._send_command("set_freq", {"freq": freq_hz, "receiver": receiver})
        if result.get("throttled"):
            return
        branch = self._receiver_branch(receiver)
        self._state.setdefault(branch, {})["freqHz"] = result.get("freq", freq_hz)

    # ---- Mode ------------------------------------------------------------

    async def get_mode(self, receiver: int = 0) -> tuple:
        branch = self._receiver_branch(receiver)
        node = self._state.get(branch, {})
        return (node.get("mode", "USB"), node.get("filter"))

    async def set_mode(self, mode: str, filter_width=None, receiver: int = 0) -> None:
        params = {"mode": mode, "receiver": receiver}
        if filter_width is not None:
            params["filter"] = filter_width
        result = await self._send_command("set_mode", params)
        if result.get("throttled"):
            return
        branch = self._receiver_branch(receiver)
        self._state.setdefault(branch, {})["mode"] = mode

    async def get_data_mode(self) -> bool:
        return bool(self._state.get("main", {}).get("dataMode", 0))

    async def set_data_mode(self, on, receiver: int = 0) -> None:
        await self._send_command("set_data_mode", {"on": on, "receiver": receiver})

    # ---- TX ----------------------------------------------------------------

    async def set_ptt(self, on: bool) -> None:
        await self._send_command("ptt_on" if on else "ptt_off")
        self._state["ptt"] = bool(on)

    # ---- VFO / split -------------------------------------------------------

    async def get_vfo_slot(self, receiver: int = 0) -> str:
        branch = self._receiver_branch(receiver)
        return self._state.get(branch, {}).get("activeSlot", "A")

    async def set_vfo_slot(self, slot: str, receiver: int = 0) -> None:
        result = await self._send_command("set_vfo", {"vfo": slot, "receiver": receiver})
        if result.get("throttled"):
            return
        branch = self._receiver_branch(receiver)
        self._state.setdefault(branch, {})["activeSlot"] = slot

    async def get_split(self) -> bool:
        return bool(self._state.get("split", False))

    async def set_split(self, on: bool) -> None:
        result = await self._send_command("set_split", {"on": on})
        if result.get("throttled"):
            return
        self._state["split"] = bool(on)

    # ---- CW (key speed / pitch) -- matches radio_worker.py's own names ----

    async def get_key_speed(self) -> int:
        return int(self._state.get("keySpeed", 0))

    async def set_key_speed(self, wpm: int) -> None:
        result = await self._send_command("set_key_speed", {"speed": wpm})
        if result.get("throttled"):
            return
        self._state["keySpeed"] = wpm

    async def get_cw_pitch(self) -> int:
        return int(self._state.get("cwPitch", 600))

    async def set_cw_pitch(self, pitch_hz: int) -> None:
        result = await self._send_command("set_cw_pitch", {"value": pitch_hz})
        if result.get("throttled"):
            return
        self._state["cwPitch"] = pitch_hz

    async def send_cw_text(self, text: str) -> None:
        await self._send_command("send_cw_text", {"text": text})

    async def stop_cw_text(self) -> None:
        await self._send_command("stop_cw_text")

    # ---- Meters (TX-wide -- top-level state fields, confirmed live) -------

    async def get_s_meter(self, receiver: int = 0) -> int:
        return int(self._state.get(self._receiver_branch(receiver), {}).get("sMeter", 0))

    async def get_swr_meter(self) -> int:
        return int(self._state.get("swrMeter", 0))

    async def get_alc_meter(self) -> int:
        return int(self._state.get("alcMeter", 0))

    async def get_power_meter(self) -> int:
        return int(self._state.get("powerMeter", 0))

    async def get_comp_meter(self) -> int:
        return int(self._state.get("compMeter", 0))

    async def get_vd_meter(self) -> int:
        return int(self._state.get("vdMeter", 0))

    async def get_id_meter(self) -> int:
        return int(self._state.get("idMeter", 0))

    # ---- Levels/controls generated from a table (LEVEL_DEFINITIONS/ --------
    # CONTROL_DEFINITIONS in constants.py) -- pattern confirmed live for
    # af_level/rf_gain/squelch/nb/nr/preamp/agc/attenuator; the rest
    # follow the identical (command name == set_<field>, state field ==
    # <field>) shape already confirmed for those, per this file's own
    # module docstring.
    get_af_level, set_af_level = _level_property("set_af_level", ("afLevel",))
    get_rf_gain, set_rf_gain = _level_property("set_rf_gain", ("rfGain",))
    get_squelch, set_squelch = _level_property("set_squelch", ("squelch",))
    get_attenuator_level, set_attenuator_level = _level_property("set_att", ("att",), param_name="db")
    get_preamp, set_preamp = _level_property("set_preamp", ("preamp",), param_name="level")
    get_nb, set_nb = _level_property("set_nb", ("nb",), param_name="on")
    get_nr, set_nr = _level_property("set_nr", ("nr",), param_name="on")
    get_agc, set_agc = _level_property("set_agc", ("agc",), param_name="value")
    # rigplane's LevelsCapable Protocol (core/radio_protocol.py) requires
    # these five in addition to af_level/rf_gain/squelch above -- NOT used
    # by anything in LEVEL_DEFINITIONS/CONTROL_DEFINITIONS (confirmed via
    # grep; the app never calls them directly), but radio_worker.py's own
    # _setup_levels() gates its ENTIRE LEVEL_DEFINITIONS discovery loop
    # behind isinstance(self.radio, LevelsCapable) -- missing even one of
    # these silently failed that check and disabled every level slider
    # (af_gain/squelch/rf_level/monitor/tx_level) for every remote
    # connection, not just the ones these five cover. Confirmed live: this
    # was exactly the "sliders don't seem to be working" bug. nr_level/
    # nb_level take a receiver kwarg per the Protocol and the web command
    # (web/handlers/control.py's set_nr_level/set_nb_level cases) --
    # mic_gain/drive_gain/compressor_level don't (confirmed via both the
    # Protocol signatures and the web command param shapes), so those
    # three are hand-written below rather than using _level_property.
    get_nr_level, set_nr_level = _level_property("set_nr_level", ("nrLevel",))
    get_nb_level, set_nb_level = _level_property("set_nb_level", ("nbLevel",))
    # This IS a real LEVEL_DEFINITIONS entry radio_worker.py actually
    # calls ("tx_level") -- unlike the five above, it was simply never
    # implemented at all in Phase 1, a second, independent gap alongside
    # the isinstance(LevelsCapable) one. (Monitor -- get_monitor_gain/
    # set_monitor_gain -- was implemented too but later removed from
    # LEVEL_DEFINITIONS entirely; no longer needed here.)
    #
    # NOT built with _level_property, unlike every other entry in this
    # block: powerLevel is a TOP-LEVEL state field (alongside cwPitch/
    # keySpeed -- confirmed via web/state_schema.py), not nested under
    # state["main"]/["sub"] the way afLevel/rfGain/squelch/nrLevel/
    # nbLevel genuinely are. _level_property's getter/setter always reads
    # and writes under a receiver branch, so using it here would repeat
    # the exact bug monitorGain had (confirmed live before its removal):
    # the optimistic-cache read-after-write in a quick test stayed self-
    # consistent (wrong location, but the same wrong location for both
    # get and set), masking that the real server-pushed state_update --
    # which correctly lands these at the top level -- was never actually
    # being read from, making the poll loop's getter call return None
    # every single cycle and crash _normalize_level_value's float(None).
    # Hand-written the same way as mic_gain/drive_gain/compressor_level
    # above, which don't have this bug for the same reason: they were
    # never run through _level_property in the first place. powerLevel
    # is a float -- doesn't matter which scale it uses: radio_worker.py's
    # own _normalize_level_value already auto-detects raw-vs-normalized
    # on the way in, and the server's _normalized_or_raw_level does the
    # same on the way out.

    async def get_power(self):
        return self._state.get("powerLevel", 0.0)

    async def set_power(self, level) -> None:
        result = await self._send_command("set_power", {"level": level})
        if result.get("throttled"):
            return
        self._state["powerLevel"] = level

    async def get_mic_gain(self) -> int:
        return int(self._state.get("micGain", 0))

    async def set_mic_gain(self, level: int) -> None:
        result = await self._send_command("set_mic_gain", {"level": level})
        if result.get("throttled"):
            return
        self._state["micGain"] = level

    async def get_drive_gain(self) -> int:
        return int(self._state.get("driveGain", 0))

    async def set_drive_gain(self, level: int) -> None:
        result = await self._send_command("set_drive_gain", {"level": level})
        if result.get("throttled"):
            return
        self._state["driveGain"] = level

    async def get_compressor_level(self) -> int:
        return int(self._state.get("compressorLevel", 0))

    async def set_compressor_level(self, level: int) -> None:
        result = await self._send_command("set_compressor_level", {"level": level})
        if result.get("throttled"):
            return
        self._state["compressorLevel"] = level

    # ---- Scope (spectrum/waterfall) ----------------------------------------
    # Only the methods radio_worker.py actually calls (grepped directly,
    # not the full rigplane ScopeCapable Protocol surface -- radio_worker.py
    # calls these unconditionally in a try/except rather than gating behind
    # an isinstance() check, so nothing else needs to structurally match).
    #
    # Scope frames arrive unsolicited on their own WS channel (/api/v1/scope)
    # once opened -- confirmed by reading web/handlers/scope.py: the server
    # auto-enables the radio's scope the moment a client connects
    # (ScopeHandler.run -> server.ensure_scope_enabled), no control message
    # needed. There's no decode counterpart to protocol.py's
    # encode_scope_frame in rigplane itself (only the server-side encoder
    # exists -- its only client is the bundled browser SPA, which decodes
    # in JS), so the 16-byte header is unpacked here by hand, mirroring
    # encode_scope_frame's own layout byte-for-byte.

    def on_scope_data(self, callback) -> None:
        self._scope_callback = callback

    async def enable_scope(self, **kwargs) -> None:
        if self._scope_ws is not None:
            return
        self._scope_ws = await _WebSocketClient.connect(self._host, self._port, "/api/v1/scope", token=self._token)
        self._scope_reader_task = asyncio.ensure_future(self._scope_reader_loop())

    async def disable_scope(self) -> None:
        if self._scope_reader_task is not None:
            self._scope_reader_task.cancel()
            try:
                await self._scope_reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._scope_reader_task = None
        if self._scope_ws is not None:
            await self._scope_ws.close()
            self._scope_ws = None

    async def _scope_reader_loop(self) -> None:
        try:
            while True:
                opcode, payload = await self._scope_ws.recv()
                if opcode != _WS_OP_BINARY or len(payload) < _SCOPE_HEADER_SIZE:
                    continue
                if payload[0] != _SCOPE_MSG_TYPE:
                    continue
                receiver, mode = payload[1], payload[2]
                start_hz, end_hz = struct.unpack_from("<II", payload, 3)
                flags = payload[13]
                pixel_count = struct.unpack_from("<H", payload, 14)[0]
                pixels = payload[_SCOPE_HEADER_SIZE:_SCOPE_HEADER_SIZE + pixel_count]
                frame = ScopeFrame(
                    receiver=receiver,
                    mode=mode,
                    start_freq_hz=start_hz,
                    end_freq_hz=end_hz,
                    pixels=pixels,
                    out_of_range=bool(flags & 0x01),
                )
                if self._scope_callback is not None:
                    self._scope_callback(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Scope channel died -- same "let the next real call fail
            # naturally" approach as _control_reader_loop above.
            pass

    async def get_scope_span(self) -> int:
        return int(self._state.get("scopeControls", {}).get("span", 0))

    async def set_scope_span(self, span_index: int) -> None:
        result = await self._send_command("set_scope_span", {"span": span_index})
        if result.get("throttled"):
            return
        self._state.setdefault("scopeControls", {})["span"] = span_index

    async def get_scope_ref(self) -> float:
        return float(self._state.get("scopeControls", {}).get("refDb", 0.0))

    async def set_scope_ref(self, ref_db: float) -> None:
        # The web command's "ref" param is an int (confirmed via
        # web/handlers/control.py: `ref = int(params["ref"])`) -- half-dB
        # precision is lost going through the remote API even though a
        # local connection supports 0.5 dB steps. A rigplane server-side
        # limitation, not something to work around here.
        ref_int = int(round(ref_db))
        result = await self._send_command("set_scope_ref", {"ref": ref_int})
        if result.get("throttled"):
            return
        self._state.setdefault("scopeControls", {})["refDb"] = float(ref_int)

    async def get_scope_speed(self) -> int:
        return int(self._state.get("scopeControls", {}).get("speed", 0))

    async def set_scope_speed(self, speed_index: int) -> None:
        result = await self._send_command("set_scope_speed", {"speed": speed_index})
        if result.get("throttled"):
            return
        self._state.setdefault("scopeControls", {})["speed"] = speed_index

    async def set_scope_receiver(self, receiver: int) -> None:
        # Command name is "switch_scope_receiver", NOT "set_scope_receiver"
        # -- confirmed directly from web/handlers/control.py's command
        # whitelist; every other scope setter here does follow the
        # set_<field> pattern, this one just doesn't.
        await self._send_command("switch_scope_receiver", {"receiver": receiver})
        self._state.setdefault("scopeControls", {})["receiver"] = receiver

    # ---- Audio (RX/TX, PCM16 only for v1 -- Opus/WebRTC deferred per plan) --
    # Unlike scope, radio_worker.py DOES gate audio behind a structural
    # isinstance(self.radio, AudioTransport) check (radio_worker.py:364) --
    # confirmed by reading rigplane's AudioTransport Protocol in full
    # (core/radio_protocol.py:877-963): 5 properties + 5 methods, all
    # implemented below. RX and TX share ONE /api/v1/audio WebSocket
    # connection (confirmed via web/handlers/audio.py's AudioHandler:
    # JSON audio_start/audio_stop control text interleaved with binary
    # RX frames from the server and binary TX frames from the client, on
    # the same connection) -- opened lazily on whichever of start_rx()/
    # start_tx() is called first, closed only in disconnect(). TX frames
    # are built with rigplane's own encode_audio_frame (reused directly,
    # like apply_delta above) since the server's _handle_tx_audio only
    # ever reads the first 2 header bytes (msg_type, codec) -- confirmed
    # by reading it -- so the seq/sample_rate/channels/frame_ms fields
    # sent here are accepted but functionally unused server-side.

    @property
    def audio_bus(self):
        # Nothing in this app reads .audio_bus directly (confirmed via
        # grep of audio.py/radio_worker.py) -- it exists only so
        # isinstance(remote_radio, AudioTransport) is structurally true.
        return None

    @property
    def audio_codec(self) -> AudioCodec:
        # audio.py's _is_stereo_rx_codec() keys off "2CH" in this name to
        # decide whether to downmix -- report the server's actual
        # negotiated channel count (from the audio_format ack) rather
        # than assuming mono.
        return AudioCodec.PCM_2CH_16BIT if self._audio_channels == 2 else AudioCodec.PCM_1CH_16BIT

    @property
    def audio_tx_codec(self) -> AudioCodec:
        # TX capture (audio.py) is always mono (constants.AUDIO_CHANNELS
        # == 1); the server transcodes to whatever the actual radio's TX
        # codec needs, so this only needs to satisfy audio.py's own
        # "contains pcm" validation, not describe the real radio.
        return AudioCodec.PCM_1CH_16BIT

    @property
    def audio_sample_rate(self) -> int:
        return self._audio_sample_rate

    @property
    def audio_duplex_mode(self):
        # RX and TX flow over the same full-duplex WebSocket/TCP
        # connection simultaneously -- no exclusive-device contention
        # the way a local shared OS audio device would have.
        return "full"

    async def _ensure_audio_ws(self) -> None:
        if self._audio_ws is not None:
            return
        self._audio_ws = await _WebSocketClient.connect(self._host, self._port, "/api/v1/audio", token=self._token)
        self._audio_reader_task = asyncio.ensure_future(self._audio_reader_loop())

    async def _close_audio_ws(self) -> None:
        if self._audio_reader_task is not None:
            self._audio_reader_task.cancel()
            try:
                await self._audio_reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._audio_reader_task = None
        if self._audio_ws is not None:
            await self._audio_ws.close()
            self._audio_ws = None

    async def _audio_reader_loop(self) -> None:
        try:
            while True:
                opcode, payload = await self._audio_ws.recv()
                if opcode == _WS_OP_TEXT:
                    try:
                        msg = json.loads(payload)
                    except ValueError:
                        continue
                    if msg.get("type") == "audio_format":
                        self._audio_sample_rate = int(msg.get("sample_rate", self._audio_sample_rate))
                        self._audio_channels = int(msg.get("channels", self._audio_channels))
                    continue
                if opcode != _WS_OP_BINARY or len(payload) < AUDIO_HEADER_SIZE:
                    continue
                if payload[0] != MSG_TYPE_AUDIO_RX:
                    continue
                seq = struct.unpack_from("<H", payload, 2)[0]
                data = payload[AUDIO_HEADER_SIZE:]
                if self._rx_callback is not None:
                    self._rx_callback(AudioPacket(ident=0, send_seq=seq, data=data))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def start_rx(self, callback) -> None:
        await self._ensure_audio_ws()
        self._rx_callback = callback
        await self._audio_ws.send_text(json.dumps({
            "type": "audio_start", "direction": "rx", "preferred_rx_codec": "pcm16",
        }))

    async def stop_rx(self) -> None:
        self._rx_callback = None
        if self._audio_ws is not None:
            try:
                await self._audio_ws.send_text(json.dumps({"type": "audio_stop", "direction": "rx"}))
            except Exception:
                pass

    async def start_tx(self) -> None:
        await self._ensure_audio_ws()
        await self._audio_ws.send_text(json.dumps({"type": "audio_start", "direction": "tx"}))

    async def push_tx(self, data: bytes) -> None:
        if self._audio_ws is None:
            raise RemoteRadioError("push_tx: audio channel not open (call start_tx() first).")
        # frame_ms is advisory only (confirmed via encode_audio_frame's own
        # docstring and _handle_tx_audio ignoring it server-side) -- derived
        # from the actual payload the same way the broadcaster derives it
        # for RX, purely for wire-format consistency.
        frame_ms = max(1, min((len(data) * 1000) // max(1, self._audio_sample_rate * 2), 255))
        frame = encode_audio_frame(
            MSG_TYPE_AUDIO_TX, AUDIO_CODEC_PCM16, self._audio_tx_seq,
            self._audio_sample_rate // 100, 1, frame_ms, data,
        )
        self._audio_tx_seq = (self._audio_tx_seq + 1) & 0xFFFF
        await self._audio_ws.send_binary(frame)

    async def stop_tx(self) -> None:
        if self._audio_ws is not None:
            try:
                await self._audio_ws.send_text(json.dumps({"type": "audio_stop", "direction": "tx"}))
            except Exception:
                pass
