"""
CwToolWindow: the per-radio CW (Morse code) send/decode tool. Opened
as a singleton from its owning RadioWindow (main_window.py), same
show-or-raise pattern as ham_dashboard.py's LogBookWindow -- closeEvent
stops any active decode tap or in-progress CW send (real hardware side
effects, unlike Log Book) before hiding rather than destroying, so the
same instance can be reopened freely without losing its transcript.

Send: the radio's own built-in keyer over CI-V (RadioWorker.
send_cw_text/stop_cw_text) -- no local audio/DSP involved at all.

Decode: an auto-adaptive tone decoder (cw.CwDecoder) fed live PCM from
a direct RadioWorker.start_cw_decode() audio tap. Can't run at the same
time as an active Virtual Cable for the same radio (rigplane's
start_rx() supports only one registered callback per radio) -- the
toggle is disabled with an explanatory tooltip in that case rather than
trying to arbitrate between the two.
"""

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTextEdit,
)

from constants import AUDIO_DEFAULT_SAMPLE_RATE
from cw import CwDecoder


class CwToolWindow(QWidget):
    # Internal cross-thread hop only: _on_decode_audio_frame runs on
    # RadioWorker's asyncio-loop thread (same requirement as every
    # other RadioWorker callback in this app) and must not touch any
    # widget directly -- emitting this signal is the hand-off back to
    # the GUI thread, same pattern as RadioWorker's own signals.
    _decoded_text_ready = Signal(str)

    def __init__(self, radio_window):
        super().__init__()
        self._radio_window = radio_window
        self.setWindowTitle(f"CW Tool -- {radio_window._connection_label}")
        self.resize(480, 440)

        self._decoder = None  # cw.CwDecoder, constructed fresh each time decoding starts

        # ---- Send ----

        self.send_text_edit = QLineEdit()
        self.send_text_edit.setPlaceholderText("Text to send as CW...")
        self.send_text_edit.returnPressed.connect(self._on_send_clicked)

        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._on_send_clicked)
        self.stop_send_button = QPushButton("Stop")
        self.stop_send_button.clicked.connect(self._on_stop_send_clicked)

        send_buttons_row = QHBoxLayout()
        send_buttons_row.addWidget(self.send_button)
        send_buttons_row.addWidget(self.stop_send_button)
        send_buttons_row.addStretch()

        self.wpm_spin = QSpinBox()
        self.wpm_spin.setRange(6, 48)
        self.wpm_spin.setSuffix(" WPM")
        self.wpm_spin.valueChanged.connect(self._on_wpm_spin_changed)

        self.pitch_spin = QSpinBox()
        self.pitch_spin.setRange(300, 900)
        self.pitch_spin.setSingleStep(5)
        self.pitch_spin.setSuffix(" Hz")
        self.pitch_spin.valueChanged.connect(self._on_pitch_spin_changed)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Speed:"))
        speed_row.addWidget(self.wpm_spin)
        speed_row.addWidget(QLabel("Pitch:"))
        speed_row.addWidget(self.pitch_spin)
        speed_row.addStretch()

        send_group = QVBoxLayout()
        send_group.addWidget(QLabel("<b>Send</b>"))
        send_group.addWidget(self.send_text_edit)
        send_group.addLayout(send_buttons_row)
        send_group.addLayout(speed_row)

        # ---- Decode ----

        self.decode_toggle_button = QPushButton("Start Decoding")
        self.decode_toggle_button.setCheckable(True)
        self.decode_toggle_button.toggled.connect(self._on_decode_toggled)

        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self._on_clear_clicked)

        decode_buttons_row = QHBoxLayout()
        decode_buttons_row.addWidget(self.decode_toggle_button)
        decode_buttons_row.addWidget(self.clear_button)
        decode_buttons_row.addStretch()

        self.decode_status_label = QLabel("Not decoding.")
        self.decode_status_label.setStyleSheet("color: #aaa;")

        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)

        decode_group = QVBoxLayout()
        decode_group.addWidget(QLabel("<b>Decode</b>"))
        decode_group.addLayout(decode_buttons_row)
        decode_group.addWidget(self.decode_status_label)
        decode_group.addWidget(self.transcript)

        layout = QVBoxLayout()
        layout.addLayout(send_group)
        layout.addLayout(decode_group)
        self.setLayout(layout)

        self._decoded_text_ready.connect(self._on_decoded_text_ready)

        worker = radio_window.worker
        worker.key_speed_changed.connect(self._on_key_speed_changed)
        worker.cw_pitch_changed.connect(self._on_cw_pitch_changed)

        # One-shot initial sync from RadioWorker's own already-polled
        # cache, not a fresh read: unlike every other synced control in
        # this app (built inside RadioWindow.__init__, before the
        # worker's poll loop has ever run once), this window is opened
        # on demand, potentially long after polling started -- the
        # poll loop only emits key_speed_changed/cw_pitch_changed on a
        # genuine CHANGE from its last-observed value, so relying on a
        # future emission to arrive would risk waiting forever if
        # nothing happens to change after this window opens.
        if worker._last_observed_key_speed is not None:
            self.wpm_spin.blockSignals(True)
            self.wpm_spin.setValue(worker._last_observed_key_speed)
            self.wpm_spin.blockSignals(False)
        if worker._last_observed_cw_pitch is not None:
            self.pitch_spin.blockSignals(True)
            self.pitch_spin.setValue(worker._last_observed_cw_pitch)
            self.pitch_spin.blockSignals(False)

        # Decoder's WPM estimate/tone-detected state changes
        # continuously as marks arrive, not on a discrete event -- this
        # just refreshes the status label periodically while decoding,
        # rather than needing cw.CwDecoder to push its own updates.
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._update_decode_status)

        self._update_decode_toggle_enabled()

    # ---- Send ----

    def _on_send_clicked(self):
        text = self.send_text_edit.text().strip()
        if not text:
            return
        self._radio_window.worker.send_cw_text(text)

    def _on_stop_send_clicked(self):
        self._radio_window.worker.stop_cw_text()

    def _on_wpm_spin_changed(self, value):
        self._radio_window.worker.set_key_speed(value)

    def _on_pitch_spin_changed(self, value):
        self._radio_window.worker.set_cw_pitch(value)

    def _on_key_speed_changed(self, wpm):
        if self.wpm_spin.value() != wpm:
            self.wpm_spin.blockSignals(True)
            self.wpm_spin.setValue(wpm)
            self.wpm_spin.blockSignals(False)

    def _on_cw_pitch_changed(self, hz):
        if self.pitch_spin.value() != hz:
            self.pitch_spin.blockSignals(True)
            self.pitch_spin.setValue(hz)
            self.pitch_spin.blockSignals(False)

    # ---- Decode ----

    def _update_decode_toggle_enabled(self):
        audio_bridge_active = self._radio_window.worker.audio_bridge is not None
        # Never disable it out from under an already-running decode
        # session -- only gates STARTING a new one.
        self.decode_toggle_button.setEnabled(
            not audio_bridge_active or self.decode_toggle_button.isChecked()
        )
        if audio_bridge_active:
            self.decode_toggle_button.setToolTip(
                "Disabled: this radio's audio is already in use by an active "
                "Virtual Cable. rigplane only supports one registered RX "
                "audio callback per radio at a time -- stop the Virtual "
                "Cable for this radio (Ham Dashboard) before decoding."
            )
        else:
            self.decode_toggle_button.setToolTip("")

    def _on_decode_toggled(self, checked):
        worker = self._radio_window.worker
        if checked:
            sample_rate = getattr(worker.radio, "audio_sample_rate", None) or AUDIO_DEFAULT_SAMPLE_RATE
            self._decoder = CwDecoder(sample_rate, self.pitch_spin.value())
            try:
                worker.start_cw_decode(self._on_decode_audio_frame)
            except RuntimeError as exc:
                self._decoder = None
                self.decode_status_label.setText(str(exc))
                # Nothing was actually started (start_cw_decode raised
                # before that), so this is just a UI correction, not a
                # real stop -- block signals so it doesn't re-enter
                # this method via toggled(False) and overwrite the
                # error message above with "Not decoding."
                self.decode_toggle_button.blockSignals(True)
                self.decode_toggle_button.setChecked(False)
                self.decode_toggle_button.blockSignals(False)
                self._update_decode_toggle_enabled()
                return
            self.decode_toggle_button.setText("Stop Decoding")
            self.decode_status_label.setText("Decoding -- listening for tone...")
            self._status_timer.start()
        else:
            self._status_timer.stop()
            worker.stop_cw_decode()
            if self._decoder is not None:
                trailing = self._decoder.flush()
                if trailing:
                    self._append_transcript(trailing)
            self._decoder = None
            self.decode_toggle_button.setText("Start Decoding")
            self.decode_status_label.setText("Not decoding.")
        self._update_decode_toggle_enabled()

    def _on_decode_audio_frame(self, pcm_bytes):
        # Runs on RadioWorker's asyncio-loop thread. cw.CwDecoder has
        # no thread-safety of its own, but it's only ever touched here
        # (RadioWorker delivers its own audio callbacks serially) and
        # from _on_decode_toggled's start/stop on the GUI thread, which
        # only replace/clear self._decoder while decoding is NOT
        # active -- so there's never genuine concurrent access to the
        # same CwDecoder instance.
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

    def _update_decode_status(self):
        if self._decoder is None:
            return
        if self._decoder.has_signal:
            self.decode_status_label.setText(f"Decoding -- tone detected, ~{self._decoder.current_wpm:.0f} WPM")
        else:
            self.decode_status_label.setText("Decoding -- listening for tone...")

    def _on_clear_clicked(self):
        self.transcript.clear()

    # ---- Lifecycle ----

    def showEvent(self, event):
        super().showEvent(event)
        self._update_decode_toggle_enabled()

    def closing_for_real(self):
        """Called by RadioWindow.closeEvent when the OWNING radio
        window is actually going away for good (not just this window
        being closed by the operator) -- lets closeEvent below really
        close instead of its usual hide-and-keep-the-singleton-alive
        behavior. Without this, closing the radio connection would
        leave an orphaned CW Tool window on screen, still wired to a
        RadioWorker that just stopped."""
        self._closing_for_real = True
        self.close()

    def closeEvent(self, event):
        # Singleton -- hidden, not destroyed (main_window.py reopens
        # this same instance) -- but an active decode tap or an
        # in-progress CW send is a real hardware side effect that must
        # be stopped first: an abandoned radio.start_rx() tap would
        # otherwise keep running with nothing consuming it, and leaving
        # CW sending mid-message is worse than leaving PTT keyed (same
        # "always attempt to clean up" reasoning as RadioWorker.
        # _stop_ptt).
        if self.decode_toggle_button.isChecked():
            self.decode_toggle_button.setChecked(False)  # synchronously runs _on_decode_toggled(False)
        self._radio_window.worker.stop_cw_text()
        if getattr(self, "_closing_for_real", False):
            event.accept()
            return
        event.ignore()
        self.hide()
