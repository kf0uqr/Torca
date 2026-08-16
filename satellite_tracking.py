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
    QMenu,
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
                        observer_elevation_km=0.0, uplink=False):
    """Corrects base_freq_hz for the satellite's Doppler shift at
    dt_utc, as seen from observer_lat/lon/elevation. base_freq_hz is a
    transponder's nominal downlink by default; pass uplink=True to
    correct its nominal uplink instead (see below for why that's not
    just a sign flip).

    Standard non-relativistic Doppler, downlink: f_observed = f_emitted
    * (1 - range_rate / c), where range_rate is how fast the slant
    range to the satellite is changing (negative while approaching --
    range shrinking -- which is why that shows up as a HIGHER
    frequency). That's the frequency to tune the receiver to so a
    signal actually emitted at base_freq_hz is heard correctly.

    Uplink is the same physics from the other direction: WE'RE the
    emitter now, and we want the satellite's receiver to see exactly
    base_freq_hz (its nominal uplink) after the same Doppler shift is
    applied to our transmission during propagation. That means solving
    the downlink formula for f_emitted given f_observed = base_freq_hz:
    f_emitted = base_freq_hz / (1 - range_rate / c) -- the exact
    inverse, not base_freq_hz * (1 + range_rate/c) (flipping the sign
    on the multiply is only a first-order approximation of that
    inverse; at LEO velocities the two agree to a small fraction of a
    Hz, but the exact form costs nothing extra to compute).

    Both the satellite (from sgp4) and the observer are expressed in
    the same TEME inertial frame via _observer_teme_frame().

    Returns a dict with frequency_hz (the corrected frequency),
    doppler_hz (how much correction was applied), range_km,
    range_rate_km_s, elevation_deg, and azimuth_deg (degrees clockwise
    from true North). Returns None if propagation fails (sgp4 missing/
    invalid TLE)."""
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

    doppler_factor = 1.0 - range_rate_km_s / SPEED_OF_LIGHT_KM_S
    corrected_hz = base_freq_hz / doppler_factor if uplink else base_freq_hz * doppler_factor
    return {
        "frequency_hz": corrected_hz,
        "doppler_hz": corrected_hz - base_freq_hz,
        "range_km": range_km,
        "range_rate_km_s": range_rate_km_s,
        "elevation_deg": elevation_deg,
        "azimuth_deg": azimuth_deg,
    }


def _find_crossing(line1, line2, dt_utc, observer_lat, observer_lon, observer_elevation_km,
                    step_seconds, max_steps):
    """Scans from dt_utc in steps of step_seconds (negative steps search
    backward in time instead of forward) until elevation crosses zero,
    then bisects for a precise crossing time. Direction-agnostic: the
    bisection midpoint formula (lo + (hi-lo)/2) lands at the arithmetic
    midpoint between two datetimes regardless of which is chronologically
    earlier, so the same loop serves both next_aos_los() (forward) and
    current_pass()'s backward search for a pass's actual start. Returns
    the crossing datetime, or None if none found within max_steps or
    propagation fails."""
    current = satellite_look_angles(line1, line2, dt_utc, observer_lat, observer_lon, observer_elevation_km)
    if current is None:
        return None
    prev_t, prev_elevation = dt_utc, current["elevation_deg"]
    for step in range(1, max_steps + 1):
        t = dt_utc + datetime.timedelta(seconds=step * step_seconds)
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
            return hi
        prev_t, prev_elevation = t, elevation
    return None


def _pass_max_elevation_deg(line1, line2, aos_time, los_time, observer_lat, observer_lon, observer_elevation_km):
    """Samples elevation across an AOS->LOS window (~40 samples) for the
    peak reached during the pass -- shared by current_pass() and
    find_passes(), which both need this over a known aos/los pair."""
    max_elevation_deg = 0.0
    sample_step = max(5.0, (los_time - aos_time).total_seconds() / 40.0)
    t = aos_time
    while t <= los_time:
        look = satellite_look_angles(line1, line2, t, observer_lat, observer_lon, observer_elevation_km)
        if look is not None:
            max_elevation_deg = max(max_elevation_deg, look["elevation_deg"])
        t += datetime.timedelta(seconds=sample_step)
    return max_elevation_deg


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
    crossing_time = _find_crossing(
        line1, line2, dt_utc, observer_lat, observer_lon, observer_elevation_km,
        coarse_step_seconds, max_steps,
    )
    if crossing_time is None:
        return None
    return {"event": event, "time_utc": crossing_time, "seconds_away": (crossing_time - dt_utc).total_seconds()}


def current_pass(line1, line2, dt_utc, observer_lat, observer_lon, observer_elevation_km=0.0,
                  search_horizon_hours=24, coarse_step_seconds=30):
    """If the satellite is above the horizon right now, returns the
    full pass it's in the middle of: {"aos_time" (in the past),
    "los_time" (upcoming), "duration_seconds", "max_elevation_deg"} --
    by searching backward for when it actually rose (so max_elevation_deg
    and duration_seconds cover the WHOLE pass, same as every entry
    find_passes() returns, not just the part still ahead) and forward
    for when it'll set. Returns None if it's not currently above the
    horizon, or if either search/propagation fails.

    24h default, not a tighter one matched to a typical LEO pass's
    length (tens of minutes) -- confirmed live against AO-10 (a
    Molniya-type highly elliptical orbit, not a LEO cubesat, also in
    this app's own amateur-satellite list): elevation 45 degrees "now"
    with an AOS that turned out to be ~9 hours earlier. A tighter bound
    would silently miss that satellite's active pass entirely (it'd
    just fall through to find_passes()'s forward search and report the
    wrong, later pass as "next" instead of recognizing it's up right
    now)."""
    current = satellite_look_angles(line1, line2, dt_utc, observer_lat, observer_lon, observer_elevation_km)
    if current is None or current["elevation_deg"] < 0:
        return None
    max_steps = int(search_horizon_hours * 3600 / coarse_step_seconds)
    aos_time = _find_crossing(
        line1, line2, dt_utc, observer_lat, observer_lon, observer_elevation_km,
        -coarse_step_seconds, max_steps,
    )
    los_time = _find_crossing(
        line1, line2, dt_utc, observer_lat, observer_lon, observer_elevation_km,
        coarse_step_seconds, max_steps,
    )
    if aos_time is None or los_time is None:
        return None
    max_elevation_deg = max(
        current["elevation_deg"],
        _pass_max_elevation_deg(line1, line2, aos_time, los_time, observer_lat, observer_lon, observer_elevation_km),
    )
    return {
        "aos_time": aos_time,
        "los_time": los_time,
        "duration_seconds": (los_time - aos_time).total_seconds(),
        "max_elevation_deg": max_elevation_deg,
    }


def find_passes(line1, line2, dt_utc, observer_lat, observer_lon, observer_elevation_km=0.0,
                 max_passes=3, search_horizon_hours=168, coarse_step_seconds=60):
    """Finds up to max_passes passes for one satellite starting from
    dt_utc, soonest first -- including one already in progress right now
    (see current_pass()), counted against max_passes same as any other.
    Returns a list of dicts: {"aos_time", "los_time", "duration_seconds",
    "max_elevation_deg", "active"} ("active" True only for a pass
    already under way at dt_utc). Stops early (possibly with fewer than
    max_passes, or an empty list) once no further crossing is found
    within search_horizon_hours of dt_utc -- e.g. a geometry where this
    satellite doesn't rise again for this observer within that window,
    or a decayed/invalid TLE.

    coarse_step_seconds defaults coarser than next_aos_los()'s own 30s
    default -- fine for a multi-day-ahead pass *listing* (a minute of
    slop on when a pass starts days out doesn't matter the way it does
    for a live countdown), and multiple searches per satellite times
    multiple satellites adds up fast otherwise."""
    passes = []
    cursor = dt_utc

    active = current_pass(
        line1, line2, dt_utc, observer_lat, observer_lon, observer_elevation_km,
        search_horizon_hours=min(24, search_horizon_hours), coarse_step_seconds=coarse_step_seconds,
    )
    if active is not None:
        active["active"] = True
        passes.append(active)
        cursor = active["los_time"] + datetime.timedelta(seconds=1)

    while len(passes) < max_passes:
        remaining_hours = search_horizon_hours - (cursor - dt_utc).total_seconds() / 3600.0
        if remaining_hours <= 0:
            break
        first = next_aos_los(
            line1, line2, cursor, observer_lat, observer_lon, observer_elevation_km,
            search_horizon_hours=remaining_hours, coarse_step_seconds=coarse_step_seconds,
        )
        if first is None:
            break
        if first["event"] == "LOS":
            # Shouldn't normally happen (the active-pass check above
            # already accounts for a pass in progress at dt_utc) -- but
            # if it does, skip past it rather than report a bare LOS
            # with no AOS.
            cursor = first["time_utc"] + datetime.timedelta(seconds=1)
            continue

        aos_time = first["time_utc"]
        remaining_hours = search_horizon_hours - (aos_time - dt_utc).total_seconds() / 3600.0
        second = next_aos_los(
            line1, line2, aos_time + datetime.timedelta(seconds=1),
            observer_lat, observer_lon, observer_elevation_km,
            search_horizon_hours=remaining_hours, coarse_step_seconds=coarse_step_seconds,
        )
        if second is None or second["event"] != "LOS":
            break  # an AOS should always be followed by a LOS -- bail out safely if not

        los_time = second["time_utc"]
        passes.append({
            "aos_time": aos_time,
            "los_time": los_time,
            "duration_seconds": (los_time - aos_time).total_seconds(),
            "max_elevation_deg": _pass_max_elevation_deg(
                line1, line2, aos_time, los_time, observer_lat, observer_lon, observer_elevation_km
            ),
            "active": False,
        })
        cursor = los_time + datetime.timedelta(seconds=1)
    return passes


def upcoming_passes(satellites, dt_utc, observer_lat, observer_lon, observer_elevation_km=0.0, count=10):
    """Combines find_passes() across every satellite in `satellites`
    (each a dict with "name"/"line1"/"line2") into one soonest-first
    list of the next `count` passes overall, each tagged with "name".

    Asks each satellite for at most a handful of its own passes (not
    `count` from every one of them, which scales badly with satellite
    count for no benefit -- filling a combined top-10 practically never
    needs more than 2-3 upcoming passes from any single satellite,
    unless there's only one or two satellites total, which the max(...)
    below covers)."""
    per_satellite_cap = max(3, -(-count // max(1, len(satellites))) + 1)  # ceil(count / n) + 1, floored at 3
    all_passes = []
    for sat in satellites:
        name = sat.get("name", "?")
        for pass_info in find_passes(
            sat.get("line1", ""), sat.get("line2", ""), dt_utc,
            observer_lat, observer_lon, observer_elevation_km, max_passes=per_satellite_cap,
        ):
            pass_info["name"] = name
            all_passes.append(pass_info)
    all_passes.sort(key=lambda p: p["aos_time"])
    return all_passes[:count]


def format_countdown(seconds):
    """Formats a duration in seconds as "MM:SS" or "H:MM:SS" for
    display (AOS/LOS countdowns). Negative/None input clamps to 0."""
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def compute_satellite_state(satellite, transponder, observer_lat, observer_lon,
                             observer_elevation_km, freq_offset_hz, cached_crossing=None):
    """Pure computation, no radio I/O and no instance state -- look
    angles, next AOS/LOS, and both directions' Doppler-corrected
    frequencies for the given satellite/transponder right now.

    Extracted from what used to be RadioWindow._compute_satellite_state
    (a per-radio-window method) into a standalone function now that
    satellite/transponder selection lives centrally in SatelliteSession
    (satellite_session.py) instead of being duplicated per connected
    radio -- multiple radios coordinating on one pass need everyone
    computing against the exact same inputs, not each independently
    reading its own local widgets.

    cached_crossing is whatever this function last returned as
    next_crossing (or None on the very first call) -- callers own that
    caching themselves (this function has no state of its own) and
    should pass the same value back in next time; the AOS/LOS search is
    real, if cheap, work, only worth re-running once the previously-
    found crossing has actually passed, not on every call.

    Returns (look, crossing_text, downlink_hz, downlink_doppler_hz,
    uplink_hz, uplink_doppler_hz, next_crossing), or None if propagation
    fails (invalid/missing TLE)."""
    line1, line2 = satellite.get("line1", ""), satellite.get("line2", "")
    now = datetime.datetime.now(datetime.timezone.utc)

    look = satellite_look_angles(line1, line2, now, observer_lat, observer_lon, observer_elevation_km)
    if look is None:
        return None

    next_crossing = cached_crossing
    if next_crossing is None or now >= next_crossing["time_utc"]:
        next_crossing = next_aos_los(
            line1, line2, now, observer_lat, observer_lon, observer_elevation_km
        )
    crossing_text = "AOS/LOS: unknown"
    if next_crossing:
        remaining = (next_crossing["time_utc"] - now).total_seconds()
        crossing_text = f"{next_crossing['event']} in {format_countdown(remaining)}"

    downlink_hz, downlink_doppler_hz = None, None
    uplink_hz, uplink_doppler_hz = None, None
    if transponder is not None:
        try:
            base_downlink_hz = round(float(transponder.get("downlink_mhz")) * 1e6)
        except (TypeError, ValueError):
            base_downlink_hz = None
        if base_downlink_hz is not None:
            # The tuning knob adjusts this offset instead of the radio
            # directly while tracking is active -- so manual tuning
            # within a linear satellite's passband, or a nudge to line
            # up with where it's actually transmitting, survives the
            # next tick instead of being overwritten by a recompute from
            # the transponder's bare nominal downlink. RX only -- there's
            # no equivalent TX offset.
            base_downlink_hz += freq_offset_hz
            result = doppler_correction(
                base_downlink_hz, line1, line2, now, observer_lat, observer_lon, observer_elevation_km,
                uplink=False,
            )
            if result is not None:
                downlink_hz = round(result["frequency_hz"])
                downlink_doppler_hz = result["doppler_hz"]

        try:
            base_uplink_hz = round(float(transponder.get("uplink_mhz")) * 1e6)
        except (TypeError, ValueError):
            base_uplink_hz = None
        if base_uplink_hz is not None:
            result = doppler_correction(
                base_uplink_hz, line1, line2, now, observer_lat, observer_lon, observer_elevation_km,
                uplink=True,
            )
            if result is not None:
                uplink_hz = round(result["frequency_hz"])
                uplink_doppler_hz = result["doppler_hz"]

    return look, crossing_text, downlink_hz, downlink_doppler_hz, uplink_hz, uplink_doppler_hz, next_crossing


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


def orbital_period_minutes(line2):
    """Parses a TLE's mean motion (line 2, standard columns 53-63:
    revolutions per day) into an orbital period in minutes. Returns
    None if line2 isn't a valid/parseable TLE line."""
    try:
        mean_motion_rev_per_day = float(line2[52:63].strip())
        if mean_motion_rev_per_day <= 0:
            return None
        return 1440.0 / mean_motion_rev_per_day
    except (ValueError, IndexError, TypeError):
        return None


def ground_track_points(line1, line2, dt_utc, num_points=72):
    """Computes the satellite's ground track (lat/lon points, no
    altitude) over one full orbit centered on dt_utc -- half an orbit
    before "now" and half an orbit after, the same visual convention
    as other satellite tracking apps' orbit path line. Returns [] if
    the orbital period can't be determined (invalid TLE) or sgp4
    isn't installed."""
    period_minutes = orbital_period_minutes(line2)
    if period_minutes is None:
        return []
    half_period = period_minutes / 2.0
    points = []
    for i in range(num_points + 1):
        offset_minutes = -half_period + (period_minutes * i / num_points)
        t = dt_utc + datetime.timedelta(minutes=offset_minutes)
        result = propagate_satellite(line1, line2, t)
        if result is None:
            continue
        lat, lon, _altitude_km = result
        points.append((lat, lon))
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


def tle_epoch_datetime(line1):
    """Parses a TLE's epoch (standard TLE format, columns 19-32 of the
    first line: 2-digit year + fractional day-of-year) into a
    timezone-aware UTC datetime -- lets the Satellite Info dialog show
    how stale a stored TLE is. Returns None if line1 isn't a valid/
    parseable TLE line."""
    try:
        epoch_str = line1[18:32].strip()
        year_2digit = int(epoch_str[:2])
        day_of_year_frac = float(epoch_str[2:])
        # Standard TLE convention (same pivot NORAD itself uses):
        # 57-99 -> 1957-1999, 00-56 -> 2000-2056.
        year = (1900 if year_2digit >= 57 else 2000) + year_2digit
        return (
            datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc)
            + datetime.timedelta(days=day_of_year_frac - 1)
        )
    except (ValueError, IndexError, TypeError):
        return None


def fetch_transponders(norad_cat_id):
    """Fetches known transmitters/transponders for a satellite from
    SatNOGS DB. Returns a list of dicts: {"description", "uplink_mhz",
    "downlink_mhz", "mode", "uplink_mode", "invert", "alive"} --
    frequencies converted from SatNOGS' native Hz to MHz (matching this
    app's display convention), alive/active entries sorted first since a
    satellite can have several transmitters (e.g. a voice repeater vs. a
    telemetry beacon) and decommissioned ones are still useful reference
    but shouldn't be the default pick. Raises on failure -- callers
    should catch and report.

    "mode" is the DOWNLINK mode; "uplink_mode" is captured separately --
    confirmed via a live fetch of https://db.satnogs.org/api/
    transmitters/?satellite__norad_cat_id=7530 (AO-7, a known inverting
    linear transponder) that SatNOGS records these independently, along
    with an "invert" bool, specifically because some linear transponders
    invert sidebands between uplink and downlink (e.g. that fetch showed
    a Mode U/V AO-7 entry with mode="USB"/uplink_mode="LSB"/invert=true).
    Without uplink_mode, there'd be no reliable way to auto-set Main's
    TX mode correctly for those -- see radio_mode_for_transponder and
    RadioWindow._apply_transponder_mode."""
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
            "uplink_mode": entry.get("uplink_mode") or "",
            "invert": bool(entry.get("invert")),
            "alive": bool(entry.get("alive")),
        })
    results.sort(key=lambda r: not r["alive"])
    return results


# Maps a transponder's stored "mode" string (SatNOGS DB or hand-entered
# via TransponderEditDialog) to one of this app's own confirmed-valid
# radio mode control values -- see CONTROL_DEFINITIONS["mode"]["options"]
# in constants.py: LSB, USB, AM, CW, RTTY, FM, WFM, CW_R, RTTY_R, DV,
# confirmed via a real set_mode() runtime error message. Distinct raw
# mode strings sampled from a live fetch of
# https://db.satnogs.org/api/transmitters/ (2026-08): USB, FMN, FM, CW,
# AFSK, FSK, BPSK, LSB, APT, HRPT, PSK, GMSK, DSB, SSTV, QPSK, DVB-S2, AM.
#
# Deliberately only maps the ones that correspond UNAMBIGUOUSLY to one of
# the radio's own real RF modes. Everything else here (AFSK/FSK/BPSK/
# PSK/QPSK/GMSK/DSB/SSTV/DVB-S2/APT/HRPT, and anything unrecognized) is a
# digital protocol/encoding layered on top of some actual RF carrier --
# which carrier varies satellite to satellite and isn't reliably
# inferable from the mode string alone (e.g. GMSK telemetry might ride
# on FM or a raw carrier depending on the bird). Guessing wrong here
# would silently mistune the radio for an actual reception/transmission
# attempt, which is worse than leaving mode alone -- callers should treat
# None as "tell the operator to set mode manually" rather than apply a
# guess.
_TRANSPONDER_MODE_TO_RADIO_MODE = {
    "FM": "FM",
    "FMN": "FM",  # narrow FM -- no separate narrow-FM value in this app's mode control
    "USB": "USB",
    "LSB": "LSB",
    "CW": "CW",
    "AM": "AM",
}


def radio_mode_for_transponder(transponder_mode):
    """Maps a transponder's stored mode string to one of this app's own
    confirmed-valid radio mode control values (see
    _TRANSPONDER_MODE_TO_RADIO_MODE's comment), or None if it isn't one
    of the unambiguous ones -- callers should leave the radio's mode
    alone and let the operator set it manually when this returns None,
    rather than guess."""
    if not transponder_mode:
        return None
    return _TRANSPONDER_MODE_TO_RADIO_MODE.get(transponder_mode.strip().upper())


class TransponderEditDialog(QDialog):
    """Shows/edits the full list of known transponders for one satellite
    -- populated from a SatNOGS fetch, hand-entered, or both. Picking
    which one to actually use when tuning is a later feature; this
    dialog is just for storing and correcting the data."""

    COLUMNS = ["Description", "Uplink (MHz)", "Downlink (MHz)", "Mode", "Uplink Mode", "Invert", "Active"]

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
        # Separate from "Mode" (the downlink mode) since some linear
        # transponders invert sidebands between uplink and downlink
        # (e.g. LSB up/USB down or vice versa) -- see fetch_transponders'
        # docstring. Left blank for e.g. FM transponders, where uplink
        # and downlink mode are the same anyway.
        self.table.setItem(row, 4, QTableWidgetItem(transponder.get("uplink_mode", "")))
        # Whether this transponder swaps sideband between uplink and
        # downlink (e.g. AO-7's Mode U/V, RS-44's Mode V/u) -- see
        # apply_satellite_mode in main_window.py, which flips Main's
        # USB/LSB uplink mode based on this flag. Populated automatically
        # by fetch_transponders() for SatNOGS-sourced entries; editable
        # here for hand-entered ones or to correct SatNOGS data.
        invert_item = QTableWidgetItem()
        invert_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        invert_item.setCheckState(Qt.Checked if transponder.get("invert") else Qt.Unchecked)
        self.table.setItem(row, 5, invert_item)
        active_item = QTableWidgetItem()
        active_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        active_item.setCheckState(Qt.Checked if transponder.get("alive", True) else Qt.Unchecked)
        self.table.setItem(row, 6, active_item)

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
            uplink_mode = self.table.item(row, 4).text().strip()
            if not (description or uplink or downlink or mode or uplink_mode):
                continue  # skip fully-blank rows
            transponders.append({
                "description": description,
                "uplink_mhz": uplink,
                "downlink_mhz": downlink,
                "mode": mode,
                "uplink_mode": uplink_mode,
                "invert": self.table.item(row, 5).checkState() == Qt.Checked,
                "alive": self.table.item(row, 6).checkState() == Qt.Checked,
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

    def __init__(self, satellites, parent=None, on_change=None,
                 observer_lat=None, observer_lon=None, observer_elevation_km=0.0):
        super().__init__(parent)
        self.setWindowTitle("Satellite Tracking")
        self.resize(560, 400)
        self._satellites = [dict(sat) for sat in satellites]  # local working copy until OK is pressed
        self._on_change = on_change
        # Threaded through to SatelliteInfoDialog (right-click ->
        # Satellite Info...) so its Upcoming Passes section can compute
        # real pass predictions without needing its own copy of this.
        self._observer_lat = observer_lat
        self._observer_lon = observer_lon
        self._observer_elevation_km = observer_elevation_km

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Show", "Name"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
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

    def _on_table_context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self._satellites):
            return
        sat = self._satellites[row]
        menu = QMenu(self)
        info_action = menu.addAction("Satellite Info...")
        path_action = menu.addAction("Show Orbit Path")
        path_action.setCheckable(True)
        path_action.setChecked(bool(sat.get("show_path")))
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is info_action:
            SatelliteInfoDialog(
                sat, self,
                observer_lat=self._observer_lat, observer_lon=self._observer_lon,
                observer_elevation_km=self._observer_elevation_km,
            ).exec()
        elif chosen is path_action:
            # Applies immediately (same as fetch/edit transponders
            # above) rather than waiting for OK -- it's a map display
            # toggle with an instantly visible effect, not a form field.
            sat["show_path"] = path_action.isChecked()
            if self._on_change:
                self._on_change(self._satellites)

    def _on_accept(self):
        for row, sat in enumerate(self._satellites):
            sat["selected"] = self.table.item(row, 0).checkState() == Qt.Checked
        self.accept()

    def result_satellites(self):
        return self._satellites


class SatelliteInfoDialog(QDialog):
    """Read-only view of everything this app has stored/knows about one
    satellite -- right-click it (on the map, in the upcoming-passes
    table, or in the Satellite Tracking management dialog's list) to
    open this. Purely informational, no OK/Cancel distinction -- just
    Close."""

    UPCOMING_PASSES_COUNT = 10

    def __init__(self, satellite, parent=None, observer_lat=None, observer_lon=None, observer_elevation_km=0.0):
        super().__init__(parent)
        name = satellite.get("name", "?")
        self.setWindowTitle(f"Satellite Info -- {name}")
        self.resize(600, 560)

        line1 = satellite.get("line1", "")
        line2 = satellite.get("line2", "")
        norad_id = norad_id_from_tle_line1(line1)
        epoch = tle_epoch_datetime(line1)

        form = QFormLayout()
        form.addRow("Name:", QLabel(name))
        form.addRow("NORAD Catalog #:", QLabel(str(norad_id) if norad_id is not None else "Unknown"))
        if epoch is not None:
            age = datetime.datetime.now(datetime.timezone.utc) - epoch
            age_text = f"{age.days}d {age.seconds // 3600}h old" if age.total_seconds() >= 0 else "in the future?"
            form.addRow("TLE Epoch:", QLabel(f"{epoch.strftime('%Y-%m-%d %H:%M:%S')} UTC ({age_text})"))
        else:
            form.addRow("TLE Epoch:", QLabel("Unknown (unparseable TLE)"))
        form.addRow("Shown on Map:", QLabel("Yes" if satellite.get("selected") else "No"))

        line1_input = QLineEdit(line1)
        line1_input.setReadOnly(True)
        line2_input = QLineEdit(line2)
        line2_input.setReadOnly(True)
        form.addRow("TLE Line 1:", line1_input)
        form.addRow("TLE Line 2:", line2_input)

        transponders = satellite.get("transponders", [])
        columns = ["Description", "Downlink (MHz)", "Uplink (MHz)", "Mode", "Uplink Mode", "Inverting", "Active"]
        table = QTableWidget(len(transponders), len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for row, transponder in enumerate(transponders):
            table.setItem(row, 0, QTableWidgetItem(transponder.get("description", "")))
            table.setItem(row, 1, QTableWidgetItem(transponder.get("downlink_mhz", "")))
            table.setItem(row, 2, QTableWidgetItem(transponder.get("uplink_mhz", "")))
            table.setItem(row, 3, QTableWidgetItem(transponder.get("mode", "")))
            table.setItem(row, 4, QTableWidgetItem(transponder.get("uplink_mode", "")))
            table.setItem(row, 5, QTableWidgetItem("Yes" if transponder.get("invert") else "No"))
            table.setItem(row, 6, QTableWidgetItem("Yes" if transponder.get("alive", True) else "No"))

        # (0, 0) is what an unset observer location defaults to elsewhere
        # in this app (nobody's actual station is at 0N 0E) -- same
        # "treat as not set" convention as ham_dashboard.py's own
        # _on_satellite_double_clicked.
        passes_columns = ["Start (UTC)", "Max El", "Duration", "Status"]
        passes_table = QTableWidget(0, len(passes_columns))
        passes_table.setHorizontalHeaderLabels(passes_columns)
        passes_table.setEditTriggers(QTableWidget.NoEditTriggers)
        passes_table.setSelectionMode(QTableWidget.NoSelection)
        passes_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        if not observer_lat and not observer_lon:
            passes_label_text = "Upcoming Passes: set your location (Connect New Radio dialog) to see these."
        elif not SGP4_AVAILABLE:
            passes_label_text = "Upcoming Passes: needs the 'sgp4' package for orbital propagation, which isn't installed."
        else:
            upcoming = find_passes(
                line1, line2, datetime.datetime.now(datetime.timezone.utc),
                observer_lat, observer_lon, observer_elevation_km,
                max_passes=self.UPCOMING_PASSES_COUNT,
            )
            passes_label_text = (
                f"Upcoming Passes (next {len(upcoming)}):" if upcoming
                else "Upcoming Passes: none found in the next 7 days (invalid TLE, or this satellite doesn't rise here)."
            )
            passes_table.setRowCount(len(upcoming))
            for row, pass_info in enumerate(upcoming):
                start_item = QTableWidgetItem(pass_info["aos_time"].strftime("%Y-%m-%d %H:%M:%S"))
                el_item = QTableWidgetItem(f"{pass_info['max_elevation_deg']:.0f}°")
                el_item.setTextAlignment(Qt.AlignCenter)
                duration_item = QTableWidgetItem(format_countdown(pass_info["duration_seconds"]))
                duration_item.setTextAlignment(Qt.AlignCenter)
                status_item = QTableWidgetItem("IN PROGRESS" if pass_info.get("active") else "Upcoming")
                status_item.setTextAlignment(Qt.AlignCenter)
                passes_table.setItem(row, 0, start_item)
                passes_table.setItem(row, 1, el_item)
                passes_table.setItem(row, 2, duration_item)
                passes_table.setItem(row, 3, status_item)

        button_box = QDialogButtonBox(QDialogButtonBox.Close)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(QLabel(
            "Transponders:" if transponders
            else "No transponder data stored -- fetch from SatNOGS or add it in Edit Transponders."
        ))
        layout.addWidget(table)
        layout.addWidget(QLabel(passes_label_text))
        layout.addWidget(passes_table)
        layout.addWidget(button_box)
        self.setLayout(layout)
