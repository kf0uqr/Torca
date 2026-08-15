"""
HamClockWindow: the Ham Dashboard window itself, tying together the
day/night world map, live clocks, solar-terrestrial data, HF band
conditions, and satellite tracking into one window. Opened via the "Ham
Dashboard" button in the main radio window -- doesn't need a radio
connection. The operator's location (used for Doppler correction, and
shown as a marker on the map) is set in ConnectionDialog, not here.

Double-clicking a tracked satellite on the map doesn't drive the radio
from here -- it just emits satellite_selected and leaves Doppler
correction, transponder choice, and the live tracking overlay to
RadioWindow (main_window.py) so tracking keeps running and stays
switchable to another satellite without this window (or the whole app,
since a QDialog's exec() used to block it) becoming unusable.
"""

import datetime

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
)

from solar_data import SolarDataWorker, BAND_CONDITION_RANGES
from theme import position_on_screen_half
from world_map import WorldMapWidget
from satellite_tracking import (
    SatelliteConfigDialog,
    load_satellite_data,
    save_satellite_data,
    propagate_satellite,
    footprint_points,
    upcoming_passes,
    format_countdown,
    SGP4_AVAILABLE,
)

# Recomputing pass predictions (upcoming_passes) is real work -- SGP4
# propagation across a multi-day search window for every selected
# satellite -- unlike the map position update, which is one propagation
# call per selected satellite per tick. Confirmed live: ~1s for 10
# selected satellites, ~4s worst case with all 97 in this app's own
# amateur-satellite list selected at once. A 5-minute refresh keeps that
# cost rare and pass predictions don't meaningfully change minute to
# minute anyway; the *displayed* countdown to each pass still updates
# every second by simple subtraction from the cached absolute times
# (see _update_passes_countdowns), piggybacked on the existing clock
# timer rather than a separate one.
PASSES_REFRESH_INTERVAL_MS = 5 * 60 * 1000

class HamClockWindow(QWidget):
    """The Ham Dashboard window -- see the module comment above this
    section for what's in scope and why. observer_lat/observer_lon/
    observer_elevation_m come from ConnectionDialog."""

    satellite_selected = Signal(dict)  # emitted on a valid double-click; RadioWindow does the rest
    PASSES_DISPLAY_COUNT = 10

    def __init__(self, observer_lat=None, observer_lon=None,
                 observer_elevation_m=0.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ham Dashboard")
        # Right half of the screen -- RadioWindow (main.py) takes the left
        # half on launch, so the two sit side by side by default.
        position_on_screen_half(self, "right")
        self._observer_lat = observer_lat
        self._observer_lon = observer_lon
        self._observer_elevation_m = observer_elevation_m or 0.0

        self.map_widget = WorldMapWidget()
        self.map_widget.satellite_double_clicked.connect(self._on_satellite_double_clicked)

        self.utc_label = QLabel("--:--:-- UTC")
        self.utc_label.setStyleSheet("font-size: 20px; font-weight: bold;")
        self.local_label = QLabel("--:--:-- Local")
        self.local_label.setStyleSheet("font-size: 20px; font-weight: bold;")

        self.sfi_label = QLabel("SFI: --")
        self.ssn_label = QLabel("SSN: --")
        self.k_index_label = QLabel("K-index: --")
        for label in (self.sfi_label, self.ssn_label, self.k_index_label):
            label.setStyleSheet("font-size: 16px; font-weight: bold;")

        self.solar_updated_label = QLabel("Solar data: waiting for first update from NOAA...")
        self.solar_updated_label.setStyleSheet("color: #888; font-size: 11px;")

        # Band conditions: rows = the four standard combined band ranges,
        # columns = Day/Night. Colored per condition (green=Good,
        # yellow=Fair, red=Poor/Band Closed) for a quick-glance read,
        # matching the color conventions used elsewhere in this app.
        self.band_conditions_table = QTableWidget(len(BAND_CONDITION_RANGES), 2)
        self.band_conditions_table.setHorizontalHeaderLabels(["Day", "Night"])
        self.band_conditions_table.setVerticalHeaderLabels(BAND_CONDITION_RANGES)
        self.band_conditions_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.band_conditions_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.band_conditions_table.setSelectionMode(QTableWidget.NoSelection)
        self.band_conditions_table.setFixedHeight(
            self.band_conditions_table.horizontalHeader().height()
            + len(BAND_CONDITION_RANGES) * 28 + 4
        )
        for row in range(len(BAND_CONDITION_RANGES)):
            for col in range(2):
                item = QTableWidgetItem("--")
                item.setTextAlignment(Qt.AlignCenter)
                self.band_conditions_table.setItem(row, col, item)

        # Upcoming passes: the next PASSES_DISPLAY_COUNT AOS/LOS windows
        # across every satellite currently checked to display on the map
        # (same "selected" scope as the map itself), soonest first. Row
        # count grows/shrinks with however many passes were actually
        # found (never more than PASSES_DISPLAY_COUNT) rather than
        # padding out to a fixed 10 with blank rows.
        self.upcoming_passes_table = QTableWidget(0, 4)
        self.upcoming_passes_table.setHorizontalHeaderLabels(["Satellite", "Status", "Max El", "Duration"])
        self.upcoming_passes_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.upcoming_passes_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.upcoming_passes_table.setSelectionMode(QTableWidget.NoSelection)
        self.upcoming_passes_table.verticalHeader().setVisible(False)
        self.upcoming_passes_table.setFixedHeight(
            self.upcoming_passes_table.horizontalHeader().height()
            + self.PASSES_DISPLAY_COUNT * 24 + 4
        )
        self.upcoming_passes_table.setToolTip("Double-click a row to select that satellite, same as double-clicking it on the map.")
        self.upcoming_passes_table.cellDoubleClicked.connect(self._on_pass_row_double_clicked)

        self.satellites = load_satellite_data()
        self._upcoming_passes = []  # cached results from the last upcoming_passes() call

        self.satellite_button = QPushButton("Satellites: OFF")
        self.satellite_button.setCheckable(True)
        self.satellite_button.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.satellite_button.setToolTip(
            "Toggle satellite tracking on the map. Right-click to manage TLE "
            "data, transponder info, and which satellites are shown."
        )
        self.satellite_button.toggled.connect(self._on_satellite_toggled)
        self.satellite_button.setContextMenuPolicy(Qt.CustomContextMenu)
        self.satellite_button.customContextMenuRequested.connect(self._on_satellite_config_requested)

        clocks_row = QHBoxLayout()
        clocks_row.addWidget(self.utc_label)
        clocks_row.addWidget(self.local_label)
        clocks_row.addWidget(self.satellite_button)

        solar_row = QHBoxLayout()
        solar_row.addWidget(self.sfi_label)
        solar_row.addWidget(self.ssn_label)
        solar_row.addWidget(self.k_index_label)

        band_conditions_column = QVBoxLayout()
        band_conditions_column.addWidget(QLabel("HF Band Conditions:"))
        band_conditions_column.addWidget(self.band_conditions_table)

        upcoming_passes_column = QVBoxLayout()
        upcoming_passes_column.addWidget(QLabel("Upcoming Satellite Passes:"))
        upcoming_passes_column.addWidget(self.upcoming_passes_table)

        bottom_row = QHBoxLayout()
        bottom_row.addLayout(band_conditions_column)
        bottom_row.addLayout(upcoming_passes_column)

        layout = QVBoxLayout()
        # No stretch factor -- the map claims only what its own
        # (letterboxed, aspect-correct) heightForWidth calls for, rather
        # than greedily taking all extra vertical space in the window
        # (which is what a stretch factor of 1 here used to do, and the
        # only thing keeping the map from severely distorting once
        # HamClockWindow started occupying a tall/narrow half-screen
        # region). Leaves more room below for the clocks/solar/band-
        # conditions/passes rows -- and for whatever gets added there later.
        layout.addWidget(self.map_widget)
        layout.addLayout(clocks_row)
        layout.addLayout(solar_row)
        layout.addWidget(self.solar_updated_label)
        layout.addLayout(bottom_row)
        self.setLayout(layout)

        if self._observer_lat is not None and self._observer_lon is not None:
            self.map_widget.set_operator_location(
                self._observer_lat, self._observer_lon,
                f"{self._observer_lat:.3f}, {self._observer_lon:.3f}",
            )

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clocks)
        self._clock_timer.start(1000)
        self._update_clocks()

        # The terminator moves slowly enough that redrawing once a
        # minute is plenty -- no need to repaint every second.
        self._map_timer = QTimer(self)
        self._map_timer.timeout.connect(self.map_widget.update)
        self._map_timer.start(60_000)

        # Satellites move fast enough (LEO: ~7.8 km/s) that a much
        # shorter interval than the terminator's makes sense, without
        # being wasteful -- only actually runs while satellite mode is on.
        self._satellite_timer = QTimer(self)
        self._satellite_timer.timeout.connect(self._update_satellite_positions)

        # The actual pass search only reruns on this much coarser timer
        # (also only while satellite mode is on) -- see the module
        # comment on PASSES_REFRESH_INTERVAL_MS for why.
        self._passes_timer = QTimer(self)
        self._passes_timer.timeout.connect(self._refresh_upcoming_passes)

        self.solar_worker = SolarDataWorker()
        self.solar_worker.data_updated.connect(self._on_solar_data)
        self.solar_worker.start()

    def _update_clocks(self):
        self.utc_label.setText(datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S UTC"))
        self.local_label.setText(datetime.datetime.now().strftime("%H:%M:%S Local"))
        self._update_passes_countdowns()  # cheap (no orbital math) -- piggybacks on this 1s timer

    def _on_satellite_toggled(self, checked):
        if checked and not SGP4_AVAILABLE:
            QMessageBox.warning(
                self, "Satellite Tracking",
                "Satellite tracking needs the 'sgp4' package for orbital "
                "propagation, which isn't installed.\n\n"
                "Run: pip install sgp4 --break-system-packages"
            )
            self.satellite_button.setChecked(False)
            return
        self.satellite_button.setText("Satellites: ON" if checked else "Satellites: OFF")
        self.map_widget.set_satellite_mode(checked)
        if checked:
            self._update_satellite_positions()
            self._satellite_timer.start(5000)
            self._refresh_upcoming_passes()
            self._passes_timer.start(PASSES_REFRESH_INTERVAL_MS)
        else:
            self._satellite_timer.stop()
            self._passes_timer.stop()

    def _on_satellite_config_requested(self, _pos):
        def persist(satellites):
            self.satellites = satellites
            save_satellite_data(self.satellites)

        # persist() also runs immediately on every transponder fetch/edit
        # inside the dialog (see SatelliteConfigDialog's on_change) -- so
        # that data survives even if the dialog gets closed/cancelled
        # afterward instead of OK'd.
        dialog = SatelliteConfigDialog(self.satellites, self, on_change=persist)
        if dialog.exec() == QDialog.Accepted:
            persist(dialog.result_satellites())
            if self.satellite_button.isChecked():
                self._update_satellite_positions()
                self._refresh_upcoming_passes()  # the selected/TLE list may have changed

    def _update_satellite_positions(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        positions = []
        for sat in self.satellites:
            if not sat.get("selected"):
                continue
            result = propagate_satellite(sat.get("line1", ""), sat.get("line2", ""), now)
            if result is None:
                continue
            lat, lon, altitude_km = result
            positions.append({
                "name": sat.get("name", "?"),
                "lat": lat,
                "lon": lon,
                "altitude_km": altitude_km,
                "footprint": footprint_points(lat, lon, altitude_km),
            })
        self.map_widget.set_satellite_positions(positions)

    def _refresh_upcoming_passes(self):
        """Reruns the actual pass search (upcoming_passes()) -- the
        expensive part; see PASSES_REFRESH_INTERVAL_MS. No-op (clears
        the table) without a configured location, same as Doppler
        correction -- there's no meaningful pass prediction without
        knowing where the observer is."""
        selected = [sat for sat in self.satellites if sat.get("selected")]
        if not selected or (not self._observer_lat and not self._observer_lon):
            self._upcoming_passes = []
        else:
            now = datetime.datetime.now(datetime.timezone.utc)
            observer_elevation_km = self._observer_elevation_m / 1000.0
            self._upcoming_passes = upcoming_passes(
                selected, now, self._observer_lat, self._observer_lon, observer_elevation_km,
                count=self.PASSES_DISPLAY_COUNT,
            )

        self.upcoming_passes_table.setRowCount(len(self._upcoming_passes))
        for row, pass_info in enumerate(self._upcoming_passes):
            name_item = QTableWidgetItem(pass_info["name"])
            self.upcoming_passes_table.setItem(row, 0, name_item)
            for col in (1, 2, 3):
                item = QTableWidgetItem("")
                item.setTextAlignment(Qt.AlignCenter)
                self.upcoming_passes_table.setItem(row, col, item)
            self.upcoming_passes_table.item(row, 2).setText(f"{pass_info['max_elevation_deg']:.0f}°")
            self.upcoming_passes_table.item(row, 3).setText(format_countdown(pass_info["duration_seconds"]))
        self._update_passes_countdowns()

    def _update_passes_countdowns(self):
        """Refreshes just the "Status" column's text from the cached
        self._upcoming_passes -- plain subtraction against already-known
        absolute times, no orbital math, cheap enough to run on the
        1-second clock timer even though the underlying search only
        reruns every few minutes. Derived from a live now-vs-aos/los
        comparison rather than the "active" flag set when the pass was
        found, so a pass that starts (or ends) between full searches is
        reflected correctly here too, not just at the next 5-minute
        refresh."""
        if not self._upcoming_passes:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        for row, pass_info in enumerate(self._upcoming_passes):
            if now < pass_info["aos_time"]:
                text = f"in {format_countdown((pass_info['aos_time'] - now).total_seconds())}"
            elif now <= pass_info["los_time"]:
                text = "ACTIVE"
            else:
                text = "passed"  # stale -- _refresh_upcoming_passes will drop it at the next full search
            item = self.upcoming_passes_table.item(row, 1)
            if item is not None:
                item.setText(text)

    def _on_pass_row_double_clicked(self, row, _column):
        """Double-clicking a row here is the same as double-clicking
        that satellite's marker on the map."""
        if row >= len(self._upcoming_passes):
            return
        self._on_satellite_double_clicked(self._upcoming_passes[row]["name"])

    def _on_satellite_double_clicked(self, name):
        satellite = next((sat for sat in self.satellites if sat.get("name") == name), None)
        if satellite is None:
            return
        # (0, 0) is what an unset lat/lon defaults to (nobody's actual
        # station is at 0N 0E), so treat it the same as "not set". Unlike
        # transponder data (see main_window.py's tracking overlay, which
        # handles "no transponders" gracefully -- elevation/azimuth/AOS-
        # LOS don't need one), there's no useful degraded mode without a
        # location at all.
        if not self._observer_lat and not self._observer_lon:
            QMessageBox.warning(
                self, "Satellite Tracking",
                "Set your location in the Connect dialog first (Latitude/"
                "Longitude/Elevation) -- satellite tracking needs to know "
                "where you're observing from. You'll need to restart the "
                "app to change it."
            )
            return
        # RadioWindow already independently has observer_lat/lon/elevation
        # (same source: ConnectionDialog's details) -- just the satellite
        # itself goes over the signal, not a location that could in
        # principle drift out of sync with RadioWindow's own copy.
        self.satellite_selected.emit(satellite)

    @Slot(dict)
    def _on_solar_data(self, data):
        if "k_index" in data:
            self.k_index_label.setText(f"K-index: {data['k_index']:.0f}")
        if "solar_flux" in data:
            self.sfi_label.setText(f"SFI: {data['solar_flux']:.0f}")
        if "sunspot_number" in data:
            self.ssn_label.setText(f"SSN: {data['sunspot_number']}")
        if "band_conditions" in data:
            self._update_band_conditions_table(data["band_conditions"])

        errors = {k: v for k, v in data.items() if k.endswith("_error")}
        if errors:
            # Full detail goes to the console -- easier to copy/paste from
            # there than out of a small status label -- while the label
            # itself just shows a short summary.
            for key, message in errors.items():
                print(f"[ERROR] Ham Dashboard: {key}: {message}")
            self.solar_updated_label.setText(
                f"Solar data: partial update, some values unavailable "
                f"({', '.join(errors)} -- see console for full detail)"
            )
        else:
            self.solar_updated_label.setText(
                f"Solar data: updated {datetime.datetime.now().strftime('%H:%M:%S')}"
            )

    _BAND_CONDITION_COLORS = {
        "good": QColor(40, 130, 60),
        "fair": QColor(160, 140, 30),
        "poor": QColor(140, 50, 50),
        "band closed": QColor(70, 70, 70),
    }

    def _update_band_conditions_table(self, conditions):
        for row, band_range in enumerate(BAND_CONDITION_RANGES):
            for col, time_of_day in enumerate(("day", "night")):
                value = conditions.get((band_range, time_of_day), "--")
                item = self.band_conditions_table.item(row, col)
                item.setText(value)
                color = self._BAND_CONDITION_COLORS.get(value.strip().lower())
                if color:
                    item.setBackground(color)
                    item.setForeground(QColor(255, 255, 255))

    def closeEvent(self, event):
        self._satellite_timer.stop()
        self._passes_timer.stop()
        self.solar_worker.requestInterruption()
        self.solar_worker.wait(2000)
        event.accept()
