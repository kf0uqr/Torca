"""Uploading WSPR spots to WSPRnet (wsprnet.org) -- the network the WSPR community
reads, which is where a WSPR receiver's reports belong (PSKReporter is for other modes).

One spot is one HTTP POST of form fields to http://wsprnet.org/post/ (the same interface
WSJT-X uses): function=wspr, the receiver's callsign / grid / dial frequency, and for the
beacon heard its callsign, grid, frequency, power, signal, DT and drift, plus the slot's
date and time. The server answers with a page containing "spot(s) added" when it took it.

What gets uploaded is public: your callsign and grid square and each station you hear.
The WSPR tool only uploads when the operator has switched it on.
"""

import time
import urllib.error
import urllib.parse
import urllib.request

URL = "http://wsprnet.org/post/"
MODE_WSPR2 = "2"
VERSION = "TORCA-1.0"
_USER_AGENT = "TORCA/1.0 (desktop ham radio control application)"


def build_form(spot, my_call, my_grid, dial_mhz, version=VERSION):
    """The form fields for one wspr.WsprSpot heard on a radio dialled to `dial_mhz`."""
    when = time.gmtime(spot.slot_start)
    return {
        "function": "wspr",
        "rcall": my_call.upper(),
        "rgrid": my_grid,
        "rqrg": f"{dial_mhz:.6f}",
        "date": time.strftime("%y%m%d", when),
        "time": time.strftime("%H%M", when),
        "sig": str(int(spot.snr)),
        "dt": f"{spot.dt:.1f}",
        "drift": str(int(spot.drift)),
        "tqrg": f"{spot.frequency_mhz:.6f}",
        "tcall": spot.callsign,
        "tgrid": spot.grid,
        "dbm": "" if spot.power_dbm is None else str(int(spot.power_dbm)),
        "version": version,
        "mode": MODE_WSPR2,
    }


def uploadable(spot):
    """WSPRnet needs the transmitter's callsign, grid and power."""
    return bool(spot.callsign and spot.grid and spot.power_dbm is not None)


def upload(spots, my_call, my_grid, dial_mhz, url=URL, timeout=20, opener=None):
    """Posts each uploadable spot. Returns (number accepted, [problem messages]).
    `opener` (a urllib opener) is injectable for tests."""
    opener = opener or urllib.request.build_opener()
    accepted, problems = 0, []
    for spot in spots:
        if not uploadable(spot):
            continue
        body = urllib.parse.urlencode(build_form(spot, my_call, my_grid, dial_mhz)).encode("ascii")
        request = urllib.request.Request(url, data=body, headers={
            "User-Agent": _USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with opener.open(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as exc:
            problems.append(f"{spot.callsign}: {exc}")
            break                      # the network is down: no point trying the rest
        if "spot(s) added" in text:
            accepted += 1
        else:
            problems.append(f"{spot.callsign}: not accepted by WSPRnet")
    return accepted, problems
