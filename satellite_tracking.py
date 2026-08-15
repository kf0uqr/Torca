"""
Satellite tracking for the Ham Dashboard and main radio window: real
orbital propagation via the sgp4 library (NORAD's standard SGP4/SDP4
algorithm -- not reimplemented here, same reasoning as not hand-rolling
FT8 decoding), TLE data from CelesTrak, transponder data from SatNOGS
DB, Doppler correction / look-angle (elevation+azimuth) / AOS-LOS math
(pure functions, no Qt dependency -- RadioWindow in main_window.py
drives the radio and overlay display with these), and the dialogs for
managing tracked satellites (refresh TLEs, add/remove, store all known
transponder data per satellite, pick which satellites display).
"""

import datetime
import json
import math
import pathlib
import urllib.error
import urllib.request

try:
    # pip install sgp4 -- the standard, widely-used Python implementation
    # of NORAD's SGP4/SDP4 satellite orbital propagation algorithm (by
    # Brandon Rhodes). This is precise numerical algorithm work in the
    # same category as FT8 decoding -- not something to hand-roll -- so
    # a real, established library is used here rather than an
    # approximation.
    from sgp4.api import Satrec, jday
    SGP4_AVAILABLE = True
except ImportError:
    Satrec = None
    jday = None
    SGP4_AVAILABLE = False

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
)

# ==================== Satellite tracking ====================
#
# Real orbital propagation (SGP4/SDP4) is precise numerical algorithm
# work in the same category as FT8 decoding -- not something to hand-
# roll -- so the well-established `sgp4` PyPI package (by Brandon
# Rhodes) is used for that specifically. TLE data comes from CelesTrak's
# confirmed current amateur-radio-satellite endpoint (their older .txt
# format is being phased out due to a 5-digit catalog number limit).
# Transponder info (uplink/downlink/mode) has no equivalently reliable
# machine-readable public source that was found, so that's entered and
# edited manually via SatelliteConfigDialog rather than guessed/assumed.
SATELLITE_TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle"
SATELLITE_DATA_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "satellites.json"
EARTH_RADIUS_KM = 6371.0


def load_satellite_data():
    """Loads the locally-stored satellite list (TLE + transponder info +
    which ones are checked to display) -- a plain JSON file, same
    pattern as this app's other local caches. Returns an empty list if
    nothing's stored yet (e.g. first run)."""
    if not SATELLITE_DATA_PATH.exists():
        return []
    try:
        satellites = json.loads(SATELLITE_DATA_PATH.read_text())
    except Exception as exc:
        print(f"[ERROR] Ham Dashboard: couldn't read stored satellite data ({exc}); starting with an empty list.")
        return []
    for sat in satellites:
        _migrate_legacy_transponder_fields(sat)
    return satellites


def _migrate_legacy_transponder_fields(sat):
    """Satellites used to store a single chosen uplink/downlink/mode
    directly on the satellite dict; that's now a "transponders" list
    holding everything SatNOGS knows about the satellite. Converts an
    old-format entry in place the first time it's loaded."""
    if "transponders" in sat:
        return
    uplink = sat.pop("uplink_mhz", "")
    downlink = sat.pop("downlink_mhz", "")
    mode = sat.pop("mode", "")
    if uplink or downlink or mode:
        sat["transponders"] = [{
            "description": "Transponder",
            "uplink_mhz": uplink,
            "downlink_mhz": downlink,
            "mode": mode,
            "alive": True,
        }]
    else:
        sat["transponders"] = []


def save_satellite_data(satellites):
    try:
        SATELLITE_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        SATELLITE_DATA_PATH.write_text(json.dumps(satellites, indent=2))
    except Exception as exc:
        print(f"[ERROR] Ham Dashboard: couldn't save satellite data ({exc}).")


def fetch_amateur_tles():
    """Fetches current TLEs for amateur radio satellites from CelesTrak.
    Returns a list of (name, line1, line2) tuples. Raises on failure --
    callers should catch and report."""
    request = urllib.request.Request(
        SATELLITE_TLE_URL,
        # Same lesson learned from Wikimedia's 403: send a descriptive
        # User-Agent proactively rather than waiting for another block.
        headers={"User-Agent": "IcomRadioControlApp/1.0 (desktop ham radio control application)"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        text = response.read().decode("utf-8", errors="replace")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    entries = []
    for i in range(0, len(lines) - 2, 3):
        name, line1, line2 = lines[i], lines[i + 1], lines[i + 2]
        if line1.startswith("1 ") and line2.startswith("2 "):
            entries.append((name.strip(), line1, line2))
    return entries


def _gmst_degrees(jd, fr):
    """Greenwich Mean Sidereal Time in degrees -- standard IAU 1982
    formula. Needed because sgp4's output (TEME frame) is inertial
    (fixed relative to the stars), while lat/lon needs an Earth-fixed
    frame that accounts for Earth's rotation since epoch."""
    t = jd + fr
    d = t - 2451545.0
    return (280.46061837 + 360.98564736629 * d) % 360.0


def _propagate_teme(line1, line2, dt_utc):
    """Runs sgp4 and returns (jd, fr, position_km, velocity_km_s) in the
    TEME (inertial) frame -- the shared first step behind both
    propagate_satellite (position -> lat/lon/altitude, for the map) and
    doppler_correction (position + velocity -> range rate). Returns None
    if sgp4 isn't installed or propagation fails for this specific TLE."""
    if not SGP4_AVAILABLE:
        return None
    try:
        sat = Satrec.twoline2rv(line1, line2)
        jd, fr = jday(
            dt_utc.year, dt_utc.month, dt_utc.day,
            dt_utc.hour, dt_utc.minute, dt_utc.second + dt_utc.microsecond / 1e6,
        )
        error_code, position, velocity = sat.sgp4(jd, fr)
        if error_code != 0:
            return None
        return jd, fr, position, velocity
    except Exception:
        return None


def propagate_satellite(line1, line2, dt_utc):
    """Computes (lat, lon, altitude_km) for a satellite at the given UTC
    datetime. sgp4 does the actual orbital propagation (TEME-frame
    position); converting that to geodetic lat/lon here uses a standard
    GMST rotation with a SPHERICAL Earth approximation -- reasonable for
    a map-scale dashboard visualization, not precision antenna pointing
    (a full WGS84 ellipsoid conversion would add real complexity for a
    negligible visual difference at this zoom level -- same tradeoff
    already made for the day/night terminator). Returns None if sgp4
    isn't installed or propagation fails for this specific TLE."""
    result = _propagate_teme(line1, line2, dt_utc)
    if result is None:
        return None
    jd, fr, position, _velocity = result
    x, y, z = position  # km, TEME frame
    r = math.sqrt(x * x + y * y + z * z)
    if r == 0:
        return None
    lat = math.degrees(math.asin(z / r))
    gmst = _gmst_degrees(jd, fr)
    lon = math.degrees(math.atan2(y, x)) - gmst
    lon = ((lon + 180) % 360) - 180
    altitude_km = r - EARTH_RADIUS_KM
    return lat, lon, altitude_km


# Earth's rotation rate, in rad/s -- same sidereal rate _gmst_degrees uses
# (360.98564736629 deg/day), just converted for the observer velocity
# calculation below (an object fixed on the ground still moves in the
# inertial TEME frame, purely because Earth is rotating under it).
EARTH_ROTATION_RATE_RAD_S = math.radians(360.98564736629) / 86400.0
SPEED_OF_LIGHT_KM_S = 299792.458


def _observer_teme_frame(observer_lat, observer_lon, observer_elevation_km, gmst_rad):
    """The observer's position + velocity in the TEME inertial frame at
    the instant gmst_rad corresponds to, plus the local Up/East/North
    unit vectors at that same instant (used to turn a satellite's TEME
    position into elevation/azimuth). Same spherical-Earth approximation
    as propagate_satellite (observer_elevation_km is added straight onto
    EARTH_RADIUS_KM rather than modeled against a WGS84 ellipsoid) --
    Earth's oblateness affects this by well under the resolution a human
    retunes a radio to or points an antenna with. The observer's TEME-
    frame velocity isn't zero even though it's "standing still" on the
    ground, because Earth's rotation is carrying it around the polar
    axis -- needed for Doppler's range-rate calculation."""
    obs_radius_km = EARTH_RADIUS_KM + observer_elevation_km
    lat_rad = math.radians(observer_lat)
    lon_rad = math.radians(observer_lon) + gmst_rad  # Earth-fixed longitude -> TEME longitude
    cos_lat, sin_lat = math.cos(lat_rad), math.sin(lat_rad)
    cos_lon, sin_lon = math.cos(lon_rad), math.sin(lon_rad)

    position = (obs_radius_km * cos_lat * cos_lon, obs_radius_km * cos_lat * sin_lon, obs_radius_km * sin_lat)
    # Earth's rotation vector (0, 0, omega) crossed with the observer's position.
    velocity = (-EARTH_ROTATION_RATE_RAD_S * position[1], EARTH_ROTATION_RATE_RAD_S * position[0], 0.0)
    # On a sphere, the local zenith ("Up") is just the observer's own
    # radial direction -- East and North complete a standard topocentric
    # ENU frame from there.
    up = (cos_lat * cos_lon, cos_lat * sin_lon, sin_lat)
    east = (-sin_lon, cos_lon, 0.0)
    north = (-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat)
    return position, velocity, up, east, north


def satellite_look_angles(line1, line2, dt_utc, observer_lat, observer_lon, observer_elevation_km=0.0):
    """Returns {"elevation_deg", "azimuth_deg", "range_km"} for the
    satellite as seen from the observer at dt_utc -- azimuth is degrees
    clockwise from true North. Position-only (no velocity/Doppler), so
    it's cheap enough to call repeatedly for an AOS/LOS search. Returns
    None if propagation fails (sgp4 missing/invalid TLE)."""
    result = _propagate_teme(line1, line2, dt_utc)
    if result is None:
        return None
    jd, fr, sat_pos, _sat_vel = result
    gmst_rad = math.radians(_gmst_degrees(jd, fr))
    obs_pos, _obs_vel, up, east, north = _observer_teme_frame(
        observer_lat, observer_lon, observer_elevation_km, gmst_rad
    )

    range_vec = tuple(s - o for s, o in zip(sat_pos, obs_pos))
    range_km = math.sqrt(sum(c * c for c in range_vec))
    if range_km == 0:
        return None
    range_up = sum(c * u for c, u in zip(range_vec, up))
    range_east = sum(c * u for c, u in zip(range_vec, east))
    range_north = sum(c * u for c, u in zip(range_vec, north))

    elevation_deg = math.degrees(math.asin(max(-1.0, min(1.0, range_up / range_km))))
    azimuth_deg = math.degrees(math.atan2(range_east, range_north)) % 360.0
    return {"elevation_deg": elevation_deg, "azimuth_deg": azimuth_deg, "range_km": range_km}


def doppler_correction(base_freq_hz, line1, line2, dt_utc, observer_lat, observer_lon,
                        observer_elevation_km=0.0):
    """Corrects base_freq_hz (a transponder's downlink) for the
    satellite's Doppler shift at dt_utc, as seen from observer_lat/lon/
    elevation.

    Standard non-relativistic Doppler: f_observed = f_transmitted *
    (1 - range_rate / c), where range_rate is how fast the slant range
    to the satellite is changing (negative while approaching -- range
    shrinking -- which is why that shows up as a HIGHER frequency).
    Both the satellite (from sgp4) and the observer are expressed in the
    same TEME inertial frame via _observer_teme_frame().

    Returns a dict with frequency_hz (the corrected downlink), doppler_hz
    (how much correction was applied), range_km, range_rate_km_s,
    elevation_deg, and azimuth_deg (degrees clockwise from true North).
    Returns None if propagation fails (sgp4 missing/invalid TLE)."""
    result = _propagate_teme(line1, line2, dt_utc)
    if result is None:
        return None
    jd, fr, sat_pos, sat_vel = result
    gmst_rad = math.radians(_gmst_degrees(jd, fr))
    obs_pos, obs_vel, up, east, north = _observer_teme_frame(
        observer_lat, observer_lon, observer_elevation_km, gmst_rad
    )

    range_vec = tuple(s - o for s, o in zip(sat_pos, obs_pos))
    range_km = math.sqrt(sum(c * c for c in range_vec))
    if range_km == 0:
        return None
    rel_vel = tuple(s - o for s, o in zip(sat_vel, obs_vel))
    range_rate_km_s = sum(r * v for r, v in zip(range_vec, rel_vel)) / range_km

    range_up = sum(c * u for c, u in zip(range_vec, up))
    range_east = sum(c * u for c, u in zip(range_vec, east))
    range_north = sum(c * u for c, u in zip(range_vec, north))
    elevation_deg = math.degrees(math.asin(max(-1.0, min(1.0, range_up / range_km))))
    azimuth_deg = math.degrees(math.atan2(range_east, range_north)) % 360.0

    corrected_hz = base_freq_hz * (1.0 - range_rate_km_s / SPEED_OF_LIGHT_KM_S)
    return {
        "frequency_hz": corrected_hz,
        "doppler_hz": corrected_hz - base_freq_hz,
        "range_km": range_km,
        "range_rate_km_s": range_rate_km_s,
        "elevation_deg": elevation_deg,
        "azimuth_deg": azimuth_deg,
    }


def next_aos_los(line1, line2, dt_utc, observer_lat, observer_lon, observer_elevation_km=0.0,
                  search_horizon_hours=48, coarse_step_seconds=30):
    """Finds this satellite's next horizon crossing (elevation 0
    degrees) after dt_utc: AOS ("Acquisition of Signal", it's about to
    rise) if it's currently below the horizon, LOS ("Loss of Signal",
    it's about to set) if it's currently above. A coarse linear scan
    (step size chosen well under a typical LEO pass's width, which is
    several minutes, so a pass can't be stepped over entirely) brackets
    the crossing, then 20 rounds of bisection refine it to sub-second
    precision -- this is prediction, not live tracking, so it doesn't
    need to be fast, just correct. 48h default because a given LEO
    ground track can leave an observer's visible window for a day or
    more at a time (confirmed live: ISS, a mid-latitude observer, no
    pass for the next ~8 hours at the moment this was written) -- even
    so, a full run only takes tens of milliseconds, and this only runs
    once per crossing, not every tracking tick.

    Returns {"event": "AOS" or "LOS", "time_utc": datetime,
    "seconds_away": float}, or None if propagation fails or no crossing
    is found within search_horizon_hours (e.g. a geometry where this
    satellite never rises for this observer, or a decayed/invalid TLE)."""
    current = satellite_look_angles(line1, line2, dt_utc, observer_lat, observer_lon, observer_elevation_km)
    if current is None:
        return None
    event = "LOS" if current["elevation_deg"] >= 0 else "AOS"

    max_steps = int(search_horizon_hours * 3600 / coarse_step_seconds)
    prev_t, prev_elevation = dt_utc, current["elevation_deg"]
    for step in range(1, max_steps + 1):
        t = dt_utc + datetime.timedelta(seconds=step * coarse_step_seconds)
        look = satellite_look_angles(line1, line2, t, observer_lat, observer_lon, observer_elevation_km)
        if look is None:
            return None
        elevation = look["elevation_deg"]
        if (elevation >= 0) != (prev_elevation >= 0):
            lo, hi, lo_elevation = prev_t, t, prev_elevation
            for _ in range(20):
                mid = lo + (hi - lo) / 2
                mid_look = satellite_look_angles(line1, line2, mid, observer_lat, observer_lon, observer_elevation_km)
                if mid_look is None:
                    break
                if (mid_look["elevation_deg"] >= 0) == (lo_elevation >= 0):
                    lo, lo_elevation = mid, mid_look["elevation_deg"]
                else:
                    hi = mid
            return {"event": event, "time_utc": hi, "seconds_away": (hi - dt_utc).total_seconds()}
        prev_t, prev_elevation = t, elevation
    return None


def format_countdown(seconds):
    """Formats a duration in seconds as "MM:SS" or "H:MM:SS" for
    display (AOS/LOS countdowns). Negative/None input clamps to 0."""
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def footprint_points(lat, lon, altitude_km, num_points=72):
    """Computes the actual great-circle boundary of a satellite's
    footprint (the area from which it's above the horizon) as a list of
    (lat, lon) points -- standard spherical navigation "destination
    point given start point, bearing, and angular distance" formula, not
    a naive ellipse (a great circle doesn't project to one on an
    equirectangular map, especially approaching the poles)."""
    if altitude_km is None or altitude_km <= 0:
        return []
    central_angle = math.acos(EARTH_RADIUS_KM / (EARTH_RADIUS_KM + altitude_km))
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    points = []
    for i in range(num_points + 1):
        bearing = math.radians(360.0 * i / num_points)
        point_lat = math.asin(
            math.sin(lat_rad) * math.cos(central_angle)
            + math.cos(lat_rad) * math.sin(central_angle) * math.cos(bearing)
        )
        point_lon = lon_rad + math.atan2(
            math.sin(bearing) * math.sin(central_angle) * math.cos(lat_rad),
            math.cos(central_angle) - math.sin(lat_rad) * math.sin(point_lat),
        )
        points.append((math.degrees(point_lat), (math.degrees(point_lon) + 540) % 360 - 180))
    return points


# SatNOGS DB (Libre Space Foundation) -- an open, community-maintained
# database of satellite transmitters/transponders. Confirmed real
# endpoint and field names from a live fetch of
# https://db.satnogs.org/api/transmitters/: downlink_low/uplink_low are
# integer Hz (not MHz), mode is a plain string, alive is a bool. Filtering
# is done with satellite__norad_cat_id -- a bare norad_cat_id query param
# is silently ignored by the API (confirmed live: it returns the entire
# ~5000-row unfiltered table instead of erroring), which is why every
# satellite used to show every other satellite's transponders too. Reads
# are keyless -- "API access is open to anyone" per SatNOGS' own docs; a
# key is only needed for write operations, which this app never does.
SATNOGS_TRANSMITTERS_URL = "https://db.satnogs.org/api/transmitters/"


def norad_id_from_tle_line1(line1):
    """Extracts the NORAD catalog number from a TLE's first line --
    standard TLE format places it in columns 3-7 (1-indexed)."""
    try:
        return int(line1[2:7].strip())
    except (ValueError, IndexError, TypeError):
        return None


def fetch_transponders(norad_cat_id):
    """Fetches known transmitters/transponders for a satellite from
    SatNOGS DB. Returns a list of dicts: {"description", "uplink_mhz",
    "downlink_mhz", "mode", "alive"} -- frequencies converted from
    SatNOGS' native Hz to MHz (matching this app's display convention),
    alive/active entries sorted first since a satellite can have several
    transmitters (e.g. a voice repeater vs. a telemetry beacon) and
    decommissioned ones are still useful reference but shouldn't be the
    default pick. Raises on failure -- callers should catch and report."""
    url = f"{SATNOGS_TRANSMITTERS_URL}?satellite__norad_cat_id={norad_cat_id}&format=json"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "IcomRadioControlApp/1.0 (desktop ham radio control application)"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))

    results = []
    for entry in data:
        # Defense in depth: only keep entries that actually match, in case
        # the API's filtering behavior changes again in the future.
        if entry.get("norad_cat_id") != norad_cat_id:
            continue
        downlink_hz = entry.get("downlink_low")
        uplink_hz = entry.get("uplink_low")
        results.append({
            "description": entry.get("description") or entry.get("type") or "Transmitter",
            "downlink_mhz": f"{downlink_hz / 1e6:.4f}" if downlink_hz else "",
            "uplink_mhz": f"{uplink_hz / 1e6:.4f}" if uplink_hz else "",
            "mode": entry.get("mode") or "",
            "alive": bool(entry.get("alive")),
        })
    results.sort(key=lambda r: not r["alive"])
    return results


class TransponderEditDialog(QDialog):
    """Shows/edits the full list of known transponders for one satellite
    -- populated from a SatNOGS fetch, hand-entered, or both. Picking
    which one to actually use when tuning is a later feature; this
    dialog is just for storing and correcting the data."""

    COLUMNS = ["Description", "Uplink (MHz)", "Downlink (MHz)", "Mode", "Active"]

    def __init__(self, satellite_name, transponders, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Transponders -- {satellite_name}")
        self.resize(560, 340)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self._load_rows(transponders)

        add_button = QPushButton("Add")
        add_button.clicked.connect(self._on_add_row)
        remove_button = QPushButton("Remove Selected")
        remove_button.clicked.connect(self._on_remove_row)
        button_row = QHBoxLayout()
        button_row.addWidget(add_button)
        button_row.addWidget(remove_button)
        button_row.addStretch(1)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(QLabel(
            "All known transponders/transmitters for this satellite (e.g. a "
            "voice repeater vs. a telemetry beacon)."
        ))
        layout.addWidget(self.table)
        layout.addLayout(button_row)
        layout.addWidget(button_box)
        self.setLayout(layout)

    def _load_rows(self, transponders):
        self.table.setRowCount(len(transponders))
        for row, transponder in enumerate(transponders):
            self._set_row(row, transponder)

    def _set_row(self, row, transponder):
        self.table.setItem(row, 0, QTableWidgetItem(transponder.get("description", "")))
        self.table.setItem(row, 1, QTableWidgetItem(transponder.get("uplink_mhz", "")))
        self.table.setItem(row, 2, QTableWidgetItem(transponder.get("downlink_mhz", "")))
        self.table.setItem(row, 3, QTableWidgetItem(transponder.get("mode", "")))
        active_item = QTableWidgetItem()
        active_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        active_item.setCheckState(Qt.Checked if transponder.get("alive", True) else Qt.Unchecked)
        self.table.setItem(row, 4, active_item)

    def _on_add_row(self):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._set_row(row, {"alive": True})
        self.table.selectRow(row)

    def _on_remove_row(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def result_transponders(self):
        transponders = []
        for row in range(self.table.rowCount()):
            description = self.table.item(row, 0).text().strip()
            uplink = self.table.item(row, 1).text().strip()
            downlink = self.table.item(row, 2).text().strip()
            mode = self.table.item(row, 3).text().strip()
            if not (description or uplink or downlink or mode):
                continue  # skip fully-blank rows
            transponders.append({
                "description": description,
                "uplink_mhz": uplink,
                "downlink_mhz": downlink,
                "mode": mode,
                "alive": self.table.item(row, 4).checkState() == Qt.Checked,
            })
        return transponders


class ManualTleDialog(QDialog):
    """Simple form for manually adding a satellite by pasting its name
    and two TLE lines directly -- e.g. one not in CelesTrak's amateur
    group, or a custom/private TLE."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Satellite")
        self.name_input = QLineEdit()
        self.line1_input = QLineEdit()
        self.line1_input.setPlaceholderText("1 25544U 98067A   ...")
        self.line2_input = QLineEdit()
        self.line2_input.setPlaceholderText("2 25544  51.6400 ...")

        form = QFormLayout()
        form.addRow("Name:", self.name_input)
        form.addRow("TLE Line 1:", self.line1_input)
        form.addRow("TLE Line 2:", self.line2_input)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(button_box)
        self.setLayout(layout)
        self._result = None

    def _on_accept(self):
        name = self.name_input.text().strip()
        line1 = self.line1_input.text().strip()
        line2 = self.line2_input.text().strip()
        if not name or not line1.startswith("1 ") or not line2.startswith("2 "):
            QMessageBox.warning(
                self, "Add Satellite",
                "Name is required, and TLE lines must start with \"1 \" and \"2 \" respectively."
            )
            return
        self._result = {
            "name": name, "line1": line1, "line2": line2,
            "transponders": [], "selected": True,
        }
        self.accept()

    def result_satellite(self):
        return self._result


class SatelliteConfigDialog(QDialog):
    """Right-click the Satellites button to open this: manage tracked
    satellites -- refresh TLEs from CelesTrak, add/remove satellites,
    store known transponder data (fetched from SatNOGS or hand-entered),
    and choose which ones display on the map. Picking a transponder to
    actually use is a later feature -- this just manages the data.

    Everything else here is only kept if OK is pressed (Cancel/closing
    the window discards it, same as any other form) -- EXCEPT
    transponder fetches/edits, which call on_change() (if given)
    immediately, right when they happen. Those go through a real
    network fetch and already show a "success" confirmation, so losing
    them to an accidental window-close (Qt's default for the X button/
    Escape is the same as Cancel) would be a surprising, silent data
    loss -- not just an unsaved-edit annoyance."""

    def __init__(self, satellites, parent=None, on_change=None):
        super().__init__(parent)
        self.setWindowTitle("Satellite Tracking")
        self.resize(560, 400)
        self._satellites = [dict(sat) for sat in satellites]  # local working copy until OK is pressed
        self._on_change = on_change

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Show", "Name"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._rebuild_table()

        self.refresh_button = QPushButton("Refresh TLEs from CelesTrak")
        self.refresh_button.clicked.connect(self._on_refresh_tles)
        self.fetch_transponder_button = QPushButton("Fetch Transponder Data (SatNOGS)")
        self.fetch_transponder_button.setToolTip(
            "Looks up known transmitters in SatNOGS DB (an open, community-"
            "maintained database) by NORAD catalog number, and stores all "
            "of them -- for every tracked satellite, not just selected ones."
        )
        self.fetch_transponder_button.clicked.connect(self._on_fetch_transponders)
        self.edit_transponder_button = QPushButton("Edit Transponders...")
        self.edit_transponder_button.setToolTip(
            "View or hand-edit the stored transponder list for the selected satellite."
        )
        self.edit_transponder_button.clicked.connect(self._on_edit_transponders)
        self.add_button = QPushButton("Add Satellite...")
        self.add_button.clicked.connect(self._on_add_satellite)
        self.remove_button = QPushButton("Remove Selected")
        self.remove_button.clicked.connect(self._on_remove_satellite)

        button_row = QHBoxLayout()
        button_row.addWidget(self.refresh_button)
        button_row.addWidget(self.fetch_transponder_button)
        button_row.addWidget(self.edit_transponder_button)
        button_row.addWidget(self.add_button)
        button_row.addWidget(self.remove_button)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(QLabel(
            "Check satellites to display on the map. \"Fetch Transponder "
            "Data\" updates every tracked satellite from SatNOGS DB; select "
            "one row and click \"Edit Transponders...\" to view or hand-edit "
            "its list."
        ))
        layout.addWidget(self.table)
        layout.addLayout(button_row)
        layout.addWidget(button_box)
        self.setLayout(layout)

    def _rebuild_table(self):
        self.table.setRowCount(len(self._satellites))
        for row, sat in enumerate(self._satellites):
            show_item = QTableWidgetItem()
            show_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            show_item.setCheckState(Qt.Checked if sat.get("selected") else Qt.Unchecked)
            self.table.setItem(row, 0, show_item)

            name_item = QTableWidgetItem(sat.get("name", ""))
            name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 1, name_item)

    def _on_refresh_tles(self):
        try:
            entries = fetch_amateur_tles()
        except Exception as exc:
            QMessageBox.critical(self, "Refresh TLEs", f"Couldn't fetch TLE data from CelesTrak:\n{exc}")
            return
        by_name = {name: (line1, line2) for name, line1, line2 in entries}
        updated = 0
        for sat in self._satellites:
            if sat["name"] in by_name:
                sat["line1"], sat["line2"] = by_name[sat["name"]]
                updated += 1
        existing_names = {sat["name"] for sat in self._satellites}
        added = 0
        for name, line1, line2 in entries:
            if name not in existing_names:
                self._satellites.append({
                    "name": name, "line1": line1, "line2": line2,
                    "transponders": [],
                    "selected": False,
                })
                added += 1
        self._rebuild_table()
        QMessageBox.information(
            self, "Refresh TLEs",
            f"Updated {updated} existing satellite(s), added {added} new one(s) "
            "from CelesTrak's amateur radio satellite list."
        )

    def _on_add_satellite(self):
        dialog = ManualTleDialog(self)
        if dialog.exec() == QDialog.Accepted and dialog.result_satellite():
            self._satellites.append(dialog.result_satellite())
            self._rebuild_table()

    def _on_remove_satellite(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            del self._satellites[row]
        self._rebuild_table()

    def _on_fetch_transponders(self):
        if not self._satellites:
            QMessageBox.information(self, "Fetch Transponder Data", "No tracked satellites yet -- add some first.")
            return
        confirm = QMessageBox.question(
            self, "Fetch Transponder Data",
            f"This looks up transponder data from SatNOGS DB for all "
            f"{len(self._satellites)} tracked satellite(s), one at a time -- "
            "it may take a while and the window won't respond until it's "
            "done. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        fetched = 0
        skipped = 0
        for sat in self._satellites:
            norad_id = norad_id_from_tle_line1(sat.get("line1", ""))
            if norad_id is None:
                skipped += 1
                print(f"[ERROR] Ham Dashboard: couldn't determine a NORAD catalog number for {sat.get('name', '?')} from its TLE -- skipped.")
                continue
            try:
                sat["transponders"] = fetch_transponders(norad_id)
                fetched += 1
            except Exception as exc:
                skipped += 1
                print(f"[ERROR] Ham Dashboard: transponder fetch failed for {sat.get('name', '?')} (NORAD {norad_id}): {exc}")
        self._rebuild_table()
        if fetched:
            if self._on_change:
                self._on_change(self._satellites)
        QMessageBox.information(
            self, "Fetch Transponder Data",
            f"Updated stored transponder data for {fetched} of {len(self._satellites)} "
            f"tracked satellite(s) from SatNOGS DB."
            + (f" {skipped} skipped due to errors -- see console for detail." if skipped else "")
        )

    def _on_edit_transponders(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if len(rows) != 1:
            QMessageBox.information(
                self, "Edit Transponders", "Select exactly one satellite in the table first."
            )
            return
        sat = self._satellites[rows[0]]
        dialog = TransponderEditDialog(sat.get("name", "?"), sat.get("transponders", []), self)
        if dialog.exec() == QDialog.Accepted:
            sat["transponders"] = dialog.result_transponders()
            self._rebuild_table()
            if self._on_change:
                self._on_change(self._satellites)

    def _on_accept(self):
        for row, sat in enumerate(self._satellites):
            sat["selected"] = self.table.item(row, 0).checkState() == Qt.Checked
        self.accept()

    def result_satellites(self):
        return self._satellites
