"""
WsprToolWindow: the per-radio WSPR receive tool. Opened as a singleton from its
owning RadioWindow (main_window.py), same show-or-raise / hide-not-destroy
pattern as the other tools -- closeEvent stops an active decode first, so the
instance can be reopened without losing its list of spots.

Put the radio on a WSPR dial frequency in USB (the band box + Tune Radio does
it: e.g. 14.0956 MHz on 20 m) and press Start Decoding. Every two minutes
(starting on even minutes) the slot's audio is decoded and each beacon heard
is listed with its signal, callsign, grid, power and distance from you. Stations
with a grid also appear on the map (the PSKReporter overlay, "stations you are
hearing"). See wspr.py for how decoding works (it needs WSJT-X's `wsprd`).

Receive only (no transmitting). Optionally uploads each spot to WSPRnet (wsprnet.py), off
until the operator ticks "Upload spots to WSPRnet" and has been told what becomes public.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QSettings, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QCheckBox,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import ft8_decoder
import ft8_qso
import wspr
import wsprnet
from constants import AUDIO_DEFAULT_SAMPLE_RATE
from rx_radio import ReceiveRadioSelector

_SETTINGS_ORG = "IcomRadioApp"
_SETTINGS_APP = "RadioControl"

_COLUMNS = ["UTC", "dB", "DT", "Freq (MHz)", "Drift", "Call", "Grid", "Power", "km"]
_MAX_ROWS = 2000
_AUDIO_STALL_SECONDS = 5.0


class WsprToolWindow(QWidget):
    _decoded = Signal(float, list, bool)   # decoder thread -> GUI: (slot start, spots, was it decodable)
    # Stations heard, for the map (same shape as the FT8 tool's; "report": False keeps them off PSKReporter)
    stations_heard = Signal(list)
    _upload_done = Signal(int, list)       # upload thread -> GUI: (accepted, problems)

    def __init__(self, radio_window, clock=time.time):
        super().__init__()
        self._radio_window = radio_window
        self._clock = clock
        self.setWindowTitle(radio_window.tool_title("WSPR Tool"))
        self.resize(900, 460)

        self._capture = None
        self._executor = None
        self._pending = False
        self._pending_lock = threading.Lock()
        self._dial_mhz = None
        self._last_audio_monotonic = None
        self._slots_decoded = 0
        self._last_slot_summary = ""
        self._heard_calls = set()
        self._closing_for_real = False

        self.band_combo = QComboBox()
        for band, dial in wspr.BAND_DIAL_MHZ.items():
            self.band_combo.addItem(f"{band}  ({dial:.4f} MHz)", band)
        saved = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value("wspr_band", "20m")
        self.band_combo.setCurrentIndex(max(self.band_combo.findData(saved), 0))
        self.tune_button = QPushButton("Tune Radio")
        self.tune_button.setToolTip("Sets the radio to this band's standard WSPR dial frequency, in USB.")
        self.tune_button.clicked.connect(self._on_tune_clicked)

        self.decode_toggle_button = QPushButton("Start Decoding")
        self.decode_toggle_button.setCheckable(True)
        self.decode_toggle_button.toggled.connect(self._on_decode_toggled)
        self.rx_selector = ReceiveRadioSelector(radio_window, "WSPR Tool")
        self.decode_toggle_button.toggled.connect(self.rx_selector.set_locked)   # fixed while decoding
        self.rx_selector.lost.connect(self._on_receive_radio_lost)
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._on_clear_clicked)

        self.upload_check = QCheckBox("Upload spots to WSPRnet")
        self.upload_check.setToolTip(
            "Sends each station you decode to wsprnet.org, where it is public: your callsign, your 6-character "
            "grid square (from your profile) and the station heard with its frequency, signal and power. Off unless you turn it on.")
        self.upload_check.setChecked(QSettings(_SETTINGS_ORG, _SETTINGS_APP).value("wspr_upload", False, type=bool))
        self.upload_check.clicked.connect(self._on_upload_clicked)
        self._upload_executor = None
        self._uploaded = 0
        self._upload_status = ""

        row = QHBoxLayout()
        row.addWidget(QLabel("Band:"))
        row.addWidget(self.band_combo)
        row.addWidget(self.tune_button)
        row.addWidget(self.decode_toggle_button)
        row.addWidget(self.rx_selector)
        row.addWidget(clear_button)
        row.addStretch()
        row.addWidget(self.upload_check)

        self.hint_label = QLabel(
            "Set the radio to USB on a WSPR dial frequency. WSPR slots are two minutes, starting on even minutes, and "
            "decoded about two minutes after they start, so the first results take a few minutes. Your computer's clock "
            "must be accurate to about a second (use NTP)."
        )
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #aaa;")
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #aaa;")
        self.audio_warning_label = QLabel("")
        self.audio_warning_label.setWordWrap(True)
        self.audio_warning_label.setStyleSheet("color: #e8a33d; font-weight: bold;")
        self.audio_warning_label.setVisible(False)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        header = self.table.horizontalHeader()
        for column in range(len(_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.audio_warning_label)
        layout.addWidget(self.table, stretch=1)

        self._decoded.connect(self._on_decoded)
        self._upload_done.connect(self._on_upload_done)
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)
        self._refresh_availability()

    # ---- availability / tuning ----

    def _refresh_availability(self):
        available = wspr.is_available()
        self.decode_toggle_button.setEnabled(available)
        if not available:
            self.status_label.setText(
                "WSPR decoding needs `wsprd`, the decoder that comes with WSJT-X. Install WSJT-X "
                "(Debian/Ubuntu: sudo apt install wsjtx), then reopen this tool."
            )
        elif not self.decode_toggle_button.isChecked():
            self.status_label.setText("Not decoding.")

    def _on_tune_clicked(self):
        band = self.band_combo.currentData()
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue("wspr_band", band)
        dial = wspr.BAND_DIAL_MHZ[band]
        worker = self.rx_selector.worker()                      # tune the radio being listened to
        try:
            worker.set_frequency(int(round(dial * 1e6)), check_conflict=False)
            worker.set_control_value("mode", "USB")
        except Exception as exc:   # not connected, etc.: say so rather than failing silently
            self.status_label.setText(f"Couldn't tune the radio: {exc}")
            return
        self.status_label.setText(f"Tuning the radio to {dial:.4f} MHz USB.")

    # ---- decode ----

    def _radio_dial_mhz(self):
        hz = getattr(self.rx_selector.radio(), "_current_freq_hz", None)     # the radio being listened to
        return hz / 1e6 if hz else None

    def _on_receive_radio_lost(self):
        """The radio this tool was decoding was closed: stop decoding it (the choice is back to 'this radio')."""
        if self.decode_toggle_button.isChecked():
            self.decode_toggle_button.setChecked(False)

    def _on_decode_toggled(self, checked):
        worker = self.rx_selector.worker()   # the radio whose audio is decoded
        if checked:
            if not wspr.is_available():
                self._refresh_availability()
                self._uncheck()
                return
            self._sample_rate = getattr(worker.radio, "audio_sample_rate", None) or AUDIO_DEFAULT_SAMPLE_RATE
            self._capture = ft8_decoder.Ft8SlotCapture("WSPR", self._sample_rate, clock=self._clock)
            try:
                worker.add_audio_tap(self._on_audio_frame)
            except RuntimeError as exc:
                self._capture = None
                self.status_label.setText(str(exc))
                self._uncheck()
                return
            self._last_audio_monotonic = time.monotonic()
            self.decode_toggle_button.setText("Stop Decoding")
            self._tick_timer.start()
            self._on_tick()
        else:
            worker.remove_audio_tap(self._on_audio_frame)
            self._capture = None
            self._tick_timer.stop()
            self.audio_warning_label.setVisible(False)
            self.decode_toggle_button.setText("Start Decoding")
            self.status_label.setText("Not decoding.")

    def _uncheck(self):
        self.decode_toggle_button.blockSignals(True)
        self.decode_toggle_button.setChecked(False)
        self.decode_toggle_button.blockSignals(False)

    def _on_audio_frame(self, pcm_bytes):
        # Radio asyncio thread: only cut slots and hand them over; never touch a widget here.
        self._last_audio_monotonic = time.monotonic()
        capture = self._capture
        if capture is None:
            return
        capture.feed(pcm_bytes)                  # the end-of-slot copy is not needed: the early one has everything
        for slot in capture.take_early():
            self._submit(slot, capture.sample_rate)

    def _submit(self, slot, sample_rate):
        dial = self._radio_dial_mhz()
        if dial is None:
            return
        with self._pending_lock:
            if self._pending:
                return                           # the last slot is still being decoded: skip rather than fall behind
            self._pending = True
        self._dial_mhz = dial
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wspr-decode")
        self._executor.submit(self._decode_job, slot, sample_rate, dial)

    def _decode_job(self, slot, sample_rate, dial):
        try:
            spots = wspr.decode_slot(slot, sample_rate, dial)
        except Exception:
            spots = []                           # a decoder problem must never take the tool down
        finally:
            with self._pending_lock:
                self._pending = False
        self._decoded.emit(slot.slot_start, spots, slot.decodable)

    def _on_decoded(self, slot_start, spots, decodable):
        if not decodable:
            return
        self._slots_decoded += 1
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        try:
            my_grid = ft8_qso.latlon_to_grid(float(settings.value("operator_lat", 0.0) or 0.0),
                                             float(settings.value("operator_lon", 0.0) or 0.0))
        except (TypeError, ValueError):
            my_grid = ""
        for spot in spots:
            self._append_row(spot, my_grid)
        n = len(spots)
        self._last_slot_summary = f"last slot: {n} spot{'s' if n != 1 else ''}"
        if spots:
            self.table.scrollToBottom()
        heard = {}
        for spot in spots:
            if spot.grid:
                heard[spot.callsign] = {
                    "callsign": spot.callsign, "locator": spot.grid, "frequency_hz": int(round(spot.frequency_mhz * 1e6)),
                    "snr": spot.snr, "mode": "WSPR", "epoch": int(spot.slot_start), "report": False,
                }
        if heard:
            self.stations_heard.emit(list(heard.values()))
        if self.upload_check.isChecked():
            self._upload(spots)

    # ---- WSPRnet upload ----

    def _receiver(self):
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        call = (settings.value("operator_callsign", "") or "").strip().upper()
        try:
            grid = ft8_qso.latlon_to_grid(float(settings.value("operator_lat", 0.0) or 0.0),
                                          float(settings.value("operator_lon", 0.0) or 0.0), digits=6)
        except (TypeError, ValueError):
            grid = ""
        return call, grid

    def _on_upload_clicked(self, checked):
        if checked:
            call, grid = self._receiver()
            if not call:
                self.status_label.setText("Set your callsign first (Profile) to upload to WSPRnet.")
                self.upload_check.setChecked(False)
                return
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("Upload to WSPRnet")
            box.setText(
                f"TORCA will send to wsprnet.org, publicly, every station this tool decodes, as received by "
                f"{call} in grid {grid}, with each station's frequency, signal and power.\n\nTurn uploading on?")
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            box.setDefaultButton(QMessageBox.No)
            if box.exec() != QMessageBox.Yes:
                self.upload_check.setChecked(False)
                return
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue("wspr_upload", bool(self.upload_check.isChecked()))

    def _upload(self, spots):
        call, grid = self._receiver()
        dial = self._dial_mhz
        spots = [s for s in spots if wsprnet.uploadable(s)]
        if not (call and grid and dial and spots):
            return
        if self._upload_executor is None:
            self._upload_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wsprnet")

        def job():
            try:
                accepted, problems = wsprnet.upload(spots, call, grid, dial)
            except Exception as exc:        # never take the tool down
                accepted, problems = 0, [str(exc)]
            self._upload_done.emit(accepted, problems)
        self._upload_executor.submit(job)

    def _on_upload_done(self, accepted, problems):
        self._uploaded += accepted
        if problems:
            self._upload_status = f"WSPRnet upload problem: {problems[0]}"
        elif accepted:
            self._upload_status = f"{self._uploaded} spot{'s' if self._uploaded != 1 else ''} uploaded to WSPRnet"
        if self._upload_status:
            self.status_label.setText(self._upload_status)

    def _append_row(self, spot, my_grid):
        row = self.table.rowCount()
        self.table.insertRow(row)
        km = wspr.distance_km(my_grid, spot.grid) if spot.grid else None
        power = "" if spot.power_dbm is None else f"{spot.power_dbm} dBm ({spot.power_watts:.2g} W)"
        values = [spot.utc, f"{spot.snr:+d}", f"{spot.dt:+.1f}", f"{spot.frequency_mhz:.6f}", f"{spot.drift:+d}",
                  spot.callsign, spot.grid, power, "" if km is None else f"{km:.0f}"]
        right = (1, 2, 3, 4, 8)
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in right:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if column == 0:
                item.setData(Qt.UserRole, spot)
            self.table.setItem(row, column, item)
        for _ in range(max(self.table.rowCount() - _MAX_ROWS, 0)):
            self.table.removeRow(0)

    # ---- status ----

    def _on_tick(self):
        if not self.decode_toggle_button.isChecked():
            return
        remaining = ft8_decoder.seconds_to_next_slot("WSPR", clock=self._clock)
        parts = ["Decoding WSPR"]
        dial = self._radio_dial_mhz()
        if dial is None:
            parts.append("waiting for the radio's frequency")
        else:
            band = wspr.nearest_band(dial * 1e6)
            parts.append(f"{dial:.4f} MHz" + (f" ({band[0]})" if band else " -- not a WSPR frequency"))
        parts.append(f"next slot in {int(remaining // 60)}:{int(remaining % 60):02d}")
        if self._last_slot_summary:
            parts.append(self._last_slot_summary)
        if self._upload_status and self.upload_check.isChecked():
            parts.append(self._upload_status)
        self.status_label.setText(" -- ".join(parts))
        self._check_audio_flow()

    def _check_audio_flow(self, now=None):
        last = self._last_audio_monotonic
        if last is None:
            return
        silent_for = (time.monotonic() if now is None else now) - last
        if silent_for >= _AUDIO_STALL_SECONDS:
            self.audio_warning_label.setText(
                f"No audio received from the radio for {int(silent_for)} s -- nothing can be decoded. "
                "Try Stop/Start Decoding, or reconnect the radio."
            )
            self.audio_warning_label.setVisible(True)
        else:
            self.audio_warning_label.setVisible(False)

    # ---- table ----

    def _on_clear_clicked(self):
        self.table.setRowCount(0)

    def _on_table_context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        item = self.table.item(row, 5) if row >= 0 else None
        if item is None:
            return
        spot = self.table.item(row, 0).data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction("Copy Callsign").triggered.connect(lambda: QGuiApplication.clipboard().setText(item.text()))
        menu.addAction("Copy Line").triggered.connect(lambda: QGuiApplication.clipboard().setText(spot.raw))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ---- lifecycle ----

    def closing_for_real(self):
        self._closing_for_real = True
        self.close()

    def closeEvent(self, event):
        if self.decode_toggle_button.isChecked():
            self.decode_toggle_button.setChecked(False)
        if self._closing_for_real:
            for executor in (self._executor, self._upload_executor):
                if executor is not None:
                    executor.shutdown(wait=False, cancel_futures=True)
            event.accept()
            return
        event.ignore()
        self.hide()
