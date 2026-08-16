"""
HamClockWindow: the app's central/main window -- the one thing that
exists independently of any radio connection, tying together the day/
night world map, live clocks, solar-terrestrial data, HF band
conditions, satellite tracking, and now (see below) radio connection
management, satellite Doppler control, Virtual Cables, rigctld, and
WSJT-X launching. Constructed directly by main.py; no radio connects at
startup any more -- radios are added from here ("Connect New Radio..."),
each assigned a satellite role (RADIO_ROLES in constants.py) in
ConnectionDialog, and get their own RadioWindow (main_window.py).

Satellite/transponder selection, the Doppler-tracking clock, and per-
role dispatch to however many radios are currently connected all live
in SatelliteSession (satellite_session.py), owned by this window --
the single shared source of truth multiple radios need to cooperate on
one pass. This window owns the UI for it (transponder combo, Start/
Stop Tracking, the tracking overlay); RadioWindow has none of that any
more, only role-dispatch logic that reacts to what SatelliteSession
tells it. Double-clicking a satellite on the map (or a row in the
upcoming-passes table) starts a session here directly -- no signal
hop out to some other window needed, since this DOES own tracking now.

Virtual Cables (its own dialog -- RX radio, TX radio, and RX Main/Sub/
Both mix, independently chosen, since a "poor man's full duplex" setup
may want to decode one radio's downlink while transmitting through a
different uplink radio) and rigctld (its own "which connected radio"
target picker) are also both here, rather than living on whichever
RadioWindow happened to have the button -- makes sense once more than
one radio can be connected. WSJT-X launching has no radio dependency
at all and moved here too, for the same "one central place" reasoning.
"""

import datetime
import os
import platform

from PySide6.QtCore import Qt, QTimer, QThread, Signal, Slot, QSettings
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QPushButton,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QListWidget,
    QListWidgetItem,
    QComboBox,
    QSpinBox,
    QFileDialog,
    QDialogButtonBox,
    QMenu,
    QRadioButton,
    QButtonGroup,
    QInputDialog,
    QLineEdit,
)

from solar_data import SolarDataWorker, BAND_CONDITION_RANGES
from theme import position_on_screen_half
from world_map import WorldMapWidget
from connection_dialog import ConnectionDialog
from main_window import RadioWindow
from satellite_session import SatelliteSession
from log_book_window import LogBookWindow
import adif
import qso_log
import pskreporter
import pota
from wsjtx_rigctld import RigctldServer, RIGCTLD_DEFAULT_PORT, find_wsjtx_executable, launch_wsjtx, WSJTX_RIG_NAME
from wsjtx_udp import WsjtxUdpListener, WSJTX_DEFAULT_PORT
from audio import (
    pactl_available,
    create_null_sink,
    unload_pactl_module,
    get_default_sink_name,
    get_default_source_name,
    set_default_sink,
    set_default_source,
    find_own_sink_input_ids,
    find_own_source_output_ids,
    move_sink_input,
    move_source_output,
    VIRTUAL_CABLE_RX_NAME,
    VIRTUAL_CABLE_RX_DESC,
    VIRTUAL_CABLE_TX_NAME,
    VIRTUAL_CABLE_TX_DESC,
)
from constants import RADIO_ROLES, HF_6M_BANDS, VHF_UHF_BANDS

_ROLE_LABELS = {value: label for label, value in RADIO_ROLES}  # "full_duplex" -> "Satellite Full Duplex", etc.
from satellite_tracking import (
    SatelliteConfigDialog,
    SatelliteInfoDialog,
    load_satellite_data,
    save_satellite_data,
    propagate_satellite,
    footprint_points,
    ground_track_points,
    upcoming_passes,
    format_countdown,
    SGP4_AVAILABLE,
)

class VirtualCableDialog(QDialog):
    """Lets the operator pick, independently, which connected radio's
    audio feeds the RX virtual cable (what an external app like WSJT-X
    hears) and which one the TX virtual cable drives (what it
    transmits through) -- not necessarily the same radio, since a "poor
    man's full duplex" setup might decode one radio's downlink while
    transmitting through a separate uplink radio. Also picks the RX
    mix (Main/Sub/Both) for whichever radio is chosen for RX.

    Start/Stop apply immediately (via the on_start/on_stop callbacks --
    actually calling HamClockWindow._apply_virtual_cable_config(), this
    dialog never touches audio/PulseAudio state itself) and the dialog
    stays open afterward, so the RX/TX/mix choice can be adjusted and
    re-applied without reopening it -- e.g. start with one radio,
    listen for a moment, then switch which radio feeds RX without
    losing the dialog. Close just dismisses it; it doesn't itself
    start or stop anything."""

    def __init__(self, connected_radios, current_rx_window, current_tx_window, current_channel,
                 on_start, on_stop, is_active, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Virtual Cables")
        self._on_start = on_start
        self._on_stop = on_stop

        self.rx_combo = QComboBox()
        self.rx_combo.addItem("None (no RX audio cable)", None)
        self.tx_combo = QComboBox()
        self.tx_combo.addItem("None (no TX audio cable)", None)
        for window in connected_radios:
            details = window._details
            role_label = _ROLE_LABELS.get(details.get("role", "non_sat"), details.get("role", "non_sat"))
            connection_label = details.get("host") or details.get("serial_port") or "?"
            label = f"{details['radio_model']} ({role_label}) -- {connection_label}"
            self.rx_combo.addItem(label, window)
            self.tx_combo.addItem(label, window)

        rx_index = self.rx_combo.findData(current_rx_window)
        if rx_index != -1:
            self.rx_combo.setCurrentIndex(rx_index)
        tx_index = self.tx_combo.findData(current_tx_window)
        if tx_index != -1:
            self.tx_combo.setCurrentIndex(tx_index)

        self.channel_combo = QComboBox()
        self.channel_combo.addItem("Both (Main + Sub mixed)", "mix")
        self.channel_combo.addItem("Main only", "main")
        self.channel_combo.addItem("Sub only", "sub")
        self.channel_combo.setToolTip(
            "Which receiver's audio goes to the RX cable -- only meaningful "
            "when the RX radio above is dual-receiver; harmless no-op "
            "otherwise."
        )
        channel_index = self.channel_combo.findData(current_channel)
        if channel_index != -1:
            self.channel_combo.setCurrentIndex(channel_index)

        form = QFormLayout()
        form.addRow("RX Radio (what you hear):", self.rx_combo)
        form.addRow("TX Radio (what you transmit through):", self.tx_combo)
        form.addRow("RX Audio Mix:", self.channel_combo)

        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: #aaa;")
        self._set_status(is_active, current_rx_window, current_tx_window)

        self.start_button = QPushButton("Start")
        self.start_button.setStyleSheet("QPushButton { background-color: #2a6; color: white; font-weight: bold; }")
        self.start_button.setToolTip("Applies the RX/TX/mix selection above immediately.")
        self.start_button.clicked.connect(self._on_start_clicked)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Disables the virtual cables (restores each radio's original audio devices).")
        self.stop_button.clicked.connect(self._on_stop_clicked)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)

        button_row = QHBoxLayout()
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.stop_button)
        button_row.addStretch()
        button_row.addWidget(close_button)

        layout = QVBoxLayout()
        layout.addWidget(QLabel(
            "Creates virtual audio devices (Linux/PulseAudio or PipeWire only) "
            "so an external app (WSJT-X, etc.) can send/receive audio through "
            "the radios below. RX and TX can be different radios -- useful "
            "for a separate uplink/downlink pair."
        ))
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addLayout(button_row)
        self.setLayout(layout)

    def _set_status(self, active, rx_window, tx_window):
        if not active:
            self.status_label.setText("Status: OFF")
            return
        parts = []
        if rx_window is not None:
            parts.append(f"RX: {rx_window._details['radio_model']}")
        if tx_window is not None:
            parts.append(f"TX: {tx_window._details['radio_model']}")
        self.status_label.setText(f"Status: ON ({', '.join(parts) or 'nothing selected'})")

    def _on_start_clicked(self):
        rx_window = self.rx_combo.currentData()
        tx_window = self.tx_combo.currentData()
        self._on_start(rx_window, tx_window, self.channel_combo.currentData())
        self._set_status(rx_window is not None or tx_window is not None, rx_window, tx_window)

    def _on_stop_clicked(self):
        self._on_stop()
        self._set_status(False, None, None)


class RigctldDialog(QDialog):
    """One row per connected radio -- rigctld is inherently one-radio-
    per-port, so a real multi-radio setup (e.g. WSJT-X reaching both an
    uplink and a downlink radio) needs a separate server, on a separate
    port, per radio, started/stopped independently. Rows for radios
    with no remembered port yet default to sequential free ports
    starting at RIGCTLD_DEFAULT_PORT (skipping any already claimed by a
    remembered or just-assigned port in this same dialog), so two newly
    -connected radios don't default to the same port -- the user can
    still freely retype either one, though; an actual collision (with
    another of our own servers or anything else) surfaces as a normal
    QTcpServer bind failure via on_start's return value. Never touches
    RigctldServer directly -- on_start(window, port) -> bool and
    on_stop(window) do the real work in HamClockWindow."""

    def __init__(self, connected_radios, running_servers, ports, on_start, on_stop, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rigctld")
        self._on_start = on_start
        self._on_stop = on_stop
        self._rows = {}  # RadioWindow -> (port_spinbox, toggle_button)

        form = QFormLayout()
        used_ports = set(ports.values())
        next_free_port = RIGCTLD_DEFAULT_PORT
        for window in connected_radios:
            details = window._details
            role_label = _ROLE_LABELS.get(details.get("role", "non_sat"), details.get("role", "non_sat"))
            connection_label = details.get("host") or details.get("serial_port") or "?"
            label = f"{details['radio_model']} ({role_label}) -- {connection_label}"

            if window in ports:
                default_port = ports[window]
            else:
                while next_free_port in used_ports:
                    next_free_port += 1
                default_port = next_free_port
                used_ports.add(default_port)

            port_spinbox = QSpinBox()
            port_spinbox.setRange(1, 65535)
            port_spinbox.setValue(default_port)
            port_spinbox.setToolTip("TCP port for this radio's rigctld server -- must differ from any other radio's.")

            is_running = window in running_servers
            toggle_button = QPushButton("Stop" if is_running else "Start")
            toggle_button.setCheckable(True)
            toggle_button.setChecked(is_running)
            toggle_button.setStyleSheet(
                "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
            )
            port_spinbox.setEnabled(not is_running)
            toggle_button.toggled.connect(lambda checked, w=window: self._on_row_toggled(w, checked))

            row = QHBoxLayout()
            row.addWidget(port_spinbox)
            row.addWidget(toggle_button)
            form.addRow(label, row)
            self._rows[window] = (port_spinbox, toggle_button)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)

        layout = QVBoxLayout()
        message = QLabel(
            "Each radio gets its own rigctld server (select \"Hamlib NET "
            "rigctl\", rig model 2, and 127.0.0.1:<port> in the external "
            "app) -- radios need different ports."
            if connected_radios else "No radios connected."
        )
        message.setWordWrap(True)
        layout.addWidget(message)
        layout.addLayout(form)
        layout.addWidget(close_button)
        self.setLayout(layout)

    def _on_row_toggled(self, window, checked):
        port_spinbox, toggle_button = self._rows[window]
        if checked:
            success = self._on_start(window, port_spinbox.value())
            if not success:
                toggle_button.blockSignals(True)
                toggle_button.setChecked(False)
                toggle_button.blockSignals(False)
                return
            toggle_button.setText("Stop")
            port_spinbox.setEnabled(False)
        else:
            self._on_stop(window)
            toggle_button.setText("Start")
            port_spinbox.setEnabled(True)


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
# (label, seconds) -- offered in PskReporterSettingsDialog's lookback
# combo. Capped at pskreporter.MAX_LOOKBACK_SECONDS (24h), which is
# PSKReporter's own documented hard limit on flowStartSeconds.
PSKREPORTER_LOOKBACK_OPTIONS = [
    ("Last 15 minutes", 15 * 60),
    ("Last 30 minutes", 30 * 60),
    ("Last hour", 60 * 60),
    ("Last 3 hours", 3 * 60 * 60),
    ("Last 6 hours", 6 * 60 * 60),
    ("Last 12 hours", 12 * 60 * 60),
    ("Last 24 hours", pskreporter.MAX_LOOKBACK_SECONDS),
]


class PskReporterWorker(QThread):
    """One-shot fetch off the GUI thread -- same shape as world_map.py's
    WorldMapImageFetcher (a single blocking urllib call, not the full
    persistent-connection RadioWorker machinery). Never runs on a
    timer -- only ever started by an explicit user action (toggling the
    PSKReporter button on, or changing its settings while already on) --
    PSKReporter's own usage guidance asks clients not to poll
    frequently."""

    spots_ready = Signal(list)
    failed = Signal(str)

    def __init__(self, callsign, direction, since_seconds, parent=None):
        super().__init__(parent)
        self._callsign = callsign
        self._direction = direction
        self._since_seconds = since_seconds

    def run(self):
        try:
            spots = pskreporter.fetch_pskreporter_spots(self._callsign, self._direction, self._since_seconds)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.spots_ready.emit(spots)


class PotaWorker(QThread):
    """One-shot fetch off the GUI thread, same shape as
    PskReporterWorker -- POTA's activator-spot list has no per-caller
    scoping (unlike PSKReporter's callsign-specific query), so there's
    nothing to configure here at all."""

    spots_ready = Signal(list)
    failed = Signal(str)

    def run(self):
        try:
            spots = pota.fetch_pota_spots()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.spots_ready.emit(spots)


class PskReporterSettingsDialog(QDialog):
    """Right-click the PSKReporter button to open this: your callsign
    (every PSKReporter query is scoped to just this one -- editable
    here in case the first-launch prompt was skipped, mistyped, or the
    operator wants to change it later), how far back to look, and
    which direction of reception reports to show -- stations that
    heard you, stations you're hearing, or both."""

    def __init__(self, callsign, since_seconds, direction, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PSKReporter Settings")

        self.callsign_edit = QLineEdit(callsign)
        self.callsign_edit.setPlaceholderText("e.g. KF0UQR")

        self.lookback_combo = QComboBox()
        for label, seconds in PSKREPORTER_LOOKBACK_OPTIONS:
            self.lookback_combo.addItem(label, seconds)
        index = self.lookback_combo.findData(since_seconds)
        self.lookback_combo.setCurrentIndex(index if index != -1 else 1)  # default: 30 minutes

        self.heard_you_radio = QRadioButton("Stations that heard you")
        self.hearing_radio = QRadioButton("Stations you are hearing")
        self.both_radio = QRadioButton("Both")
        self._direction_group = QButtonGroup(self)
        for button in (self.heard_you_radio, self.hearing_radio, self.both_radio):
            self._direction_group.addButton(button)
        {
            pskreporter.DIRECTION_HEARD_YOU: self.heard_you_radio,
            pskreporter.DIRECTION_HEARING: self.hearing_radio,
            "both": self.both_radio,
        }.get(direction, self.heard_you_radio).setChecked(True)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Your callsign:", self.callsign_edit)
        form.addRow("Time frame:", self.lookback_combo)
        form.addRow("Direction:", self.heard_you_radio)
        form.addRow("", self.hearing_radio)
        form.addRow("", self.both_radio)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(button_box)
        self.setLayout(layout)

    def result_callsign(self):
        return self.callsign_edit.text().strip().upper()

    def result_since_seconds(self):
        return self.lookback_combo.currentData()

    def result_direction(self):
        if self.hearing_radio.isChecked():
            return pskreporter.DIRECTION_HEARING
        if self.both_radio.isChecked():
            return "both"
        return pskreporter.DIRECTION_HEARD_YOU


class HamClockWindow(QWidget):
    """The Ham Dashboard window -- see the module comment above this
    section for what's in scope and why."""

    PASSES_DISPLAY_COUNT = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ham Dashboard")
        position_on_screen_half(self, "right")

        self._ensure_operator_callsign()

        # No radio connects at startup any more -- this window is the
        # first thing that opens, so observer location (for Doppler
        # correction and the map marker) comes straight from QSettings
        # (the same keys ConnectionDialog itself reads/writes) instead
        # of from a RadioWindow's connection details. Re-read after each
        # successful new-radio connection too (_on_connect_radio_clicked),
        # in case that dialog updated it.
        self._load_observer_location()

        self.satellite_session = SatelliteSession(self)
        self.satellite_session.set_observer_location(
            self._observer_lat, self._observer_lon, self._observer_elevation_m
        )
        self.satellite_session.state_updated.connect(self._on_satellite_state_updated)
        self.satellite_session.tracking_changed.connect(self._on_session_tracking_changed)

        self._connected_radios = []  # RadioWindow instances, in connection order
        self._active_satellite = None  # for populating the transponder combo -- SatelliteSession keeps its own copy for computation

        self.map_widget = WorldMapWidget()
        self.map_widget.satellite_double_clicked.connect(self._on_satellite_double_clicked)
        self.map_widget.satellite_right_clicked.connect(self._on_satellite_right_clicked)
        self.map_widget.satellite_left_clicked.connect(self._on_satellite_left_clicked)

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
        self.upcoming_passes_table.setToolTip(
            "Click a row to make it the active (highlighted) satellite on the map. "
            "Double-click to select it for tracking, same as double-clicking it on the map. "
            "Right-click for satellite info."
        )
        self.upcoming_passes_table.cellClicked.connect(self._on_pass_row_clicked)
        self.upcoming_passes_table.cellDoubleClicked.connect(self._on_pass_row_double_clicked)
        self.upcoming_passes_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.upcoming_passes_table.customContextMenuRequested.connect(self._on_passes_table_context_menu)

        self.satellites = load_satellite_data()
        self._upcoming_passes = []  # cached results from the last upcoming_passes() call
        # Which satellite is currently highlighted on the map (brighter
        # marker/footprint/path colors, and its ground track always shown
        # regardless of its own "Show Orbit Path" setting) -- purely
        # visual, set by left-clicking a satellite (map marker or a
        # passes-table row) or double-clicking one (which also starts
        # tracking, see _on_satellite_double_clicked). Distinct from
        # _active_satellite (the Doppler-tracked satellite) -- the two are
        # usually the same bird in practice but aren't the same concept.
        self._map_highlighted_satellite_name = None

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

        # ---- QSO map overlay ----
        # How many of the most recent logged QSOs to plot -- persisted
        # across restarts (same QSettings group/key convention as every
        # other saved preference in this app), same shape as the
        # satellite button: click toggles the overlay on/off, right-
        # click picks the count.
        settings = QSettings("IcomRadioApp", "RadioControl")
        _stored_qso_map_count = settings.value("qso_map_count", 25, type=int)
        self._qso_map_count = None if _stored_qso_map_count == -1 else _stored_qso_map_count
        self.qso_map_button = QPushButton("QSO Map: OFF")
        self.qso_map_button.setCheckable(True)
        self.qso_map_button.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.qso_map_button.setToolTip(
            "Toggle showing your most recent logged QSOs' locations on the "
            "map (from each QSO's Grid Square). Right-click to choose how many."
        )
        self.qso_map_button.toggled.connect(self._on_qso_map_toggled)
        self.qso_map_button.setContextMenuPolicy(Qt.CustomContextMenu)
        self.qso_map_button.customContextMenuRequested.connect(self._on_qso_map_menu_requested)

        # ---- PSKReporter map overlay ----
        self._pskreporter_since_seconds = settings.value(
            "pskreporter_lookback_seconds", PSKREPORTER_LOOKBACK_OPTIONS[1][1], type=int
        )
        self._pskreporter_direction = settings.value(
            "pskreporter_direction", pskreporter.DIRECTION_HEARD_YOU
        )
        self._pskreporter_spots_cache = []  # raw fetched spots (unfiltered by band) -- see _build_pskreporter_markers
        self._pskreporter_worker = None
        self.pskreporter_button = QPushButton("PSKReporter: OFF")
        self.pskreporter_button.setCheckable(True)
        self.pskreporter_button.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.pskreporter_button.setToolTip(
            "Toggle showing your PSKReporter spots on the map (only for "
            "your own callsign). Right-click to choose the time frame "
            "and direction."
        )
        self.pskreporter_button.toggled.connect(self._on_pskreporter_toggled)
        self.pskreporter_button.setContextMenuPolicy(Qt.CustomContextMenu)
        self.pskreporter_button.customContextMenuRequested.connect(self._on_pskreporter_menu_requested)

        # ---- POTA (Parks on the Air) map overlay ----
        self._pota_spots_cache = []  # raw fetched spots (unfiltered by band) -- see _build_pota_markers
        self._pota_worker = None
        self.pota_button = QPushButton("POTA: OFF")
        self.pota_button.setCheckable(True)
        self.pota_button.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.pota_button.setToolTip(
            "Toggle showing currently active POTA (Parks on the Air) "
            "activator spots on the map. Right-click to refresh -- spots "
            "go stale within their own short activation window."
        )
        self.pota_button.toggled.connect(self._on_pota_toggled)
        self.pota_button.setContextMenuPolicy(Qt.CustomContextMenu)
        self.pota_button.customContextMenuRequested.connect(self._on_pota_menu_requested)

        # Filters which band's QSOs the map overlay shows -- every band
        # this app knows about (same list/order as HF_6M_BANDS +
        # VHF_UHF_BANDS, already ADIF's own lowercase BAND convention --
        # see adif.band_for_freq_hz) plus "All Bands". Deliberately only
        # affects QSO markers -- the operator location and satellite
        # markers/footprints/paths are a different concept entirely (not
        # tied to any one band) and stay shown regardless.
        self.qso_band_filter_combo = QComboBox()
        self.qso_band_filter_combo.addItem("All Bands", None)
        for label, _low_hz, _high_hz in HF_6M_BANDS + VHF_UHF_BANDS:
            self.qso_band_filter_combo.addItem(label, label)
        self.qso_band_filter_combo.setToolTip("Only show QSOs on this band on the map.")
        _stored_band_filter = settings.value("qso_map_band_filter", "") or None
        if _stored_band_filter is not None:
            index = self.qso_band_filter_combo.findData(_stored_band_filter)
            if index != -1:
                self.qso_band_filter_combo.setCurrentIndex(index)
        self.qso_band_filter_combo.currentIndexChanged.connect(self._on_qso_band_filter_changed)

        # ---- Connected radios ----
        self.connect_radio_button = QPushButton("Connect New Radio...")
        self.connect_radio_button.setToolTip(
            "Opens the connection dialog for another radio -- pick its "
            "satellite role there (Full Duplex / Downlink / Uplink / "
            "Non-Sat). Each connected radio gets its own window."
        )
        self.connect_radio_button.clicked.connect(self._on_connect_radio_clicked)

        # Singleton, non-modal, tracked as self.log_book_window rather
        # than a list (unlike RadioWindow, only one is ever open) --
        # LogBookWindow.closeEvent hides rather than destroys it, so
        # this same instance is always safe to show()/raise_() again.
        self.log_book_window = None
        self.log_book_button = QPushButton("Log Book...")
        self.log_book_button.setToolTip("QSO log -- view/sort every contact, log a new one, edit and re-sync with QRZ.com.")
        self.log_book_button.clicked.connect(self._on_log_book_clicked)

        # Opens the same New QSO window LogBookWindow itself uses --
        # lazily builds that window (without showing it) if it doesn't
        # exist yet, so a QSO can be logged quickly without opening the
        # full log table first. See _on_new_qso_clicked.
        self.new_qso_button = QPushButton("New QSO...")
        self.new_qso_button.setToolTip("Log a new QSO -- auto-fills from a connected radio's live state if one is picked.")
        self.new_qso_button.clicked.connect(self._on_new_qso_clicked)

        self.connected_radios_list = QListWidget()
        self.connected_radios_list.setToolTip("Double-click to bring that radio's window to the front.")
        self.connected_radios_list.setFixedHeight(70)
        self.connected_radios_list.itemDoubleClicked.connect(self._on_connected_radio_item_double_clicked)

        connected_radios_header = QHBoxLayout()
        connected_radios_header.addWidget(QLabel("Connected Radios:"))
        connected_radios_header.addWidget(self.connect_radio_button)
        connected_radios_header.addWidget(self.log_book_button)
        connected_radios_header.addWidget(self.new_qso_button)
        connected_radios_header.addStretch()

        connected_radios_column = QVBoxLayout()
        connected_radios_column.addLayout(connected_radios_header)
        connected_radios_column.addWidget(self.connected_radios_list)

        # ---- Satellite tracking controls (moved from RadioWindow --
        # now the single shared control point for however many radios
        # are cooperating on one pass, driving SatelliteSession) ----
        self.satellite_name_label = QLabel("No satellite selected")
        self.transponder_combo = QComboBox()
        self.transponder_combo.setEnabled(False)
        self.transponder_combo.setToolTip(
            "Which of the selected satellite's stored transponders to "
            "Doppler-correct against."
        )
        self.transponder_combo.currentIndexChanged.connect(self._on_transponder_changed)
        # Toggle, not a one-way stop button -- pausing/resuming keeps the
        # satellite selected (only double-clicking a different one on
        # the map, or a different upcoming-pass row, replaces it).
        # Starts unchecked/disabled; _on_satellite_double_clicked enables
        # it and checks it (tracking starts immediately on select, same
        # as the pre-refactor per-radio-window behavior).
        self.tracking_button = QPushButton("Start Tracking")
        self.tracking_button.setCheckable(True)
        self.tracking_button.setEnabled(False)
        self.tracking_button.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.tracking_button.setToolTip(
            "Pause/resume Doppler re-tuning on every connected satellite-role "
            "radio. The satellite stays selected either way -- double-click a "
            "different one on the map (or an upcoming-pass row) to switch."
        )
        self.tracking_button.toggled.connect(self._on_tracking_toggled)

        tracking_row = QHBoxLayout()
        tracking_row.addWidget(self.satellite_name_label)
        tracking_row.addWidget(self.transponder_combo, 1)
        tracking_row.addWidget(self.tracking_button)

        # Live El/Az/Doppler/AOS-LOS text -- replaces what used to be
        # RadioWindow's own per-window overlay (one shared readout now,
        # not one per connected radio).
        self.satellite_overlay_label = QLabel("")
        self.satellite_overlay_label.setWordWrap(True)
        self.satellite_overlay_label.setStyleSheet("color: #ccc; font-size: 12px;")

        # ---- Virtual Cables (moved from RadioWindow -- opens a dialog
        # to pick RX radio / TX radio (independently -- doesn't have to
        # be the same one) / RX Main-Sub-Both mix, rather than living
        # inline here, now that there are two radio choices plus the
        # mix to make instead of just one target) ----
        self._virtual_cable_rx_window = None
        self._virtual_cable_tx_window = None
        self._virtual_cable_channel = "mix"
        self._virtual_cable_modules = None    # (rx_module_id, tx_module_id) while active
        self._virtual_cable_previous_sink = None
        self._virtual_cable_previous_source = None
        self._virtual_cable_move_timer = None
        self._virtual_cable_move_attempts = 0
        self._virtual_cable_sink_input_ids = []
        self._virtual_cable_source_output_ids = []

        self.virtual_cable_button = QPushButton("Virtual Cables: OFF")
        self.virtual_cable_button.setEnabled(False)
        self.virtual_cable_button.setToolTip(
            "Configure virtual audio devices (Linux/PulseAudio or PipeWire "
            "only) so an external app (WSJT-X, etc.) can send/receive audio "
            "through a connected radio's connection -- RX and TX can be "
            "different radios, e.g. decoding one radio's downlink while "
            "transmitting through a separate uplink radio."
        )
        self.virtual_cable_button.clicked.connect(self._on_virtual_cable_button_clicked)

        # ---- Rigctld (moved from RadioWindow -- opens a dialog with
        # one row per connected radio, each independently started/
        # stopped on its own port, since rigctld is inherently one-
        # radio-per-port and a real setup might want e.g. WSJT-X
        # reaching both an uplink and a downlink radio at once, which
        # needs two separate servers on two separate ports) ----
        self._rigctld_servers = {}  # RadioWindow -> running RigctldServer
        self._rigctld_ports = {}    # RadioWindow -> remembered port (kept even while stopped)

        self.rigctld_button = QPushButton("Rigctld: OFF")
        self.rigctld_button.setEnabled(False)
        self.rigctld_button.setToolTip(
            "Configure per-radio rigctld servers so other CAT-aware apps "
            "(WSJT-X, JTDX, fldigi, ...) can control them -- select \"Hamlib "
            "NET rigctl\" (rig model 2) and 127.0.0.1:<port> in that app. "
            "Each radio needs its own port."
        )
        self.rigctld_button.clicked.connect(self._on_rigctld_button_clicked)

        # ---- WSJT-X launch (moved from RadioWindow -- no radio
        # dependency at all, so this is just a relocation) ----
        self.wsjtx_button = QPushButton("Launch WSJT-X")
        self.wsjtx_button.setToolTip(
            f"Launches WSJT-X in its own isolated profile (--rig-name={WSJTX_RIG_NAME}), "
            "separate from your main WSJT-X settings. Use the Rigctld button below to "
            "let it control a radio."
        )
        self.wsjtx_button.clicked.connect(self._on_wsjtx_button_clicked)

        # ---- WSJT-X UDP auto-log ----
        # Listens for WSJT-X's own "Logged ADIF" UDP broadcast (sent
        # automatically the instant the operator clicks OK on WSJT-X's
        # "Log QSO" dialog -- see wsjtx_udp.py) and logs it into the
        # local QSO log the same way the New QSO dialog would, with no
        # manual entry at all. Independent of "Launch WSJT-X"/Rigctld
        # above -- works with ANY running WSJT-X instance (this app's
        # own isolated profile or the operator's regular one) as long
        # as its UDP Server port (Settings > Reporting) matches.
        self._wsjtx_udp_port = settings.value("wsjtx_udp_port", WSJTX_DEFAULT_PORT, type=int)
        self._wsjtx_udp_listener = None
        self.wsjtx_autolog_button = QPushButton("WSJT-X Auto-Log: OFF")
        self.wsjtx_autolog_button.setCheckable(True)
        self.wsjtx_autolog_button.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.wsjtx_autolog_button.setToolTip(
            "Automatically log every QSO the instant WSJT-X logs it (its "
            "own \"Log QSO\" dialog -- OK). Make sure WSJT-X's UDP Server "
            f"port (Settings > Reporting) matches this app's (default {WSJTX_DEFAULT_PORT}). "
            "Right-click to change the port."
        )
        self.wsjtx_autolog_button.toggled.connect(self._on_wsjtx_autolog_toggled)
        self.wsjtx_autolog_button.setContextMenuPolicy(Qt.CustomContextMenu)
        self.wsjtx_autolog_button.customContextMenuRequested.connect(self._on_wsjtx_autolog_menu_requested)

        external_apps_row = QHBoxLayout()
        external_apps_row.addWidget(self.wsjtx_button)
        external_apps_row.addWidget(self.rigctld_button)
        external_apps_row.addWidget(self.virtual_cable_button)
        external_apps_row.addWidget(self.wsjtx_autolog_button)
        external_apps_row.addStretch()

        clocks_row = QHBoxLayout()
        clocks_row.addWidget(self.utc_label)
        clocks_row.addWidget(self.local_label)
        clocks_row.addStretch()

        map_buttons_row = QHBoxLayout()
        map_buttons_row.addWidget(self.satellite_button)
        map_buttons_row.addWidget(self.qso_map_button)
        map_buttons_row.addWidget(self.pskreporter_button)
        map_buttons_row.addWidget(self.pota_button)
        map_buttons_row.addWidget(self.qso_band_filter_combo)
        map_buttons_row.addStretch()

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
        # clocks_row and map_buttons_row (satellite/QSO Map toggles, in
        # their own row per explicit instruction) both above the map.
        # Map itself still with no stretch factor -- it claims only what
        # its own (letterboxed, aspect-correct) heightForWidth calls for,
        # rather than greedily taking all extra vertical space in the
        # window (which is what a stretch factor of 1 here used to do,
        # and the only thing keeping the map from severely distorting
        # once HamClockWindow started occupying a tall/narrow half-
        # screen region). Leaves more room below for the solar/band-
        # conditions/passes rows -- and for whatever gets added there later.
        layout.addLayout(clocks_row)
        layout.addLayout(map_buttons_row)
        layout.addWidget(self.map_widget)
        layout.addLayout(connected_radios_column)
        layout.addLayout(tracking_row)
        layout.addWidget(self.satellite_overlay_label)
        layout.addLayout(external_apps_row)
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

    def _ensure_operator_callsign(self):
        """Prompts for the operator's own callsign immediately on first
        launch ever (per explicit instruction) -- PSKReporter queries
        are always scoped to "reports mentioning THIS callsign", so
        there's no meaningful PSKReporter map without one. Persisted
        via QSettings once given; if the operator cancels or leaves it
        blank, nothing is saved and this just asks again next launch
        (and _on_pskreporter_toggled re-prompts if they try to use the
        feature before then) -- never blocks startup or nags mid-
        session."""
        settings = QSettings("IcomRadioApp", "RadioControl")
        self._operator_callsign = settings.value("operator_callsign", "") or ""
        if self._operator_callsign:
            return
        text, ok = QInputDialog.getText(
            self, "Your Callsign",
            "Enter your callsign (used to look up your PSKReporter spots):"
        )
        callsign = text.strip().upper() if ok else ""
        if callsign:
            self._operator_callsign = callsign
            settings.setValue("operator_callsign", callsign)

    def _load_observer_location(self):
        """Reads observer_lat/lon/elevation from QSettings -- the same
        keys ConnectionDialog itself reads/writes on every accept -- so
        the map marker and Doppler correction have a location without
        needing a radio connected first. Called once at startup and
        again after each successful new-radio connection, in case that
        dialog just updated it."""
        settings = QSettings("IcomRadioApp", "RadioControl")
        self._observer_lat = float(settings.value("operator_lat", 0.0)) or None
        self._observer_lon = float(settings.value("operator_lon", 0.0)) or None
        self._observer_elevation_m = float(settings.value("operator_elevation_m", 0.0))

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
        dialog = SatelliteConfigDialog(
            self.satellites, self, on_change=persist,
            observer_lat=self._observer_lat, observer_lon=self._observer_lon,
            observer_elevation_km=self._observer_elevation_m / 1000.0,
        )
        if dialog.exec() == QDialog.Accepted:
            persist(dialog.result_satellites())
            if self.satellite_button.isChecked():
                self._update_satellite_positions()
                self._refresh_upcoming_passes()  # the selected/TLE list may have changed

    def _on_qso_map_toggled(self, checked):
        self.qso_map_button.setText("QSO Map: ON" if checked else "QSO Map: OFF")
        self.map_widget.set_qso_markers(self._build_qso_markers() if checked else [])

    def _on_qso_map_menu_requested(self, _pos):
        menu = QMenu(self)
        for count in (10, 25, 50, 100, None):  # None = "All"
            label = "All" if count is None else str(count)
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(count == self._qso_map_count)
            action.triggered.connect(lambda _checked=False, c=count: self._on_qso_map_count_selected(c))
        menu.exec(self.qso_map_button.mapToGlobal(_pos))

    def _on_qso_map_count_selected(self, count):
        self._qso_map_count = count
        QSettings("IcomRadioApp", "RadioControl").setValue(
            "qso_map_count", -1 if count is None else count
        )
        if self.qso_map_button.isChecked():
            self.map_widget.set_qso_markers(self._build_qso_markers())

    def _on_qso_band_filter_changed(self, _index):
        band = self.qso_band_filter_combo.currentData()
        QSettings("IcomRadioApp", "RadioControl").setValue("qso_map_band_filter", band or "")
        if self.qso_map_button.isChecked():
            self.map_widget.set_qso_markers(self._build_qso_markers())
        # Per explicit instruction, the band filter also applies to
        # PSKReporter/POTA spots -- re-filters the already-fetched
        # cache (no new network fetch needed, same reasoning as QSO
        # markers).
        if self.pskreporter_button.isChecked():
            self.map_widget.set_pskreporter_markers(self._build_pskreporter_markers())
        if self.pota_button.isChecked():
            self.map_widget.set_pota_markers(self._build_pota_markers())

    def _build_qso_markers(self):
        """Newest self._qso_map_count logged QSOs (None = all) that have
        a parseable Grid Square -- same newest-first sort key as
        log_book_window.py's default. QSOs with no/invalid grid square
        are silently skipped (nothing to plot them at) rather than
        guessed.

        Filtered to the band picked in qso_band_filter_combo (None =
        "All Bands", no filtering) BEFORE slicing to _qso_map_count --
        so e.g. picking "20m" and a count of 50 shows the 50 most
        recent 20m QSOs specifically, not the 50 most recent QSOs
        overall with only a handful happening to be 20m."""
        band_filter = self.qso_band_filter_combo.currentData()
        qsos = qso_log.load_qso_log()
        if band_filter is not None:
            qsos = [qso for qso in qsos if qso.get("BAND") == band_filter]
        qsos.sort(key=lambda qso: (qso.get("QSO_DATE", ""), qso.get("TIME_ON", "")), reverse=True)
        if self._qso_map_count is not None:
            qsos = qsos[:self._qso_map_count]
        markers = []
        for qso in qsos:
            latlon = adif.grid_square_to_latlon(qso.get("GRIDSQUARE"))
            if latlon is None:
                continue
            lat, lon = latlon
            date = qso.get("QSO_DATE", "")
            time_on = qso.get("TIME_ON", "")
            if len(date) == 8 and len(time_on) >= 4:
                time_label = f"{date[0:4]}-{date[4:6]}-{date[6:8]} {time_on[0:2]}:{time_on[2:4]} UTC"
            else:
                time_label = time_on or date or "?"
            markers.append({
                "lat": lat, "lon": lon,
                "band": qso.get("BAND") or "?",
                "time": time_label,
                "callsign": qso.get("CALL") or "?",
            })
        return markers

    def _on_pskreporter_toggled(self, checked):
        if checked and not self._operator_callsign:
            # Callsign wasn't given (or was skipped) at first launch --
            # ask again now, since the feature is unusable without one.
            self._ensure_operator_callsign()
            if not self._operator_callsign:
                self.pskreporter_button.setChecked(False)
                return
        self.pskreporter_button.setText("PSKReporter: ON" if checked else "PSKReporter: OFF")
        if checked:
            self._start_pskreporter_fetch()
        else:
            self.map_widget.set_pskreporter_markers([])

    def _on_pskreporter_menu_requested(self, _pos):
        dialog = PskReporterSettingsDialog(
            self._operator_callsign, self._pskreporter_since_seconds, self._pskreporter_direction, self
        )
        if dialog.exec() == QDialog.Accepted:
            self._operator_callsign = dialog.result_callsign()
            self._pskreporter_since_seconds = dialog.result_since_seconds()
            self._pskreporter_direction = dialog.result_direction()
            settings = QSettings("IcomRadioApp", "RadioControl")
            settings.setValue("operator_callsign", self._operator_callsign)
            settings.setValue("pskreporter_lookback_seconds", self._pskreporter_since_seconds)
            settings.setValue("pskreporter_direction", self._pskreporter_direction)
            if self.pskreporter_button.isChecked():
                if self._operator_callsign:
                    self._start_pskreporter_fetch()
                else:
                    # Callsign was cleared -- nothing meaningful to show.
                    # setChecked(False) itself fires _on_pskreporter_toggled,
                    # which clears the map's markers -- no need to also do
                    # it here.
                    self.pskreporter_button.setChecked(False)

    def _start_pskreporter_fetch(self):
        if self._pskreporter_worker is not None and self._pskreporter_worker.isRunning():
            return  # a fetch is already in flight -- let it finish rather than piling up requests
        worker = PskReporterWorker(
            self._operator_callsign, self._pskreporter_direction, self._pskreporter_since_seconds, self
        )
        worker.spots_ready.connect(self._on_pskreporter_spots_ready)
        worker.failed.connect(self._on_pskreporter_fetch_failed)
        self._pskreporter_worker = worker
        worker.start()

    def _on_pskreporter_spots_ready(self, spots):
        self._pskreporter_spots_cache = spots
        if self.pskreporter_button.isChecked():
            self.map_widget.set_pskreporter_markers(self._build_pskreporter_markers())

    def _on_pskreporter_fetch_failed(self, message):
        QMessageBox.warning(self, "PSKReporter", f"Couldn't fetch PSKReporter spots:\n{message}")
        self.pskreporter_button.setChecked(False)

    def _build_pskreporter_markers(self):
        """Filters self._pskreporter_spots_cache (the last fetch's raw
        results, unfiltered by band) to the band picked in
        qso_band_filter_combo -- per explicit instruction, the same
        band filter that applies to QSO markers also applies here, and
        this re-filters the already-fetched cache rather than
        re-fetching, same reasoning as _build_qso_markers. Spots
        without a parseable locator are skipped (nothing to plot them
        at)."""
        band_filter = self.qso_band_filter_combo.currentData()
        spots = self._pskreporter_spots_cache
        if band_filter is not None:
            spots = [spot for spot in spots if spot.get("band") == band_filter]
        markers = []
        for spot in spots:
            latlon = adif.grid_square_to_latlon(spot.get("locator"))
            if latlon is None:
                continue
            lat, lon = latlon
            markers.append({"lat": lat, "lon": lon, "tooltip": self._pskreporter_tooltip_text(spot)})
        return markers

    @staticmethod
    def _pskreporter_tooltip_text(spot):
        """Everything PSKReporter reported for this spot, per explicit
        instruction ("all of the pskreporter data for that spot")."""
        direction = spot.get("direction")
        lines = [
            spot.get("callsign", "?"),
            {
                pskreporter.DIRECTION_HEARD_YOU: "Heard you",
                pskreporter.DIRECTION_HEARING: "You heard them",
            }.get(direction, direction or "?"),
            f"Band: {spot.get('band', '?')}",
            f"Mode: {spot.get('mode', '?')}",
        ]
        if spot.get("snr") is not None:
            lines.append(f"SNR: {spot['snr']} dB")
        flow_start = spot.get("flow_start_seconds")
        if flow_start:
            dt = datetime.datetime.fromtimestamp(flow_start, tz=datetime.timezone.utc)
            lines.append(f"Time: {dt.strftime('%Y-%m-%d %H:%M')} UTC")
        # senderRegion/senderDXCC describe whoever transmitted -- for a
        # "hearing" spot, that's the OTHER station; for "heard_you",
        # it's YOU, so receiverRegion/receiverDXCC (the other station,
        # who received you) is the correct side to show instead.
        raw = spot.get("raw") or {}
        if direction == pskreporter.DIRECTION_HEARING:
            region, country = raw.get("senderRegion"), raw.get("senderDXCC")
        else:
            region, country = raw.get("receiverRegion"), raw.get("receiverDXCC")
        if region:
            lines.append(f"Region: {region}")
        if country:
            lines.append(f"Country: {country}")
        lines.append(f"Grid: {spot.get('locator', '?')}")
        return "\n".join(lines)

    def _on_pota_toggled(self, checked):
        self.pota_button.setText("POTA: ON" if checked else "POTA: OFF")
        if checked:
            self._start_pota_fetch()
        else:
            self.map_widget.set_pota_markers([])

    def _on_pota_menu_requested(self, _pos):
        menu = QMenu(self)
        menu.addAction("Refresh Now").triggered.connect(self._start_pota_fetch)
        menu.exec(self.pota_button.mapToGlobal(_pos))

    def _start_pota_fetch(self):
        if self._pota_worker is not None and self._pota_worker.isRunning():
            return  # a fetch is already in flight -- let it finish
        worker = PotaWorker(self)
        worker.spots_ready.connect(self._on_pota_spots_ready)
        worker.failed.connect(self._on_pota_fetch_failed)
        self._pota_worker = worker
        worker.start()

    def _on_pota_spots_ready(self, spots):
        self._pota_spots_cache = spots
        if self.pota_button.isChecked():
            self.map_widget.set_pota_markers(self._build_pota_markers())

    def _on_pota_fetch_failed(self, message):
        QMessageBox.warning(self, "POTA", f"Couldn't fetch POTA spots:\n{message}")
        self.pota_button.setChecked(False)

    def _build_pota_markers(self):
        """Filters self._pota_spots_cache (the last fetch's raw
        results, unfiltered by band) to the band picked in
        qso_band_filter_combo -- same shared filter as QSO/PSKReporter
        markers, re-filtering the already-fetched cache rather than
        re-fetching."""
        band_filter = self.qso_band_filter_combo.currentData()
        spots = self._pota_spots_cache
        if band_filter is not None:
            spots = [spot for spot in spots if spot.get("band") == band_filter]
        return [
            {"lat": spot["lat"], "lon": spot["lon"], "tooltip": self._pota_tooltip_text(spot)}
            for spot in spots
        ]

    @staticmethod
    def _pota_tooltip_text(spot):
        lines = [
            spot.get("activator", "?"),
            f"{spot.get('reference', '?')}" + (f" -- {spot['park_name']}" if spot.get("park_name") else ""),
            f"Band: {spot.get('band', '?')}",
            f"Mode: {spot.get('mode', '?')}",
        ]
        spot_time = spot.get("spot_time")
        if spot_time:
            try:
                dt = datetime.datetime.fromisoformat(spot_time)
                lines.append(f"Time: {dt.strftime('%Y-%m-%d %H:%M')} UTC")
            except ValueError:
                lines.append(f"Time: {spot_time}")
        if spot.get("count") is not None:
            lines.append(f"QSOs so far: {spot['count']}")
        if spot.get("comments"):
            lines.append(f"Comment: {spot['comments']}")
        lines.append(f"Spotted by: {spot.get('spotter', '?')}")
        return "\n".join(lines)

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
            is_active = sat.get("name") == self._map_highlighted_satellite_name
            position = {
                "name": sat.get("name", "?"),
                "lat": lat,
                "lon": lon,
                "altitude_km": altitude_km,
                "footprint": footprint_points(lat, lon, altitude_km),
                "active": is_active,
            }
            # The active satellite's ground track always shows, on top of
            # (not instead of) its own persisted "Show Orbit Path" setting
            # -- and disappears the instant a different satellite becomes
            # active, per the satellite's own show_path value only.
            if sat.get("show_path") or is_active:
                position["path"] = ground_track_points(sat.get("line1", ""), sat.get("line2", ""), now)
            positions.append(position)
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

    def _on_pass_row_clicked(self, row, _column):
        """Single-click a row here is the same as left-clicking that
        satellite's marker on the map -- makes it the active
        (highlighted) satellite. Purely visual, no tracking change."""
        if row >= len(self._upcoming_passes):
            return
        self._on_satellite_left_clicked(self._upcoming_passes[row]["name"])

    def _on_pass_row_double_clicked(self, row, _column):
        """Double-clicking a row here is the same as double-clicking
        that satellite's marker on the map."""
        if row >= len(self._upcoming_passes):
            return
        self._on_satellite_double_clicked(self._upcoming_passes[row]["name"])

    def _on_passes_table_context_menu(self, pos):
        row = self.upcoming_passes_table.rowAt(pos.y())
        if row < 0 or row >= len(self._upcoming_passes):
            return
        satellite = next(
            (sat for sat in self.satellites if sat.get("name") == self._upcoming_passes[row]["name"]), None
        )
        if satellite is None:
            return
        menu = QMenu(self)
        info_action = menu.addAction("Satellite Info...")
        path_action = menu.addAction("Show Orbit Path")
        path_action.setCheckable(True)
        path_action.setChecked(bool(satellite.get("show_path")))
        chosen = menu.exec(self.upcoming_passes_table.viewport().mapToGlobal(pos))
        if chosen is info_action:
            self._open_satellite_info(satellite)
        elif chosen is path_action:
            self._toggle_satellite_show_path(satellite, path_action.isChecked())

    def _toggle_satellite_show_path(self, satellite, show):
        satellite["show_path"] = show
        save_satellite_data(self.satellites)
        if self.satellite_button.isChecked():
            self._update_satellite_positions()  # immediate feedback -- the 5s timer would also catch up on its own

    def _open_satellite_info(self, satellite):
        SatelliteInfoDialog(
            satellite, self,
            observer_lat=self._observer_lat, observer_lon=self._observer_lon,
            observer_elevation_km=self._observer_elevation_m / 1000.0,
        ).exec()

    def _on_satellite_right_clicked(self, name):
        """Right-clicked on the map marker -- opens a read-only info
        dialog with everything this app has stored on the satellite
        (TLE, NORAD ID, transponders, upcoming passes)."""
        satellite = next((sat for sat in self.satellites if sat.get("name") == name), None)
        if satellite is None:
            return
        self._open_satellite_info(satellite)

    def _on_satellite_left_clicked(self, name):
        """Left-clicked on the map marker, or a passes-table row --
        makes this the "active" satellite on the map: brighter marker/
        footprint/path colors, and its ground track always shown
        (regardless of its own Show Orbit Path setting) until a
        different satellite becomes active. Purely visual -- does NOT
        start Doppler tracking (see _on_satellite_double_clicked for
        that)."""
        if self._map_highlighted_satellite_name == name:
            return
        self._map_highlighted_satellite_name = name
        if self.satellite_button.isChecked():
            self._update_satellite_positions()  # immediate feedback -- the 5s timer would also catch up on its own

    def _on_satellite_double_clicked(self, name):
        """(Re)starts tracking this satellite -- replaces whatever was
        active before (it stays selected/tracked until another double-
        click, or upcoming-pass row double-click, replaces it; pausing
        via Stop Tracking doesn't clear it). Drives SatelliteSession
        directly now -- this window owns tracking control, not just a
        signal source for some other window to react to. Also makes it
        the map-active satellite (same as a single left-click) -- doesn't
        need a location configured, so this happens before that check
        below, unlike the tracking behavior it gates."""
        self._on_satellite_left_clicked(name)
        satellite = next((sat for sat in self.satellites if sat.get("name") == name), None)
        if satellite is None:
            return
        # (0, 0) is what an unset lat/lon defaults to (nobody's actual
        # station is at 0N 0E), so treat it the same as "not set". Unlike
        # transponder data (handled gracefully below -- elevation/
        # azimuth/AOS-LOS don't need one), there's no useful degraded
        # mode without a location at all.
        if not self._observer_lat and not self._observer_lon:
            QMessageBox.warning(
                self, "Satellite Tracking",
                "Set your location in the Connect New Radio dialog first "
                "(Latitude/Longitude/Elevation) -- satellite tracking needs "
                "to know where you're observing from."
            )
            return

        self._active_satellite = satellite
        self.satellite_name_label.setText(satellite.get("name", "?"))

        self.transponder_combo.blockSignals(True)
        self.transponder_combo.clear()
        transponders = satellite.get("transponders", [])
        if transponders:
            for transponder in transponders:
                downlink = transponder.get("downlink_mhz") or "?"
                mode = transponder.get("mode") or "?"
                uplink_mode = transponder.get("uplink_mode") or ""
                # Only show a "uplink/downlink" mode split when they
                # actually differ (an inverting linear transponder) --
                # redundant otherwise (FM transponders, or entries with
                # no uplink_mode recorded at all).
                mode_label = f"{uplink_mode}/{mode}" if uplink_mode and uplink_mode != mode else mode
                description = transponder.get("description") or "Transponder"
                self.transponder_combo.addItem(f"{description} -- {downlink} MHz {mode_label}", transponder)
            self.transponder_combo.setEnabled(True)
        else:
            self.transponder_combo.addItem("No transponders stored -- Doppler correction unavailable", None)
            self.transponder_combo.setEnabled(False)
        self.transponder_combo.blockSignals(False)

        # Tracking always (re)starts on selection -- if it's already
        # checked (switching straight from one satellite to another),
        # setChecked(True) won't re-emit toggled, so start explicitly
        # rather than relying on it.
        self.tracking_button.setEnabled(True)
        self.tracking_button.blockSignals(True)
        self.tracking_button.setChecked(True)
        self.tracking_button.blockSignals(False)
        self.tracking_button.setText("Stop Tracking")

        self.satellite_session.start(satellite)
        self.satellite_session.set_transponder(self.transponder_combo.currentData())

    def _on_transponder_changed(self, _index):
        if self._active_satellite is not None:
            self.satellite_session.set_transponder(self.transponder_combo.currentData())

    def _on_tracking_toggled(self, checked):
        self.tracking_button.setText("Stop Tracking" if checked else "Start Tracking")
        if checked:
            if self._active_satellite is not None:
                self.satellite_session.start(self._active_satellite)
        else:
            self.satellite_session.stop()

    def _on_session_tracking_changed(self, tracking):
        # Keeps the button in sync if SatelliteSession's state changes
        # from somewhere other than this button itself (there's no such
        # path today, but this is cheap insurance against the button and
        # the session silently disagreeing).
        if self.tracking_button.isChecked() != tracking:
            self.tracking_button.blockSignals(True)
            self.tracking_button.setChecked(tracking)
            self.tracking_button.blockSignals(False)
            self.tracking_button.setText("Stop Tracking" if tracking else "Start Tracking")

    def _on_satellite_state_updated(self, satellite, look, crossing_text, downlink_doppler_hz, uplink_doppler_hz, warning_text):
        if look is None:
            self.satellite_overlay_label.setText(f"{satellite.get('name', '?')}\nOrbit propagation failed (invalid TLE?)")
            return
        doppler_text = ""
        if downlink_doppler_hz is not None:
            doppler_text += f"  RX Doppler {downlink_doppler_hz:+.0f} Hz"
        if uplink_doppler_hz is not None:
            doppler_text += f"  TX Doppler {uplink_doppler_hz:+.0f} Hz"
        visibility = "up" if look["elevation_deg"] >= 0 else "down"
        self.satellite_overlay_label.setText(
            f"{satellite.get('name', '?')} ({visibility})  "
            f"El {look['elevation_deg']:.1f}°  Az {look['azimuth_deg']:.1f}°{doppler_text}{warning_text}\n"
            f"{crossing_text}"
        )

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

    # ---- Connected radios ----

    def _on_connect_radio_clicked(self):
        # Restricts the role combo to whatever still makes sense given
        # already-connected radios (e.g. Downlink/Uplink hidden once a
        # Full Duplex radio is connected) -- see
        # connection_dialog.allowed_satellite_roles.
        active_roles = {getattr(window, "_role", "non_sat") for window in self._connected_radios}
        dialog = ConnectionDialog(self, active_roles=active_roles)
        if dialog.exec() != QDialog.Accepted:
            return
        details = dialog.details

        # ConnectionDialog may have just updated the saved location --
        # re-read and push it to the map/session so it's not stuck on
        # whatever was there (or nothing) at startup.
        self._load_observer_location()
        self.satellite_session.set_observer_location(
            self._observer_lat, self._observer_lon, self._observer_elevation_m
        )
        if self._observer_lat is not None and self._observer_lon is not None:
            self.map_widget.set_operator_location(
                self._observer_lat, self._observer_lon,
                f"{self._observer_lat:.3f}, {self._observer_lon:.3f}",
            )

        window = RadioWindow(details, self.satellite_session)
        window.closed.connect(self._on_radio_window_closed)
        self._connected_radios.append(window)

        role_label = _ROLE_LABELS.get(details.get("role", "non_sat"), details.get("role", "non_sat"))
        connection_label = details.get("host") or details.get("serial_port") or "?"
        item = QListWidgetItem(f"{details['radio_model']} ({role_label}) -- {connection_label}")
        item.setData(Qt.UserRole, window)
        self.connected_radios_list.addItem(item)

        self._update_external_app_buttons_enabled()
        window.show()

    def _on_connected_radio_item_double_clicked(self, item):
        window = item.data(Qt.UserRole)
        if window is not None:
            window.show()
            window.raise_()
            window.activateWindow()

    def _on_log_book_clicked(self):
        if self.log_book_window is None:
            self.log_book_window = LogBookWindow(self)
        self.log_book_window.show()
        self.log_book_window.raise_()
        self.log_book_window.activateWindow()

    def _on_new_qso_clicked(self):
        # Deliberately does NOT show()/raise_() the Log Book window --
        # logging a QSO shouldn't force that table open too, just needs
        # the (possibly not-yet-created, and left hidden either way)
        # LogBookWindow instance around to own persistence/QRZ upload,
        # same as _on_log_book_clicked's lazy construction.
        if self.log_book_window is None:
            self.log_book_window = LogBookWindow(self)
        self.log_book_window._on_new_qso_clicked()

    def _on_radio_window_closed(self, window):
        """Connected to RadioWindow.closed, emitted from closeEvent
        BEFORE that window's worker actually stops -- so it's still
        safe here to call something on it one last time (disabling
        Virtual Cables/rigctld if this was their target), same best-
        effort guarantee those calls already carry on their own."""
        if window in self._connected_radios:
            self._connected_radios.remove(window)
        for row in range(self.connected_radios_list.count()):
            item = self.connected_radios_list.item(row)
            if item.data(Qt.UserRole) is window:
                self.connected_radios_list.takeItem(row)
                break
        if window is self._virtual_cable_rx_window or window is self._virtual_cable_tx_window:
            # Reconfigure without the closing radio -- drops it from
            # whichever side(s) it held; the OTHER side (if it's a
            # different, still-open radio) keeps running.
            new_rx = None if window is self._virtual_cable_rx_window else self._virtual_cable_rx_window
            new_tx = None if window is self._virtual_cable_tx_window else self._virtual_cable_tx_window
            self._apply_virtual_cable_config(new_rx, new_tx, self._virtual_cable_channel)
        self._stop_rigctld_for_window(window)
        self._rigctld_ports.pop(window, None)
        self._update_external_app_buttons_enabled()

    def _update_external_app_buttons_enabled(self):
        has_radios = bool(self._connected_radios)
        self.virtual_cable_button.setEnabled(has_radios)
        self.rigctld_button.setEnabled(has_radios)

    # ---- Virtual Cables ----

    def _on_virtual_cable_button_clicked(self):
        is_active = self._virtual_cable_rx_window is not None or self._virtual_cable_tx_window is not None
        dialog = VirtualCableDialog(
            self._connected_radios, self._virtual_cable_rx_window, self._virtual_cable_tx_window,
            self._virtual_cable_channel,
            on_start=self._on_virtual_cable_dialog_start,
            on_stop=self._on_virtual_cable_dialog_stop,
            is_active=is_active,
            parent=self,
        )
        dialog.exec()  # Start/Stop inside already applied whatever was clicked -- Close just dismisses

    def _on_virtual_cable_dialog_start(self, rx_window, tx_window, channel):
        self._virtual_cable_channel = channel
        self._apply_virtual_cable_config(rx_window, tx_window, channel)

    def _on_virtual_cable_dialog_stop(self):
        self._apply_virtual_cable_config(None, None, self._virtual_cable_channel)

    def _apply_virtual_cable_config(self, rx_window, tx_window, channel):
        """rx_window/tx_window are RadioWindow instances or None (no
        cable that direction). Always fully tears down whatever was
        previously configured first, rather than incrementally diffing
        -- this is a deliberate, infrequent action (opening the dialog
        and clicking OK, or a radio closing), not a hot path, and a full
        reset is far simpler to reason about correctly than tracking
        every possible from-state to-state transition (same radio for
        both -> different radios, one side dropped, etc.)."""
        self._teardown_virtual_cables()

        if rx_window is None and tx_window is None:
            self.virtual_cable_button.setText("Virtual Cables: OFF")
            return

        if not pactl_available():
            QMessageBox.warning(
                self, "Virtual Cables",
                "Requires Linux with PulseAudio or PipeWire (pactl not found) "
                "-- on Windows/macOS, install a virtual audio cable driver "
                "like VB-CABLE or BlackHole instead, then select it directly "
                "in each radio's own connection dialog."
            )
            return

        try:
            rx_module = create_null_sink(VIRTUAL_CABLE_RX_NAME, VIRTUAL_CABLE_RX_DESC)
            tx_module = create_null_sink(VIRTUAL_CABLE_TX_NAME, VIRTUAL_CABLE_TX_DESC)
        except Exception as exc:
            QMessageBox.critical(self, "Virtual Cables", f"Couldn't create sinks ({exc}).")
            return
        self._virtual_cable_modules = (rx_module, tx_module)

        # Same reasoning as the original per-worker implementation this
        # replaced: redirect PulseAudio's own DEFAULT sink/source at the
        # new cables (this PortAudio build doesn't expose each
        # individually-named sink, only a generic "default" device) --
        # done exactly ONCE here now, rather than per-worker, since two
        # different radios' bridges may both be about to open against
        # "system default" (RX radio's output, TX radio's input) and the
        # null sinks themselves must only ever be created once.
        self._virtual_cable_previous_sink = get_default_sink_name()
        self._virtual_cable_previous_source = get_default_source_name()
        set_default_sink(VIRTUAL_CABLE_RX_NAME)
        set_default_source(f"{VIRTUAL_CABLE_TX_NAME}.monitor")

        self._virtual_cable_rx_window = rx_window
        self._virtual_cable_tx_window = tx_window
        if rx_window is not None and rx_window is tx_window:
            rx_window.worker.set_virtual_cable_bridge(rx=True, tx=True)
        else:
            if rx_window is not None:
                rx_window.worker.set_virtual_cable_bridge(rx=True, tx=False)
            if tx_window is not None:
                tx_window.worker.set_virtual_cable_bridge(rx=False, tx=True)
        if rx_window is not None:
            rx_window.worker.set_rx_downmix_channel(channel)

        # Confirmed by direct testing (same as the original per-worker
        # implementation): changing PulseAudio's default sink/source
        # doesn't retroactively redirect where an already-open stream
        # connects -- what actually works is MOVING the stream once
        # it's open. Non-blocking QTimer retry (not a blocking sleep
        # loop) since this runs on the GUI thread now, not inside a
        # worker's own asyncio loop -- freezing the whole app for up to
        # ~2s while PulseAudio catches up would be a bad trade for what
        # pactl itself does near-instantly once the stream exists.
        self._virtual_cable_move_attempts = 0
        self._virtual_cable_sink_input_ids = []
        self._virtual_cable_source_output_ids = []
        self._virtual_cable_move_timer = QTimer(self)
        self._virtual_cable_move_timer.timeout.connect(self._try_move_virtual_cable_streams)
        self._virtual_cable_move_timer.start(300)
        self._try_move_virtual_cable_streams()  # also try immediately -- no need to wait a full 300ms for the first attempt

        label_parts = []
        if rx_window is not None:
            label_parts.append(f"RX: {rx_window._details['radio_model']}")
        if tx_window is not None:
            label_parts.append(f"TX: {tx_window._details['radio_model']}")
        self.virtual_cable_button.setText(f"Virtual Cables: ON ({', '.join(label_parts)})")

    def _try_move_virtual_cable_streams(self):
        my_pid = os.getpid()
        needs_sink = self._virtual_cable_rx_window is not None
        needs_source = self._virtual_cable_tx_window is not None
        if needs_sink and not self._virtual_cable_sink_input_ids:
            self._virtual_cable_sink_input_ids = find_own_sink_input_ids(my_pid)
        if needs_source and not self._virtual_cable_source_output_ids:
            self._virtual_cable_source_output_ids = find_own_source_output_ids(my_pid)
        for sink_input_id in self._virtual_cable_sink_input_ids:
            move_sink_input(sink_input_id, VIRTUAL_CABLE_RX_NAME)
        for source_output_id in self._virtual_cable_source_output_ids:
            move_source_output(source_output_id, f"{VIRTUAL_CABLE_TX_NAME}.monitor")

        self._virtual_cable_move_attempts += 1
        sink_done = not needs_sink or bool(self._virtual_cable_sink_input_ids)
        source_done = not needs_source or bool(self._virtual_cable_source_output_ids)
        if (sink_done and source_done) or self._virtual_cable_move_attempts >= 6:
            if self._virtual_cable_move_timer is not None:
                self._virtual_cable_move_timer.stop()
                self._virtual_cable_move_timer = None
            if not (sink_done and source_done):
                missing = []
                if not sink_done:
                    missing.append("playback")
                if not source_done:
                    missing.append("recording")
                print(
                    f"[AUDIO] Virtual Cables: couldn't automatically find/move this "
                    f"app's own {' and '.join(missing)} stream in PulseAudio -- you "
                    "may need to reassign it manually in pavucontrol (Playback/"
                    "Recording tabs)."
                )

    def _teardown_virtual_cables(self):
        """Safe to call unconditionally, including when nothing is
        currently configured (every guard below is a no-op against the
        cleared-out init state)."""
        if self._virtual_cable_move_timer is not None:
            self._virtual_cable_move_timer.stop()
            self._virtual_cable_move_timer = None

        if self._virtual_cable_rx_window is not None:
            self._virtual_cable_rx_window.worker.set_virtual_cable_bridge(rx=False, tx=False)
        if self._virtual_cable_tx_window is not None and self._virtual_cable_tx_window is not self._virtual_cable_rx_window:
            self._virtual_cable_tx_window.worker.set_virtual_cable_bridge(rx=False, tx=False)
        self._virtual_cable_rx_window = None
        self._virtual_cable_tx_window = None

        if self._virtual_cable_previous_sink:
            set_default_sink(self._virtual_cable_previous_sink)
        if self._virtual_cable_previous_source:
            set_default_source(self._virtual_cable_previous_source)
        self._virtual_cable_previous_sink = None
        self._virtual_cable_previous_source = None

        if self._virtual_cable_modules:
            for module_id in self._virtual_cable_modules:
                unload_pactl_module(module_id)
            self._virtual_cable_modules = None

    # ---- Rigctld ----

    def _on_rigctld_button_clicked(self):
        dialog = RigctldDialog(
            self._connected_radios, self._rigctld_servers, self._rigctld_ports,
            on_start=self._on_rigctld_dialog_start,
            on_stop=self._on_rigctld_dialog_stop,
            parent=self,
        )
        dialog.exec()  # each row already started/stopped its own server directly -- Close just dismisses

    def _on_rigctld_dialog_start(self, window, port):
        """Returns True/False so the dialog's row can reflect whether
        the server actually started -- a port collision (with another
        of our own radios' servers, or anything else already using that
        port) surfaces as a normal QTcpServer bind failure here, same
        as the original single-target implementation's own handling."""
        server = RigctldServer(
            get_freq=lambda: window._current_freq_hz or 0,
            set_freq=lambda hz: window.worker.set_frequency(int(hz)),
            get_mode=lambda: (window.control_widgets["mode"].currentData() or "USB", 0),
            set_mode=lambda mode: window.worker.set_control_value("mode", mode),
            get_ptt=lambda: window.ptt_button.isChecked(),
            set_ptt=lambda on: window.ptt_button.setChecked(on),  # reuses the normal PTT path via its toggled signal
            port=port,
        )
        try:
            server.start()
        except RuntimeError as exc:
            QMessageBox.critical(self, "Rigctld", str(exc))
            return False
        self._rigctld_servers[window] = server
        self._rigctld_ports[window] = port
        self._update_rigctld_button_label()
        return True

    def _on_rigctld_dialog_stop(self, window):
        self._stop_rigctld_for_window(window)
        self._update_rigctld_button_label()

    def _stop_rigctld_for_window(self, window):
        server = self._rigctld_servers.pop(window, None)
        if server is not None:
            server.stop()

    def _update_rigctld_button_label(self):
        count = len(self._rigctld_servers)
        self.rigctld_button.setText(f"Rigctld: {count} running" if count else "Rigctld: OFF")

    # ---- WSJT-X ----

    def _on_wsjtx_button_clicked(self):
        settings = QSettings("IcomRadioApp", "RadioControl")
        path = settings.value("wsjtx_executable_path", "")

        if not path or not os.path.isfile(path):
            path = find_wsjtx_executable()

        if not path:
            path, _filter = QFileDialog.getOpenFileName(
                self, "Locate the WSJT-X executable",
                "", "Executable (*.exe);;All files (*)" if platform.system() == "Windows" else "All files (*)",
            )
            if not path:
                return  # user cancelled the browse dialog

        try:
            launch_wsjtx(path)
        except OSError as exc:
            QMessageBox.critical(self, "WSJT-X", f"Couldn't launch WSJT-X at:\n{path}\n\n{exc}")
            return

        # Only remember the path once it's actually confirmed to work
        # (subprocess.Popen not raising means the OS accepted it).
        settings.setValue("wsjtx_executable_path", path)

    def _on_wsjtx_autolog_toggled(self, checked):
        if checked:
            listener = WsjtxUdpListener(port=self._wsjtx_udp_port, parent=self)
            try:
                listener.start()
            except RuntimeError as exc:
                QMessageBox.critical(self, "WSJT-X Auto-Log", str(exc))
                self.wsjtx_autolog_button.blockSignals(True)
                self.wsjtx_autolog_button.setChecked(False)
                self.wsjtx_autolog_button.blockSignals(False)
                return
            listener.qso_logged.connect(self._on_wsjtx_qso_logged)
            listener.error.connect(self._on_wsjtx_udp_error)
            self._wsjtx_udp_listener = listener
            self.wsjtx_autolog_button.setText("WSJT-X Auto-Log: ON")
        else:
            if self._wsjtx_udp_listener is not None:
                self._wsjtx_udp_listener.stop()
                self._wsjtx_udp_listener = None
            self.wsjtx_autolog_button.setText("WSJT-X Auto-Log: OFF")

    def _on_wsjtx_autolog_menu_requested(self, _pos):
        port, ok = QInputDialog.getInt(
            self, "WSJT-X Auto-Log",
            "UDP port to listen on (must match WSJT-X's own \"UDP Server "
            "port\" in Settings > Reporting):",
            self._wsjtx_udp_port, 1, 65535,
        )
        if not ok or port == self._wsjtx_udp_port:
            return
        self._wsjtx_udp_port = port
        QSettings("IcomRadioApp", "RadioControl").setValue("wsjtx_udp_port", port)
        if self.wsjtx_autolog_button.isChecked():
            # Restart on the new port -- toggling off then back on reuses
            # the exact same start/stop/error-handling path as a manual click.
            self.wsjtx_autolog_button.setChecked(False)
            self.wsjtx_autolog_button.setChecked(True)

    def _on_wsjtx_qso_logged(self, fields):
        """fields: one adif.parse_adif_records() result straight off
        WSJT-X's own "Logged ADIF" UDP message -- already genuine ADIF,
        no field-name translation needed. Reuses LogBookWindow's own
        submit handler directly (append + save + refresh + best-effort
        QRZ upload) rather than duplicating that logic -- same
        established pattern as the dashboard's "New QSO..." button,
        which already reaches into LogBookWindow the same way. Doesn't
        show/raise the window -- this should never steal focus just
        because a QSO came in in the background."""
        if self.log_book_window is None:
            self.log_book_window = LogBookWindow(self)
        self.log_book_window._on_new_qso_submitted(fields)

    def _on_wsjtx_udp_error(self, message):
        print(f"[WSJT-X Auto-Log] {message}")

    def closeEvent(self, event):
        self.satellite_session.stop()
        if self._wsjtx_udp_listener is not None:
            self._wsjtx_udp_listener.stop()
            self._wsjtx_udp_listener = None
        for window in list(self._rigctld_servers):
            self._stop_rigctld_for_window(window)
        self._teardown_virtual_cables()  # best-effort; each affected window's own worker.stop() (below) may cut this off before it finishes
        for window in list(self._connected_radios):
            window.close()
        self._satellite_timer.stop()
        self._passes_timer.stop()
        self.solar_worker.requestInterruption()
        self.solar_worker.wait(2000)
        event.accept()
