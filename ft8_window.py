"""
Ft8ToolWindow: the per-radio FT8 / FT4 decode tool. Opened as a singleton
from its owning RadioWindow (main_window.py), same show-or-raise /
hide-not-destroy pattern as CwToolWindow/RttyToolWindow/Psk31ToolWindow/
AprsToolWindow -- closeEvent stops any active decode tap before hiding, so the
same instance can be reopened freely without losing its list of decodes.

Receive only: it listens to the radio's audio (the radio should be on an FT8
or FT4 frequency in USB / USB-D, e.g. 14.074 MHz) and lists every message it
decodes, one batch after each time slot (15 s for FT8, 7.5 s for FT4). See
ft8_decoder.py for how slots are captured and decoded -- and for the caveat
that the SNR column is the decoder's rough estimate, not WSJT-X's.

Decoding takes a second or two of CPU per slot, so it runs on a worker thread:
the audio callback (which runs on the radio's own asyncio thread) only cuts
slots and hands them over, and results come back to the GUI by signal, so a
slow decode can never hold up the radio's audio.

Messages calling CQ are shown in green, and anything involving the operator's
own callsign (a reply to you, a QSO in progress) stands out in amber.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QSettings, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QWidget,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

import ft8_decoder as ft8
from constants import AUDIO_DEFAULT_SAMPLE_RATE

_SETTINGS_ORG = "IcomRadioApp"
_SETTINGS_APP = "RadioControl"

_COLUMNS = ["UTC", "dB", "DT", "Freq (Hz)", "Message"]
_MAX_ROWS = 1000

# Same idea as the APRS tool: audio chunks arrive many times a second whenever
# the radio's RX stream is up, so a long silence means the stream stalled.
_AUDIO_STALL_SECONDS = 5.0

# More than this many slots waiting for the decoder means it cannot keep up
# (an overloaded machine): newer slots are dropped rather than falling ever
# further behind real time.
_MAX_PENDING_DECODES = 2

_COLOR_CQ = QColor("#5fd068")
_COLOR_MINE = QColor("#ffb347")


class Ft8ToolWindow(QWidget):
    # Decoder thread -> GUI thread: (slot start epoch, messages, was the slot decodable)
    _decoded = Signal(float, list, bool)
    # Build thread -> GUI thread
    _build_finished = Signal(bool, str)

    def __init__(self, radio_window, clock=time.time):
        super().__init__()
        self._radio_window = radio_window
        self._clock = clock
        self.setWindowTitle(radio_window.tool_title("FT8 Tool"))
        self.resize(720, 460)

        self._capture = None          # ft8_decoder.Ft8SlotCapture, built fresh each time decoding starts
        self._sample_rate = AUDIO_DEFAULT_SAMPLE_RATE
        self._mode = "FT8"
        self._executor = None         # ThreadPoolExecutor(1), created on first use
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._last_audio_monotonic = None
        self._slots_decoded = 0
        self._last_slot_summary = ""
        self._closing_for_real = False

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(ft8.MODES)
        self.mode_combo.setToolTip("FT8: 15 second slots. FT4: 7.5 second slots (faster, used mostly in contests).")

        self.decode_toggle_button = QPushButton("Start Decoding")
        self.decode_toggle_button.setCheckable(True)
        self.decode_toggle_button.toggled.connect(self._on_decode_toggled)

        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._on_clear_clicked)

        self.build_button = QPushButton("Build Decoder")
        self.build_button.setToolTip("Compiles the FT8 decoder that ships with TORCA (needs make and a C compiler).")
        self.build_button.clicked.connect(self._on_build_clicked)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(QLabel("Mode:"))
        buttons_row.addWidget(self.mode_combo)
        buttons_row.addWidget(self.decode_toggle_button)
        buttons_row.addWidget(clear_button)
        buttons_row.addWidget(self.build_button)
        buttons_row.addStretch()

        self.hint_label = QLabel(
            "Set the radio to USB (or USB-D) on an FT8 frequency such as 14.074 MHz. Your computer's clock "
            "must be accurate to about a second (use NTP). The dB column is an estimate."
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
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)

        layout = QVBoxLayout()
        layout.addLayout(buttons_row)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.audio_warning_label)
        layout.addWidget(self.table, stretch=1)
        self.setLayout(layout)

        self._decoded.connect(self._on_decoded)
        self._build_finished.connect(self._on_build_finished)

        # Once a second while decoding: the slot countdown, and the audio watchdog.
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)

        self._refresh_availability()

    # ---- decoder availability ----

    def _refresh_availability(self):
        available = ft8.is_available()
        self.build_button.setVisible(not available)
        if not available:
            if ft8.can_build():
                self.status_label.setText("The FT8 decoder has not been built yet -- click Build Decoder.")
            else:
                self.status_label.setText(
                    "The FT8 decoder has not been built, and building it needs make and a C compiler "
                    "(Debian/Ubuntu: sudo apt install build-essential)."
                )
        elif not self.decode_toggle_button.isChecked():
            self.status_label.setText("Not decoding.")

    def _on_build_clicked(self):
        self.build_button.setEnabled(False)
        self.status_label.setText("Building the FT8 decoder...")

        def build():
            ok, message = ft8.build_decoder()
            self._build_finished.emit(ok, message)

        threading.Thread(target=build, daemon=True).start()

    def _on_build_finished(self, ok, message):
        self.build_button.setEnabled(True)
        if ok:
            self._refresh_availability()
            self.status_label.setText("FT8 decoder built. Click Start Decoding.")
        else:
            self.status_label.setText(message)

    # ---- decode ----

    def _on_decode_toggled(self, checked):
        worker = self._radio_window.worker
        if checked:
            if not ft8.is_available():
                self._refresh_availability()
                self._uncheck()
                return
            self._mode = self.mode_combo.currentText()
            self._sample_rate = getattr(worker.radio, "audio_sample_rate", None) or AUDIO_DEFAULT_SAMPLE_RATE
            self._capture = ft8.Ft8SlotCapture(self._mode, self._sample_rate, clock=self._clock)
            try:
                worker.start_ft8_decode(self._on_decode_audio_frame)
            except RuntimeError as exc:
                self._capture = None
                self.status_label.setText(str(exc))
                self._uncheck()
                return
            self._last_audio_monotonic = time.monotonic()  # grace period: the stream gets a few seconds to start
            self.mode_combo.setEnabled(False)
            self.decode_toggle_button.setText("Stop Decoding")
            self._tick_timer.start()
            self._on_tick()
        else:
            worker.stop_ft8_decode()
            self._capture = None
            self._tick_timer.stop()
            self.audio_warning_label.setVisible(False)
            self.mode_combo.setEnabled(True)
            self.decode_toggle_button.setText("Start Decoding")
            self.status_label.setText("Not decoding.")

    def _uncheck(self):
        self.decode_toggle_button.blockSignals(True)
        self.decode_toggle_button.setChecked(False)
        self.decode_toggle_button.blockSignals(False)

    def _on_decode_audio_frame(self, pcm_bytes):
        # Runs on RadioWorker's asyncio-loop thread. Only cuts slots and hands them to the
        # decoder thread -- never decodes here, and never touches a widget. (self._capture is
        # only replaced by the GUI thread while decoding is NOT active, same reasoning as the
        # APRS tool's decoder.)
        self._last_audio_monotonic = time.monotonic()
        capture = self._capture
        if capture is None:
            return
        for slot in capture.feed(pcm_bytes):
            self._submit_decode(slot, capture.sample_rate, capture.mode)

    def _submit_decode(self, slot, sample_rate, mode):
        with self._pending_lock:
            if self._pending >= _MAX_PENDING_DECODES:
                return  # the decoder cannot keep up: skip this slot rather than lag further behind
            self._pending += 1
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ft8-decode")
        self._executor.submit(self._decode_job, slot, sample_rate, mode)

    def _decode_job(self, slot, sample_rate, mode):
        try:
            messages = ft8.decode_slot(slot, sample_rate, mode)
        except Exception:
            messages = []  # a decoder problem must never take the tool down
        finally:
            with self._pending_lock:
                self._pending -= 1
        self._decoded.emit(slot.slot_start, messages, slot.decodable)

    def _on_decoded(self, slot_start, messages, decodable):
        if not decodable:
            return  # the partial slot at the start: nothing could be decoded from it
        self._slots_decoded += 1
        mine = (QSettings(_SETTINGS_ORG, _SETTINGS_APP).value("operator_callsign", "") or "").strip()
        for message in messages:
            self._append_row(message, mine)
        count = len(messages)
        self._last_slot_summary = f"last slot: {count} decode{'s' if count != 1 else ''}"
        if messages:
            self.table.scrollToBottom()

    def _append_row(self, message, my_callsign):
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [message.utc, f"{message.snr:+.0f}", f"{message.dt:+.1f}", f"{message.freq:.0f}", message.text]
        color = _COLOR_MINE if message.involves(my_callsign) else (_COLOR_CQ if message.is_cq else None)
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in (1, 2, 3):
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if color is not None:
                item.setForeground(color)
            if column == 0:
                item.setData(Qt.UserRole, message)
            self.table.setItem(row, column, item)
        excess = self.table.rowCount() - _MAX_ROWS
        for _ in range(max(excess, 0)):
            self.table.removeRow(0)

    # ---- status ----

    def _on_tick(self):
        """Once a second while decoding: the countdown to the next slot, and
        the audio-flow watchdog."""
        if not self.decode_toggle_button.isChecked():
            return
        remaining = ft8.seconds_to_next_slot(self._mode, clock=self._clock)
        parts = [f"Decoding {self._mode}", f"next slot in {remaining:.0f} s"]
        if self._last_slot_summary:
            parts.append(self._last_slot_summary)
        self.status_label.setText(" -- ".join(parts))
        self._check_audio_flow()

    def _check_audio_flow(self, now=None):
        last = self._last_audio_monotonic
        if last is None:
            return
        silent_for = (time.monotonic() if now is None else now) - last
        if silent_for >= _AUDIO_STALL_SECONDS:
            self.audio_warning_label.setText(
                f"No audio received from the radio for {int(silent_for)} s -- the audio stream may have "
                "stalled, so nothing can be decoded. Try Stop/Start Decoding, or reconnect the radio."
            )
            self.audio_warning_label.setVisible(True)
        else:
            self.audio_warning_label.setVisible(False)

    # ---- table ----

    def _on_clear_clicked(self):
        self.table.setRowCount(0)

    def _on_table_context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        item = self.table.item(row, 4) if row >= 0 else None
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction("Copy Message").triggered.connect(lambda: QGuiApplication.clipboard().setText(item.text()))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ---- lifecycle ----

    def closing_for_real(self):
        """Called by RadioWindow.closeEvent when the OWNING radio window is
        actually going away for good -- see CwToolWindow.closing_for_real."""
        self._closing_for_real = True
        self.close()

    def closeEvent(self, event):
        # Singleton -- hidden, not destroyed -- but an active decode tap is a real side effect
        # (an abandoned radio.start_rx() tap would keep running with nothing consuming it)
        # that must be stopped first, same as the other tools.
        if self.decode_toggle_button.isChecked():
            self.decode_toggle_button.setChecked(False)  # synchronously runs _on_decode_toggled(False)
        if self._closing_for_real:
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
            event.accept()
            return
        event.ignore()
        self.hide()
