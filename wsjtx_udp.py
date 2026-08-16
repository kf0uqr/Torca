"""
WSJT-X UDP Message Protocol listener -- auto-logs a QSO into the local
ADIF log the instant the WSJT-X operator clicks "OK" on WSJT-X's own
"Log QSO" dialog, via the "Logged ADIF" message WSJT-X broadcasts for
exactly that moment (message type 12 in WSJT-X's own protocol --
confirmed directly against WSJT-X's own protocol header comment block,
Network/NetworkMessage.hpp, not guessed or reconstructed from a
third-party client): "The logged ADIF message is sent to the server(s)
when the WSJT-X user accepts the 'Log QSO' dialog by clicking the 'OK'
button." Sent automatically, unrequested -- no handshake/subscription
needed, this listener just has to be listening on the right port.

Every UDP datagram WSJT-X sends (regardless of message type) starts
with the same header, confirmed from the same source:
  quint32 magic    0xadbccbda  ("never change this", per WSJT-X's own comment)
  quint32 schema   2 or 3 depending on WSJT-X/Qt version -- irrelevant
                    here, only the fields AFTER it matter
  quint32 type     message type -- only 12 (LoggedADIF) is ever acted
                    on; every other type (Heartbeat=0, Status=1,
                    Decode=2, Clear=3, Reply=4, QSOLogged=5, Close=6,
                    Replay=7, HaltTx=8, FreeText=9, WSPRDecode=10,
                    Location=11, HighlightCallsign=13) is received
                    (they're all broadcast to the same port) and
                    silently ignored.
For type 12 specifically, two more fields follow:
  utf8   Id         WSJT-X's own instance id string -- read but unused
  utf8   ADIF text  the exact ADIF record text for the just-logged QSO

"utf8" is WSJT-X's own custom field type, NOT Qt's native QString wire
format -- confirmed from the same source: a quint32 byte-length prefix
(0xffffffff means null/absent, 0 means an empty string) followed by
that many raw UTF-8 bytes, no terminator. All integers are big-endian
(QDataStream's default, also confirmed from the same source).
"""

import struct

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QUdpSocket, QHostAddress, QAbstractSocket

import adif

WSJTX_UDP_MAGIC = 0xADBCCBDA
WSJTX_MESSAGE_TYPE_LOGGED_ADIF = 12
WSJTX_DEFAULT_PORT = 2237  # WSJT-X's own documented default "UDP Server port" (Settings > Reporting)


def _read_uint32(data: bytes, offset: int):
    if offset + 4 > len(data):
        raise ValueError("truncated message reading a uint32")
    return struct.unpack_from(">I", data, offset)[0], offset + 4


def _read_utf8(data: bytes, offset: int):
    length, offset = _read_uint32(data, offset)
    if length == 0xFFFFFFFF:
        return None, offset
    if offset + length > len(data):
        raise ValueError("truncated message reading a utf8 field")
    return data[offset:offset + length].decode("utf-8", errors="replace"), offset + length


def parse_logged_adif_message(data: bytes):
    """Returns the ADIF text (str) if `data` is a well-formed WSJT-X
    "Logged ADIF" (type 12) UDP message, else None -- covers both
    "genuinely not one" (any other WSJT-X message type, sent to the
    same port, or unrelated UDP traffic) and "malformed/truncated"
    with the same "return None, caller just skips it" shape as
    adif.grid_square_to_latlon, rather than raising for what's a
    routine, expected non-match most of the time (only a small
    fraction of WSJT-X's own broadcast traffic is ever type 12)."""
    try:
        magic, offset = _read_uint32(data, 0)
        if magic != WSJTX_UDP_MAGIC:
            return None
        _schema, offset = _read_uint32(data, offset)
        msg_type, offset = _read_uint32(data, offset)
        if msg_type != WSJTX_MESSAGE_TYPE_LOGGED_ADIF:
            return None
        _id, offset = _read_utf8(data, offset)
        adif_text, _offset = _read_utf8(data, offset)
        return adif_text
    except (ValueError, struct.error):
        return None


class WsjtxUdpListener(QObject):
    """Binds a UDP socket on `port` -- MUST match WSJT-X's own "UDP
    Server port" setting (Settings > Reporting; WSJTX_DEFAULT_PORT,
    2237, is WSJT-X's own documented default for it) -- and emits
    qso_logged(dict) -- one adif.parse_adif_records() result, the same
    ADIF-field-dict shape NewQsoDialog's submitted signal already
    produces -- every time a "Logged ADIF" message arrives. Every
    other WSJT-X message type received on the same port (Heartbeat,
    Status, Decode, ...) is silently dropped; only LoggedADIF is ever
    acted on.

    Binds with ShareAddress|ReuseAddressHint so this can coexist with
    another already-running UDP listener on the same port (e.g.
    JTAlert, GridTracker) rather than stealing/blocking it -- WSJT-X
    broadcasts to one configured port, and multiple local listeners
    genuinely can all receive their own copy of each datagram this way
    on the platforms that support it."""

    qso_logged = Signal(dict)
    error = Signal(str)

    def __init__(self, port=WSJTX_DEFAULT_PORT, parent=None):
        super().__init__(parent)
        self._port = port
        self._socket = QUdpSocket(self)
        self._socket.readyRead.connect(self._on_ready_read)

    def start(self):
        """Raises RuntimeError on bind failure -- same convention as
        wsjtx_rigctld.py's RigctldServer.start(), since both are a
        direct, synchronous response to the user's own explicit toggle-
        on action (ham_dashboard.py wraps this in the same try/except
        QMessageBox.critical shape as its Rigctld handling). The
        `error` signal is separate and only ever used for problems
        AFTER a successful start (a malformed ADIF payload in an
        otherwise-valid LoggedADIF message) -- genuinely asynchronous
        failures with no direct user action to blame them on."""
        ok = self._socket.bind(
            QHostAddress.AnyIPv4, self._port,
            QAbstractSocket.ShareAddress | QAbstractSocket.ReuseAddressHint,
        )
        if not ok:
            raise RuntimeError(f"Couldn't listen on UDP port {self._port}: {self._socket.errorString()}")

    def stop(self):
        self._socket.close()

    def _on_ready_read(self):
        while self._socket.hasPendingDatagrams():
            datagram = self._socket.receiveDatagram()
            data = bytes(datagram.data())
            adif_text = parse_logged_adif_message(data)
            if not adif_text:
                continue
            try:
                records = adif.parse_adif_records(adif_text)
            except Exception as exc:
                self.error.emit(f"Couldn't parse WSJT-X's logged QSO ADIF: {exc}")
                continue
            for record in records:
                self.qso_logged.emit(record)
