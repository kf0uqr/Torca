"""The conversation of an FT8 / FT4 QSO, as a state machine with no Qt in it.

A standard QSO is six kinds of message, numbered as WSJT-X does:

    1  DX ME GRID        calling someone (usually who just sent CQ)
    2  DX ME -10         "I hear you at -10" (reply to a call)
    3  DX ME R-10        "and I hear you at -10"
    4  DX ME RR73        "got it, 73"  (RRR is the older form)
    5  DX ME 73          the last word
    6  CQ ME GRID        calling CQ

Qso picks the message to send next from what was just heard, and says when the
contact is complete (so the caller can log it). It never touches the radio or
the clock: the window feeds it decoded messages and asks it what to send.

The parity of a slot (even = 0, odd = 1: which half of each minute) matters
because two stations cannot transmit in the same slot -- we transmit in the
parity opposite to the one the other station used.
"""

import re
from dataclasses import dataclass
from typing import Optional

MAX_TRIES = 5          # one message sent this many times with no answer: give up

_GRID = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$")
_REPORT = re.compile(r"^[+-]\d{2}$")
_R_REPORT = re.compile(r"^R[+-]\d{2}$")

CQ, CALL, REPORT, R_REPORT, RRR, RR73, SIGN_OFF = (
    "cq", "grid", "report", "r_report", "rrr", "rr73", "73")


def latlon_to_grid(lat, lon, digits=4):
    """Maidenhead locator (4 or 6 characters) for a position."""
    lon, lat = lon + 180.0, lat + 90.0
    grid = chr(ord("A") + int(lon // 20)) + chr(ord("A") + int(lat // 10))
    grid += str(int((lon % 20) // 2)) + str(int(lat % 10))
    if digits >= 6:
        grid += chr(ord("a") + int((lon % 2) * 12)) + chr(ord("a") + int((lat % 1) * 24))
    return grid


def format_report(snr):
    """An FT8 signal report: whole dB, signed, two digits, limited to -30..+32."""
    return f"{max(-30, min(32, int(round(snr)))):+03d}"


@dataclass(frozen=True)
class Parsed:
    kind: str              # one of the constants above
    to: Optional[str]      # who it is for (None for CQ)
    sender: str
    value: str = ""        # the grid, or the report digits


def parse(text):
    """Classifies one decoded message, or None if it is not a standard
    exchange (free text, hashed callsigns, telemetry...)."""
    tokens = text.upper().split()
    if len(tokens) >= 2 and tokens[0] == "CQ":
        # "CQ DX KF0UQR EM17" / "CQ POTA ..." carry a modifier before the call
        call = tokens[-2] if _GRID.match(tokens[-1]) and len(tokens) >= 3 else tokens[-1]
        grid = tokens[-1] if _GRID.match(tokens[-1]) and len(tokens) >= 3 else ""
        return Parsed(CQ, None, call, grid)
    if len(tokens) != 3 or any(t.startswith("<") for t in tokens[:2]):
        return None
    to, sender, payload = tokens
    if payload == "RR73":
        return Parsed(RR73, to, sender)
    if payload == "RRR":
        return Parsed(RRR, to, sender)
    if payload == "73":
        return Parsed(SIGN_OFF, to, sender)
    if _REPORT.match(payload):
        return Parsed(REPORT, to, sender, payload)
    if _R_REPORT.match(payload):
        return Parsed(R_REPORT, to, sender, payload[1:])
    if _GRID.match(payload):
        return Parsed(CALL, to, sender, payload)
    return None


def heard_station(text):
    """(callsign, locator) of the station that SENT a decoded message, as far as
    the message says -- the locator is only there for a CQ or a first call. None
    if the message has no usable sender (free text, a hashed callsign...)."""
    parsed = parse(text)
    if parsed is None or not parsed.sender or parsed.sender.startswith("<"):
        return None
    locator = parsed.value if parsed.kind in (CQ, CALL) else ""
    return parsed.sender, locator


def _base(call):
    return (call or "").upper().split("/")[0]


class Qso:
    """One contact in progress (or, with no dx_call, the idle / calling-CQ state)."""

    def __init__(self, my_call, my_grid):
        self.my_call = my_call.upper()
        self.my_grid = my_grid.upper()
        self.reset()

    def reset(self):
        self.dx_call = None
        self.dx_grid = ""
        self.stage = None          # the message number (1-6) to send next, None = nothing to send
        self.tries = 0             # times the current message has been sent without an answer
        self.rst_sent = ""         # the report we gave them
        self.rst_rcvd = ""         # the report they gave us
        self.tx_parity = 0
        self.dx_freq = None        # their audio frequency, so we can transmit near it
        self.complete = False      # the exchange is done (log it)
        self.gave_up = False
        self.from_cq = False       # we were calling CQ when it started

    # ---- starting ----

    def call_cq(self, parity=0):
        self.reset()
        self.stage = 6
        self.tx_parity = parity

    def work(self, parsed, snr, slot_parity, freq=None):
        """The operator chose a decoded message to answer. `parsed` is parse() of
        it, `snr` the signal strength we heard, `slot_parity` the slot it came in."""
        was_cq = self.stage == 6
        self.reset()
        self.from_cq = was_cq
        self.dx_call = parsed.sender
        self.tx_parity = 1 - slot_parity
        self.dx_freq = freq
        self.rst_sent = format_report(snr)
        mine = _base(parsed.to) == _base(self.my_call)
        if parsed.kind == CQ or not mine:
            self.stage = 1
            self.dx_grid = parsed.value if parsed.kind == CQ else ""
        elif parsed.kind == CALL:
            self.dx_grid = parsed.value
            self.stage = 2
        elif parsed.kind == REPORT:
            self.rst_rcvd = parsed.value
            self.stage = 3
        elif parsed.kind == R_REPORT:
            self.rst_rcvd = parsed.value
            self.stage = 4
        else:                       # RRR / RR73 / 73: all that is left is to say 73
            self.stage = 5

    # ---- the message to send ----

    def message(self):
        """The text to send now, or None if there is nothing to send."""
        me, dx = self.my_call, self.dx_call
        stage = self.stage
        if stage == 6:
            return f"CQ {me} {self.my_grid}".strip()
        if stage is None or dx is None:
            return None
        return {
            1: f"{dx} {me} {self.my_grid}",
            2: f"{dx} {me} {self.rst_sent}",
            3: f"{dx} {me} R{self.rst_sent}",
            4: f"{dx} {me} RR73",
            5: f"{dx} {me} 73",
        }[stage].strip()

    def sent(self):
        """Call after the current message has been transmitted. Returns True the
        moment the contact becomes complete (log it now)."""
        self.tries += 1
        if self.stage in (4, 5) and not self.complete:
            self.complete = True    # we have sent our final acknowledgement
            if self.stage == 5:
                self.stage = None   # 73 sent: nothing more to say
            return True
        if self.stage == 4 and self.tries > 2:
            self.stage = None       # RR73 sent twice: they have heard enough
        elif self.stage not in (4, 5, 6) and self.tries >= MAX_TRIES:
            self.gave_up = True
            self.stage = None
        return False

    # ---- what was heard ----

    def heard(self, parsed, snr, slot_parity, freq=None):
        """Feed every decoded message. Returns True if it changed what we send next."""
        if parsed is None:
            return False
        mine = _base(parsed.to) == _base(self.my_call) if parsed.to else False

        if self.stage == 6 and mine:                    # someone answered our CQ
            if parsed.kind in (CALL, REPORT):
                self.work(parsed, snr, slot_parity, freq)
                self.from_cq = True
                return True
            return False

        if self.dx_call is None or _base(parsed.sender) != _base(self.dx_call) or not mine:
            return False

        self.dx_freq = freq or self.dx_freq
        self.tries = 0
        kind = parsed.kind
        if kind == REPORT and self.stage in (1, 2, 3):
            self.rst_rcvd, self.stage = parsed.value, 3
        elif kind == R_REPORT and self.stage in (1, 2, 3):
            self.rst_rcvd, self.stage = parsed.value, 4
        elif kind in (RRR, RR73) and self.stage in (1, 2, 3):
            self.stage = 5
        elif kind == SIGN_OFF and self.stage in (3, 4, 5, None):
            self.complete = True
            self.stage = None
        elif kind == CALL and self.stage == 1:
            return False
        elif kind == CALL and self.stage == 2:
            self.dx_grid = parsed.value
        else:
            return False
        if self.stage in (2, 3):
            self.rst_sent = format_report(snr)   # the freshest report we can give
        return True

    def after_complete(self):
        """What to do once a contact is logged: go back to CQ if that is how it
        began, otherwise stop transmitting."""
        was_cq, parity = self.from_cq, self.tx_parity
        self.reset()
        if was_cq:
            self.call_cq(parity)
