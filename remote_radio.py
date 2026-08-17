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

Not yet implemented (see the plan's phasing -- this file covers
control/state only so far): scope (`/api/v1/scope`) and audio
(`/api/v1/audio`). `isinstance(remote_radio, ScopeCapable)` /
`isinstance(remote_radio, AudioTransport)` are both `@runtime_checkable`
Protocols (confirmed by reading rigplane/core/radio_protocol.py) --
purely structural, satisfied by which methods a class defines, no
registration needed -- so those capabilities will "turn on" for
radio_worker.py automatically the moment their methods are added here,
with no changes needed on radio_worker.py's side.
"""

import asyncio
import base64
import json
import os
import struct

from rigplane.web._delta_encoder import apply_delta

_WS_OP_TEXT = 0x1
_WS_OP_BINARY = 0x2
_WS_OP_CLOSE = 0x8
_WS_OP_PING = 0x9
_WS_OP_PONG = 0xA

_DEFAULT_COMMAND_TIMEOUT = 5.0


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
        await self._send_command(command_name, {param_name: value, "receiver": receiver})
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
        return "radione_remote_web"

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
        command_id = f"radione-{self._next_command_id}"
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
        result = await self._send_command("set_freq", {"freq": freq_hz, "receiver": receiver})
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
        await self._send_command("set_mode", params)
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
        await self._send_command("set_vfo", {"vfo": slot, "receiver": receiver})
        branch = self._receiver_branch(receiver)
        self._state.setdefault(branch, {})["activeSlot"] = slot

    async def get_split(self) -> bool:
        return bool(self._state.get("split", False))

    async def set_split(self, on: bool) -> None:
        await self._send_command("set_split", {"on": on})
        self._state["split"] = bool(on)

    # ---- CW (key speed / pitch) -- matches radio_worker.py's own names ----

    async def get_key_speed(self) -> int:
        return int(self._state.get("keySpeed", 0))

    async def set_key_speed(self, wpm: int) -> None:
        await self._send_command("set_key_speed", {"speed": wpm})
        self._state["keySpeed"] = wpm

    async def get_cw_pitch(self) -> int:
        return int(self._state.get("cwPitch", 600))

    async def set_cw_pitch(self, pitch_hz: int) -> None:
        await self._send_command("set_cw_pitch", {"value": pitch_hz})
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
