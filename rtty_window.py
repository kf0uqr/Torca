"""
RttyToolWindow: the per-radio RTTY (radioteletype) decode tool. Opened
as a singleton from its owning RadioWindow (main_window.py), same
show-or-raise / hide-not-destroy pattern as CwToolWindow/
SstvToolWindow -- closeEvent stops any active decode tap before
hiding, so the same instance can be reopened freely without losing its
transcript.

Decode-only (no send -- see rtty.py's own module docstring for why).
A continuously-adaptive-free tone decoder (rtty.RttyDecoder -- RTTY's
45.45 baud and mark/space tones are a fixed public standard, not
something that needs adapting to like CW's operator-variable WPM) fed
live PCM via RadioWorker.start_rtty_decode(). Works alongside normal
listening audio or an active Virtual Cable on the same radio, exactly
like CW/SSTV decode -- RadioWorker shares whichever RX stream is
already running rather than requiring exclusive access.

Text push mirrors CwToolWindow exactly: RttyDecoder.feed() returns any
newly-decoded text from that call, hopped to the GUI thread via a Qt
signal (_decoded_text_ready) and appended to a transcript.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
)

from constants import AUDIO_DEFAULT_SAMPLE_RATE
from rtty import RttyDecoder


class RttyToolWindow(QWidget):
    # Internal cross-thread hop only: _on_decode_audio_frame runs on
    # RadioWorker's asyncio-loop thread (same requirement as every
    # other RadioWorker callback in this app) and must not touch any
    # widget directly.
    _decoded_text_ready = Signal(str)

    def __init__(self, radio_window):
        super().__init__()
        self._radio_window = radio_window
        self.setWindowTitle(f"RTTY Tool -- {radio_window._connection_label}")
        self.resize(480, 420)

        self._decoder = None  # rtty.RttyDecoder, constructed fresh each time decoding starts

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

        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setStyleSheet("font-family: monospace;")

        layout = QVBoxLayout()
        layout.addLayout(buttons_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.transcript, stretch=1)
        self.setLayout(layout)

        self._decoded_text_ready.connect(self._on_decoded_text_ready)

    # ---- Decode ----

    def _on_decode_toggled(self, checked):
        worker = self._radio_window.worker
        if checked:
            sample_rate = getattr(worker.radio, "audio_sample_rate", None) or AUDIO_DEFAULT_SAMPLE_RATE
            self._decoder = RttyDecoder(sample_rate)
            try:
                worker.start_rtty_decode(self._on_decode_audio_frame)
            except RuntimeError as exc:
                # Defensive backstop only -- with memories_button-style
                # enable-on-connect (main_window.py's _on_connected),
                # start_rtty_decode() raising synchronously (not-
                # connected-yet) shouldn't actually be reachable from
                # here. A failure to actually attach (e.g. the radio
                # doesn't support RX audio at all) happens
                # asynchronously instead and is reported via
                # worker.error, not this exception.
                self._decoder = None
                self.status_label.setText(str(exc))
                self.decode_toggle_button.blockSignals(True)
                self.decode_toggle_button.setChecked(False)
                self.decode_toggle_button.blockSignals(False)
                return
            self.decode_toggle_button.setText("Stop Decoding")
            self.status_label.setText("Decoding -- listening for mark/space tones...")
        else:
            worker.stop_rtty_decode()
            self._decoder = None
            self.decode_toggle_button.setText("Start Decoding")
            self.status_label.setText("Not decoding.")

    def _on_decode_audio_frame(self, pcm_bytes):
        # Runs on RadioWorker's asyncio-loop thread. rtty.RttyDecoder
        # has no thread-safety of its own, but it's only ever touched
        # here (RadioWorker delivers its own audio callbacks serially)
        # and from _on_decode_toggled's start/stop on the GUI thread,
        # which only replace/clear self._decoder while decoding is NOT
        # active -- so there's never genuine concurrent access to the
        # same RttyDecoder instance.
        if self._decoder is None:
            return
        text = self._decoder.feed(pcm_bytes)
        if text:
            self._decoded_text_ready.emit(text)

    def _on_decoded_text_ready(self, text):
        self._append_transcript(text)

    def _append_transcript(self, text):
        cursor = self.transcript.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.transcript.setTextCursor(cursor)
        self.transcript.ensureCursorVisible()

    def _on_clear_clicked(self):
        self.transcript.clear()

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
        # consuming it) that must be stopped first, same as CW/SSTV
        # Tool.
        if self.decode_toggle_button.isChecked():
            self.decode_toggle_button.setChecked(False)  # synchronously runs _on_decode_toggled(False)
        if getattr(self, "_closing_for_real", False):
            event.accept()
            return
        event.ignore()
        self.hide()
