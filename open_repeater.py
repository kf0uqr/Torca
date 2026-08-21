"""
Open Repeater (openrepeater.org) API client -- pure, no-Qt, mirrors
pota.py's style: a single GET, JSON response, raises on transport/
parse failure.

Chosen over RepeaterBook per explicit instruction: RepeaterBook's API
now requires an approved app token and its own docs explicitly say
public repeater-directory/discovery tools like this one are "generally
not authorized without explicit written permission" -- not something
to build against without that permission. Open Repeater's API is
openly self-service (free API key after registering, no special
approval), confirmed via its own published docs
(https://www.openrepeater.org/api-docs, https://www.openrepeater.org/docs).

GET https://www.openrepeater.org/api/v1/search?lat=<>&lng=<>&radius=<km>
returns repeaters within `radius` km of (lat, lng) -- confirmed via
Open Repeater's own API reference. Requires an API key (free,
self-service registration), passed as the `api_key` query parameter
(also documented to accept an X-API-Key header or Bearer token, but
query param is simplest for a single GET here). Rate limit: 30
requests/key/day (Open Repeater's own stated limit) -- so this is only
ever queried on an explicit user action (creating/refreshing the Local
Repeaters memories tab), never on a timer, same "don't hammer it"
convention as pota.py.

The /search endpoint isn't documented to accept a band filter itself
(unlike /repeaters), so band filtering (2m/70cm) happens client-side
here instead of being requested from the API.

Response schema per Open Repeater's own docs, one entry:
{"id", "callsign", "frequency" (MHz, float -- the repeater's output/
downlink), "offset" (MHz, float, added to frequency for the input/
uplink -- e.g. -0.6), "ctcss" (Hz, float or null), "dcs", "mode",
"band" ("2m"/"70cm"/"23cm"/etc), "country", "city", "lat", "lng",
"coverage_km", "owner", "status" ("active"/"inactive"),
"last_verified"}.
"""

import json
import urllib.error
import urllib.request

OPEN_REPEATER_BASE_URL = "https://www.openrepeater.org/api/v1"

# Same identifying-User-Agent convention used for every other external
# fetch in this app.
_USER_AGENT = "TORCA/1.0 (desktop ham radio control application)"

# What "local repeaters" means for this feature -- per explicit
# instruction, 2m and 70cm only (not 23cm, not digital-only bands).
LOCAL_REPEATER_BANDS = {"2m", "70cm"}


def fetch_nearby_repeaters(api_key: str, lat: float, lon: float, radius_km: float = 80.0) -> list:
    """Returns every 2m/70cm repeater Open Repeater has within
    radius_km of (lat, lon), each as a dict: {"callsign",
    "output_freq_hz", "input_freq_hz", "mode", "band", "city",
    "ctcss_hz", "status"}. Raises on transport/parse failure or a
    missing/invalid API key (401) -- callers catch and report, same
    convention as every other fetch in this app."""
    if not api_key:
        raise ValueError("Open Repeater API key not set -- see Repeaters Settings...")
    url = f"{OPEN_REPEATER_BASE_URL}/search?lat={lat}&lng={lon}&radius={radius_km}&api_key={api_key}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ValueError("Open Repeater rejected the API key -- check Repeaters Settings...") from exc
        if exc.code == 429:
            raise ValueError("Open Repeater daily request limit reached (30/key/day) -- try again after midnight UTC.") from exc
        raise

    if isinstance(data, dict):
        entries = data.get("results") or data.get("repeaters") or data.get("data") or []
    else:
        entries = data

    repeaters = []
    for entry in entries:
        band = (entry.get("band") or "").strip().lower()
        if band not in LOCAL_REPEATER_BANDS:
            continue
        try:
            output_mhz = float(entry.get("frequency"))
        except (TypeError, ValueError):
            continue  # no usable frequency -- skip rather than guess
        offset_mhz = entry.get("offset")
        try:
            input_mhz = output_mhz + float(offset_mhz) if offset_mhz is not None else output_mhz
        except (TypeError, ValueError):
            input_mhz = output_mhz
        repeaters.append({
            "callsign": entry.get("callsign") or "",
            "output_freq_hz": round(output_mhz * 1e6),
            "input_freq_hz": round(input_mhz * 1e6),
            "mode": entry.get("mode") or "FM",
            "band": band,
            "city": entry.get("city") or "",
            "ctcss_hz": entry.get("ctcss"),
            "status": entry.get("status") or "",
        })
    return repeaters
