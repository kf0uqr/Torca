"""
PSKReporter.info query API client -- https://pskreporter.info/pskdev.html.
Thin, synchronous urllib client mirroring qrz_logbook.py/satellite_
tracking.py's fetch_transponders() style: no new HTTP library
dependency, raises on transport failure, callers catch and report.

GET https://retrieve.pskreporter.info/query with query-string
parameters -- senderCallsign=<call> asks "who received THIS callsign's
signal" (i.e. reports of stations that heard you); receiverCallsign=
<call>&rronly=1 asks "what did THIS callsign receive" (i.e. reports of
stations you heard). Response is XML, one <receptionReport ...> element
per spot (attributes only, no child elements) -- confirmed via
jasonhancock/go-pskreporter's response.go struct tags, a maintained
open-source client: receiverCallsign, receiverLocator, senderCallsign,
senderLocator, frequency (Hz), flowStartSeconds (unix epoch), mode,
sNR. Parsed with ElementTree's .iter("receptionReport") rather than
assuming a specific root/wrapper element name, since that wasn't
confirmed.

flowStartSeconds is also the QUERY parameter for how far back to look
-- a negative integer, capped at -86400 (24 hours) per the same
reference client; PSKReporter documents that heavier/more frequent
polling than that may get an IP rate-limited or blocked, so this app
only ever fetches on an explicit user action (toggling the map overlay
on, or changing the lookback/direction in its settings dialog) --
never on a timer.
"""

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import adif

PSKREPORTER_QUERY_URL = "https://retrieve.pskreporter.info/query"

# Same identifying-User-Agent convention used for every other external
# fetch in this app (map_tiles.py's tile fetcher, satellite_tracking.py's
# fetch_transponders/fetch_amateur_tles) -- PSKReporter's own docs ask
# API consumers to identify themselves.
_USER_AGENT = "TORCA/1.0 (desktop ham radio control application)"

# PSKReporter's own documented cap on flowStartSeconds -- confirmed via
# jasonhancock/go-pskreporter (WithFlowStartSeconds, max -86400).
MAX_LOOKBACK_SECONDS = 86400

# Per-query result cap -- generous for "reports mentioning one specific
# callsign" (the actual scope every query here uses), just a safety
# ceiling against an unexpectedly huge response.
_RESULT_LIMIT = 5000

DIRECTION_HEARD_YOU = "heard_you"   # other stations reported hearing your callsign
DIRECTION_HEARING = "hearing"       # your callsign reported hearing other stations


def _fetch_reports(callsign: str, direction: str, since_seconds: int) -> list:
    """One HTTP request for a single direction. since_seconds is a
    positive number of seconds to look back (capped to
    MAX_LOOKBACK_SECONDS); PSKReporter's own parameter wants it
    negative."""
    since_seconds = min(since_seconds, MAX_LOOKBACK_SECONDS)
    params = {
        "flowStartSeconds": str(-since_seconds),
        "rptlimit": str(_RESULT_LIMIT),
        "appcontact": _USER_AGENT,
        # Suppresses PSKReporter's large "who's currently monitoring"
        # <activeReceiver> listing (confirmed live: several thousand
        # entries, megabytes, completely unrelated to the queried
        # callsign) -- we only want actual <receptionReport> spots.
        "noactive": "1",
    }
    if direction == DIRECTION_HEARD_YOU:
        params["senderCallsign"] = callsign
    elif direction == DIRECTION_HEARING:
        params["receiverCallsign"] = callsign
        params["rronly"] = "1"
    else:
        raise ValueError(f"unknown direction: {direction!r}")
    url = f"{PSKREPORTER_QUERY_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read()
    root = ET.fromstring(data)

    reports = []
    for element in root.iter("receptionReport"):
        attrs = element.attrib
        try:
            frequency_hz = int(attrs["frequency"])
        except (KeyError, ValueError):
            frequency_hz = None
        try:
            flow_start = int(attrs["flowStartSeconds"])
        except (KeyError, ValueError):
            flow_start = None
        if direction == DIRECTION_HEARD_YOU:
            other_callsign = attrs.get("receiverCallsign")
            other_locator = attrs.get("receiverLocator")
        else:
            other_callsign = attrs.get("senderCallsign")
            other_locator = attrs.get("senderLocator")
        if not other_callsign or not other_locator:
            continue  # nothing to plot without a locator
        reports.append({
            "callsign": other_callsign,
            "locator": other_locator,
            "frequency_hz": frequency_hz,
            "band": adif.band_for_freq_hz(frequency_hz) or "?",
            "mode": attrs.get("mode") or "?",
            "snr": attrs.get("sNR"),
            "flow_start_seconds": flow_start,
            "direction": direction,
            # Every attribute PSKReporter actually sent for this report
            # (region, DXCC/country, LoTW upload date, etc. -- varies by
            # report, not all confirmed present on every one) -- kept
            # verbatim so the map tooltip can show everything available
            # for a spot, not just the handful of fields cherry-picked
            # above for filtering/sorting.
            "raw": dict(attrs),
        })
    return reports


def fetch_pskreporter_spots(callsign: str, direction: str, since_seconds: int) -> list:
    """Returns a list of spot dicts for `callsign`: {"callsign"
    (the OTHER station), "locator", "frequency_hz", "band", "mode",
    "snr", "flow_start_seconds", "direction"}. direction is one of
    DIRECTION_HEARD_YOU, DIRECTION_HEARING, or "both" (fetches both and
    concatenates). Deduplicated to the single most recent report per
    (direction, callsign, band) -- a busy digital-mode station can have
    dozens of near-identical reports within one lookback window, which
    would otherwise draw dozens of overlapping map markers for what is,
    for mapping purposes, one contact. Raises on transport/parse
    failure -- callers catch and report, same convention as every
    other fetch in this app."""
    if direction == "both":
        reports = (
            _fetch_reports(callsign, DIRECTION_HEARD_YOU, since_seconds)
            + _fetch_reports(callsign, DIRECTION_HEARING, since_seconds)
        )
    else:
        reports = _fetch_reports(callsign, direction, since_seconds)

    latest = {}
    for report in reports:
        key = (report["direction"], report["callsign"], report["band"])
        existing = latest.get(key)
        if existing is None or (report["flow_start_seconds"] or 0) > (existing["flow_start_seconds"] or 0):
            latest[key] = report
    return list(latest.values())
