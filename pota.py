"""
POTA (Parks on the Air) API client -- https://api.pota.app. Pure,
no-Qt, mirrors pskreporter.py's style: a single GET, JSON response,
raises on transport/parse failure.

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

Full park DIRECTORY (as opposed to just currently-active spots) needs
three more endpoints, all confirmed via real live fetches (not
guessed from docs -- docs.pota.app/api is itself marked "under
construction"):

- GET /programs -- every POTA "program" (country/region eligible for
  POTA activations), ~249 entries: {"programPrefix", "programName",
  "isActive", ...}. This is the REQUIRED scoping unit for the park
  list below -- there is no single "all parks on Earth" endpoint.
- GET /program/parks/{prefix} -- every park in that program/country,
  regardless of activation status. Confirmed live (US alone): 12,956
  entries, ~2.7MB JSON. Flat array of {"reference", "name",
  "latitude", "longitude", "grid", "locationDesc", "attempts",
  "activations", "qsos"}.
- GET /park/{reference} -- richer single-park detail (type, access/
  activation methods, managing agency + website, first activator/
  date) -- a SEPARATE, complementary shape from the list entries
  above (this one lacks attempts/activations/qsos, the list lacks
  everything else).
"""

import json
import math
import pathlib
import time
import urllib.error
import urllib.request

import adif

POTA_SPOTS_URL = "https://api.pota.app/spot/activator"
POTA_PROGRAMS_URL = "https://api.pota.app/programs"
POTA_PROGRAM_PARKS_URL_TEMPLATE = "https://api.pota.app/program/parks/{prefix}"
POTA_PARK_DETAIL_URL_TEMPLATE = "https://api.pota.app/park/{reference}"

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


def fetch_pota_programs() -> list:
    """Every POTA "program" (country/region eligible for POTA
    activations) -- the required scoping unit for fetch_program_parks()
    below, since there is no single "all parks on Earth" endpoint.
    Returns [{"prefix", "name"}, ...] for only currently-active
    programs, sorted by name. Confirmed live: ~249 total programs."""
    request = urllib.request.Request(POTA_PROGRAMS_URL, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read())
    programs = [
        {"prefix": entry["programPrefix"], "name": entry.get("programName") or entry["programPrefix"]}
        for entry in data if entry.get("isActive") and entry.get("programPrefix")
    ]
    programs.sort(key=lambda p: p["name"])
    return programs


def fetch_program_parks(program_prefix: str) -> list:
    """Every park in the given POTA program (country/region), regardless
    of activation status -- confirmed live shape: {"reference", "name",
    "lat", "lon", "grid", "location_desc", "attempts", "activations",
    "qsos"}. Large for bigger countries (the US alone: ~12,956 entries,
    ~2.7MB) -- callers should cache this (see load_cached_program_parks/
    save_program_parks_cache below) rather than refetching on every use.
    Skips any entry missing a latitude/longitude, same convention as
    fetch_pota_spots()."""
    url = POTA_PROGRAM_PARKS_URL_TEMPLATE.format(prefix=program_prefix)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = json.loads(response.read())
    parks = []
    for entry in data:
        lat, lon = entry.get("latitude"), entry.get("longitude")
        if lat is None or lon is None:
            continue
        parks.append({
            "reference": entry.get("reference") or "?",
            "name": entry.get("name") or "",
            "lat": lat, "lon": lon,
            "grid": entry.get("grid") or "",
            "location_desc": entry.get("locationDesc") or "",
            "attempts": entry.get("attempts"),
            "activations": entry.get("activations"),
            "qsos": entry.get("qsos"),
        })
    return parks


def fetch_park_details(reference: str) -> dict:
    """Richer single-park info for `reference` (e.g. "US-0001") -- park
    type, access/activation methods, managing agency + website, first
    activator/date. A SEPARATE, complementary shape from fetch_
    program_parks's own entries (that one has attempts/activations/qsos
    counts this one lacks; this one has everything else). Confirmed via
    a real live fetch, not guessed from docs."""
    url = POTA_PARK_DETAIL_URL_TEMPLATE.format(reference=reference)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read())
    return {
        "reference": data.get("reference") or reference,
        "name": data.get("name") or "",
        "lat": data.get("latitude"),
        "lon": data.get("longitude"),
        "grid": data.get("grid6") or data.get("grid4") or "",
        "park_type": data.get("parktypeDesc") or "",
        "location_name": data.get("locationName") or "",
        "entity_name": data.get("entityName") or "",
        "active": bool(data.get("active")),
        "comments": data.get("parkComments") or "",
        "access_methods": data.get("accessMethods") or "",
        "activation_methods": data.get("activationMethods") or "",
        "agencies": data.get("agencies") or "",
        "agency_url": data.get("agencyURLs") or "",
        "park_url": data.get("parkURLs") or data.get("website") or "",
        "first_activator": data.get("firstActivator") or "",
        "first_activation_date": data.get("firstActivationDate") or "",
    }


# ---- Local disk cache for fetch_program_parks() ----
#
# Park directories change rarely (occasional new parks added, existing
# ones essentially never move) -- unlike POTA's own SPOT data (minutes-
# scale staleness, deliberately never cached), a week-old park list is
# still genuinely useful, and re-fetching a multi-MB program on every
# tab open would be wasteful. Same cache-root convention as every other
# on-disk cache in this app (world_map.py's old map image, map_tiles.py's
# OSM tiles).
_PARKS_CACHE_DIR = pathlib.Path.home() / ".icom_radio_app_cache" / "pota_parks"
_PARKS_CACHE_MAX_AGE_S = 7 * 24 * 3600  # a week


def _parks_cache_path(program_prefix):
    return _PARKS_CACHE_DIR / f"{program_prefix}.json"


def load_cached_program_parks(program_prefix):
    """Returns the cached park list for program_prefix if a reasonably
    fresh (< _PARKS_CACHE_MAX_AGE_S old) local copy exists, else None --
    callers fetch fresh via fetch_program_parks() on a cache miss/stale
    hit and then save_program_parks_cache() the result."""
    path = _parks_cache_path(program_prefix)
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > _PARKS_CACHE_MAX_AGE_S:
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_program_parks_cache(program_prefix, parks):
    _PARKS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _parks_cache_path(program_prefix).write_text(json.dumps(parks))


_EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in km between two lat/lon points -- standard
    haversine formula. Used for the Parks tab's "within X km of my
    location" filter -- the park-directory API has no server-side radius
    parameter (unlike e.g. open_repeater.py's search), so this filtering
    has to happen client-side against an already-fetched park list."""
    lat1_r, lon1_r, lat2_r, lon2_r = (math.radians(v) for v in (lat1, lon1, lat2, lon2))
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))
