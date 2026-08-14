"""
Satellite tracking for the Ham Dashboard: real orbital propagation via
the sgp4 library (NORAD's standard SGP4/SDP4 algorithm -- not
reimplemented here, same reasoning as not hand-rolling FT8 decoding),
TLE data from CelesTrak, transponder data from SatNOGS DB, and the
dialogs for managing tracked satellites (refresh TLEs, add/remove,
choose which transponder to use, pick which satellites display).
"""

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
        return json.loads(SATELLITE_DATA_PATH.read_text())
    except Exception as exc:
        print(f"[ERROR] Ham Dashboard: couldn't read stored satellite data ({exc}); starting with an empty list.")
        return []


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
    if not SGP4_AVAILABLE:
        return None
    try:
        sat = Satrec.twoline2rv(line1, line2)
        jd, fr = jday(
            dt_utc.year, dt_utc.month, dt_utc.day,
            dt_utc.hour, dt_utc.minute, dt_utc.second + dt_utc.microsecond / 1e6,
        )
        error_code, position, _velocity = sat.sgp4(jd, fr)
        if error_code != 0:
            return None
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
    except Exception:
        return None


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
# integer Hz (not MHz), mode is a plain string, alive is a bool, and the
# API supports filtering by norad_cat_id as a query parameter. Reads are
# keyless -- "API access is open to anyone" per SatNOGS' own docs; a key
# is only needed for write operations, which this app never does.
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
    url = f"{SATNOGS_TRANSMITTERS_URL}?norad_cat_id={norad_cat_id}&format=json"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "IcomRadioControlApp/1.0 (desktop ham radio control application)"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))

    results = []
    for entry in data:
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


class TransponderChoiceDialog(QDialog):
    """Shown when SatNOGS DB has multiple known transmitters for a
    satellite -- lets the user pick which one to use to fill in the
    uplink/downlink/mode fields (e.g. a satellite's FM voice repeater
    vs. its telemetry beacon)."""

    def __init__(self, satellite_name, transponders, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Choose Transponder -- {satellite_name}")
        self.resize(460, 300)
        self._transponders = transponders

        self.list_widget = QTableWidget(len(transponders), 4)
        self.list_widget.setHorizontalHeaderLabels(["Description", "Uplink (MHz)", "Downlink (MHz)", "Mode"])
        self.list_widget.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.list_widget.setEditTriggers(QTableWidget.NoEditTriggers)
        self.list_widget.setSelectionBehavior(QTableWidget.SelectRows)
        for row, transponder in enumerate(transponders):
            description = transponder["description"] + ("" if transponder["alive"] else " (inactive)")
            self.list_widget.setItem(row, 0, QTableWidgetItem(description))
            self.list_widget.setItem(row, 1, QTableWidgetItem(transponder["uplink_mhz"]))
            self.list_widget.setItem(row, 2, QTableWidgetItem(transponder["downlink_mhz"]))
            self.list_widget.setItem(row, 3, QTableWidgetItem(transponder["mode"]))
        self.list_widget.selectRow(0)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("SatNOGS DB found multiple transmitters for this satellite:"))
        layout.addWidget(self.list_widget)
        layout.addWidget(button_box)
        self.setLayout(layout)

    def chosen_transponder(self):
        row = self.list_widget.currentRow()
        if 0 <= row < len(self._transponders):
            return self._transponders[row]
        return None


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
            "uplink_mhz": "", "downlink_mhz": "", "mode": "", "selected": True,
        }
        self.accept()

    def result_satellite(self):
        return self._result


class SatelliteConfigDialog(QDialog):
    """Right-click the Satellites button to open this: manage tracked
    satellites -- refresh TLEs from CelesTrak, add/remove satellites,
    edit transponder info, and choose which ones display on the map."""

    def __init__(self, satellites, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Satellite Tracking")
        self.resize(560, 400)
        self._satellites = [dict(sat) for sat in satellites]  # local working copy until OK is pressed

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Show", "Name", "Uplink (MHz)", "Downlink (MHz)", "Mode"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._rebuild_table()

        self.refresh_button = QPushButton("Refresh TLEs from CelesTrak")
        self.refresh_button.clicked.connect(self._on_refresh_tles)
        self.fetch_transponder_button = QPushButton("Fetch Transponder Data (SatNOGS)")
        self.fetch_transponder_button.setToolTip(
            "Looks up known transmitters for the selected satellite(s) in "
            "SatNOGS DB (an open, community-maintained database) by NORAD "
            "catalog number."
        )
        self.fetch_transponder_button.clicked.connect(self._on_fetch_transponders)
        self.add_button = QPushButton("Add Satellite...")
        self.add_button.clicked.connect(self._on_add_satellite)
        self.remove_button = QPushButton("Remove Selected")
        self.remove_button.clicked.connect(self._on_remove_satellite)

        button_row = QHBoxLayout()
        button_row.addWidget(self.refresh_button)
        button_row.addWidget(self.fetch_transponder_button)
        button_row.addWidget(self.add_button)
        button_row.addWidget(self.remove_button)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(QLabel(
            "Check satellites to display on the map. Select one or more rows "
            "and click \"Fetch Transponder Data\" to look up uplink/downlink/"
            "mode from SatNOGS DB, or enter/edit it directly in the table."
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

            self.table.setItem(row, 2, QTableWidgetItem(sat.get("uplink_mhz", "")))
            self.table.setItem(row, 3, QTableWidgetItem(sat.get("downlink_mhz", "")))
            self.table.setItem(row, 4, QTableWidgetItem(sat.get("mode", "")))

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
                    "uplink_mhz": "", "downlink_mhz": "", "mode": "",
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
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.information(
                self, "Fetch Transponder Data", "Select one or more satellites in the table first."
            )
            return
        for row in rows:
            sat = self._satellites[row]
            norad_id = norad_id_from_tle_line1(sat.get("line1", ""))
            if norad_id is None:
                QMessageBox.warning(
                    self, "Fetch Transponder Data",
                    f"Couldn't determine a NORAD catalog number for {sat.get('name', '?')} from its TLE."
                )
                continue
            try:
                transponders = fetch_transponders(norad_id)
            except Exception as exc:
                QMessageBox.critical(
                    self, "Fetch Transponder Data",
                    f"Couldn't fetch data for {sat.get('name', '?')} (NORAD {norad_id}):\n{exc}"
                )
                continue
            if not transponders:
                QMessageBox.information(
                    self, "Fetch Transponder Data",
                    f"SatNOGS DB has no transmitters listed for {sat.get('name', '?')} (NORAD {norad_id})."
                )
                continue
            if len(transponders) == 1:
                chosen = transponders[0]
            else:
                chooser = TransponderChoiceDialog(sat.get("name", "?"), transponders, self)
                if chooser.exec() != QDialog.Accepted:
                    continue
                chosen = chooser.chosen_transponder()
                if chosen is None:
                    continue
            sat["uplink_mhz"] = chosen["uplink_mhz"]
            sat["downlink_mhz"] = chosen["downlink_mhz"]
            sat["mode"] = chosen["mode"]
        self._rebuild_table()

    def _on_accept(self):
        # Pull edited transponder text + checked state back out of the
        # table before closing.
        for row, sat in enumerate(self._satellites):
            sat["selected"] = self.table.item(row, 0).checkState() == Qt.Checked
            sat["uplink_mhz"] = self.table.item(row, 2).text()
            sat["downlink_mhz"] = self.table.item(row, 3).text()
            sat["mode"] = self.table.item(row, 4).text()
        self.accept()

    def result_satellites(self):
        return self._satellites
