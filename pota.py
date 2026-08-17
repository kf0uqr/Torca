"""
POTA (Parks on the Air) spots API client -- https://api.pota.app.
Pure, no-Qt, mirrors pskreporter.py's style: a single GET, JSON
response, raises on transport/parse failure.

GET https://api.pota.app/spot/activator returns every currently active
activator spot as a JSON array -- confirmed via a live fetch (not
guessed): each entry already includes latitude/longitude directly (no
grid-square conversion needed, unlike QSO/PSKReporter markers), plus
frequency (a STRING, in kHz -- e.g. "14052.0"), mode, activator
callsign, park reference/name, spotTime (ISO 8601 UTC, no offset --
confirmed live, always UTC despite the missing "Z"), comments,
spotter, source, and count (QSO count so far this activation). No
authentication, no documented rate limit found, but per the same
"don't hammer it" spirit as every other external fetch in this app,
only ever queried on an explicit user action (toggling the map overlay
on, or a manual refresh) -- never on a timer. Spots naturally go stale
within their own "expire" window (POTA's own field, typically well
under an hour), so a manual refresh is genuinely useful here, unlike
e.g. the QSO log overlay.
"""

import json
import urllib.error
import urllib.request

import adif

POTA_SPOTS_URL = "https://api.pota.app/spot/activator"

# Same identifying-User-Agent convention used for every other external
# fetch in this app.
_USER_AGENT = "TORCA/1.0 (desktop ham radio control application)"


def fetch_pota_spots() -> list:
    """Returns every currently active POTA activator spot as a dict:
    {"lat", "lon", "frequency_hz", "band", "mode", "activator",
    "reference", "park_name", "spot_time" (raw ISO string from POTA),
    "comments", "spotter", "count"}. Skips any entry missing a
    latitude/longitude (confirmed live: not expected to happen, POTA's
    API always includes it, but this app never plots a guessed
    position -- same convention as adif.grid_square_to_latlon's
    callers). Raises on transport/parse failure -- callers catch and
    report, same convention as every other fetch in this app."""
    request = urllib.request.Request(POTA_SPOTS_URL, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read())

    spots = []
    for entry in data:
        lat, lon = entry.get("latitude"), entry.get("longitude")
        if lat is None or lon is None:
            continue
        try:
            freq_hz = int(round(float(entry.get("frequency")) * 1000))
        except (TypeError, ValueError):
            freq_hz = None
        spots.append({
            "lat": lat, "lon": lon,
            "frequency_hz": freq_hz,
            "band": adif.band_for_freq_hz(freq_hz) or "?",
            "mode": entry.get("mode") or "?",
            "activator": entry.get("activator") or "?",
            "reference": entry.get("reference") or "?",
            "park_name": entry.get("name") or "",
            "spot_time": entry.get("spotTime") or "",
            "comments": entry.get("comments") or "",
            "spotter": entry.get("spotter") or "?",
            "count": entry.get("count"),
        })
    return spots
