"""
HamClockWindow: the app's central/main window -- the one thing that
exists independently of any radio connection, tying together the day/
night world map, live clocks, solar-terrestrial data, HF band
conditions, satellite tracking, and now (see below) radio connection
management, satellite Doppler control, Virtual Cables, rigctld, and
WSJT-X launching. Constructed directly by main.py; no radio connects at
startup any more -- radios are added from the Radios dialog
(RadiosDialog, opened via the "Radios" button -- "Connect New
Radio..." lives there now), each assigned a satellite role
(RADIO_ROLES in constants.py) in ConnectionDialog, and get their own
RadioWindow (main_window.py).

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
    QCheckBox,
    QComboBox,
    QSpinBox,
    QFileDialog,
    QDialogButtonBox,
    QMenu,
    QRadioButton,
    QButtonGroup,
    QInputDialog,
    QLineEdit,
    QTabWidget,
)

from solar_data import SolarDataWorker, BAND_CONDITION_RANGES
from theme import position_on_screen_half
from world_map import WorldMapWidget
from connection_dialog import ConnectionDialog
from operator_profile import OperatorProfileDialog
from main_window import RadioWindow
from satellite_session import SatelliteSession
from log_book_window import LogBookWindow
import adif
import qso_log
import pskreporter
import pota
import contests
import dxcluster
import updater
from wsjtx_rigctld import (
    RigctldServer,
    RIGCTLD_DEFAULT_PORT,
    find_wsjtx_executable,
    launch_wsjtx,
    WSJTX_RIG_NAME,
    find_js8call_executable,
    launch_js8call,
)
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
    """Configures virtual cables for ONE radio -- selected beforehand
    in RadiosDialog's connected-radios list, per explicit instruction
    (this used to let you pick RX radio/TX radio/mix all from one
    dialog with two independent combo boxes; now the radio itself is
    chosen by list selection first, and this dialog just asks which
    direction(s) THAT radio should handle).

    RX and TX are independent checkboxes, not a single on/off --
    preserves the original "poor man's full duplex" capability (RX
    from one radio, TX through a different one): opening this dialog
    for radio A and checking only TX, then separately opening it for
    radio B and checking only RX, ends up with exactly that pairing,
    with each apply computed by HamClockWindow.
    _on_virtual_cable_dialog_apply against the CURRENT global RX/TX
    state (unaffected radios keep whatever they were).

    Apply calls straight through to HamClockWindow.
    _apply_virtual_cable_config (via on_apply) -- this dialog never
    touches audio/PulseAudio state itself -- and stays open
    afterward so the selection can be adjusted and reapplied. Close
    just dismisses it."""

    def __init__(self, label, is_rx, is_tx, channel, on_apply, on_stop, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Virtual Cables -- {label}")
        self._on_apply = on_apply
        self._on_stop = on_stop

        self.rx_checkbox = QCheckBox("Use for RX (what you hear)")
        self.rx_checkbox.setChecked(is_rx)
        self.tx_checkbox = QCheckBox("Use for TX (what you transmit through)")
        self.tx_checkbox.setChecked(is_tx)

        self.channel_combo = QComboBox()
        self.channel_combo.addItem("Both (Main + Sub mixed)", "mix")
        self.channel_combo.addItem("Main only", "main")
        self.channel_combo.addItem("Sub only", "sub")
        self.channel_combo.setToolTip(
            "Which receiver's audio goes to the RX cable -- only meaningful "
            "when this radio is dual-receiver; harmless no-op otherwise."
        )
        channel_index = self.channel_combo.findData(channel)
        if channel_index != -1:
            self.channel_combo.setCurrentIndex(channel_index)

        mix_row = QHBoxLayout()
        mix_row.addWidget(QLabel("RX Audio Mix:"))
        mix_row.addWidget(self.channel_combo)
        mix_row.addStretch()

        apply_button = QPushButton("Apply")
        apply_button.setStyleSheet("QPushButton { background-color: #2a6; color: white; font-weight: bold; }")
        apply_button.setToolTip("Applies the RX/TX selection above for this radio immediately.")
        apply_button.clicked.connect(self._on_apply_clicked)
        stop_button = QPushButton("Stop (this radio)")
        stop_button.setToolTip("Removes this radio from both RX and TX -- any OTHER radio's role is left alone.")
        stop_button.clicked.connect(self._on_stop_clicked)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)

        button_row = QHBoxLayout()
        button_row.addWidget(apply_button)
        button_row.addWidget(stop_button)
        button_row.addStretch()
        button_row.addWidget(close_button)

        layout = QVBoxLayout()
        layout.addWidget(QLabel(
            "Creates virtual audio devices (Linux/PulseAudio or PipeWire only) "
            "so an external app (WSJT-X, etc.) can send/receive audio through "
            "this radio. A separate radio can independently be assigned the "
            "other direction from its own row in the Radios dialog -- useful "
            "for a separate uplink/downlink pair."
        ))
        layout.addWidget(self.rx_checkbox)
        layout.addWidget(self.tx_checkbox)
        layout.addLayout(mix_row)
        layout.addLayout(button_row)
        self.setLayout(layout)

    def _on_apply_clicked(self):
        self._on_apply(self.rx_checkbox.isChecked(), self.tx_checkbox.isChecked(), self.channel_combo.currentData())

    def _on_stop_clicked(self):
        self.rx_checkbox.setChecked(False)
        self.tx_checkbox.setChecked(False)
        self._on_stop()


class RigctldDialog(QDialog):
    """Configures rigctld for ONE radio -- selected beforehand in
    RadiosDialog's connected-radios list, per explicit instruction
    (this used to be one row per connected radio in a single dialog;
    now the radio itself is chosen by list selection first). rigctld
    is inherently one-radio-per-port, so a real multi-radio setup
    (e.g. WSJT-X reaching both an uplink and a downlink radio) still
    needs a separate server, on a separate port, per radio -- just
    started/stopped from that radio's own dialog now instead of a
    shared row. Never touches RigctldServer directly -- on_start(port)
    -> bool and on_stop() do the real work in HamClockWindow."""

    def __init__(self, label, is_running, port, on_start, on_stop, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Rigctld -- {label}")
        self._on_start = on_start
        self._on_stop = on_stop

        self.port_spinbox = QSpinBox()
        self.port_spinbox.setRange(1, 65535)
        self.port_spinbox.setValue(port)
        self.port_spinbox.setToolTip("TCP port for this radio's rigctld server -- must differ from any other radio's.")
        self.port_spinbox.setEnabled(not is_running)

        self.toggle_button = QPushButton("Stop" if is_running else "Start")
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(is_running)
        self.toggle_button.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.toggle_button.toggled.connect(self._on_toggled)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)

        form = QFormLayout()
        form.addRow("Port:", self.port_spinbox)

        layout = QVBoxLayout()
        layout.addWidget(QLabel(
            "Select \"Hamlib NET rigctl\" (rig model 2) and 127.0.0.1:<port> "
            "in the external app (WSJT-X, JTDX, fldigi, ...)."
        ))
        layout.addLayout(form)
        layout.addWidget(self.toggle_button)
        layout.addWidget(close_button)
        self.setLayout(layout)

    def _on_toggled(self, checked):
        if checked:
            success = self._on_start(self.port_spinbox.value())
            if not success:
                self.toggle_button.blockSignals(True)
                self.toggle_button.setChecked(False)
                self.toggle_button.blockSignals(False)
                return
            self.toggle_button.setText("Stop")
            self.port_spinbox.setEnabled(False)
        else:
            self._on_stop()
            self.toggle_button.setText("Start")
            self.port_spinbox.setEnabled(True)


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
    """One-shot fetch off the GUI thread -- this app's usual small-
    dedicated-QThread-per-operation shape (a single blocking urllib
    call, not the full persistent-connection RadioWorker machinery).
    Never runs on a timer -- only ever started by an explicit user action (toggling the
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


class PotaProgramsWorker(QThread):
    """One-shot fetch off the GUI thread, same shape as PotaWorker --
    populates the Parks tab's program (country/region) combo box.
    Auto-started once at startup (see the end of __init__), same "just
    load it" treatment as Contests -- read-only reference data, not a
    live overlay."""

    programs_ready = Signal(list)
    failed = Signal(str)

    def run(self):
        try:
            programs = pota.fetch_pota_programs()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.programs_ready.emit(programs)


class ParksWorker(QThread):
    """One-shot fetch of a single POTA program's full park directory --
    same shape as PotaWorker, but scoped to one program_prefix (there's
    no "all parks on Earth" endpoint -- see pota.py's own module
    docstring). Checks the on-disk cache (pota.load_cached_program_
    parks) before hitting the network at all."""

    parks_ready = Signal(list, bool)  # parks, from_cache
    failed = Signal(str)

    def __init__(self, program_prefix, parent=None):
        super().__init__(parent)
        self._program_prefix = program_prefix

    def run(self):
        cached = pota.load_cached_program_parks(self._program_prefix)
        if cached is not None:
            self.parks_ready.emit(cached, True)
            return
        try:
            parks = pota.fetch_program_parks(self._program_prefix)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        try:
            pota.save_program_parks_cache(self._program_prefix, parks)
        except OSError:
            pass  # cache write failure isn't fatal -- just means next open refetches too
        self.parks_ready.emit(parks, False)


class ParkDetailsWorker(QThread):
    """One-shot fetch of one park's richer detail fields (park type,
    access methods, managing agency, first activation, ...) -- opened
    alongside ParkDetailsDialog (which shows what's already known from
    the list row immediately, then fills in these fields once this
    lands, only if the dialog is still open -- see _on_park_row_double_
    clicked)."""

    details_ready = Signal(dict)
    failed = Signal(str)

    def __init__(self, reference, parent=None):
        super().__init__(parent)
        self._reference = reference

    def run(self):
        try:
            details = pota.fetch_park_details(self._reference)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.details_ready.emit(details)


class ContestsWorker(QThread):
    """One-shot fetch off the GUI thread, same shape as PotaWorker --
    the contest calendar feed is ~5MB (years of history, see
    contests.py), so this genuinely matters for keeping the GUI thread
    responsive, not just consistency with the other overlay workers."""

    events_ready = Signal(list)
    failed = Signal(str)

    def run(self):
        try:
            events = contests.fetch_contests()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.events_ready.emit(events)


class ContestDetailsDialog(QDialog):
    """Opened by double-clicking a row in the Contests tab -- shows the
    full name (the table column is elided when narrow), start/end in
    both UTC and local time (the table only shows UTC), duration, and
    -- when contests.py's DESCRIPTION parsing found one -- a link to
    the contest's details page on contestcalendar.com/hornucopia.com,
    opened in the user's own default browser (QLabel's
    setOpenExternalLinks, backed by QDesktopServices) only when they
    click it, not automatically."""

    def __init__(self, event, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Contest Details")
        self.setMinimumWidth(420)

        name_label = QLabel(event["name"])
        name_label.setWordWrap(True)
        name_label.setStyleSheet("font-size: 14px; font-weight: bold;")

        start_utc = event["start"]
        end_utc = event["end"]
        start_local = start_utc.astimezone()
        end_local = end_utc.astimezone()
        duration = end_utc - start_utc

        form = QFormLayout()
        form.addRow("Start (UTC):", QLabel(start_utc.strftime("%Y-%m-%d %H:%M")))
        form.addRow("End (UTC):", QLabel(end_utc.strftime("%Y-%m-%d %H:%M")))
        form.addRow("Start (local):", QLabel(start_local.strftime("%Y-%m-%d %H:%M %Z")))
        form.addRow("End (local):", QLabel(end_local.strftime("%Y-%m-%d %H:%M %Z")))
        form.addRow("Duration:", QLabel(_format_duration(duration)))

        layout = QVBoxLayout()
        layout.addWidget(name_label)
        layout.addLayout(form)

        info_url = event.get("info_url")
        if info_url:
            link_label = QLabel(f'<a href="{info_url}">{info_url}</a>')
            link_label.setOpenExternalLinks(True)
            link_label.setWordWrap(True)
            layout.addWidget(link_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.setLayout(layout)


class ParkDetailsDialog(QDialog):
    """Opened by double-clicking a row in the Parks tab. Shows what's
    already known from the list row (reference, location, grid,
    activation counts) immediately -- no waiting on a second fetch just
    to see basic info -- then apply_details() fills in the richer
    fields (park type, access/activation methods, managing agency,
    first activation, a clickable park/agency website link) once
    ParkDetailsWorker's fetch lands, same "show what you have, enrich
    when the rest arrives" shape as ContestDetailsDialog's own link
    handling."""

    def __init__(self, park, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"POTA Park -- {park.get('reference', '?')}")
        self.setMinimumWidth(420)

        name_label = QLabel(park.get("name") or park.get("reference", "?"))
        name_label.setWordWrap(True)
        name_label.setStyleSheet("font-size: 14px; font-weight: bold;")

        self._form = QFormLayout()
        self._form.addRow("Reference:", QLabel(park.get("reference", "?")))
        if park.get("location_desc"):
            self._form.addRow("Location:", QLabel(park["location_desc"]))
        if park.get("grid"):
            self._form.addRow("Grid:", QLabel(park["grid"]))
        if park.get("lat") is not None and park.get("lon") is not None:
            self._form.addRow("Coordinates:", QLabel(f"{park['lat']:.5f}, {park['lon']:.5f}"))
        if park.get("attempts") is not None:
            self._form.addRow("Activation Attempts:", QLabel(str(park["attempts"])))
        if park.get("activations") is not None:
            self._form.addRow("Successful Activations:", QLabel(str(park["activations"])))
        if park.get("qsos") is not None:
            self._form.addRow("Total QSOs:", QLabel(str(park["qsos"])))

        self.status_label = QLabel("Loading more details...")
        self.status_label.setStyleSheet("color: #888; font-size: 11px;")

        self._link_layout = QVBoxLayout()  # populated once apply_details() lands, if a URL is present

        layout = QVBoxLayout()
        layout.addWidget(name_label)
        layout.addLayout(self._form)
        layout.addWidget(self.status_label)
        layout.addLayout(self._link_layout)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def apply_details(self, details):
        """Called once ParkDetailsWorker's fetch lands -- caller
        (_on_park_details_ready) only calls this if this dialog is
        still the one currently open for that same park reference."""
        self.status_label.setText("")
        if details.get("park_type"):
            self._form.addRow("Type:", QLabel(details["park_type"]))
        if details.get("entity_name"):
            self._form.addRow("Entity:", QLabel(details["entity_name"]))
        if details.get("access_methods"):
            self._form.addRow("Access:", QLabel(details["access_methods"]))
        if details.get("activation_methods"):
            self._form.addRow("Activation:", QLabel(details["activation_methods"]))
        if details.get("agencies"):
            self._form.addRow("Managed by:", QLabel(details["agencies"]))
        if details.get("first_activator"):
            first = details["first_activator"]
            if details.get("first_activation_date"):
                first += f" ({details['first_activation_date']})"
            self._form.addRow("First Activated:", QLabel(first))
        if details.get("comments"):
            comment_label = QLabel(details["comments"])
            comment_label.setWordWrap(True)
            self._form.addRow("Comments:", comment_label)
        url = details.get("park_url") or details.get("agency_url")
        if url:
            link_label = QLabel(f'<a href="{url}">{url}</a>')
            link_label.setOpenExternalLinks(True)
            link_label.setWordWrap(True)
            self._link_layout.addWidget(link_label)

    def apply_details_failed(self, message):
        self.status_label.setText(f"Couldn't load more details: {message}")


def _format_duration(delta: datetime.timedelta) -> str:
    total_minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


class UpdateCheckWorker(QThread):
    """One-shot check off the GUI thread -- updater.check_for_update()
    does real network I/O (git ls-remote against GitHub)."""

    result_ready = Signal(object)  # updater.UpdateCheckResult
    failed = Signal(str)

    def run(self):
        try:
            result = updater.check_for_update()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.result_ready.emit(result)


class UpdatePerformWorker(QThread):
    """Runs updater.perform_update() off the GUI thread -- it shells
    out to git/pip/install.sh, which can take up to a minute or two.
    status_callback=self.status.emit is safe to call from this
    thread -- Qt signal emission is thread-safe, same as every other
    worker in this app reporting back to the GUI thread."""

    status = Signal(str)
    succeeded = Signal()
    failed = Signal(str)

    def run(self):
        try:
            updater.perform_update(status_callback=self.status.emit)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit()


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


class RadiosDialog(QDialog):
    """The connected-radios list plus "Connect New Radio..." button --
    per explicit instruction, moved out of the main dashboard's own
    layout into this dedicated dialog (opened via the dashboard's
    "Radios" button) so the dashboard itself stays less cluttered.

    Doesn't own connected_radios_list/connect_radio_button/
    rigctld_button/virtual_cable_button itself -- HamClockWindow still
    constructs and owns them (and every method that reads/mutates
    them, e.g. _on_connect_radio_clicked, _on_radio_window_closed,
    _on_rigctld_button_clicked, _on_virtual_cable_button_clicked),
    exactly as before this dialog existed. This just reparents those
    same widget instances into its own layout, so nothing about the
    connect/disconnect/rigctld/virtual-cable logic needed to change at
    all -- Rigctld/Virtual Cables act on whichever radio is currently
    SELECTED in the list (see HamClockWindow._selected_radio_window),
    each radio's own live status (rigctld port if running, RX/TX if
    part of the virtual cables) shown right in its row -- see
    HamClockWindow._refresh_connected_radios_list_status.

    Non-modal (exec() would block the rest of the dashboard while
    open -- e.g. double-clicking a connected radio in the list to
    bring its window forward should work without closing this dialog
    first) -- singleton, hide-not-destroy on close, same pattern as
    LogBookWindow."""

    def __init__(self, parent_window):
        super().__init__(parent_window)
        self.setWindowTitle("Radios")

        header = QHBoxLayout()
        header.addWidget(QLabel("Connected Radios:"))
        header.addStretch()
        header.addWidget(parent_window.connect_radio_button)

        service_buttons_row = QHBoxLayout()
        service_buttons_row.addWidget(parent_window.rigctld_button)
        service_buttons_row.addWidget(parent_window.virtual_cable_button)
        service_buttons_row.addStretch()

        layout = QVBoxLayout()
        layout.addLayout(header)
        layout.addWidget(parent_window.connected_radios_list)
        layout.addLayout(service_buttons_row)
        self.setLayout(layout)
        self.resize(480, 260)

    def closeEvent(self, event):
        event.ignore()
        self.hide()


class HamClockWindow(QWidget):
    """The Ham Dashboard window -- see the module comment above this
    section for what's in scope and why."""

    PASSES_DISPLAY_COUNT = 20

    # How many rows tall the passes table (and the same-height tabbed
    # section next to it) actually shows before scrolling -- per
    # explicit instruction, smaller than PASSES_DISPLAY_COUNT (which
    # still controls how many passes are fetched/listed, just scrolled
    # to beyond this) so the bottom of the dashboard isn't cut off on
    # a shorter screen.
    PASSES_VISIBLE_ROWS = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ham Dashboard")
        position_on_screen_half(self, "right")

        # main.py's OperatorProfileDialog already ran before this window
        # was even constructed, so operator_callsign/operator_lat/
        # operator_lon are normally already set by the time either of
        # these runs -- both are still kept as a fallback (a genuinely
        # first-ever run where that dialog was cancelled with nothing
        # saved yet, or an old QSettings file from before profiles
        # existed at all). Re-read after the operator reopens the
        # Profile dialog later too (_on_profile_button_clicked), in
        # case it changed.
        self._ensure_operator_callsign()
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
        self.local_label = QLabel("--:--:-- Local")
        self.sfi_label = QLabel("SFI: --")
        self.ssn_label = QLabel("SSN: --")
        self.k_index_label = QLabel("K-index: --")
        # Same size/weight for all five clocks_row labels (previously
        # the clocks were 20px and SFI/SSN/K-index 16px) -- per
        # explicit instruction, one consistent size across the row.
        for label in (self.utc_label, self.local_label, self.sfi_label, self.ssn_label, self.k_index_label):
            label.setStyleSheet("font-size: 18px; font-weight: bold;")

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
        # No setFixedHeight here any more -- this now lives inside
        # band_data_tabs (below), which owns the overall fixed height
        # for the whole tabbed area so every tab (this one, Contests,
        # DX Spots, and whatever's added later) shares one consistent
        # size regardless of which is active.
        for row in range(len(BAND_CONDITION_RANGES)):
            for col in range(2):
                item = QTableWidgetItem("--")
                item.setTextAlignment(Qt.AlignCenter)
                self.band_conditions_table.setItem(row, col, item)

        # ---- Contests tab ----
        # Auto-fetched once at startup (see the end of __init__, same
        # "just load it, no explicit opt-in toggle" treatment as Solar
        # Data) -- this is read-only reference data, not a live overlay
        # or a persistent connection sending anything to a third party,
        # so it doesn't need the same explicit-action gating as the
        # map's PSKReporter/POTA buttons or the DX Spots tab below.
        self._contests_cache = []  # every event fetched (years of history) -- see _build_contest_rows
        self._contests_worker = None
        self.contests_table = QTableWidget(0, 3)
        self.contests_table.setHorizontalHeaderLabels(["Contest", "Start (UTC)", "End (UTC)"])
        self.contests_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.contests_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.contests_table.setSelectionMode(QTableWidget.NoSelection)
        self.contests_table.verticalHeader().setVisible(False)
        self.contests_table.cellDoubleClicked.connect(self._on_contest_row_double_clicked)
        self.contests_status_label = QLabel("Loading contest calendar...")
        self.contests_status_label.setStyleSheet("color: #888; font-size: 11px;")
        self.contests_refresh_button = QPushButton("Refresh")
        self.contests_refresh_button.clicked.connect(self._start_contests_fetch)
        contests_header_row = QHBoxLayout()
        contests_header_row.addWidget(self.contests_status_label, 1)
        contests_header_row.addWidget(self.contests_refresh_button)
        contests_tab = QWidget()
        contests_tab_layout = QVBoxLayout()
        contests_tab_layout.setContentsMargins(4, 4, 4, 4)
        contests_tab_layout.addLayout(contests_header_row)
        contests_tab_layout.addWidget(self.contests_table)
        contests_tab.setLayout(contests_tab_layout)

        # ---- DX Spots tab ----
        # Unlike Contests, this is a PERSISTENT connection that sends
        # the operator's own callsign to a third-party server the
        # moment it connects -- explicit opt-in (Connect button),
        # never auto-started, same reasoning as every other network-
        # connecting feature in this app (rigctld, Virtual Cables,
        # WSJT-X Auto-Log, PSKReporter, POTA).
        self._dx_cluster_client = None
        # Local QSettings instance here rather than the shared
        # `settings` variable used further down in __init__ (e.g. by
        # the QSO map/PSKReporter setup) -- this block runs earlier in
        # construction, before that variable exists yet.
        _dx_settings = QSettings("IcomRadioApp", "RadioControl")
        self.dx_host_input = QLineEdit(
            _dx_settings.value("dx_cluster_host", dxcluster.DEFAULT_HOST)
        )
        self.dx_host_input.setToolTip(
            "Any AK1A-compatible DX cluster server -- there's no single "
            "\"correct\" one; different servers carry different mixes of "
            "automated skimmer spots vs. human-submitted ones."
        )
        self.dx_port_input = QLineEdit(
            str(_dx_settings.value("dx_cluster_port", dxcluster.DEFAULT_PORT))
        )
        self.dx_port_input.setFixedWidth(60)
        self.dx_connect_button = QPushButton("Connect")
        self.dx_connect_button.setCheckable(True)
        self.dx_connect_button.toggled.connect(self._on_dx_connect_toggled)
        self.dx_status_label = QLabel("Disconnected")
        self.dx_status_label.setStyleSheet("color: #888; font-size: 11px;")
        dx_controls_row = QHBoxLayout()
        dx_controls_row.addWidget(QLabel("Host:"))
        dx_controls_row.addWidget(self.dx_host_input, 1)
        dx_controls_row.addWidget(QLabel("Port:"))
        dx_controls_row.addWidget(self.dx_port_input)
        dx_controls_row.addWidget(self.dx_connect_button)
        self.dx_spots_table = QTableWidget(0, 6)
        self.dx_spots_table.setHorizontalHeaderLabels(["Time", "Band", "Freq (MHz)", "DX Call", "Spotter", "Comment"])
        self.dx_spots_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.dx_spots_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.dx_spots_table.setSelectionMode(QTableWidget.NoSelection)
        self.dx_spots_table.verticalHeader().setVisible(False)
        dx_spots_tab = QWidget()
        dx_spots_tab_layout = QVBoxLayout()
        dx_spots_tab_layout.setContentsMargins(4, 4, 4, 4)
        dx_spots_tab_layout.addLayout(dx_controls_row)
        dx_spots_tab_layout.addWidget(self.dx_status_label)
        dx_spots_tab_layout.addWidget(self.dx_spots_table)
        dx_spots_tab.setLayout(dx_spots_tab_layout)

        # ---- Parks tab -- searchable directory of every POTA park (not
        # just currently-active spots, unlike the map's own POTA button/
        # overlay above, which stays a separate, still-live thing).
        # There's no "all parks on Earth" endpoint (see pota.py's own
        # module docstring) -- parks_program_combo scopes the fetch to
        # one country/region ("program", POTA's own term) at a time,
        # remembered across restarts via QSettings so re-opening the
        # dashboard doesn't require re-picking it every time. Populated
        # once at startup (see the end of __init__), same auto-load
        # treatment as Contests.
        self._parks_all = []  # every park in the CURRENTLY LOADED program, unfiltered -- see _apply_parks_filter
        self._parks_radius_km = None  # None = no radius filter active -- see _on_parks_filter_within_radius
        self._parks_programs_worker = None
        self._parks_worker = None
        self._park_details_worker = None
        self._park_details_dialog = None
        self._park_details_dialog_reference = None  # which park the currently-open dialog is for
        self._selected_park = None  # {"lat","lon"} of the last-clicked row, shown on the map only while the Parks tab is active
        self.parks_program_combo = QComboBox()
        self.parks_program_combo.setToolTip(
            "POTA has no single \"all parks\" list -- pick a country/region "
            "(POTA's own \"program\") to load its park directory."
        )
        self.parks_program_combo.currentIndexChanged.connect(self._on_parks_program_changed)
        self.parks_search_edit = QLineEdit()
        self.parks_search_edit.setPlaceholderText("Search by name or reference...")
        self.parks_search_edit.textChanged.connect(self._on_parks_search_changed)
        self._parks_search_debounce = QTimer(self)
        self._parks_search_debounce.setSingleShot(True)
        self._parks_search_debounce.timeout.connect(self._apply_parks_filter)
        self.parks_filter_button = QPushButton("Filter...")
        self.parks_filter_button.setToolTip("Filter to parks within a chosen radius of your saved GPS location.")
        self.parks_filter_button.clicked.connect(self._on_parks_filter_button_clicked)
        parks_controls_row = QHBoxLayout()
        parks_controls_row.addWidget(self.parks_program_combo)
        parks_controls_row.addWidget(self.parks_search_edit, 1)
        parks_controls_row.addWidget(self.parks_filter_button)
        self.parks_status_label = QLabel("Loading program list...")
        self.parks_status_label.setStyleSheet("color: #888; font-size: 11px;")
        self.parks_table = QTableWidget(0, 4)
        self.parks_table.setHorizontalHeaderLabels(["Reference", "Name", "Location", "Activations"])
        self.parks_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.parks_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.parks_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.parks_table.verticalHeader().setVisible(False)
        self.parks_table.cellClicked.connect(self._on_park_row_clicked)
        self.parks_table.cellDoubleClicked.connect(self._on_park_row_double_clicked)
        # Kept as an attribute (not a local) -- _on_band_data_tab_changed
        # (wired below, once band_data_tabs itself exists) needs to
        # identify this specific tab by object identity to know when to
        # show/hide the map's selected-park marker.
        self.parks_tab = QWidget()
        parks_tab_layout = QVBoxLayout()
        parks_tab_layout.setContentsMargins(4, 4, 4, 4)
        parks_tab_layout.addLayout(parks_controls_row)
        parks_tab_layout.addWidget(self.parks_status_label)
        parks_tab_layout.addWidget(self.parks_table)
        self.parks_tab.setLayout(parks_tab_layout)

        # ---- Tabbed container -- per explicit instruction, replaces
        # the old single-purpose "HF Band Conditions" area with a
        # general one that can hold whatever other data sets get added
        # later (Contests and DX Spots today, more later). Fixed height
        # roughly matching upcoming_passes_table's own so the row this
        # sits in (see bottom_row, below) stays visually balanced
        # regardless of which tab is active.
        self.band_data_tabs = QTabWidget()
        self.band_data_tabs.addTab(self.band_conditions_table, "Band Conditions")
        self.band_data_tabs.addTab(contests_tab, "Contests")
        self.band_data_tabs.addTab(dx_spots_tab, "DX Spots")
        self.band_data_tabs.addTab(self.parks_tab, "Parks")
        self.band_data_tabs.currentChanged.connect(self._on_band_data_tab_changed)
        # upcoming_passes_table (built further down in __init__, same
        # row as this) isn't constructed yet at this point, so this
        # doesn't reference it -- just uses the same row-count/height
        # shape its own fixed-height calculation does (PASSES_
        # VISIBLE_ROWS rows worth), plus room for the tab bar itself
        # and the extra host/port/connect controls row the DX Spots
        # tab has that the other two don't (Parks' own extra controls
        # row + status label is the same shape/height, so no separate
        # allowance needed for it).
        self.band_data_tabs.setFixedHeight(
            self.band_conditions_table.horizontalHeader().height()
            + self.PASSES_VISIBLE_ROWS * 24 + 4  # table content, matching upcoming_passes_table's own row count
            + 34   # tab bar
            + 44   # DX Spots tab's extra controls/status rows
        )

        # Upcoming passes: the next PASSES_DISPLAY_COUNT AOS/LOS windows
        # across every satellite currently checked to display on the map
        # (same "selected" scope as the map itself), soonest first. Row
        # count grows/shrinks with however many passes were actually
        # found (never more than PASSES_DISPLAY_COUNT) rather than
        # padding out to a fixed 10 with blank rows -- beyond
        # PASSES_VISIBLE_ROWS' fixed height, the extra rows just
        # scroll rather than growing the table/window further.
        self.upcoming_passes_table = QTableWidget(0, 4)
        self.upcoming_passes_table.setHorizontalHeaderLabels(["Satellite", "Status", "Max El", "Duration"])
        self.upcoming_passes_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.upcoming_passes_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.upcoming_passes_table.setSelectionMode(QTableWidget.NoSelection)
        self.upcoming_passes_table.verticalHeader().setVisible(False)
        self.upcoming_passes_table.setFixedHeight(
            self.upcoming_passes_table.horizontalHeader().height()
            + self.PASSES_VISIBLE_ROWS * 24 + 4
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

        # Filters out low, grazing passes (max elevation below this)
        # from the list above -- per explicit instruction. 0 (default)
        # means no filtering, same as before this control existed.
        self.min_elevation_spin = QSpinBox()
        self.min_elevation_spin.setRange(0, 90)
        self.min_elevation_spin.setValue(0)
        self.min_elevation_spin.setSuffix("°")
        self.min_elevation_spin.setToolTip("Only show upcoming passes reaching at least this maximum elevation")
        self.min_elevation_spin.valueChanged.connect(self._on_min_elevation_changed)

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

        # ---- APRS station overlay ----
        # Keyed by source callsign (not appended per-packet) -- APRS
        # stations beacon repeatedly, so this keeps one marker per
        # station, updated in place to its latest reported position,
        # rather than piling up a duplicate dot per retransmit. Fed
        # live from every connected radio's RadioWindow.
        # aprs_packet_decoded signal (see _on_connect_radio_clicked),
        # not fetched -- unlike PSKReporter/POTA there's no polling
        # worker here, just an always-listening accumulator; the
        # toggle only controls whether it's DRAWN, not whether it's
        # collected.
        self._aprs_stations = {}  # {source_callsign: {"lat", "lon", "tooltip"}}
        self.aprs_button = QPushButton("APRS: OFF")
        self.aprs_button.setCheckable(True)
        self.aprs_button.setStyleSheet(
            "QPushButton:checked { background-color: #c33; color: white; font-weight: bold; }"
        )
        self.aprs_button.setToolTip(
            "Toggle showing APRS station positions (from any connected "
            "radio's APRS Tool decoder) on the map, in red. Hover a "
            "marker for the packet's details."
        )
        self.aprs_button.toggled.connect(self._on_aprs_toggled)

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

        # ---- Connected radios (list + Connect New Radio... button
        # live in RadiosDialog, opened via self.radios_button below --
        # still constructed/owned here since every method that reads/
        # mutates them (e.g. _on_connect_radio_clicked, _on_radio_
        # window_closed) is a HamClockWindow method, unchanged) ----
        self.connect_radio_button = QPushButton("Connect New Radio...")
        self.connect_radio_button.setToolTip(
            "Opens the connection dialog for another radio -- pick its "
            "satellite role there (Full Duplex / Downlink / Uplink / "
            "Non-Sat). Each connected radio gets its own window."
        )
        self.connect_radio_button.clicked.connect(self._on_connect_radio_clicked)

        self.connected_radios_list = QListWidget()
        self.connected_radios_list.setSelectionMode(QListWidget.SingleSelection)
        self.connected_radios_list.setToolTip(
            "Double-click to bring that radio's window to the front. Select a radio, then use "
            "Rigctld/Virtual Cables below to configure that service for it -- each radio's own "
            "status (rigctld port if running, RX/TX if part of the virtual cables) shows in its row."
        )
        self.connected_radios_list.itemDoubleClicked.connect(self._on_connected_radio_item_double_clicked)
        self.connected_radios_list.itemSelectionChanged.connect(self._update_radio_service_buttons_enabled)

        # ---- Virtual Cables (moved from RadioWindow -- opens a dialog
        # scoped to whichever radio is currently selected in
        # connected_radios_list, per explicit instruction. RX and TX
        # are independent per-radio checkboxes in that dialog (not
        # picked from two combo boxes here anymore), so a "poor man's
        # full duplex" setup -- one radio for RX, a different one for
        # TX -- is still fully supported, just configured one radio's
        # dialog at a time instead of both from one shared dialog.) ----
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

        self.virtual_cable_button = QPushButton("Virtual Cables...")
        self.virtual_cable_button.setEnabled(False)
        self.virtual_cable_button.setToolTip(
            "Configure virtual audio devices (Linux/PulseAudio or PipeWire "
            "only) for the SELECTED radio above, so an external app (WSJT-X, "
            "etc.) can send/receive audio through it -- RX and TX can be "
            "different radios, e.g. decoding one radio's downlink while "
            "transmitting through a separate uplink radio (configure each "
            "radio's direction from its own selection)."
        )
        self.virtual_cable_button.clicked.connect(self._on_virtual_cable_button_clicked)

        # ---- Rigctld (moved from RadioWindow -- opens a dialog scoped
        # to whichever radio is currently selected in
        # connected_radios_list, per explicit instruction. rigctld is
        # inherently one-radio-per-port, so a real multi-radio setup
        # (e.g. WSJT-X reaching both an uplink and a downlink radio)
        # still needs a separate server, on a separate port, per
        # radio -- just configured one radio's dialog at a time now.) ----
        self._rigctld_servers = {}  # RadioWindow -> running RigctldServer
        self._rigctld_ports = {}    # RadioWindow -> remembered port (kept even while stopped)

        self.rigctld_button = QPushButton("Rigctld...")
        self.rigctld_button.setEnabled(False)
        self.rigctld_button.setToolTip(
            "Configure a rigctld server for the SELECTED radio above so "
            "other CAT-aware apps (WSJT-X, JTDX, fldigi, ...) can control "
            "it -- select \"Hamlib NET rigctl\" (rig model 2) and "
            "127.0.0.1:<port> in that app. Each radio needs its own port."
        )
        self.rigctld_button.clicked.connect(self._on_rigctld_button_clicked)

        # Reopens the same startup operator-profile dialog (callsign +
        # location, save/load/delete by name) -- see operator_profile.py's
        # own docstring for why this lives here instead of the radio
        # connection dialog. Useful for a shared club station switching
        # operators, or one operator switching between a home and
        # portable/POTA location, without reconnecting any radio.
        self.profile_button = QPushButton("Profile...")
        self.profile_button.setToolTip("Set your callsign and location, or switch to a saved profile.")
        self.profile_button.clicked.connect(self._on_profile_button_clicked)

        # Singleton, non-modal -- see RadiosDialog's own docstring.
        self.radios_dialog = RadiosDialog(self)
        self.radios_button = QPushButton("Radios")
        self.radios_button.setToolTip("Connect a new radio, or bring an already-connected one's window to the front.")
        self.radios_button.clicked.connect(self._on_radios_button_clicked)

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

        # See updater.py's own docstring for the two ways this can
        # actually update things (a dev git checkout vs. an install.sh
        # install) and why neither needs a root/sudo prompt from here.
        self._update_check_worker = None
        self._update_perform_worker = None
        self.update_button = QPushButton("Check for Updates...")
        self.update_button.setToolTip("Checks GitHub for a newer version of TORCA, and can update in place.")
        self.update_button.clicked.connect(self._on_update_button_clicked)

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
        self.satellite_overlay_label.setStyleSheet("color: #ccc; font-size: 18px;")

        # ---- WSJT-X launch (moved from RadioWindow -- no radio
        # dependency at all, so this is just a relocation) ----
        self.wsjtx_button = QPushButton("Launch WSJT-X")
        self.wsjtx_button.setToolTip(
            f"Launches WSJT-X in its own isolated profile (--rig-name={WSJTX_RIG_NAME}), "
            "separate from your main WSJT-X settings. Use the Rigctld button below to "
            "let it control a radio."
        )
        self.wsjtx_button.clicked.connect(self._on_wsjtx_button_clicked)

        # ---- JS8Call launch -- same find/remember/browse-fallback
        # pattern as "Launch WSJT-X" above, own separate QSettings key
        # (js8call_executable_path) and own executable-search helpers
        # (find_js8call_executable/launch_js8call, wsjtx_rigctld.py) ----
        self.js8call_button = QPushButton("Launch JS8Call")
        self.js8call_button.setToolTip(
            "Launches JS8Call. Use the Rigctld button below to let it control a radio "
            "(JS8Call's own Settings > Radio, rig \"Hamlib NET rigctl\", 127.0.0.1:<port>)."
        )
        self.js8call_button.clicked.connect(self._on_js8call_button_clicked)

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

        # Per explicit instruction, both WSJT-X buttons moved here
        # (between Radios and Log Book) from their own row.
        top_buttons_row = QHBoxLayout()
        top_buttons_row.addWidget(self.profile_button)
        top_buttons_row.addWidget(self.radios_button)
        top_buttons_row.addWidget(self.wsjtx_button)
        top_buttons_row.addWidget(self.js8call_button)
        top_buttons_row.addWidget(self.wsjtx_autolog_button)
        top_buttons_row.addWidget(self.log_book_button)
        top_buttons_row.addWidget(self.new_qso_button)
        top_buttons_row.addWidget(self.update_button)
        top_buttons_row.addStretch()

        # Stretches BETWEEN each label (not before the first or after
        # the last) rather than one trailing addStretch() -- per
        # explicit instruction, spreads all five across the full
        # window width (first flush-left, last flush-right) instead of
        # clustering them on the left with empty space on the right.
        clocks_row = QHBoxLayout()
        clocks_row.addWidget(self.utc_label)
        clocks_row.addStretch()
        clocks_row.addWidget(self.local_label)
        clocks_row.addStretch()
        clocks_row.addWidget(self.sfi_label)
        clocks_row.addStretch()
        clocks_row.addWidget(self.ssn_label)
        clocks_row.addStretch()
        clocks_row.addWidget(self.k_index_label)

        map_buttons_row = QHBoxLayout()
        map_buttons_row.addWidget(self.satellite_button)
        map_buttons_row.addWidget(self.qso_map_button)
        map_buttons_row.addWidget(self.pskreporter_button)
        map_buttons_row.addWidget(self.pota_button)
        map_buttons_row.addWidget(self.aprs_button)
        map_buttons_row.addWidget(self.qso_band_filter_combo)
        map_buttons_row.addStretch()

        passes_header_row = QHBoxLayout()
        passes_header_row.addWidget(QLabel("Upcoming Satellite Passes:"))
        passes_header_row.addStretch()
        passes_header_row.addWidget(QLabel("Min El:"))
        passes_header_row.addWidget(self.min_elevation_spin)

        upcoming_passes_column = QVBoxLayout()
        upcoming_passes_column.addLayout(passes_header_row)
        upcoming_passes_column.addWidget(self.upcoming_passes_table)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self.band_data_tabs)
        bottom_row.addLayout(upcoming_passes_column)

        layout = QVBoxLayout()
        # clocks_row (UTC/local time plus SFI/SSN/K-index, per explicit
        # instruction) and map_buttons_row (satellite/QSO Map toggles,
        # in their own row) both above the map. Map itself still with
        # no stretch factor -- it claims only what its own (letterboxed,
        # aspect-correct) heightForWidth calls for, rather than
        # greedily taking all extra vertical space in the window (which
        # is what a stretch factor of 1 here used to do, and the only
        # thing keeping the map from severely distorting once
        # HamClockWindow started occupying a tall/narrow half-screen
        # region). Leaves more room below for the band-conditions/
        # passes rows -- and for whatever gets added there later.
        layout.addLayout(clocks_row)
        layout.addLayout(map_buttons_row)
        layout.addWidget(self.map_widget)
        layout.addLayout(top_buttons_row)
        layout.addLayout(tracking_row)
        layout.addWidget(self.satellite_overlay_label)
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

        # Recomputes the Contests tab's row coloring/filtering (see
        # _rebuild_contests_table) against the already-fetched cache --
        # no new network fetch -- so a contest's color (and eventual
        # drop-off the list once it ends) stays correct as time passes,
        # not just at the moment it was last fetched. Same 60s cadence
        # as the map terminator redraw above; contest start/end times
        # don't need finer than that.
        self._contests_recolor_timer = QTimer(self)
        self._contests_recolor_timer.timeout.connect(self._rebuild_contests_table)
        self._contests_recolor_timer.start(60_000)

        # Satellites move fast enough (LEO: ~7.8 km/s) that a much
        # shorter interval than the terminator's makes sense, without
        # being wasteful -- only actually runs while satellite mode is on.
        self._satellite_timer = QTimer(self)
        self._satellite_timer.timeout.connect(self._update_satellite_positions)

        # The actual pass search only reruns on this much coarser timer
        # -- see the module comment on PASSES_REFRESH_INTERVAL_MS for
        # why. Per explicit instruction, independent of the Satellites
        # map-overlay toggle -- loads immediately at startup and keeps
        # itself fresh the whole session regardless of whether that
        # button is ever turned on, rather than only starting once the
        # operator clicks it (_on_satellite_toggled no longer touches
        # this timer at all).
        self._passes_timer = QTimer(self)
        self._passes_timer.timeout.connect(self._refresh_upcoming_passes)
        self._refresh_upcoming_passes()
        self._passes_timer.start(PASSES_REFRESH_INTERVAL_MS)

        self.solar_worker = SolarDataWorker()
        self.solar_worker.data_updated.connect(self._on_solar_data)
        self.solar_worker.start()

        # Contests tab: loaded once at startup, same as Solar Data
        # above -- read-only reference data, no explicit opt-in needed
        # (unlike DX Spots' persistent connection).
        self._start_contests_fetch()

        # Parks tab: the program (country/region) list, same auto-load
        # treatment -- the actual park directory for whichever program
        # ends up selected is fetched separately (_on_parks_program_
        # changed), not here.
        self._start_parks_programs_fetch()

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
        """Reads operator_lat/lon/elevation from QSettings -- the same
        keys OperatorProfileDialog reads/writes on every accept -- so
        the map marker and Doppler correction have a location without
        needing a radio connected first. Called once at startup and
        again whenever the operator reopens the Profile dialog
        (_on_profile_button_clicked), in case it changed."""
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
        else:
            self._satellite_timer.stop()

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
            # Upcoming Passes is independent of the Satellites map-
            # overlay toggle now, so this always refreshes it -- only
            # the map's own marker positions stay gated behind that
            # button (no point updating them while the overlay is off).
            self._refresh_upcoming_passes()
            if self.satellite_button.isChecked():
                self._update_satellite_positions()

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

    def _on_aprs_toggled(self, checked):
        self.aprs_button.setText("APRS: ON" if checked else "APRS: OFF")
        self.map_widget.set_aprs_markers(list(self._aprs_stations.values()) if checked else [])

    def _on_aprs_packet_decoded(self, packet):
        """Connected to every RadioWindow's aprs_packet_decoded signal
        (see _on_connect_radio_clicked) -- fires for any connected
        radio's APRS decode, position reports only (RadioWindow/
        AprsToolWindow already filter to info["type"] == "position"
        before emitting). Always updates the accumulator so switching
        the button on later shows everything heard since startup, not
        just what arrived while it happened to be checked; only pushes
        to the map widget while actually visible.

        Keyed by the packet's ACTUAL originating station, not always
        packet["source"] -- for a Third-Party/Network-Tunneled packet
        (aprs.py's "}" unwrap), packet["source"] is the IGate/relay
        station's own AX.25 source, the SAME for every station it
        relays. Keying on that collided every relayed station onto one
        map entry, so each new station heard via a given IGate
        overwrote (and appeared to erase) whichever station's marker
        was there before. info["third_party_source"] (present only on
        unwrapped packets) is the real originating callsign instead."""
        info = packet["info"]
        station_key = info.get("third_party_source") or packet["source"]
        self._aprs_stations[station_key] = {
            "lat": info["lat"],
            "lon": info["lon"],
            "tooltip": self._aprs_tooltip_text(packet, info),
        }
        if self.aprs_button.isChecked():
            self.map_widget.set_aprs_markers(list(self._aprs_stations.values()))

    @staticmethod
    def _aprs_tooltip_text(packet, info):
        destination = packet.get("destination", "?")
        digipeaters = packet.get("digipeaters") or []
        if digipeaters:
            destination += " via " + ",".join(digipeaters)
        third_party_source = info.get("third_party_source")
        station_line = third_party_source or packet.get("source", "?")
        lines = [station_line]
        if third_party_source:
            # Distinguish the real originating station (used above) from
            # the IGate/relay station that actually transmitted this
            # copy of the packet -- both are useful, but conflating them
            # is what caused the marker-collision bug this key fixes.
            lines.append(f"via IGate {packet.get('source', '?')}")
        lines.append(f"To: {destination}")
        lines.append(f"{info['lat']:.5f}, {info['lon']:.5f}")
        symbol = f"{info.get('symbol_table', '')}{info.get('symbol_code', '')}".strip()
        if symbol:
            lines.append(f"Symbol: {symbol}")
        if info.get("comment"):
            lines.append(info["comment"])
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

    # ---- Parks tab ----

    def _start_parks_programs_fetch(self):
        if self._parks_programs_worker is not None and self._parks_programs_worker.isRunning():
            return
        worker = PotaProgramsWorker(self)
        worker.programs_ready.connect(self._on_parks_programs_ready)
        worker.failed.connect(self._on_parks_programs_failed)
        self._parks_programs_worker = worker
        worker.start()

    def _on_parks_programs_ready(self, programs):
        # blockSignals around the whole populate loop -- Qt fires
        # currentIndexChanged the moment the FIRST item lands in a
        # previously-empty combo (currentIndex jumps from -1 to 0), and
        # that would otherwise race _on_parks_program_changed against
        # this method's own restore-last-used-program logic below,
        # firing a fetch for whatever program happens to be
        # alphabetically first before the real target is known.
        self.parks_program_combo.blockSignals(True)
        self.parks_program_combo.clear()
        for program in programs:
            self.parks_program_combo.addItem(program["name"], program["prefix"])
        self.parks_program_combo.blockSignals(False)

        # Restore the last-used program (if any) and load it -- QSettings
        # here (not the shared `settings` variable used elsewhere in
        # __init__) since this runs from a signal handler, well after
        # __init__ has returned, same reasoning dx_host_input's own
        # local QSettings instance documents for itself.
        settings = QSettings("IcomRadioApp", "RadioControl")
        last_prefix = settings.value("pota_parks_program", "") or None
        if last_prefix:
            index = self.parks_program_combo.findData(last_prefix)
            if index != -1:
                self.parks_program_combo.setCurrentIndex(index)  # triggers _on_parks_program_changed
                return
        # No remembered program -- blockSignals above means Qt's own
        # currentIndexChanged never fired for the index-0 default it
        # picked, so nothing has actually been fetched yet even though
        # the combo shows a program name selected (confirmed live: this
        # previously left the tab looking like a program was loaded
        # while self._parks_all stayed empty, making the radius filter
        # silently show zero results with no indication why). Trigger
        # the fetch explicitly for whatever ended up selected.
        self._on_parks_program_changed(self.parks_program_combo.currentIndex())

    def _on_parks_programs_failed(self, message):
        self.parks_status_label.setText(f"Couldn't load program list: {message}")

    def _on_parks_program_changed(self, _index):
        program_prefix = self.parks_program_combo.currentData()
        if not program_prefix:
            return
        QSettings("IcomRadioApp", "RadioControl").setValue("pota_parks_program", program_prefix)
        self._start_parks_fetch(program_prefix)

    def _start_parks_fetch(self, program_prefix):
        if self._parks_worker is not None and self._parks_worker.isRunning():
            return  # a fetch is already in flight -- let it finish
        self.parks_status_label.setText(f"Loading parks for {self.parks_program_combo.currentText()}...")
        self.parks_table.setRowCount(0)
        self._parks_all = []
        worker = ParksWorker(program_prefix, self)
        worker.parks_ready.connect(self._on_parks_ready)
        worker.failed.connect(self._on_parks_fetch_failed)
        self._parks_worker = worker
        worker.start()

    def _on_parks_ready(self, parks, from_cache):
        self._parks_all = parks
        source = "cached" if from_cache else "freshly fetched"
        self.parks_status_label.setText(f"{len(parks)} parks loaded ({source}).")
        self._apply_parks_filter()

    def _on_parks_fetch_failed(self, message):
        self.parks_status_label.setText(f"Couldn't load parks: {message}")

    def _on_parks_search_changed(self, _text):
        # Debounced -- rebuilding the table (potentially thousands of
        # rows for a big program) on every single keystroke would be
        # wasteful; 250ms comfortably absorbs normal typing speed
        # without feeling laggy once it does fire.
        self._parks_search_debounce.start(250)

    def _on_parks_filter_button_clicked(self):
        menu = QMenu(self)
        menu.addAction("Within radius of my location...").triggered.connect(self._on_parks_filter_within_radius)
        clear_action = menu.addAction("Clear radius filter")
        clear_action.setEnabled(self._parks_radius_km is not None)
        clear_action.triggered.connect(self._on_parks_clear_radius_filter)
        menu.exec(self.parks_filter_button.mapToGlobal(self.parks_filter_button.rect().bottomLeft()))

    def _on_parks_filter_within_radius(self):
        settings = QSettings("IcomRadioApp", "RadioControl")
        lat = float(settings.value("operator_lat", 0.0)) or None
        lon = float(settings.value("operator_lon", 0.0)) or None
        if lat is None or lon is None:
            QMessageBox.warning(
                self, "Parks",
                "No GPS location saved yet -- set it in Profile... first "
                "(Get GPS Coordinates, or enter it manually).",
            )
            return
        radius_km, ok = QInputDialog.getInt(self, "Parks", "Search radius (km):", 50, 1, 20000)
        if not ok:
            return
        self._parks_radius_km = radius_km
        self._apply_parks_filter()

    def _on_parks_clear_radius_filter(self):
        self._parks_radius_km = None
        self._apply_parks_filter()

    def _apply_parks_filter(self):
        """Rebuilds parks_table from self._parks_all, filtered by the
        search text and (if active) the radius filter -- both filters
        run client-side against the already-fetched program (POTA's
        park-directory API has neither a search nor a radius
        parameter). setUpdatesEnabled(False) around the rebuild avoids
        a visible flicker/redraw-per-row on a large program (the US
        alone is ~13,000 parks)."""
        search_text = self.parks_search_edit.text().strip().lower()
        parks = self._parks_all
        if search_text:
            parks = [
                p for p in parks
                if search_text in p["name"].lower() or search_text in p["reference"].lower()
            ]
        if self._parks_radius_km is not None:
            settings = QSettings("IcomRadioApp", "RadioControl")
            lat = float(settings.value("operator_lat", 0.0)) or None
            lon = float(settings.value("operator_lon", 0.0)) or None
            if lat is not None and lon is not None:
                parks = [
                    p for p in parks
                    if pota.haversine_km(lat, lon, p["lat"], p["lon"]) <= self._parks_radius_km
                ]

        self.parks_table.setUpdatesEnabled(False)
        self.parks_table.setRowCount(len(parks))
        for row, park in enumerate(parks):
            values = [
                park["reference"],
                park["name"],
                park.get("location_desc") or "",
                str(park["activations"]) if park.get("activations") is not None else "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, park)
                self.parks_table.setItem(row, col, item)
        self.parks_table.setUpdatesEnabled(True)

        if self._parks_radius_km is not None or search_text:
            message = f"{len(parks)} of {len(self._parks_all)} parks shown."
            if self._parks_radius_km is not None and len(parks) == 0 and self._parks_all:
                # The radius filter only ever searches the CURRENTLY
                # LOADED program's own park list (POTA's API has no
                # cross-country radius search) -- zero results here
                # usually means the wrong Program is loaded for the
                # operator's actual location, not that there's truly
                # nothing nearby. Spelled out explicitly since this is
                # easy to misread as "no parks near me at all."
                message += (
                    f" No parks within {self._parks_radius_km}km in "
                    f"{self.parks_program_combo.currentText()} -- pick a different "
                    "Program if that's not where you are."
                )
            self.parks_status_label.setText(message)
        else:
            self.parks_status_label.setText(f"{len(parks)} parks loaded.")

    def _on_park_row_clicked(self, row, _column):
        item = self.parks_table.item(row, 0)
        if item is None:
            return
        park = item.data(Qt.UserRole)
        self._selected_park = {"lat": park["lat"], "lon": park["lon"]}
        self.map_widget.set_selected_park_marker(self._selected_park)
        self.map_widget.center_on(park["lat"], park["lon"])

    def _on_band_data_tab_changed(self, index):
        # The park marker is only meaningful while the Parks tab is
        # actually the one on screen -- hide it the moment the operator
        # switches to Band Conditions/Contests/DX Spots so it doesn't
        # keep sitting on the map for a tab that's no longer showing
        # which park it refers to, and restore it if they switch back.
        on_parks_tab = self.band_data_tabs.widget(index) is self.parks_tab
        self.map_widget.set_selected_park_marker(self._selected_park if on_parks_tab else None)

    def _on_park_row_double_clicked(self, row, _column):
        item = self.parks_table.item(row, 0)
        if item is None:
            return
        park = item.data(Qt.UserRole)
        dialog = ParkDetailsDialog(park, self)
        self._park_details_dialog = dialog
        self._park_details_dialog_reference = park["reference"]
        dialog.finished.connect(self._on_park_details_dialog_finished)
        dialog.show()

        # If an earlier double-click's fetch is still in flight, this
        # just replaces the tracked reference -- the old worker keeps
        # running to completion in the background rather than being
        # cancelled (this app has no in-flight-HTTP-cancellation idiom;
        # see map_tiles.py's own docstring for the same limitation), but
        # _on_park_details_ready's reference check discards its result
        # harmlessly once it does land, since _park_details_dialog_
        # reference will have moved on to this new park by then.
        worker = ParkDetailsWorker(park["reference"], self)
        worker.details_ready.connect(self._on_park_details_ready)
        worker.failed.connect(self._on_park_details_failed)
        self._park_details_worker = worker
        worker.start()

    def _on_park_details_dialog_finished(self, _result):
        self._park_details_dialog = None
        self._park_details_dialog_reference = None

    def _on_park_details_ready(self, details):
        # Only apply if the dialog that requested this is STILL the one
        # open (the operator could have closed it, or double-clicked a
        # different row, while the fetch was in flight).
        if self._park_details_dialog is not None and self._park_details_dialog_reference == details["reference"]:
            self._park_details_dialog.apply_details(details)

    def _on_park_details_failed(self, message):
        if self._park_details_dialog is not None:
            self._park_details_dialog.apply_details_failed(message)

    def _start_contests_fetch(self):
        if self._contests_worker is not None and self._contests_worker.isRunning():
            return  # a fetch is already in flight -- let it finish
        self.contests_status_label.setText("Loading contest calendar...")
        worker = ContestsWorker(self)
        worker.events_ready.connect(self._on_contests_ready)
        worker.failed.connect(self._on_contests_failed)
        self._contests_worker = worker
        worker.start()

    def _on_contests_ready(self, events):
        self._contests_cache = events
        self._rebuild_contests_table()

    def _on_contests_failed(self, message):
        self.contests_status_label.setText(f"Couldn't load contest calendar: {message}")

    # Same green/yellow/red palette as _BAND_CONDITION_COLORS, reused
    # here for visual consistency across the app's two "status at a
    # glance" tables.
    _CONTEST_NOW_COLOR = QColor(40, 130, 60)       # in progress
    _CONTEST_SOON_COLOR = QColor(160, 140, 30)     # starts within 24h
    _CONTEST_LATER_COLOR = QColor(140, 50, 50)     # starts more than 24h out

    def _rebuild_contests_table(self):
        """Keeps only contests still in progress or yet to start
        (end time in the future), soonest-starting first -- the fetched
        feed itself spans years of history (see contests.py), which
        isn't what "current and upcoming" means here. Each row is
        color-coded by how soon it starts (or whether it's already
        started), per explicit instruction -- recomputed fresh every
        rebuild (not just when new data arrives) since a contest can
        cross from "starts in >24h" to "starts within 24h" to "in
        progress" without the underlying data ever changing."""
        now = datetime.datetime.now(datetime.timezone.utc)
        upcoming = sorted(
            (event for event in self._contests_cache if event["end"] >= now),
            key=lambda event: event["start"],
        )
        self.contests_table.setRowCount(len(upcoming))
        for row, event in enumerate(upcoming):
            if event["start"] <= now:
                color = self._CONTEST_NOW_COLOR
            elif event["start"] - now <= datetime.timedelta(hours=24):
                color = self._CONTEST_SOON_COLOR
            else:
                color = self._CONTEST_LATER_COLOR
            name_item = QTableWidgetItem(event["name"])
            name_item.setData(Qt.UserRole, event)  # retrieved by _on_contest_row_double_clicked
            start_item = QTableWidgetItem(event["start"].strftime("%Y-%m-%d %H:%M"))
            end_item = QTableWidgetItem(event["end"].strftime("%Y-%m-%d %H:%M"))
            for item in (name_item, start_item, end_item):
                item.setBackground(color)
                item.setForeground(QColor(255, 255, 255))
            self.contests_table.setItem(row, 0, name_item)
            self.contests_table.setItem(row, 1, start_item)
            self.contests_table.setItem(row, 2, end_item)
        self.contests_status_label.setText(
            f"{len(upcoming)} current/upcoming contest{'s' if len(upcoming) != 1 else ''}"
        )

    def _on_contest_row_double_clicked(self, row, column):
        # The event dict is stashed on column 0's item (see
        # _rebuild_contests_table) regardless of which column was
        # actually double-clicked.
        name_item = self.contests_table.item(row, 0)
        if name_item is None:
            return
        event = name_item.data(Qt.UserRole)
        if event is None:
            return
        dialog = ContestDetailsDialog(event, self)
        dialog.exec()

    def _on_dx_connect_toggled(self, checked):
        if checked:
            if not self._operator_callsign:
                self._ensure_operator_callsign()
                if not self._operator_callsign:
                    self.dx_connect_button.setChecked(False)
                    return
            host = self.dx_host_input.text().strip() or dxcluster.DEFAULT_HOST
            try:
                port = int(self.dx_port_input.text().strip())
            except ValueError:
                QMessageBox.warning(self, "DX Spots", "Port must be a number.")
                self.dx_connect_button.setChecked(False)
                return
            settings = QSettings("IcomRadioApp", "RadioControl")
            settings.setValue("dx_cluster_host", host)
            settings.setValue("dx_cluster_port", port)

            client = dxcluster.DxClusterClient(self._operator_callsign, host, port, self)
            client.connected.connect(self._on_dx_connected)
            client.disconnected.connect(self._on_dx_disconnected)
            client.error.connect(self._on_dx_error)
            client.spot_received.connect(self._on_dx_spot_received)
            self._dx_cluster_client = client
            self.dx_status_label.setText(f"Connecting to {host}:{port}...")
            self.dx_connect_button.setText("Disconnect")
            self.dx_host_input.setEnabled(False)
            self.dx_port_input.setEnabled(False)
            client.start()
        else:
            if self._dx_cluster_client is not None:
                self._dx_cluster_client.stop()
                self._dx_cluster_client = None
            self.dx_status_label.setText("Disconnected")
            self.dx_connect_button.setText("Connect")
            self.dx_host_input.setEnabled(True)
            self.dx_port_input.setEnabled(True)

    def _on_dx_connected(self):
        self.dx_status_label.setText(f"Connected as {self._operator_callsign}")

    def _on_dx_disconnected(self):
        # Also fires after an explicit Disconnect click (harmless,
        # redundant with _on_dx_connect_toggled's own status update
        # above) as well as an unexpected drop -- only the checked
        # state actually distinguishes those, and setChecked(False)
        # here is a no-op if it's already unchecked.
        self.dx_connect_button.setChecked(False)

    def _on_dx_error(self, message):
        self.dx_status_label.setText(f"Connection error: {message}")

    # Hard cap on the DX Spots table's row count -- this is a live,
    # continuously-arriving feed (unlike the map overlays' one-shot
    # fetches), so without a cap a long session would grow the table
    # without bound.
    _DX_SPOTS_MAX_ROWS = 200

    def _on_dx_spot_received(self, spot):
        self.dx_spots_table.insertRow(0)  # newest at the top
        self.dx_spots_table.setItem(0, 0, QTableWidgetItem(spot["time"]))
        self.dx_spots_table.setItem(0, 1, QTableWidgetItem(spot["band"]))
        self.dx_spots_table.setItem(0, 2, QTableWidgetItem(f"{spot['frequency_hz'] / 1e6:.4f}"))
        self.dx_spots_table.setItem(0, 3, QTableWidgetItem(spot["dx_call"]))
        self.dx_spots_table.setItem(0, 4, QTableWidgetItem(spot["spotter"]))
        self.dx_spots_table.setItem(0, 5, QTableWidgetItem(spot["comment"]))
        while self.dx_spots_table.rowCount() > self._DX_SPOTS_MAX_ROWS:
            self.dx_spots_table.removeRow(self.dx_spots_table.rowCount() - 1)

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

    # Shared red/yellow/green "traffic light" palette for the passes
    # table -- per explicit instruction: used both for the Status
    # column's AOS/LOS countdown color and the Max El column's
    # elevation color below.
    _PASS_RED = QColor(220, 60, 60)
    _PASS_YELLOW = QColor(210, 180, 30)
    _PASS_GREEN = QColor(60, 190, 90)

    @classmethod
    def _elevation_color(cls, elevation_deg):
        if elevation_deg < 10:
            return cls._PASS_RED
        if elevation_deg < 30:
            return cls._PASS_YELLOW
        return cls._PASS_GREEN

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
                count=self.PASSES_DISPLAY_COUNT, min_elevation_deg=self.min_elevation_spin.value(),
            )

        self.upcoming_passes_table.setRowCount(len(self._upcoming_passes))
        for row, pass_info in enumerate(self._upcoming_passes):
            name_item = QTableWidgetItem(pass_info["name"])
            self.upcoming_passes_table.setItem(row, 0, name_item)
            for col in (1, 2, 3):
                item = QTableWidgetItem("")
                item.setTextAlignment(Qt.AlignCenter)
                self.upcoming_passes_table.setItem(row, col, item)
            max_el = pass_info["max_elevation_deg"]
            el_item = self.upcoming_passes_table.item(row, 2)
            el_item.setText(f"{max_el:.0f}°")
            el_item.setForeground(self._elevation_color(max_el))
            self.upcoming_passes_table.item(row, 3).setText(format_countdown(pass_info["duration_seconds"]))
        self._update_passes_countdowns()

    def _on_min_elevation_changed(self, value):
        self._refresh_upcoming_passes()

    def _update_passes_countdowns(self):
        """Refreshes the "Status" column's text from the cached
        self._upcoming_passes -- plain subtraction against already-
        known absolute times, no orbital math, cheap enough to run on
        the 1-second clock timer even though the underlying search
        only reruns every few minutes. Before AOS, shows an up arrow
        (rising) with the countdown to AOS; once the pass has started
        (AOS has passed but LOS hasn't), switches to a down arrow
        (setting) with the countdown to LOS instead -- per explicit
        instruction, colored yellow while counting down to AOS and
        green while counting down to LOS (same red/yellow/green
        palette as the Max El column, see _PASS_YELLOW/_PASS_GREEN).
        Derived from a live now-vs-aos/los comparison rather than the
        "active" flag set when the pass was found, so a pass that
        starts (or ends) between full searches is reflected correctly
        here too, not just at the next 5-minute refresh."""
        if not self._upcoming_passes:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        for row, pass_info in enumerate(self._upcoming_passes):
            if now < pass_info["aos_time"]:
                text = f"↑{format_countdown((pass_info['aos_time'] - now).total_seconds())}"
                color = self._PASS_YELLOW
            elif now <= pass_info["los_time"]:
                text = f"↓{format_countdown((pass_info['los_time'] - now).total_seconds())}"
                color = self._PASS_GREEN
            else:
                text = "passed"  # stale -- _refresh_upcoming_passes will drop it at the next full search
                color = None
            item = self.upcoming_passes_table.item(row, 1)
            if item is not None:
                item.setText(text)
                if color is not None:
                    item.setForeground(color)
                else:
                    item.setData(Qt.ForegroundRole, None)  # back to the table's default text color

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
                "Set your location in Profile... first "
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
        for key, message in errors.items():
            print(f"[ERROR] Ham Dashboard: {key}: {message}")

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

    def _on_radios_button_clicked(self):
        self.radios_dialog.show()
        self.radios_dialog.raise_()
        self.radios_dialog.activateWindow()

    def _on_profile_button_clicked(self):
        OperatorProfileDialog.run(self)
        # Cheap either way (accepted or cancelled) -- just re-reads
        # QSettings and re-pushes to whatever already consumes it, same
        # "always just re-read" approach _load_observer_location's own
        # docstring already takes. Refreshes self._operator_callsign
        # too, in case the profile dialog changed it (a plain re-read
        # here is simpler than threading a signal out of a dialog whose
        # whole job is a QSettings side effect -- see its own docstring).
        settings = QSettings("IcomRadioApp", "RadioControl")
        self._operator_callsign = settings.value("operator_callsign", "") or ""
        self._load_observer_location()
        self.satellite_session.set_observer_location(
            self._observer_lat, self._observer_lon, self._observer_elevation_m
        )
        if self._observer_lat is not None and self._observer_lon is not None:
            self.map_widget.set_operator_location(
                self._observer_lat, self._observer_lon,
                f"{self._observer_lat:.3f}, {self._observer_lon:.3f}",
            )

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

        window = RadioWindow(details, self.satellite_session)
        window.closed.connect(self._on_radio_window_closed)
        window.aprs_packet_decoded.connect(self._on_aprs_packet_decoded)
        self._connected_radios.append(window)

        item = QListWidgetItem(self._radio_list_base_label(details))
        item.setData(Qt.UserRole, window)
        self.connected_radios_list.addItem(item)

        self._update_radio_service_buttons_enabled()
        window.show()

    @staticmethod
    def _radio_list_base_label(details):
        role_label = _ROLE_LABELS.get(details.get("role", "non_sat"), details.get("role", "non_sat"))
        connection_label = details.get("host") or details.get("serial_port") or details.get("remote_host") or "?"
        return f"{details['radio_model']} ({role_label}) -- {connection_label}"

    def _selected_radio_window(self):
        items = self.connected_radios_list.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.UserRole)

    def _radio_status_suffix(self, window):
        """Rigctld/Virtual Cables status for one radio's row in
        connected_radios_list -- per explicit instruction, each row
        shows its own live status directly rather than needing the
        Rigctld/Virtual Cables dialogs opened to check."""
        parts = []
        if window in self._rigctld_servers:
            parts.append(f"Rigctld:{self._rigctld_ports.get(window, '?')}")
        vc_roles = []
        if window is self._virtual_cable_rx_window:
            vc_roles.append("RX")
        if window is self._virtual_cable_tx_window:
            vc_roles.append("TX")
        if vc_roles:
            parts.append(f"VC:{'+'.join(vc_roles)}")
        return f"  [{', '.join(parts)}]" if parts else ""

    def _refresh_connected_radios_list_status(self):
        for row in range(self.connected_radios_list.count()):
            item = self.connected_radios_list.item(row)
            window = item.data(Qt.UserRole)
            if window is None:
                continue
            base = self._radio_list_base_label(window._details)
            item.setText(base + self._radio_status_suffix(window))

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

    def _on_update_button_clicked(self):
        if self._update_check_worker is not None and self._update_check_worker.isRunning():
            return
        if self._update_perform_worker is not None and self._update_perform_worker.isRunning():
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("Checking...")
        worker = UpdateCheckWorker(self)
        worker.result_ready.connect(self._on_update_check_ready)
        worker.failed.connect(self._on_update_check_failed)
        self._update_check_worker = worker
        worker.start()

    def _on_update_check_ready(self, result):
        self.update_button.setEnabled(True)
        self.update_button.setText("Check for Updates...")

        if result.mode == updater.MODE_UNSUPPORTED:
            QMessageBox.information(
                self, "Check for Updates",
                "This installation can't be self-updated: its folder has no git checkout "
                "and isn't writable by the current user -- likely an install from before "
                "this feature existed, or one still owned by root.\n\n"
                "Reinstall with install.sh to enable in-app updates."
            )
            return

        if result.version_unknown:
            proceed = QMessageBox.question(
                self, "Check for Updates",
                "Couldn't determine the currently installed version (no .install_commit "
                f"marker found). Latest available: {result.latest_commit[:12]}.\n\n"
                "Update anyway?"
            )
            if proceed != QMessageBox.Yes:
                return
        elif not result.update_available:
            QMessageBox.information(
                self, "Check for Updates",
                f"You're already running the latest version ({result.current_commit[:12]})."
            )
            return
        else:
            proceed = QMessageBox.question(
                self, "Check for Updates",
                f"Update available: {result.current_commit[:12]} -> {result.latest_commit[:12]}.\n\n"
                "Update now?"
            )
            if proceed != QMessageBox.Yes:
                return

        self._start_update()

    def _on_update_check_failed(self, message):
        self.update_button.setEnabled(True)
        self.update_button.setText("Check for Updates...")
        QMessageBox.warning(self, "Check for Updates", f"Couldn't check for updates:\n{message}")

    def _start_update(self):
        self.update_button.setEnabled(False)
        self.update_button.setText("Updating...")
        worker = UpdatePerformWorker(self)
        worker.status.connect(self._on_update_status)
        worker.succeeded.connect(self._on_update_succeeded)
        worker.failed.connect(self._on_update_failed)
        self._update_perform_worker = worker
        worker.start()

    def _on_update_status(self, message):
        self.update_button.setText(f"Updating: {message}")

    def _on_update_succeeded(self):
        self.update_button.setEnabled(True)
        self.update_button.setText("Check for Updates...")
        QMessageBox.information(
            self, "Update Complete",
            "TORCA has been updated. Restart the app to use the new version."
        )

    def _on_update_failed(self, message):
        self.update_button.setEnabled(True)
        self.update_button.setText("Check for Updates...")
        QMessageBox.critical(self, "Update Failed", f"Couldn't complete the update:\n{message}")

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
        self._update_radio_service_buttons_enabled()

    def _update_radio_service_buttons_enabled(self):
        """Rigctld/Virtual Cables act on whichever radio is currently
        SELECTED in connected_radios_list (see _selected_radio_window)
        -- both buttons stay disabled until something's actually
        selected for them to act on. Connected to itemSelectionChanged
        as well as called after connect/disconnect (a disconnect can
        clear the selection out from under an open dialog's target)."""
        has_selection = self._selected_radio_window() is not None
        self.virtual_cable_button.setEnabled(has_selection)
        self.rigctld_button.setEnabled(has_selection)

    # ---- Virtual Cables ----

    def _on_virtual_cable_button_clicked(self):
        window = self._selected_radio_window()
        if window is None:
            return  # button is disabled without a selection -- defensive only
        is_rx = window is self._virtual_cable_rx_window
        is_tx = window is self._virtual_cable_tx_window
        dialog = VirtualCableDialog(
            self._radio_list_base_label(window._details), is_rx, is_tx, self._virtual_cable_channel,
            on_apply=lambda rx, tx, channel, w=window: self._on_virtual_cable_dialog_apply(w, rx, tx, channel),
            on_stop=lambda w=window: self._on_virtual_cable_dialog_apply(w, False, False, self._virtual_cable_channel),
            parent=self,
        )
        dialog.exec()  # Apply/Stop inside already applied whatever was clicked -- Close just dismisses

    def _on_virtual_cable_dialog_apply(self, window, is_rx, is_tx, channel):
        """Computes the new GLOBAL rx/tx assignment from just this ONE
        radio's checkbox state, per VirtualCableDialog's own docstring
        -- checking a box always claims that direction for `window`;
        unchecking it clears that direction ONLY if `window` itself
        was already holding it (an unrelated radio's assignment, if
        any, is left untouched either way)."""
        self._virtual_cable_channel = channel
        new_rx = window if is_rx else (None if self._virtual_cable_rx_window is window else self._virtual_cable_rx_window)
        new_tx = window if is_tx else (None if self._virtual_cable_tx_window is window else self._virtual_cable_tx_window)
        self._apply_virtual_cable_config(new_rx, new_tx, channel)

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
            self._refresh_connected_radios_list_status()
            return

        if not pactl_available():
            QMessageBox.warning(
                self, "Virtual Cables",
                "Requires Linux with PulseAudio or PipeWire (pactl not found) "
                "-- on Windows/macOS, install a virtual audio cable driver "
                "like VB-CABLE or BlackHole instead, then select it directly "
                "in each radio's own connection dialog."
            )
            self._refresh_connected_radios_list_status()
            return

        try:
            rx_module = create_null_sink(VIRTUAL_CABLE_RX_NAME, VIRTUAL_CABLE_RX_DESC)
            tx_module = create_null_sink(VIRTUAL_CABLE_TX_NAME, VIRTUAL_CABLE_TX_DESC)
        except Exception as exc:
            QMessageBox.critical(self, "Virtual Cables", f"Couldn't create sinks ({exc}).")
            self._refresh_connected_radios_list_status()
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

        self._refresh_connected_radios_list_status()

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
        window = self._selected_radio_window()
        if window is None:
            return  # button is disabled without a selection -- defensive only
        is_running = window in self._rigctld_servers
        if window in self._rigctld_ports:
            default_port = self._rigctld_ports[window]
        else:
            used_ports = set(self._rigctld_ports.values())
            default_port = RIGCTLD_DEFAULT_PORT
            while default_port in used_ports:
                default_port += 1
        dialog = RigctldDialog(
            self._radio_list_base_label(window._details), is_running, default_port,
            on_start=lambda port, w=window: self._on_rigctld_dialog_start(w, port),
            on_stop=lambda w=window: self._on_rigctld_dialog_stop(w),
            parent=self,
        )
        dialog.exec()  # already started/stopped its own server directly -- Close just dismisses

    def _on_rigctld_dialog_start(self, window, port):
        """Returns True/False so the dialog can reflect whether the
        server actually started -- a port collision (with another of
        our own radios' servers, or anything else already using that
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
        self._refresh_connected_radios_list_status()
        return True

    def _on_rigctld_dialog_stop(self, window):
        self._stop_rigctld_for_window(window)
        self._refresh_connected_radios_list_status()

    def _stop_rigctld_for_window(self, window):
        server = self._rigctld_servers.pop(window, None)
        if server is not None:
            server.stop()

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

    # ---- JS8Call ----

    def _on_js8call_button_clicked(self):
        settings = QSettings("IcomRadioApp", "RadioControl")
        path = settings.value("js8call_executable_path", "")

        if not path or not os.path.isfile(path):
            path = find_js8call_executable()

        if not path:
            path, _filter = QFileDialog.getOpenFileName(
                self, "Locate the JS8Call executable",
                "", "Executable (*.exe);;All files (*)" if platform.system() == "Windows" else "All files (*)",
            )
            if not path:
                return  # user cancelled the browse dialog

        try:
            launch_js8call(path)
        except OSError as exc:
            QMessageBox.critical(self, "JS8Call", f"Couldn't launch JS8Call at:\n{path}\n\n{exc}")
            return

        # Only remember the path once it's actually confirmed to work
        # (subprocess.Popen not raising means the OS accepted it).
        settings.setValue("js8call_executable_path", path)

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
        if self._dx_cluster_client is not None:
            self._dx_cluster_client.stop()
            self._dx_cluster_client = None
        for window in list(self._rigctld_servers):
            self._stop_rigctld_for_window(window)
        self._teardown_virtual_cables()  # best-effort; each affected window's own worker.stop() (below) may cut this off before it finishes
        for window in list(self._connected_radios):
            window.close()
        self._satellite_timer.stop()
        self._passes_timer.stop()
        self.solar_worker.requestInterruption()
        # requestInterruption() only takes effect between fetch cycles
        # (checked in SolarDataWorker.run()'s own loop) -- it can't
        # interrupt an in-flight urllib call, and fetch_solar_data()
        # makes up to 4 SEQUENTIAL requests, each with its own 10s
        # timeout (solar_data.py). The old 2000ms wait() here was too
        # short to cover even ONE of those timing out, let alone
        # several -- confirmed live: closing during a fetch reliably
        # hit the same "QThread: Destroyed while thread is still
        # running" abort just fixed for RadioWorker (radio_worker.py's
        # stop(), which cancels its asyncio task directly rather than
        # relying on a cooperative flag). SolarDataWorker's fetches are
        # plain synchronous urllib calls, not asyncio, so that same
        # cancellation approach doesn't apply here -- 15s comfortably
        # covers the common case (one fetch stage hitting its 10s
        # timeout) without making every quit wait a full worst-case 40s
        # (all four hanging their full timeout, an unlikely combination
        # in practice since most real network failures fail fast rather
        # than hanging silently for the full duration).
        self.solar_worker.wait(15000)
        # The map tile fetcher's worker threads are PERSISTENT for the
        # widget's whole lifetime (unlike the old one-shot map-image
        # fetcher this replaced), so this always has something real to
        # stop, not just on a fresh install/cache miss -- same class of
        # QThread-destroyed-while-running abort as every other worker
        # here if skipped.
        self.map_widget.shutdown_tile_fetcher()
        # Same class of bug again, found via the same headless-testing
        # process: every other one-shot fetch this window can have in
        # flight needs waiting out too, not just solar data and the map
        # image. ContestsWorker in particular is started unconditionally
        # at startup (like solar/map) but had NO wait at all -- confirmed
        # via gc.get_objects() showing it isRunning() well after close()
        # returned. The others only ever run if the operator triggered
        # them (PSKReporter/POTA refresh, an update check or an update
        # actually installing) -- same risk if closed while one's still
        # in flight. UpdatePerformWorker gets a much longer allowance:
        # killing the app mid-install could leave a broken /opt/torca,
        # worse than a slow quit.
        for worker, timeout_ms in (
            (self._pskreporter_worker, 15000),
            (self._pota_worker, 15000),
            (self._contests_worker, 20000),
            (self._parks_programs_worker, 15000),
            (self._parks_worker, 30000),  # a full program's park list can be several MB
            (self._park_details_worker, 15000),
            (self._update_check_worker, 15000),
            (self._update_perform_worker, 300000),
        ):
            if worker is not None:
                worker.wait(timeout_ms)
        event.accept()
