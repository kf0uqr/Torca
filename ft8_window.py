"""
Ft8ToolWindow: the per-radio FT8 / FT4 decode tool. Opened as a singleton
from its owning RadioWindow (main_window.py), same show-or-raise /
hide-not-destroy pattern as CwToolWindow/RttyToolWindow/Psk31ToolWindow/
AprsToolWindow -- closeEvent stops any active decode tap before hiding, so the
same instance can be reopened freely without losing its list of decodes.

It listens to the radio's audio (the radio should be on an FT8 or FT4
frequency in USB / USB-D, e.g. 14.074 MHz) and lists every message it decodes,
one batch after each time slot (15 s for FT8, 7.5 s for FT4). See ft8_decoder.py
for how slots are captured and decoded -- and for the caveat that the SNR
column is the decoder's rough estimate, not WSJT-X's.

It can also make contacts. Double-click a decoded message to answer it (or
use Call CQ) and the conversation (ft8_qso.py) is run for you: each decode
that belongs to the contact picks the next message, which is sent in the next
slot of the right parity, through the radio's TX audio. The finished contact
is logged to the Log Book. Transmitting is always an explicit act -- Enable TX
-- and Halt TX stops it at once; a watchdog also switches it off after
WATCHDOG_SECONDS with nothing heard for the contact (as WSJT-X does), so the
station can never transmit unattended indefinitely.

Decoding takes a second or two of CPU per slot, so it runs on a worker thread:
the audio callback (which runs on the radio's own asyncio thread) only cuts
slots and hands them over, and results come back to the GUI by signal, so a
slow decode can never hold up the radio's audio.

Messages calling CQ are shown in green, and anything involving the operator's
own callsign (a reply to you, a QSO in progress) stands out in amber.
"""

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QSettings, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QWidget,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

import adif
import ft8_decoder as ft8
import ft8_qso
import ft8_waterfall
from constants import AUDIO_DEFAULT_SAMPLE_RATE, AUDIO_TX_PCM_SAMPLE_RATE

_SETTINGS_ORG = "IcomRadioApp"
_SETTINGS_APP = "RadioControl"

_COLUMNS = ["dB", "Message", "Freq", "DT"]
_MESSAGE_COLUMN = 1
_MAX_ROWS = 1000

# Same idea as the APRS tool: audio chunks arrive many times a second whenever
# the radio's RX stream is up, so a long silence means the stream stalled.
_AUDIO_STALL_SECONDS = 5.0

# More than this many slots waiting for the decoder means it cannot keep up
# (an overloaded machine): newer slots are dropped rather than falling ever
# further behind real time.
_MAX_PENDING_DECODES = 2

# Transmitting switches itself off when nothing has been heard for the contact in
# this long (WSJT-X's "Tx watchdog" does the same, default six minutes).
WATCHDOG_SECONDS = 6 * 60

# A transmission starts this long after its slot starts (see ft8_decoder), with
# the PTT keyed a little before the audio. Too late into the slot, it waits for
# the next slot of the right parity instead.
_TX_KEY_LEAD = 0.1
_TX_TOO_LATE = 1.5

_COLOR_TX = QColor("#ff6b6b")
_COLOR_CQ = QColor("#5fd068")
_COLOR_MINE = QColor("#ffb347")


class Ft8ToolWindow(QWidget):
    # Decoder thread -> GUI thread: (slot start epoch, messages, was the slot decodable)
    _decoded = Signal(float, list, bool)
    # Build thread -> GUI thread
    _build_finished = Signal(bool, str)
    # A contact is complete: the ADIF fields to log (RadioWindow forwards it to the dashboard)
    qso_logged = Signal(dict)
    # Stations heard in a slot: [{"callsign", "locator", "frequency_hz", "snr", "mode", "epoch"}], for
    # the PSKReporter map / reports (the dashboard decides what to do with them)
    stations_heard = Signal(list)
    # Audio thread -> GUI thread: new waterfall rows
    _waterfall_rows = Signal(list)

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
        self._seen = {}               # slot start -> texts already listed (an early and a final decode overlap)
        self._qso = None              # ft8_qso.Qso, built when transmitting is first used
        self._qso_started = None      # epoch of the first moment of the contact in progress
        self._last_tx_slot = None     # index of the slot we last transmitted in
        self._sending = False
        self._tx_cache = (None, None)  # (text, mode, hz) -> pcm already encoded
        self._tx_last_activity = None  # for the watchdog

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

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        self.dx_call_edit = QLineEdit()
        self.dx_call_edit.setPlaceholderText("DX call")
        self.dx_call_edit.setMaximumWidth(110)
        self.dx_call_edit.setReadOnly(True)
        self.dx_call_edit.setToolTip("The station you are working. Double-click a decoded message to choose one.")
        self.tx_freq_spin = QSpinBox()
        self.tx_freq_spin.setRange(200, 3000)
        self.tx_freq_spin.setSuffix(" Hz")
        self.tx_freq_spin.setValue(int(settings.value("ft8_tx_hz", 1500) or 1500))
        self.tx_freq_spin.setToolTip("Audio frequency of your transmission (its lowest tone). Pick a quiet spot in the passband.")
        self.tx_parity_combo = QComboBox()
        self.tx_parity_combo.addItem("1st (even)", 0)
        self.tx_parity_combo.addItem("2nd (odd)", 1)
        self.tx_parity_combo.setToolTip(
            "Which slots you transmit in. Answering a station sets this to the opposite of theirs; "
            "choose it yourself when calling CQ.")
        self.tx_level_spin = QSpinBox()
        self.tx_level_spin.setRange(1, 100)
        self.tx_level_spin.setSuffix(" %")
        self.tx_level_spin.setValue(int(settings.value("ft8_tx_level", 30) or 30))
        self.tx_level_spin.setToolTip(
            "Audio level sent to the radio. FT8 must be clean: set it (and the radio's USB audio input level) "
            "so the ALC meter barely moves, and run reduced power.")
        self.tx_enable_button = QPushButton("Enable TX")
        self.tx_enable_button.setCheckable(True)
        self.tx_enable_button.setStyleSheet("QPushButton:checked { background-color: #a02020; color: white; font-weight: bold; }")
        self.tx_enable_button.toggled.connect(self._on_tx_enable_toggled)
        self.halt_button = QPushButton("Halt TX")
        self.halt_button.clicked.connect(self.halt_tx)
        self.cq_button = QPushButton("Call CQ")
        self.cq_button.clicked.connect(self._on_cq_clicked)
        self.tx_message_edit = QLineEdit()
        self.tx_message_edit.setPlaceholderText("Next message -- chosen for you as the contact proceeds")
        self.tx_message_edit.setToolTip("What will be sent in the next slot. You can edit it before it goes out.")
        self.tx_status_label = QLabel("")
        self.tx_status_label.setStyleSheet("color: #aaa;")

        tx_row = QHBoxLayout()
        tx_row.addWidget(self.tx_enable_button)
        tx_row.addWidget(self.halt_button)
        tx_row.addWidget(self.cq_button)
        tx_row.addWidget(QLabel("DX:"))
        tx_row.addWidget(self.dx_call_edit)
        tx_row.addWidget(QLabel("Tx:"))
        tx_row.addWidget(self.tx_freq_spin)
        tx_row.addWidget(self.tx_parity_combo)
        tx_row.addWidget(QLabel("Level:"))
        tx_row.addWidget(self.tx_level_spin)
        tx_row.addStretch()
        self.tx_group = QWidget()
        tx_box = QVBoxLayout(self.tx_group)
        tx_box.setContentsMargins(0, 0, 0, 0)
        tx_box.addLayout(tx_row)
        tx_box.addWidget(self.tx_message_edit)
        tx_box.addWidget(self.tx_status_label)

        self.hold_tx_check = QCheckBox("Hold Tx freq")
        self.hold_tx_check.setToolTip("Keep your transmit frequency where it is when you double-click a station to answer it.")
        tx_row.addWidget(self.hold_tx_check)

        self.waterfall = ft8_waterfall.WaterfallWidget()
        self.waterfall.set_markers(
            rx_hz=float(settings.value("ft8_rx_hz", self.tx_freq_spin.value()) or self.tx_freq_spin.value()),
            tx_hz=self.tx_freq_spin.value())
        self._spectrum = None   # ft8_waterfall.SpectrumRows, built when decoding starts (it needs the sample rate)

        self.table = self._new_table()
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)

        layout = QVBoxLayout()
        layout.addLayout(buttons_row)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.audio_warning_label)
        layout.addWidget(self.tx_group)
        # Two lists side by side, laid out as in WSJT-X: everything heard on the band on the left,
        # what is addressed to you or sent by you (the contact in progress) on the right.
        self.qso_table = self._new_table()
        self.qso_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.qso_table.customContextMenuRequested.connect(lambda pos: self._copy_menu(self.qso_table, pos))
        self.qso_table.cellDoubleClicked.connect(self._on_row_double_clicked_qso)
        self.qso_table.setToolTip("Messages sent to you and messages you transmit. Double-click one from another station to answer it.")
        self.lists = QSplitter(Qt.Horizontal)
        for title, table in (("Band activity", self.table), ("Your messages", self.qso_table)):
            pane = QWidget()
            box = QVBoxLayout(pane)
            box.setContentsMargins(0, 0, 0, 0)
            heading = QLabel(f"<b>{title}</b>")
            box.addWidget(heading)
            box.addWidget(table)
            self.lists.addWidget(pane)
        self.lists.setStretchFactor(0, 1)
        self.lists.setStretchFactor(1, 1)
        layout.addWidget(self.lists, stretch=1)
        layout.addWidget(self.waterfall)
        self.setLayout(layout)

        self.waterfall.rx_requested.connect(self.set_rx_frequency)
        self.waterfall.tx_requested.connect(self.set_tx_frequency)
        self.waterfall.both_requested.connect(lambda hz: (self.set_rx_frequency(hz), self.set_tx_frequency(hz)))
        self.tx_freq_spin.valueChanged.connect(lambda hz: self.waterfall.set_markers(tx_hz=hz))
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        self._waterfall_rows.connect(self.waterfall.add_rows)

        self._decoded.connect(self._on_decoded)
        self._build_finished.connect(self._on_build_finished)

        # Once a second while decoding: the slot countdown, and the audio watchdog.
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)

        # Drives transmissions (and the watchdog) while TX is enabled.
        self._tx_timer = QTimer(self)
        self._tx_timer.setInterval(100)
        self._tx_timer.timeout.connect(lambda: self._tx_tick())

        self._refresh_availability()

    # ---- decoder availability ----

    def _refresh_availability(self):
        available = ft8.is_available()
        self.tx_group.setEnabled(available and ft8.can_transmit())
        self.build_button.setVisible(not (available and ft8.can_transmit()))
        if not (available and ft8.can_transmit()):
            if available:
                self.status_label.setText("Transmitting needs the FT8 encoder, which has not been built yet -- click Build Decoder (it builds both).")
            elif ft8.can_build():
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
            self._spectrum = ft8_waterfall.SpectrumRows(self._sample_rate)
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
            self.mode_combo.setEnabled(not self.tx_enable_button.isChecked())
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
        spectrum = self._spectrum
        if spectrum is not None:
            rows = spectrum.feed(pcm_bytes)
            if rows:
                self._waterfall_rows.emit(rows)
        for slot in capture.feed(pcm_bytes):
            self._submit_decode(slot, capture.sample_rate, capture.mode)
        for slot in capture.take_early():   # ready early enough to reply in the very next slot
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
        mine = (QSettings(_SETTINGS_ORG, _SETTINGS_APP).value("operator_callsign", "") or "").strip()
        seen = self._seen.setdefault(slot_start, set())
        fresh = [m for m in messages if m.text not in seen]   # the early and the final decode of a slot overlap
        if len(seen) == 0:
            self._slots_decoded += 1
        seen.update(m.text for m in fresh)
        for old in sorted(self._seen)[:-8]:
            del self._seen[old]
        for message in fresh:
            self._append_row(message, mine)
            if self._is_to_me(message, mine) or self._on_rx_frequency(message):
                self._append_qso_row(message, my_callsign=mine)
        count = len(seen)
        self._last_slot_summary = f"last slot: {count} decode{'s' if count != 1 else ''}"
        if fresh:
            self.table.scrollToBottom()
        self._publish_heard(fresh)
        for message in fresh:
            self._react_to(message)

    def _publish_heard(self, messages):
        dial = getattr(self._radio_window, "_current_freq_hz", None) or 0
        if not dial:
            return   # without the radio's frequency there is nothing meaningful to report
        stations = {}
        for message in messages:
            heard = ft8_qso.heard_station(message.text)
            if heard is None:
                continue
            callsign, locator = heard
            stations[callsign] = {
                "callsign": callsign, "locator": locator, "frequency_hz": int(dial + message.freq),
                "snr": int(round(message.snr)), "mode": message.mode, "epoch": int(message.slot_start),
            }
        if stations:
            self.stations_heard.emit(list(stations.values()))

    @staticmethod
    def _is_to_me(message, my_callsign):
        """A standard message addressed to the operator (their callsign is the first word)."""
        words = message.text.upper().split()
        return bool(my_callsign) and len(words) >= 2 and ft8_qso._base(words[0]) == ft8_qso._base(my_callsign)

    def _on_rx_frequency(self, message):
        """True if the decode sits at the green Rx marker (within a signal's width of it)."""
        width = ft8_waterfall.SIGNAL_WIDTH_HZ.get(self._mode, 50.0)
        return abs(message.freq - self.waterfall.rx_hz) <= max(20.0, width * 0.4)

    @staticmethod
    def _new_table():
        """A decode list: dB, Message, Freq, DT -- sized to fit what they hold (the message
        takes whatever room is left), with a line above each time slot's decodes."""
        table = QTableWidget(0, len(_COLUMNS))
        table.setHorizontalHeaderLabels(_COLUMNS)
        header = table.horizontalHeader()
        for column in range(len(_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.Stretch if column == _MESSAGE_COLUMN else QHeaderView.ResizeToContents)
        header.setMinimumSectionSize(30)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        table.verticalHeader().setDefaultSectionSize(22)
        table.setProperty("last_slot", None)
        table.setProperty("data_rows", 0)
        return table

    def _add_row(self, table, slot_start, values, color=None, bold=False, message=None):
        """Appends one decode (or sent message) to `table`, first adding a time line
        ("14:32:15") if it belongs to a newer time slot than the last one listed."""
        last = table.property("last_slot")
        if last is None or slot_start > last:
            row = table.rowCount()
            table.insertRow(row)
            stamp = QTableWidgetItem(time.strftime("%H:%M:%S", time.gmtime(slot_start)))
            stamp.setFlags(Qt.ItemIsEnabled)             # not selectable, not a message
            stamp.setTextAlignment(Qt.AlignCenter)
            stamp.setForeground(QColor("#9aa4ad"))
            stamp.setBackground(QColor("#262c33"))
            font = stamp.font(); font.setBold(True); stamp.setFont(font)
            table.setItem(row, 0, stamp)
            table.setSpan(row, 0, 1, len(_COLUMNS))
            table.setProperty("last_slot", slot_start)
        row = table.rowCount()
        table.insertRow(row)
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column != _MESSAGE_COLUMN:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if color is not None:
                item.setForeground(color)
            if bold:
                font = item.font(); font.setBold(True); item.setFont(font)
            if column == _MESSAGE_COLUMN and message is not None:
                item.setData(Qt.UserRole, message)
            table.setItem(row, column, item)
        table.setProperty("data_rows", table.property("data_rows") + 1)
        self._trim(table)

    @staticmethod
    def _trim(table):
        """Keeps at most _MAX_ROWS decodes (oldest go first), and never leaves a time
        line with nothing under it at the top."""
        data = table.property("data_rows")
        while data > _MAX_ROWS and table.rowCount():
            if table.item(0, _MESSAGE_COLUMN) is not None:
                data -= 1
            table.removeRow(0)
        while table.rowCount() and table.item(0, _MESSAGE_COLUMN) is None and (
                table.rowCount() == 1 or table.item(1, _MESSAGE_COLUMN) is None):
            table.removeRow(0)
        table.setProperty("data_rows", data)

    def _append_qso_row(self, message=None, sent_text=None, sent_at=None, hz=None, my_callsign=""):
        """One line in "Your messages": a message addressed to you or heard on your receive
        frequency (`message`), or one you transmitted (`sent_text`, red, Tx in the dB column)."""
        if message is not None:
            color = _COLOR_MINE if message.involves(my_callsign) else (_COLOR_CQ if message.is_cq else None)
            self._add_row(self.qso_table, message.slot_start,
                          [f"{message.snr:+.0f}", message.text, f"{message.freq:.0f}", f"{message.dt:+.1f}"],
                          color=color, message=message)
        else:
            slot = ft8.SLOT_SECONDS[self.mode_combo.currentText()]
            self._add_row(self.qso_table, math.floor(sent_at / slot) * slot,
                          ["Tx", sent_text, f"{hz:.0f}", ""], color=_COLOR_TX)
        self.qso_table.scrollToBottom()

    def _on_row_double_clicked_qso(self, row, _column):
        self._answer_item(self.qso_table.item(row, _MESSAGE_COLUMN))

    def _append_row(self, message, my_callsign):
        color = _COLOR_MINE if message.involves(my_callsign) else (_COLOR_CQ if message.is_cq else None)
        on_rx = abs(message.freq - self.waterfall.rx_hz) <= ft8_waterfall.SIGNAL_WIDTH_HZ.get(self._mode, 50.0) / 2 + 10
        self._add_row(self.table, message.slot_start,
                      [f"{message.snr:+.0f}", message.text, f"{message.freq:.0f}", f"{message.dt:+.1f}"],
                      color=color, bold=on_rx, message=message)

    # ---- making contacts ----

    def _my_call_and_grid(self):
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        call = (settings.value("operator_callsign", "") or "").strip().upper()
        try:
            grid = ft8_qso.latlon_to_grid(float(settings.value("operator_lat", 0.0) or 0.0),
                                          float(settings.value("operator_lon", 0.0) or 0.0))
        except (TypeError, ValueError):
            grid = ""
        return call, grid

    def _ensure_qso(self):
        """The conversation state, rebuilt if the callsign or location changed."""
        call, grid = self._my_call_and_grid()
        if self._qso is None or (self._qso.my_call, self._qso.my_grid) != (call, grid):
            self._qso = ft8_qso.Qso(call, grid)
        return self._qso

    def _slot_parity(self, slot_start):
        return int(slot_start // ft8.SLOT_SECONDS[self.mode_combo.currentText()]) % 2

    def set_rx_frequency(self, hz):
        """The audio frequency of the station being listened to (the green marker)."""
        hz = max(200.0, min(3000.0, float(hz)))
        self.waterfall.set_markers(rx_hz=hz)
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue("ft8_rx_hz", hz)

    def set_tx_frequency(self, hz):
        """Your transmit audio frequency (the red marker, and the Tx box)."""
        self.tx_freq_spin.setValue(int(round(max(200.0, min(3000.0, float(hz))))))

    def _on_mode_changed(self, mode):
        self.waterfall.mode = mode
        self.waterfall.update()

    def _on_row_double_clicked(self, row, _column):
        self._answer_item(self.table.item(row, _MESSAGE_COLUMN))

    def _answer_item(self, item):
        message = item.data(Qt.UserRole) if item is not None else None
        parsed = ft8_qso.parse(message.text) if message is not None else None
        qso = self._ensure_qso()
        if parsed is None or not qso.my_call:
            self.tx_status_label.setText(
                "Set your callsign first (Profile)." if not qso.my_call else "That message is not part of a standard exchange.")
            return
        if ft8_qso._base(parsed.sender) == ft8_qso._base(qso.my_call):
            return
        qso.work(parsed, message.snr, self._slot_parity(message.slot_start), message.freq)
        self.set_rx_frequency(message.freq)                   # listen where they are,
        if not self.hold_tx_check.isChecked():
            self.set_tx_frequency(message.freq)               # and (unless held) call on their frequency, as WSJT-X does
        self._qso_started = self._clock()
        self._after_qso_change()
        self.tx_enable_button.setChecked(True)

    def _on_cq_clicked(self):
        qso = self._ensure_qso()
        if not qso.my_call:
            self.tx_status_label.setText("Set your callsign first (Profile).")
            return
        qso.call_cq(parity=self.tx_parity_combo.currentData())
        self._qso_started = None
        self._after_qso_change()
        self.tx_enable_button.setChecked(True)

    def _after_qso_change(self):
        """Show what the conversation now says to send."""
        qso = self._qso
        self.dx_call_edit.setText(qso.dx_call or "")
        self.tx_message_edit.setText(qso.message() or "")
        self.tx_parity_combo.setCurrentIndex(qso.tx_parity)

    def _react_to(self, message):
        qso = self._qso
        if qso is None or not qso.my_call:
            return
        parsed = ft8_qso.parse(message.text)
        had_dx = qso.dx_call
        if qso.heard(parsed, message.snr, self._slot_parity(message.slot_start), message.freq):
            self._tx_last_activity = self._clock()      # the other station is still there
            if had_dx is None and self._qso_started is None:
                self._qso_started = self._clock()
            self._after_qso_change()
            if qso.complete:
                self._finish_contact()

    def _on_tx_enable_toggled(self, enabled):
        self.mode_combo.setEnabled(not enabled and not self.decode_toggle_button.isChecked())
        if enabled:
            qso = self._ensure_qso()
            if not qso.my_call:
                self.tx_status_label.setText("Set your callsign first (Profile).")
                self._uncheck_tx()
                return
            if not self.tx_message_edit.text().strip():
                self.tx_status_label.setText("Nothing to send yet: double-click a decoded message, or Call CQ.")
                self._uncheck_tx()
                return
            self._tx_last_activity = self._clock()
            self._tx_timer.start()
            self.tx_status_label.setText("TX enabled -- waiting for the next slot.")
        else:
            self._tx_timer.stop()
            if self._sending:
                self._radio_window.worker.stop_tx_audio_send()
                self._sending = False
            self.tx_status_label.setText("TX off.")

    def _uncheck_tx(self):
        self.tx_enable_button.blockSignals(True)
        self.tx_enable_button.setChecked(False)
        self.tx_enable_button.blockSignals(False)
        self.mode_combo.setEnabled(not self.decode_toggle_button.isChecked())

    def halt_tx(self):
        """Stops transmitting immediately (and keys the radio off)."""
        self.tx_enable_button.setChecked(False)

    def _tx_tick(self, now=None):
        """Called ten times a second while TX is enabled: the watchdog, and
        starting a transmission when the right slot comes round."""
        now = self._clock() if now is None else now
        if not self.tx_enable_button.isChecked():
            return
        if self._tx_last_activity is not None and now - self._tx_last_activity > WATCHDOG_SECONDS:
            self.halt_tx()
            self.tx_status_label.setText("TX switched off: nothing heard for the contact in %d minutes." % (WATCHDOG_SECONDS // 60))
            return
        if self._sending:
            return
        mode = self.mode_combo.currentText()
        slot = ft8.SLOT_SECONDS[mode]
        index = math.floor(now / slot)
        into = now - index * slot
        if index % 2 != self.tx_parity_combo.currentData():
            self.tx_status_label.setText(f"TX enabled -- next transmission in {self._seconds_until_tx(now, slot):.0f} s")
            return
        if index == self._last_tx_slot or into < ft8.TX_START_OFFSET - _TX_KEY_LEAD or into > _TX_TOO_LATE:
            return
        text = self.tx_message_edit.text().strip()
        if not text:
            return
        self._start_transmission(text, mode, index, slot)

    def _seconds_until_tx(self, now, slot):
        index = math.floor(now / slot)
        wanted = self.tx_parity_combo.currentData()
        start = (index + 1 if index % 2 != wanted else index + 2) * slot
        return start + ft8.TX_START_OFFSET - now

    def _build_tx_audio(self, text, mode):
        """The message as 48 kHz PCM at the chosen level, or raises ValueError / RuntimeError."""
        hz = self.tx_freq_spin.value()
        key = (text, mode, hz, self.tx_level_spin.value())
        if self._tx_cache[0] == key:
            return self._tx_cache[1]
        import numpy as np
        pcm12 = ft8.encode_message(text, mode, hz)
        samples = np.frombuffer(pcm12, dtype="<i2").astype(np.float64) * (self.tx_level_spin.value() / 100.0)
        pcm = ft8.resample(np.rint(samples).astype("<i2").tobytes(), ft8.DECODER_RATE, AUDIO_TX_PCM_SAMPLE_RATE)
        self._tx_cache = (key, pcm)
        return pcm

    def _start_transmission(self, text, mode, index, slot):
        try:
            pcm = self._build_tx_audio(text, mode)
        except (ValueError, RuntimeError) as exc:
            self.halt_tx()
            self.tx_status_label.setText(str(exc))
            return
        worker = self._radio_window.worker
        self._last_tx_slot = index
        self._sending = True
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue("ft8_tx_hz", self.tx_freq_spin.value())
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue("ft8_tx_level", self.tx_level_spin.value())
        worker.send_tx_audio_pcm(pcm)
        self._append_qso_row(sent_text=text, sent_at=self._clock(), hz=self.tx_freq_spin.value())
        duration = len(pcm) / (AUDIO_TX_PCM_SAMPLE_RATE * 2)
        self.tx_status_label.setText(f"TRANSMITTING: {text}")
        QTimer.singleShot(int((duration + 0.3) * 1000), lambda t=text: self._on_tx_finished(t))

    def _on_tx_finished(self, text):
        if not self._sending:
            return   # halted meanwhile
        self._sending = False
        qso = self._qso
        if qso is None:
            return
        if self.tx_message_edit.text().strip() == text:
            done = qso.sent()
        else:
            done = False   # the operator sent something of their own
        self.tx_status_label.setText(f"Sent: {text}")
        if done:
            self._finish_contact()
        elif qso.gave_up:
            self.halt_tx()
            self.tx_status_label.setText(f"No answer from {qso.dx_call} -- gave up.")
            qso.reset()
            self._after_qso_change()
        else:
            self._after_qso_change()

    def _finish_contact(self):
        """The exchange is done: log it, then carry on (back to CQ) or stop."""
        qso = self._qso
        fields = self._qso_fields(qso)
        if fields is not None:
            self.qso_logged.emit(fields)
            self.tx_status_label.setText(f"Logged {qso.dx_call}.")
        qso.after_complete()
        self._qso_started = None
        self._after_qso_change()
        if qso.message() is None:
            self.halt_tx()
            self.tx_status_label.setText(self.tx_status_label.text() + " TX off.")

    def _qso_fields(self, qso):
        if not qso.dx_call:
            return None
        started = time.gmtime(self._qso_started or self._clock())
        mode = self.mode_combo.currentText()
        freq_hz = getattr(self._radio_window, "_current_freq_hz", None) or 0
        fields = {
            "CALL": qso.dx_call,
            "QSO_DATE": time.strftime("%Y%m%d", started),
            "TIME_ON": time.strftime("%H%M%S", started),
            "MODE": "FT8" if mode == "FT8" else "MFSK",
            "RST_SENT": qso.rst_sent,
            "RST_RCVD": qso.rst_rcvd,
            "STATION_CALLSIGN": qso.my_call,
            "MY_GRIDSQUARE": qso.my_grid,
        }
        if mode == "FT4":
            fields["SUBMODE"] = "FT4"
        if qso.dx_grid:
            fields["GRIDSQUARE"] = qso.dx_grid
        if freq_hz:
            fields["FREQ"] = f"{freq_hz / 1e6:.6f}"
            band = adif.band_for_freq_hz(freq_hz)
            if band:
                fields["BAND"] = band
        return fields

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
        for table in (self.table, self.qso_table):
            table.clearSpans()
            table.setRowCount(0)
            table.setProperty("last_slot", None)
            table.setProperty("data_rows", 0)

    def _on_table_context_menu(self, pos):
        self._copy_menu(self.table, pos)

    def _copy_menu(self, table, pos):
        row = table.rowAt(pos.y())
        item = table.item(row, _MESSAGE_COLUMN) if row >= 0 else None
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction("Copy Message").triggered.connect(lambda: QGuiApplication.clipboard().setText(item.text()))
        menu.exec(table.viewport().mapToGlobal(pos))

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
        if self.tx_enable_button.isChecked():
            self.halt_tx()   # never leave a transmitter running behind a hidden window
        if self.decode_toggle_button.isChecked():
            self.decode_toggle_button.setChecked(False)  # synchronously runs _on_decode_toggled(False)
        if self._closing_for_real:
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
            event.accept()
            return
        event.ignore()
        self.hide()
