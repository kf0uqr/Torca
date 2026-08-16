"""
DX Cluster telnet client -- the traditional AK1A-protocol packet
cluster network hams have used for decades to see "DX spots" (who's
currently on the air and worth working). Confirmed live against a
real public cluster (telnet.reversebeacon.net:7000): plain-text login
(server sends a prompt with no trailing newline, e.g. "Please enter
your call: "; client replies with its callsign + CRLF), then a
continuous stream of spot lines, one per line, each shaped like:

    DX de K9LC-#:   21038.00  K7DCG          CW     4 dB  14 WPM  CQ      1747Z

-- spotter callsign, frequency in kHz, spotted ("DX") callsign, a
freeform comment, and a HHMMZ timestamp. This exact regex shape (spotter/
frequency/dx-call/comment/time groups) matches the widely-documented
AK1A 80-column format used by essentially every DX cluster
implementation, not just this one server -- confirmed against a real
capture, including spotter callsigns with automated-skimmer suffixes
like "-#" and "-2-#".

Raw AK1A spot lines carry NO location data at all for the spotted
station -- just its callsign -- so this is a plain scrolling list
(ham_dashboard.py's DX Spots tab), not a map overlay.

Host/port are the operator's own choice -- there is no single
"correct" cluster, and different servers carry different mixes of
automated skimmer spots vs. human-submitted ones. DEFAULT_HOST/PORT is
a public server confirmed live and reachable with no registration
beyond sending your own callsign; ham_dashboard.py's DX Spots tab lets
this be changed to any other AK1A-compatible server.
"""

import re

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QTcpSocket

import adif

DEFAULT_HOST = "telnet.reversebeacon.net"
DEFAULT_PORT = 7000

# Spotter/DX callsign charset includes "#" (RBN automated-skimmer
# spotter suffix, e.g. "K9LC-#", "RK3TD-2-#") and "/" (portable/
# maritime suffixes like "SP8WT/P") alongside the usual letters/
# digits/hyphens -- confirmed against a real live capture.
_CALLSIGN = r"[A-Za-z0-9/#-]+"
DX_SPOT_RE = re.compile(
    r"^DX de (?P<spotter>" + _CALLSIGN + r"):\s*"
    r"(?P<freq>[0-9.]+)\s+"
    r"(?P<dx_call>" + _CALLSIGN + r")\s+"
    r"(?P<comment>.*?)\s*"
    r"(?P<time>\d{4}Z)\s*$"
)


def parse_dx_spot_line(line: str):
    """Returns a dict {"spotter", "frequency_hz", "band", "dx_call",
    "comment", "time"} if `line` is a well-formed "DX de ..." spot
    line, else None -- covers both genuinely different lines (login
    banner, "Local users:"/"Spot rate:" status, WWV/WCY propagation
    bulletins, talk/announce messages, the cluster's own "de RELAY ...
    >" prompt) and malformed ones, same "return None, caller skips it"
    shape as adif.grid_square_to_latlon."""
    match = DX_SPOT_RE.match(line.strip())
    if not match:
        return None
    try:
        freq_hz = int(round(float(match.group("freq")) * 1000))
    except ValueError:
        return None
    return {
        "spotter": match.group("spotter"),
        "frequency_hz": freq_hz,
        "band": adif.band_for_freq_hz(freq_hz) or "?",
        "dx_call": match.group("dx_call"),
        "comment": match.group("comment").strip(),
        "time": match.group("time"),
    }


class DxClusterClient(QObject):
    """A persistent telnet connection to one AK1A-compatible DX
    cluster server, entirely event-driven via QTcpSocket -- no
    separate thread, same reasoning as wsjtx_rigctld.py's QTcpServer-
    based RigctldServer. Emits spot_received(dict) for every parsed
    "DX de ..." line; everything else the server sends is received and
    silently ignored. Does NOT auto-reconnect -- disconnected() tells
    the caller it dropped, and start() can be called again explicitly
    if a retry is wanted."""

    spot_received = Signal(dict)
    connected = Signal()
    disconnected = Signal()
    error = Signal(str)

    def __init__(self, callsign, host=DEFAULT_HOST, port=DEFAULT_PORT, parent=None):
        super().__init__(parent)
        self._callsign = callsign
        self._host = host
        self._port = port
        self._socket = QTcpSocket(self)
        self._socket.readyRead.connect(self._on_ready_read)
        self._socket.connected.connect(self.connected)
        self._socket.disconnected.connect(self.disconnected)
        self._socket.errorOccurred.connect(self._on_error)
        self._buffer = b""
        self._logged_in = False

    def start(self):
        self._logged_in = False
        self._buffer = b""
        self._socket.connectToHost(self._host, self._port)

    def stop(self):
        self._socket.disconnectFromHost()

    def _on_error(self, _socket_error):
        self.error.emit(self._socket.errorString())

    def _on_ready_read(self):
        self._buffer += bytes(self._socket.readAll())
        if not self._logged_in:
            # Whatever the server sends first (before we've said
            # anything at all) is the login prompt -- confirmed live
            # it has no trailing newline ("Please enter your call: "),
            # so this can't be line-buffered the normal way; different
            # cluster software also phrases the prompt differently, so
            # this replies to ANY first byte received rather than
            # trying to pattern-match specific prompt text.
            self._socket.write((self._callsign + "\r\n").encode("ascii", errors="replace"))
            self._logged_in = True
            self._buffer = b""
            return
        while b"\n" in self._buffer:
            line, _sep, self._buffer = self._buffer.partition(b"\n")
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            spot = parse_dx_spot_line(text)
            if spot is not None:
                self.spot_received.emit(spot)
