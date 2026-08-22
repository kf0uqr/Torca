"""
Parses a repeater list from a CSV file into the exact same shape
open_repeater.py's fetch_nearby_repeaters() returns (plus lat/lon --
see filter_by_distance), so memories_window.py's existing Local
Repeaters plumbing (_repeater_to_entry, LocalRepeatersTabPage) works
unchanged regardless of which source populated a tab.

Added as a key-less alternative after Open Repeater's own free
self-service signup didn't work out for one operator -- no online API
is queried here at all; the operator downloads a CSV themselves (their
own personal, manual, one-time choice of provider) and this just reads
whatever's already on disk. Two sources this was actually verified
against:

- Icom's own official repeater list download (e.g.
  https://www.icomjapan.com/support/firmware_driver/3709/ for the
  IC-705) -- a D-STAR-only, worldwide, static CSV snapshot, confirmed
  by downloading and inspecting the real file directly. Header row:
  "Group No,Group Name,Name,Sub Name,Repeater Call Sign,Gateway Call
  Sign,Frequency,Dup,Offset,Mode,TONE,Repeater Tone,RPT1USE,Position,
  Latitude,Longitude,UTC Offset". Its Latitude/Longitude columns are
  what make a static global file usable here at all -- filtered down
  to "near me" locally instead of server-side.
- RepeaterBook's own website search-results CSV export -- a manual,
  personal, one-time download through the operator's own browser
  session, NOT this app calling RepeaterBook's API (which, per its
  current published terms, explicitly disallows exactly this kind of
  automated "nearby repeater discovery tool" without separate written
  approval -- see open_repeater.py's own docstring for the earlier-
  confirmed restriction on RepeaterBook specifically). A CSV file the
  operator already has in hand from their own manual browsing carries
  no such restriction.

Column matching is alias-based and case-insensitive rather than tied
to one provider's exact header spelling, so any other reasonably-
labeled CSV (an ARRL/club-published list, etc.) has a fair chance of
working too, not just the two sources above.
"""

import csv

from pota import haversine_km

# {internal field: [acceptable header names, case-insensitive]} --
# first match wins per field.
_HEADER_ALIASES = {
    "callsign": ["callsign", "call sign", "repeater call sign", "call"],
    "output_freq_mhz": ["frequency", "freq", "output frequency", "downlink", "dl freq", "output freq"],
    "input_freq_mhz": ["input frequency", "uplink", "input freq"],
    "offset_mhz": ["offset"],
    "dup": ["dup", "duplex"],
    "ctcss_hz": ["pl", "ctcss", "tone", "pl/ctcss uplink", "repeater tone", "pl tone"],
    "city": ["city", "location/nearest city", "landmark", "name"],
    "mode": ["mode", "operating mode"],
    "lat": ["latitude", "lat"],
    "lon": ["longitude", "lon", "long"],
}

# Same 2m/70cm-only scope as open_repeater.py's own LOCAL_REPEATER_BANDS
# (per that module's own comment: not 23cm, not digital-only bands) --
# expressed here as frequency ranges instead of a pre-labeled "band"
# column, since not every CSV source labels band explicitly.
LOCAL_REPEATER_BANDS_MHZ = {"2m": (144.0, 148.0), "70cm": (420.0, 450.0)}


def _band_for_freq(freq_mhz):
    for band, (low, high) in LOCAL_REPEATER_BANDS_MHZ.items():
        if low <= freq_mhz <= high:
            return band
    return None


def _build_column_map(fieldnames):
    """Maps internal field name -> the actual CSV header name present
    in this file, matched case-insensitively against _HEADER_ALIASES.
    A field with no match at all is simply absent from the returned
    dict -- callers treat a missing optional field as unknown/None."""
    lower_to_actual = {name.strip().lower(): name for name in fieldnames}
    column_map = {}
    for field, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            if alias in lower_to_actual:
                column_map[field] = lower_to_actual[alias]
                break
    return column_map


def _parse_float(text):
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_repeater_csv(path):
    """Reads a repeater-list CSV (any of the formats this module's own
    docstring describes) and returns a list of {"callsign",
    "output_freq_hz", "input_freq_hz", "mode", "band", "city",
    "ctcss_hz", "lat", "lon"} -- open_repeater.py's own output shape,
    plus "lat"/"lon" (that API-based path doesn't need to return those
    per-entry, since the server already did the radius filtering; a
    CSV file hasn't, so filter_by_distance needs them here).

    Entries with no usable output frequency, or a frequency outside
    the 2m/70cm ham bands, are skipped -- same scope open_repeater.py's
    own LOCAL_REPEATER_BANDS filter already applies. Raises OSError/
    csv.Error on a genuinely unreadable file; a file that opens fine
    but has no recognizable frequency column at all returns an empty
    list rather than raising (same "no results" outcome as an Open
    Repeater search that just didn't find anything)."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        column_map = _build_column_map(reader.fieldnames)
        if "output_freq_mhz" not in column_map:
            return []

        repeaters = []
        for row in reader:
            output_mhz = _parse_float(row.get(column_map["output_freq_mhz"]))
            if output_mhz is None:
                continue
            band = _band_for_freq(output_mhz)
            if band is None:
                continue

            input_mhz = None
            if "input_freq_mhz" in column_map:
                input_mhz = _parse_float(row.get(column_map["input_freq_mhz"]))
            if input_mhz is None and "offset_mhz" in column_map:
                offset_mhz = _parse_float(row.get(column_map["offset_mhz"]))
                if offset_mhz is not None:
                    dup = (row.get(column_map.get("dup", ""), "") or "").strip().upper()
                    if dup.startswith("DUP-") or dup == "-":
                        input_mhz = output_mhz - offset_mhz
                    elif dup.startswith("DUP+") or dup == "+":
                        input_mhz = output_mhz + offset_mhz
                    else:
                        # No explicit direction column (e.g. RepeaterBook-
                        # style exports give a already-SIGNED offset like
                        # "-0.600000") -- just add it as-is.
                        input_mhz = output_mhz + offset_mhz
            if input_mhz is None:
                input_mhz = output_mhz

            repeaters.append({
                "callsign": (row.get(column_map.get("callsign", ""), "") or "").strip(),
                "output_freq_hz": round(output_mhz * 1e6),
                "input_freq_hz": round(input_mhz * 1e6),
                "mode": (row.get(column_map.get("mode", ""), "") or "FM").strip() or "FM",
                "band": band,
                "city": (row.get(column_map.get("city", ""), "") or "").strip(),
                "ctcss_hz": _parse_float(row.get(column_map["ctcss_hz"])) if "ctcss_hz" in column_map else None,
                "lat": _parse_float(row.get(column_map["lat"])) if "lat" in column_map else None,
                "lon": _parse_float(row.get(column_map["lon"])) if "lon" in column_map else None,
            })
        return repeaters


def filter_by_distance(repeaters, lat, lon, radius_km):
    """Keeps only entries within radius_km of (lat, lon) -- entries
    with no lat/lon at all (a source file that didn't include them)
    are dropped rather than guessed at being "close enough", since
    there's no way to tell. Strips "lat"/"lon" off the returned dicts
    -- back to open_repeater.py's own exact output shape, since
    memories_window.py's _repeater_to_entry doesn't expect those keys."""
    kept = []
    for repeater in repeaters:
        entry_lat, entry_lon = repeater.get("lat"), repeater.get("lon")
        if entry_lat is None or entry_lon is None:
            continue
        if haversine_km(lat, lon, entry_lat, entry_lon) > radius_km:
            continue
        kept.append({k: v for k, v in repeater.items() if k not in ("lat", "lon")})
    return kept
