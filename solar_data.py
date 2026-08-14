"""
Solar-terrestrial data for the Ham Dashboard: NOAA SWPC's public
K-index/solar-flux/sunspot-number feeds, N0NBH's widely-used HF band
conditions feed, a background QThread that polls both periodically, plus
two small standalone helpers used by the dashboard's map -- Maidenhead
grid-square conversion and basic day/night solar-position astronomy
(sub-solar point, solar elevation at a given lat/lon/time).
"""

import datetime
import json
import math
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from PySide6.QtCore import QThread, Signal

# ==================== Ham Dashboard (HamClock-inspired) ====================
#
# HamClock (WB0OEW) has many panes -- DX cluster, satellite tracking,
# VOACAP propagation modeling -- each of which is a substantial project
# on its own, in the same way WSJT-X's decode engine was. This is scoped
# to the core "what's the current situation" data most operators actually
# check at a glance: a day/night world map with terminator, UTC/local
# clocks, and live solar-terrestrial indices -- pulled from the same
# public NOAA SWPC data source HamClock itself uses, not reimplemented.
#
# Confirmed real, public, keyless endpoints (via direct lookup of NOAA
# SWPC's own data service directories):
NOAA_K_INDEX_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
NOAA_SOLAR_FLUX_URL = "https://services.swpc.noaa.gov/json/f107_cm_flux.json"
NOAA_SUNSPOT_URL = "https://services.swpc.noaa.gov/json/solar-cycle/observed-solar-cycle-indices.json"

# HF band-by-band Day/Night propagation conditions (Good/Fair/Poor/Band
# Closed) come from N0NBH's widely-used, ham-radio-specific public feed --
# the de facto community-standard source for this, used by countless ham
# radio tools (Ham Radio Deluxe, DXView, and many others). This is real
# ionospheric-model output computed by that feed, not something
# approximated here -- genuine per-band propagation modeling (MUF, solar
# zenith angle, disturbance effects) is real physics, not something to
# guess at with an ad-hoc formula.
HAMQSL_XML_URL = "https://www.hamqsl.com/solarxml.php"
BAND_CONDITION_RANGES = ["80m-40m", "30m-20m", "17m-15m", "12m-10m"]


def _fetch_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_band_conditions():
    """Fetches the Day/Night condition (Good/Fair/Poor/Band Closed) for
    each of BAND_CONDITION_RANGES from N0NBH's feed. Returns
    (conditions_dict, error_or_None); conditions_dict keys are
    (band_range, "day"|"night") tuples.

    The confirmed structure (from a live fetch of this exact feed):
    <solarflux>, <aindex>, <kindex>, <sunspots> etc. as direct child
    tags of <solardata>. The band-condition elements' exact nesting
    wasn't independently confirmed the same way (only the widely-
    referenced XPath pattern other tools use for this feed --
    band[@name=...][@time=...] -- was), so parsing here is deliberately
    tolerant: it searches the whole tree for any element carrying both a
    name and time attribute, rather than assuming one exact parent path."""
    try:
        with urllib.request.urlopen(HAMQSL_XML_URL, timeout=10) as response:
            xml_bytes = response.read()
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        return {}, str(exc)

    conditions = {}
    for elem in root.iter():
        name = elem.get("name")
        time_of_day = elem.get("time")
        if name and time_of_day and elem.text:
            conditions[(name.strip(), time_of_day.strip().lower())] = elem.text.strip()

    if not conditions:
        # Didn't find the expected structure at all -- report what the
        # actual tree looks like so this is fixable from real evidence
        # rather than another guess.
        tags = sorted({elem.tag for elem in root.iter()})
        return {}, f"no band-condition elements found; actual tags present: {tags}"

    return conditions, None


def fetch_solar_data():
    """Fetches current K-index, solar flux (SFI), and sunspot number
    (SSN) from NOAA's public feeds. The endpoint URLs above are
    confirmed real; the exact field NAMES within each response are NOT
    individually confirmed (couldn't fetch live content directly during
    development), so each value tries a few plausible key names
    defensively -- same pattern used throughout this app for anything
    where the exact shape wasn't directly verifiable. When none of the
    candidates match, the error includes the ACTUAL keys/shape found in
    the response, so a wrong guess is immediately diagnosable instead of
    needing another round of blind guessing."""
    result = {}

    # K-index. Confirmed via a live error: this is NOT a header-row-plus-
    # data-rows array (that assumption was wrong) -- it's a plain list of
    # dicts, one per row. Handles both shapes defensively regardless,
    # since NOAA's various endpoints aren't all consistent with each
    # other.
    try:
        rows = _fetch_json(NOAA_K_INDEX_URL)
        kp_value = None
        if rows and isinstance(rows[0], dict):
            last_row = rows[-1]
            for candidate in ("Kp", "kp", "kp_index", "estimated_kp"):
                if candidate in last_row:
                    kp_value = last_row[candidate]
                    break
            if kp_value is None:
                result["k_index_error"] = f"none of the expected keys found; actual keys: {list(last_row.keys())}"
        elif rows and isinstance(rows[0], list):
            header = rows[0]
            data_rows = rows[1:]
            kp_col = None
            for candidate in ("Kp", "kp", "kp_index", "estimated_kp"):
                if candidate in header:
                    kp_col = header.index(candidate)
                    break
            if kp_col is not None and data_rows:
                kp_value = data_rows[-1][kp_col]
            else:
                result["k_index_error"] = f"none of the expected column names found; actual header row: {header}"
        else:
            result["k_index_error"] = f"unexpected response shape: {type(rows).__name__}"
        if kp_value is not None:
            result["k_index"] = float(kp_value)
    except Exception as exc:
        result["k_index_error"] = str(exc)

    # Solar flux (SFI, 10.7cm radio flux).
    try:
        data = _fetch_json(NOAA_SOLAR_FLUX_URL)
        entry = data[-1] if isinstance(data, list) and data else data
        found = False
        for candidate in ("flux", "f10.7", "f107", "observed_flux", "adjusted_flux"):
            if isinstance(entry, dict) and candidate in entry:
                result["solar_flux"] = float(entry[candidate])
                found = True
                break
        if not found:
            shape = list(entry.keys()) if isinstance(entry, dict) else f"{type(entry).__name__}: {entry!r:.200}"
            result["solar_flux_error"] = f"none of the expected field names found; actual keys/shape: {shape}"
    except Exception as exc:
        result["solar_flux_error"] = str(exc)

    # Sunspot number (SSN). observed-solar-cycle-indices.json is NOAA's
    # own aggregate monthly-mean product (confirmed by name against
    # NOAA's own documented "Recent Solar Indices of Observed Monthly
    # Mean Values") -- the earlier sunspot_report.json was confirmed (via
    # its actual keys: Obsdate/Station/Region/Numspot/Zurich/Spotclass...)
    # to be a raw PER-REGION observation report, not a single current SSN
    # value at all, which is why that one never worked.
    try:
        data = _fetch_json(NOAA_SUNSPOT_URL)
        entry = data[-1] if isinstance(data, list) and data else data
        found = False
        for candidate in ("ssn", "SSN", "sunspot_number", "spot_num", "observed_ssn", "ssn_smoothed"):
            if isinstance(entry, dict) and candidate in entry:
                result["sunspot_number"] = int(float(entry[candidate]))
                found = True
                break
        if not found:
            shape = list(entry.keys()) if isinstance(entry, dict) else f"{type(entry).__name__}: {entry!r:.200}"
            result["sunspot_number_error"] = f"none of the expected field names found; actual keys/shape: {shape}"
    except Exception as exc:
        result["sunspot_number_error"] = str(exc)

    # HF band conditions (Day/Night, per band range) from N0NBH's feed.
    conditions, band_error = fetch_band_conditions()
    if conditions:
        result["band_conditions"] = conditions
    if band_error:
        result["band_conditions_error"] = band_error

    return result


class SolarDataWorker(QThread):
    """Fetches solar-terrestrial data from NOAA every 15 minutes (matches
    roughly how often these values actually change -- no need to hammer
    a public endpoint more often than that) on its own thread, so the
    blocking urllib calls never stall the GUI."""

    data_updated = Signal(dict)

    def run(self):
        while not self.isInterruptionRequested():
            try:
                self.data_updated.emit(fetch_solar_data())
            except Exception as exc:
                self.data_updated.emit({"fetch_error": str(exc)})
            for _ in range(15 * 60):
                if self.isInterruptionRequested():
                    return
                self.msleep(1000)


def maidenhead_to_latlon(grid):
    """Converts a Maidenhead grid locator (4 or 6 characters, e.g.
    "EM12" or "EM12ab") to the approximate (lat, lon) in degrees at the
    center of that grid square. Standard, well-established algorithm."""
    grid = grid.strip().upper()
    if len(grid) < 4 or not grid[2].isdigit() or not grid[3].isdigit():
        raise ValueError("expected a 4 or 6 character grid square, e.g. EM12 or EM12ab")
    lon = (ord(grid[0]) - ord("A")) * 20 - 180
    lat = (ord(grid[1]) - ord("A")) * 10 - 90
    lon += int(grid[2]) * 2
    lat += int(grid[3]) * 1
    if len(grid) >= 6 and grid[4].isalpha() and grid[5].isalpha():
        lon += (ord(grid[4]) - ord("A")) * (2 / 24.0) + (1 / 24.0)
        lat += (ord(grid[5]) - ord("A")) * (1 / 24.0) + (0.5 / 24.0)
    else:
        lon += 1.0   # center of the 2-degree-wide square
        lat += 0.5   # center of the 1-degree-tall square
    return lat, lon


def _solar_subpoint(dt_utc):
    """Returns (lat, lon) in degrees of the point directly under the sun
    at the given UTC datetime -- standard approximate formulas (Cooper's
    declination approximation; equation-of-time correction skipped),
    accurate to roughly a degree. Plenty for a visual terminator line on
    a small dashboard map, not intended for precision astronomy."""
    day_of_year = dt_utc.timetuple().tm_yday
    declination = 23.44 * math.sin(math.radians(360 / 365.0 * (day_of_year - 81)))
    hours_utc = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
    longitude = -(hours_utc - 12.0) * 15.0
    longitude = ((longitude + 180) % 360) - 180
    return declination, longitude


def _solar_elevation(lat, lon, dt_utc):
    """Solar elevation angle in degrees at the given lat/lon and UTC
    time -- negative means the sun is below the horizon (night)."""
    decl_deg, subpoint_lon = _solar_subpoint(dt_utc)
    decl = math.radians(decl_deg)
    lat_rad = math.radians(lat)
    hour_angle = math.radians(lon - subpoint_lon)
    elevation = math.asin(
        math.sin(lat_rad) * math.sin(decl)
        + math.cos(lat_rad) * math.cos(decl) * math.cos(hour_angle)
    )
    return math.degrees(elevation)
