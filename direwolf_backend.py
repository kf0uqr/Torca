"""
DirewolfBackend: an alternative APRS decode backend using direwolf
(https://github.com/wb2osz/direwolf), a mature, widely-used open-source
software TNC, in place of this app's own from-scratch Bell 202 AFSK/
AX.25 demodulator (aprs.py's AprsDecoder/_AfskBitSync/_HdlcDeframer).

Decode only -- direwolf CAN also transmit, but doing so needs a real
audio OUTPUT device (confirmed via direwolf's own example config:
"Something different must be specified for output" once the input is
UDP/stdin -- there's no UDP/stdin option for output), which would mean
routing that audio back out through a virtual/loopback device this app
would then have to capture -- real added complexity for no real
benefit, since this app's own AFSK encoder (aprs.py's
build_position_packet_pcm) already sends successfully via RadioWorker.
send_tx_audio_pcm with no such device juggling. So: TX still always
goes through this app's own encoder, regardless of which decode
backend is selected.

How it's wired up, each piece confirmed against direwolf's own
documentation/example config (conf/generic.conf) and command-line
help, not guessed:

- Launched as a managed subprocess (QProcess), configured via a
  generated temp direwolf.conf to read raw PCM audio from a local UDP
  port ("ADEVICE UDP:<port> <unused-output>" is a documented,
  supported input method -- generic.conf's own comment: "You can also
  specify UDP: and an optional port for input. Something different
  must be specified for output.") and export decoded packets over its
  KISS-over-TCP virtual TNC port (KISSPORT directive, default 8001 --
  this app picks its own free port instead, to avoid colliding with a
  separately-running direwolf instance).
- feed_audio() sends the SAME raw PCM bytes RadioWorker.
  start_aprs_decode()'s callback already receives, over that UDP
  socket -- no format conversion, same rate the radio already
  provides (passed to direwolf via its own -r command-line option,
  confirmed via `direwolf --help`: "-r: Audio sample rate per second
  for first channel").
- Decoded packets arrive as KISS frames over the TCP connection --
  kiss frame format (FEND=0xC0/FESC=0xDB/TFEND=0xDC/TFESC=0xDD framing
  and byte-stuffing, command byte low nibble 0x00 for a data frame)
  confirmed against the original "KISS TNC" protocol spec (Chepponis/
  Karn), unchanged in decades. The AX.25 frame bytes inside a KISS
  data frame (address/control/PID/info, FCS already verified and
  stripped by direwolf itself) are IDENTICAL in shape to what aprs.py's
  own _HdlcDeframer already hands to parse_ax25_frame -- so packet
  parsing itself (parse_ax25_frame, parse_aprs_info) is reused as-is,
  only the demodulation/framing layer is replaced.
- PTT is explicitly disabled in direwolf's own config (PTT NONE) --
  RadioWorker already owns PTT via CI-V/CAT for every other TX path
  (voice, CW, this app's own APRS send), and a second, independent PTT
  source would be a real hazard (two things deciding when to key the
  transmitter, with no coordination between them).
"""

import os
import shutil
import socket
import tempfile
import pathlib

from PySide6.QtCore import QObject, QProcess, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QTcpSocket, QUdpSocket

from aprs import parse_ax25_frame, parse_aprs_info

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD


def direwolf_available() -> bool:
    """True if a `direwolf` executable is on PATH -- checked before
    offering the Direwolf backend as an option at all, so choosing it
    fails fast with a clear message instead of a mysterious subprocess
    launch failure."""
    return shutil.which("direwolf") is not None


import random

# direwolf's own KISSPORT parser rejects anything outside this range
# ("Invalid TCP port number for KISS TCPIP Socket Interface. Use
# something in the range of 1024 to 49151.") -- confirmed via a live
# run where an OS-assigned ephemeral port above 49151 was silently
# rejected, direwolf fell back to its own default port 8001, and our
# client kept retrying against the port direwolf had actually refused,
# never connecting. The plain socket.bind(('', 0)) trick used
# elsewhere in this codebase (e.g. remote_radio.py) picks from the
# OS's full ephemeral range and isn't safe to reuse here.
_DIREWOLF_PORT_MIN = 20000
_DIREWOLF_PORT_MAX = 49151


def _find_free_port() -> int:
    for _ in range(50):
        port = random.randint(_DIREWOLF_PORT_MIN, _DIREWOLF_PORT_MAX)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("Direwolf backend: could not find a free port in the allowed range.")


def _kiss_unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        byte = data[i]
        if byte == FESC and i + 1 < n:
            nxt = data[i + 1]
            if nxt == TFEND:
                out.append(FEND)
                i += 2
                continue
            if nxt == TFESC:
                out.append(FESC)
                i += 2
                continue
        out.append(byte)
        i += 1
    return bytes(out)


class DirewolfBackend(QObject):
    """Manages one direwolf subprocess plus its UDP audio input and
    KISS TCP output, for exactly one radio's APRS decode session.
    Entirely event-driven via Qt (QProcess/QTcpSocket/QUdpSocket), no
    separate thread -- same reasoning as dxcluster.py's DxClusterClient.

    packet_received(dict) uses the exact same shape aprs.AprsDecoder.
    feed()'s return-list entries already have ({"source",
    "destination", "digipeaters", "info"}), so aprs_window.py's
    existing packet-handling code needs no changes regardless of which
    backend produced a given packet. status(str)/error(str) report
    subprocess output and connection lifecycle, same convention as
    RadioWorker's own audio_status/error signals."""

    packet_received = Signal(dict)
    status = Signal(str)
    error = Signal(str)

    def __init__(self, sample_rate: int, mycall: str = "N0CALL", parent=None):
        super().__init__(parent)
        self.sample_rate = sample_rate
        self._mycall = (mycall or "N0CALL").split("-")[0][:6].upper() or "N0CALL"
        self._audio_port = _find_free_port()
        self._kiss_port = _find_free_port()
        self._config_path = None

        self._stopping = False
        self._kiss_connect_attempts = 0

        self._process = QProcess(self)
        self._process.started.connect(self._on_process_started)
        self._process.readyReadStandardOutput.connect(self._on_process_output)
        self._process.readyReadStandardError.connect(self._on_process_output)
        self._process.errorOccurred.connect(self._on_process_error)

        self._udp_socket = QUdpSocket(self)

        self._kiss_socket = QTcpSocket(self)
        self._kiss_socket.readyRead.connect(self._on_kiss_ready_read)
        self._kiss_socket.errorOccurred.connect(self._on_kiss_socket_error)
        self._kiss_buffer = bytearray()

    def start(self):
        if not direwolf_available():
            self.error.emit("Direwolf backend: 'direwolf' executable not found on PATH -- install it first.")
            return
        self._config_path = self._write_config()
        self._process.start("direwolf", [
            "-c", str(self._config_path),
            "-r", str(self.sample_rate),
            "-n", "1",
            "-b", "16",
            "-t", "0",
        ])

    def _write_config(self):
        fd, path = tempfile.mkstemp(prefix="torca_direwolf_", suffix=".conf")
        config = (
            f"ADEVICE UDP:{self._audio_port} default\n"
            "CHANNEL 0\n"
            f"MYCALL {self._mycall}\n"
            "MODEM 1200\n"
            # No PTT line at all -- confirmed by direct test run that
            # "PTT NONE" is NOT valid direwolf config syntax (it errors
            # with "Missing RTS or DTR after PTT device name"); simply
            # omitting the PTT directive already gives the same result
            # ("PTT not configured for channel 0"), which is what's
            # wanted here -- RadioWorker is the sole PTT owner.
            f"KISSPORT {self._kiss_port}\n"
        )
        with os.fdopen(fd, "w") as f:
            f.write(config)
        return pathlib.Path(path)

    def feed_audio(self, pcm_bytes: bytes):
        """Call from the SAME callback RadioWorker.start_aprs_decode()
        already drives (radio_worker's asyncio-loop thread) -- a
        plain, synchronous UDP send, so no cross-thread marshaling is
        needed despite being called off the GUI thread; QUdpSocket is
        used here purely for its writeDatagram() convenience, nothing
        about this call touches Qt's event loop or any widget."""
        self._udp_socket.writeDatagram(pcm_bytes, QHostAddress.LocalHost, self._audio_port)

    def _on_process_started(self):
        self.status.emit(f"Direwolf: process started (PID {self._process.processId()}).")
        # direwolf needs real time (measured: close to a second) after
        # the OS process starts before its KISS TCP listener is
        # actually accepting -- confirmed via a live run where an
        # immediate connectToHost() got "Connection refused". Retry a
        # few times on a short timer rather than connecting once.
        self._kiss_connect_attempts = 0
        self._try_connect_kiss()

    def _try_connect_kiss(self):
        if self._stopping:
            return
        self._kiss_connect_attempts += 1
        self._kiss_socket.connectToHost(QHostAddress.LocalHost, self._kiss_port)

    def _on_process_output(self):
        text = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        text += bytes(self._process.readAllStandardError()).decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.strip():
                self.status.emit(f"Direwolf: {line.strip()}")

    def _on_process_error(self, _proc_error):
        if self._stopping:
            # A "Crashed" error is the normal, expected result of our
            # OWN terminate() call (Qt reports a SIGTERM exit this
            # way, confirmed via a live stop() during testing) -- not
            # a real failure worth surfacing.
            return
        self.error.emit(f"Direwolf: process error ({self._process.errorString()}).")

    def _on_kiss_socket_error(self, _socket_error):
        if self._stopping:
            return
        # The listener isn't up the instant the process starts
        # (confirmed via a live run: an immediate connect attempt got
        # "Connection refused") -- retry a handful of times before
        # giving up and surfacing a real error.
        if self._kiss_connect_attempts < 10:
            QTimer.singleShot(300, self._try_connect_kiss)
            return
        self.error.emit(f"Direwolf: KISS TCP connection error ({self._kiss_socket.errorString()}).")

    def _on_kiss_ready_read(self):
        self._kiss_buffer.extend(bytes(self._kiss_socket.readAll()))
        while True:
            try:
                start = self._kiss_buffer.index(FEND)
            except ValueError:
                self._kiss_buffer.clear()
                return
            try:
                end = self._kiss_buffer.index(FEND, start + 1)
            except ValueError:
                if start > 0:
                    del self._kiss_buffer[:start]  # drop leading garbage before the first FEND we do have
                return
            raw_frame = bytes(self._kiss_buffer[start + 1:end])
            del self._kiss_buffer[:end + 1]
            if raw_frame:
                self._handle_kiss_frame(raw_frame)

    def _handle_kiss_frame(self, raw_frame: bytes):
        command = raw_frame[0]
        if command & 0x0F != 0x00:
            return  # not a data frame (e.g. TXDELAY or another TNC-control command) -- nothing to decode
        ax25_frame = _kiss_unescape(raw_frame[1:])
        parsed = parse_ax25_frame(ax25_frame)
        if parsed is None:
            return
        parsed["info"] = parse_aprs_info(parsed["info"]) if parsed["info"] else None
        self.packet_received.emit(parsed)

    def stop(self):
        self._stopping = True
        self._kiss_socket.disconnectFromHost()
        if self._process.state() != QProcess.NotRunning:
            self._process.terminate()
            if not self._process.waitForFinished(2000):
                self._process.kill()
        if self._config_path is not None:
            try:
                self._config_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._config_path = None
