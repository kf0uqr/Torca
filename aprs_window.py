"""
AprsToolWindow: the per-radio APRS (Automatic Packet Reporting System)
decode tool. Opened as a singleton from its owning RadioWindow (main_
window.py), same show-or-raise / hide-not-destroy pattern as CwToolWindow/
SstvToolWindow/RttyToolWindow -- closeEvent stops any active decode tap
before hiding, so the same instance can be reopened freely without
losing its packet log.

Decode-only (no send -- see aprs.py's own module docstring for why).
A continuously-running Bell 202/AX.25 decoder (aprs.AprsDecoder) fed
live PCM via RadioWorker.start_aprs_decode(). Works alongside normal
listening audio or an active Virtual Cable on the same radio, exactly
like CW/SSTV/RTTY decode -- RadioWorker shares whichever RX stream is
already running rather than requiring exclusive access.

Unlike CW/RTTY (a continuous text transcript), APRS is inherently
packet-oriented -- each decode is a discrete, self-contained event
(a station's position/status/message), not a stream of characters to
append to a running line. So this shows a table, one row per decoded
packet, newest at the bottom (same append-and-scroll convention as the
CW/RTTY transcripts, just structured into columns instead of raw text).
"""

import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from constants import AUDIO_DEFAULT_SAMPLE_RATE
from aprs import AprsDecoder

_COLUMNS = ["Time", "Source", "Destination", "Type", "Details"]


def _format_packet_details(info):
    if info is None:
        return "(no info field)"
    if info["type"] == "position":
        details = f"{info['lat']:.5f}, {info['lon']:.5f}  [{info['symbol_table']}{info['symbol_code']}]"
        if info["comment"]:
            details += f"  {info['comment']}"
        return details
    return info["raw"]


class AprsToolWindow(QWidget):
    # Internal cross-thread hop only: _on_decode_audio_frame runs on
    # RadioWorker's asyncio-loop thread (same requirement as every
    # other RadioWorker callback in this app) and must not touch any
    # widget directly.
    _packets_ready = Signal(list)

    def __init__(self, radio_window):
        super().__init__()
        self._radio_window = radio_window
        self.setWindowTitle(f"APRS Tool -- {radio_window._connection_label}")
        self.resize(640, 420)

        self._decoder = None  # aprs.AprsDecoder, constructed fresh each time decoding starts

        self.decode_toggle_button = QPushButton("Start Decoding")
        self.decode_toggle_button.setCheckable(True)
        self.decode_toggle_button.toggled.connect(self._on_decode_toggled)

        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._on_clear_clicked)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.decode_toggle_button)
        buttons_row.addWidget(clear_button)
        buttons_row.addStretch()

        self.status_label = QLabel("Not decoding.")
        self.status_label.setStyleSheet("color: #aaa;")

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)

        layout = QVBoxLayout()
        layout.addLayout(buttons_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.table, stretch=1)
        self.setLayout(layout)

        self._packets_ready.connect(self._on_packets_ready)

    # ---- Decode ----

    def _on_decode_toggled(self, checked):
        worker = self._radio_window.worker
        if checked:
            sample_rate = getattr(worker.radio, "audio_sample_rate", None) or AUDIO_DEFAULT_SAMPLE_RATE
            self._decoder = AprsDecoder(sample_rate)
            try:
                worker.start_aprs_decode(self._on_decode_audio_frame)
            except RuntimeError as exc:
                # Defensive backstop only -- with aprs_tool_button only
                # ever enabled once the radio is connected (main_
                # window.py's _on_connected), start_aprs_decode()
                # raising synchronously (not-connected-yet) shouldn't
                # actually be reachable from here. A failure to
                # actually attach (e.g. the radio doesn't support RX
                # audio at all) happens asynchronously instead and is
                # reported via worker.error, not this exception.
                self._decoder = None
                self.status_label.setText(str(exc))
                self.decode_toggle_button.blockSignals(True)
                self.decode_toggle_button.setChecked(False)
                self.decode_toggle_button.blockSignals(False)
                return
            self.decode_toggle_button.setText("Stop Decoding")
            self.status_label.setText("Decoding -- listening for AX.25 packets...")
        else:
            worker.stop_aprs_decode()
            self._decoder = None
            self.decode_toggle_button.setText("Start Decoding")
            self.status_label.setText("Not decoding.")

    def _on_decode_audio_frame(self, pcm_bytes):
        # Runs on RadioWorker's asyncio-loop thread. aprs.AprsDecoder
        # has no thread-safety of its own, but it's only ever touched
        # here (RadioWorker delivers its own audio callbacks serially)
        # and from _on_decode_toggled's start/stop on the GUI thread,
        # which only replace/clear self._decoder while decoding is NOT
        # active -- so there's never genuine concurrent access to the
        # same AprsDecoder instance.
        if self._decoder is None:
            return
        packets = self._decoder.feed(pcm_bytes)
        if packets:
            self._packets_ready.emit(packets)

    def _on_packets_ready(self, packets):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        for packet in packets:
            row = self.table.rowCount()
            self.table.insertRow(row)
            digipeater_suffix = f" via {','.join(packet['digipeaters'])}" if packet["digipeaters"] else ""
            info = packet["info"]
            values = [
                now,
                packet["source"],
                packet["destination"] + digipeater_suffix,
                (info["type"] if info else "unknown"),
                _format_packet_details(info),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.table.scrollToBottom()

    def _on_clear_clicked(self):
        self.table.setRowCount(0)

    # ---- Lifecycle ----

    def closing_for_real(self):
        """Called by RadioWindow.closeEvent when the OWNING radio
        window is actually going away for good -- see CwToolWindow.
        closing_for_real for the full rationale."""
        self._closing_for_real = True
        self.close()

    def closeEvent(self, event):
        # Singleton -- hidden, not destroyed -- but an active decode
        # tap is a real hardware side effect (an abandoned
        # radio.start_rx() tap would keep running with nothing
        # consuming it) that must be stopped first, same as CW/SSTV/
        # RTTY Tool.
        if self.decode_toggle_button.isChecked():
            self.decode_toggle_button.setChecked(False)  # synchronously runs _on_decode_toggled(False)
        if getattr(self, "_closing_for_real", False):
            event.accept()
            return
        event.ignore()
        self.hide()
