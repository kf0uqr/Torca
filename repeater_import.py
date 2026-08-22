"""
Parses a RepeaterBook CSV export into memory-entry-ready repeater
dicts for memories_window.py's Local Repeaters tab (_repeater_to_entry,
LocalRepeatersTabPage) -- {"callsign", "output_freq_hz",
"input_freq_hz", "mode", "ctcss_hz", "city"}. Imports every row in the
file that has a usable frequency, unfiltered -- per explicit
instruction, no band restriction and no location/radius filtering at
all; the operator's own CSV (already whatever they chose to export
from RepeaterBook -- one state, one band, a search radius already
applied there, etc.) is trusted as-is.

This is the ONLY repeater data source in the app -- deliberately not
an API integration of any kind. RepeaterBook's own Export API requires
approval and a per-app/per-user token, and its current published terms
(repeaterbook.com/wiki/doku.php?id=api, "What Is Less Likely To Be
Approved") explicitly list "nearby repeater discovery tools" and
"standalone export/download services" as generally NOT authorized
without separate written permission -- exactly what a "Local
Repeaters" feature is. A CSV the operator has already downloaded
themselves, through their own manual browsing of RepeaterBook.com's
own search-results page, carries no such restriction -- it's their own
personal use of data they already have in hand, and this module never
talks to RepeaterBook's servers at all.

Column matching is alias-based and case-insensitive rather than locked
to one exact header spelling, so a differently-formatted RepeaterBook
export (or, incidentally, any other reasonably-labeled repeater CSV --
an ARRL/club-published list, Icom's own repeater-list download, etc.)
still has a fair chance of working, without being the design target.
"""

import csv

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
}


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
    """Reads a RepeaterBook CSV export (or any similarly-labeled
    repeater CSV -- see this module's own docstring) and returns a
    list of {"callsign", "output_freq_hz", "input_freq_hz", "mode",
    "city", "ctcss_hz"} for every row with a usable frequency --
    unfiltered by band or location, the entire file.

    Raises OSError/csv.Error on a genuinely unreadable file; a file
    that opens fine but has no recognizable frequency column at all
    returns an empty list rather than raising (same "no results"
    outcome as a search that just didn't find anything)."""
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
                "city": (row.get(column_map.get("city", ""), "") or "").strip(),
                "ctcss_hz": _parse_float(row.get(column_map["ctcss_hz"])) if "ctcss_hz" in column_map else None,
            })
        return repeaters
