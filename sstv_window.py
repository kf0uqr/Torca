"""
SstvToolWindow: the per-radio SSTV (Slow Scan Television) image decode
tool. Opened as a singleton from its owning RadioWindow (main_window.py),
same show-or-raise pattern as cw_window.py's CwToolWindow --
closeEvent stops any active decode tap before hiding rather than
destroying, so the same instance can be reopened freely without
losing whatever image was last received.

Decode-only (no send/encode side -- see sstv.py's own module
docstring for why there's no SSTV encoder in this app at all): a
continuously-updating image decoder (sstv.SstvDecoder) fed live PCM
via RadioWorker.start_sstv_decode(). Works alongside normal listening
audio or an active Virtual Cable on the same radio, exactly like CW
decode -- RadioWorker shares whichever RX stream is already running
rather than requiring exclusive access.

Unlike CwDecoder.feed() (returns newly-decoded text, pushed to the UI
via a Qt signal), SstvDecoder.feed() returns nothing -- there's no
discrete "append" event, just a continuously-mutating image buffer.
So there's no push signal for image data at all: a single 500ms
QTimer (same interval CW uses for its status label) polls .image/
.status/.progress_fraction/.has_signal directly instead. Cross-thread
reads of .image while the worker thread is mid-write are accepted as
harmless (worst case one stale/partial frame, self-corrects next
tick) -- no lock needed, per the approved plan.
"""

import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QFileDialog,
)

from constants import AUDIO_DEFAULT_SAMPLE_RATE
from sstv import SstvDecoder


class SstvToolWindow(QWidget):
    def __init__(self, radio_window):
        super().__init__()
        self._radio_window = radio_window
        self.setWindowTitle(f"SSTV Tool -- {radio_window._connection_label}")
        self.resize(360, 480)

        self._decoder = None  # sstv.SstvDecoder, constructed fresh each time decoding starts

        self.decode_toggle_button = QPushButton("Start Decoding")
        self.decode_toggle_button.setCheckable(True)
        self.decode_toggle_button.toggled.connect(self._on_decode_toggled)

        self.save_button = QPushButton("Save Image...")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._on_save_clicked)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.decode_toggle_button)
        buttons_row.addWidget(self.save_button)
        buttons_row.addStretch()

        self.status_label = QLabel("Not decoding.")
        self.status_label.setStyleSheet("color: #aaa;")

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)

        self.image_label = QLabel()
        self.image_label.setMinimumSize(320, 240)
        self.image_label.setStyleSheet("background-color: #111; border: 1px solid #444;")
        self.image_label.setAlignment(Qt.AlignCenter)

        layout = QVBoxLayout()
        layout.addLayout(buttons_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.image_label, stretch=1)
        self.setLayout(layout)

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(500)
        self._ui_timer.timeout.connect(self._update_ui)

    # ---- Decode ----

    def _on_decode_toggled(self, checked):
        worker = self._radio_window.worker
        if checked:
            sample_rate = getattr(worker.radio, "audio_sample_rate", None) or AUDIO_DEFAULT_SAMPLE_RATE
            self._decoder = SstvDecoder(sample_rate)
            try:
                worker.start_sstv_decode(self._on_decode_audio_frame)
            except RuntimeError as exc:
                # Defensive backstop only -- with sstv_tool_button only
                # ever enabled once the radio is connected (main_
                # window.py's _on_connected), start_sstv_decode()
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
            self.status_label.setText("Listening for VIS...")
            self._ui_timer.start()
        else:
            self._ui_timer.stop()
            worker.stop_sstv_decode()
            self._decoder = None
            self.decode_toggle_button.setText("Start Decoding")
            self.status_label.setText("Not decoding.")
            self.progress_bar.setValue(0)
            self.save_button.setEnabled(False)

    def _on_decode_audio_frame(self, pcm_bytes):
        # Runs on RadioWorker's asyncio-loop thread. sstv.SstvDecoder
        # has no thread-safety of its own, but it's only ever touched
        # here (RadioWorker delivers its own audio callbacks serially)
        # -- _update_ui below only READS its properties (.image/
        # .status/.progress_fraction/.has_signal), never mutates, so
        # there's no genuine write/write race, only the accepted
        # harmless read/write one described in this module's docstring.
        if self._decoder is None:
            return
        self._decoder.feed(pcm_bytes)

    def _update_ui(self):
        decoder = self._decoder
        if decoder is None:
            return
        self.status_label.setText(decoder.status)
        self.progress_bar.setValue(int(decoder.progress_fraction * 1000))
        if decoder.image is not None:
            self.save_button.setEnabled(True)
            self._refresh_image_preview(decoder.image)

    def _refresh_image_preview(self, image_array):
        height, width, _ = image_array.shape
        qimage = QImage(image_array.data, width, height, width * 3, QImage.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(qimage)
        self.image_label.setPixmap(pixmap)

    # ---- Save ----

    def _on_save_clicked(self):
        if self._decoder is None or self._decoder.image is None:
            return
        mode_name = (self._decoder.detected_mode_name or "sstv").replace(" ", "_")
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"sstv_{mode_name}_{timestamp}.png"
        path, _ = QFileDialog.getSaveFileName(self, "Save SSTV Image", default_name, "PNG Images (*.png)")
        if not path:
            return
        # Freshest pixels at click time, not whatever the last 500ms
        # timer tick happened to draw.
        image_array = self._decoder.image
        height, width, _ = image_array.shape
        qimage = QImage(image_array.data, width, height, width * 3, QImage.Format_RGB888).copy()
        qimage.save(path, "PNG")

    # ---- Lifecycle ----

    def closing_for_real(self):
        """Called by RadioWindow.closeEvent when the OWNING radio
        window is actually going away for good (not just this window
        being closed by the operator) -- see CwToolWindow.
        closing_for_real for the full rationale."""
        self._closing_for_real = True
        self.close()

    def closeEvent(self, event):
        # Singleton -- hidden, not destroyed -- but an active decode
        # tap is a real hardware side effect (an abandoned
        # radio.start_rx() tap would keep running with nothing
        # consuming it) that must be stopped first, same as CW Tool.
        if self.decode_toggle_button.isChecked():
            self.decode_toggle_button.setChecked(False)  # synchronously runs _on_decode_toggled(False)
        if getattr(self, "_closing_for_real", False):
            event.accept()
            return
        event.ignore()
        self.hide()
