"""Sending reception reports TO PSKReporter (pskreporter.info): "I, KF0UQR in
EM18, heard W1AW on 14.074 MHz FT8 at -12 dB". This is what makes the stations
you hear show up on PSKReporter's own map -- and on TORCA's, when the
PSKReporter overlay is set to show the stations you are hearing.

The protocol is IPFIX over UDP (https://pskreporter.info/pskdev.html): each
datagram carries a header, the descriptions ("templates") of the two record
kinds, one RECEIVER record (who is listening: callsign, locator, software) and
a batch of SENDER records (who was heard: callsign, frequency, SNR, mode,
locator, time). Their rules, which this follows:
- each record carries the time the signal was heard, and the server treats a record
  more than a minute away from the datagram's own time as coming from a wrong clock.
  So reports must go out promptly, in small frequent batches (as WSJT-X does, every
  15-60 s): batching for minutes makes every spot "too old" and it is not shown.
  (An earlier version of this module batched for five minutes and its spots never
  appeared on PSKReporter.)  The interval is also randomised, not in lock-step with the clock;
- a repeat of the same callsign on the same band inside REPEAT_SECONDS is dropped;
- the receiver's identity is public: it is only ever sent when the operator
  has switched reporting on.

Pure packet building (build_datagrams) is separate from the sending and timing
(PskReporterUploader) so the bytes can be tested exactly.
"""

import random
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer

HOST = "report.pskreporter.info"
PORT = 4739

ENTERPRISE = 30351                       # 0x768F, registered to PSKReporter
SEND_INTERVAL_SECONDS = 30               # short enough that every record is well under a minute old when sent
SEND_JITTER_SECONDS = 5                  # not synchronised to the clock
REPEAT_SECONDS = 600                     # the same station on the same band: report it once per this long
MAX_DATAGRAM_BYTES = 1200                # a full packet is sent at once

SOFTWARE = "TORCA"

# Descriptions of the two kinds of record, verbatim from the PSKReporter spec.
_RECEIVER_TEMPLATE = bytes.fromhex(
    "0003 0024 9992 0003 0001"
    "8002 FFFF 0000 768F"      # receiverCallsign
    "8004 FFFF 0000 768F"      # receiverLocator
    "8008 FFFF 0000 768F"      # decodingSoftware
    "0000")
_SENDER_TEMPLATE = bytes.fromhex(
    "0002 0044 9993 0008"
    "8001 FFFF 0000 768F"      # senderCallsign
    "8005 0004 0000 768F"      # frequency, Hz (4 bytes)
    "8006 0001 0000 768F"      # sNR (1 byte, signed)
    "8007 0001 0000 768F"      # iMD (1 byte)
    "800A FFFF 0000 768F"      # mode
    "800B 0001 0000 768F"      # informationSource (1 byte)
    "8003 FFFF 0000 768F"      # senderLocator
    "0096 0004")               # flowStartSeconds (4 bytes)


@dataclass(frozen=True)
class Spot:
    callsign: str
    frequency_hz: int
    snr: int
    mode: str
    locator: str
    epoch: int


def _string(text):
    data = (text or "").encode("ascii", "ignore")[:254]
    return bytes([len(data)]) + data


def _pad(data):
    return data + b"\x00" * (-len(data) % 4)


def _receiver_record(callsign, locator, software=SOFTWARE):
    body = _string(callsign) + _string(locator) + _string(software)
    body = _pad(body)
    return struct.pack(">HH", 0x9992, len(body) + 4) + body


def _sender_record(spot):
    return (_string(spot.callsign) + struct.pack(">I", int(spot.frequency_hz)) + struct.pack(">b", max(-128, min(127, int(spot.snr))))
            + b"\x00"                         # iMD: not measured
            + _string(spot.mode) + b"\x01"    # informationSource 1: automatically extracted
            + _string(spot.locator) + struct.pack(">I", int(spot.epoch)))


def build_datagrams(callsign, locator, spots, session_id, sequence, now=None, software=SOFTWARE,
                    max_bytes=MAX_DATAGRAM_BYTES):
    """The datagrams that report `spots` (a list of Spot): [(bytes, spots reported in it)].
    `sequence` is the running count of reports already sent in this session."""
    now = int(time.time() if now is None else now)
    preamble = _RECEIVER_TEMPLATE + _SENDER_TEMPLATE + _receiver_record(callsign, locator, software)
    room = max_bytes - 16 - len(preamble) - 4
    datagrams, batch, used = [], [], 0
    for spot in list(spots) + [None]:
        record = _sender_record(spot) if spot is not None else b""
        if spot is None or used + len(record) > room:
            if batch:
                body = _pad(b"".join(_sender_record(s) for s in batch))
                message = preamble + struct.pack(">HH", 0x9993, len(body) + 4) + body
                header = struct.pack(">HHIII", 0x000A, 16 + len(message), now, sequence, session_id)
                datagrams.append((header + message, batch))
                sequence += len(batch)
                batch, used = [], 0
        if spot is not None:
            batch.append(spot)
            used += len(record)
    return datagrams


class PskReporterUploader(QObject):
    """Collects what the decoders hear and reports it every few minutes.
    `send` is injectable for tests (default: a UDP datagram to PSKReporter)."""

    def __init__(self, callsign, locator, send=None, clock=time.time, parent=None):
        super().__init__(parent)
        self.callsign = callsign.upper()
        self.locator = locator
        self._send = send or self._send_udp
        self._clock = clock
        self._session = random.getrandbits(32)
        self._sequence = 0
        self._pending = []
        self._last_reported = {}         # (callsign, band-ish) -> time reported
        self.sent_count = 0
        self.last_error = ""
        self._executor = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timer)
        self._schedule()

    def _schedule(self):
        delay = SEND_INTERVAL_SECONDS + random.uniform(-SEND_JITTER_SECONDS, SEND_JITTER_SECONDS)
        self._timer.start(int(delay * 1000))

    def _on_timer(self):
        self.flush()
        self._schedule()

    def add(self, spot):
        """Queue one heard station (dropped if it was reported recently)."""
        if not spot.callsign or spot.frequency_hz <= 0:
            return False
        key = (spot.callsign.upper(), int(spot.frequency_hz // 100_000), spot.mode)
        now = self._clock()
        if now - self._last_reported.get(key, -1e12) < REPEAT_SECONDS:
            return False
        self._last_reported[key] = now
        self._pending.append(spot)
        if len(self._pending) >= 40:
            self.flush()               # a full packet goes straight away
        return True

    def flush(self):
        """Sends everything queued now. Returns the number of reports sent."""
        if not self._pending:
            return 0
        spots, self._pending = self._pending, []
        sent = 0
        for datagram, batch in build_datagrams(self.callsign, self.locator, spots, self._session, self._sequence, now=self._clock()):
            try:
                self._send(datagram)
            except OSError as exc:
                self.last_error = str(exc)
                continue
            self._sequence += len(batch)
            sent += len(batch)
        self.sent_count += sent
        return sent

    def stop(self):
        self._timer.stop()
        self.flush()
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=False)   # let the last datagram leave
            self._executor = None

    def _send_udp(self, datagram):
        """Sends on a background thread: resolving the server's name can take seconds, and
        this runs on the GUI thread."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pskreporter")
        self._executor.submit(self._send_blocking, datagram)

    def _send_blocking(self, datagram):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(5)
                sock.sendto(datagram, (HOST, PORT))
            print(f"[PSKReporter] sent a report datagram ({len(datagram)} bytes).")
        except OSError as exc:
            self.last_error = str(exc)
            print(f"[PSKReporter] couldn't send a report datagram: {exc}")
