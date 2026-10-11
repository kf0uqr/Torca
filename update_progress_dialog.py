"""Progress dialog shown while an in-app update runs.

The update itself (updater.perform_update) is a sequence of git/pip/
install.sh subprocesses that can take a minute or two with nothing else
on screen, so this shows that something is happening: the current step
(relayed from perform_update's status callback), an animated busy bar --
indeterminate on purpose, since pip gives no usable percentage -- and an
elapsed-time counter that keeps ticking even during a long single step.

Non-modal: the rest of the app (radios, decoding) stays usable while it
runs. The window has no close button and ignores Esc until finish() is
called, because closing the app mid-update is exactly what must not
happen casually (see HamClockWindow.closeEvent's long wait on the update
worker)."""

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout


def format_elapsed(seconds):
    seconds = max(int(seconds), 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


class UpdateProgressDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Updating TORCA")
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)  # title bar, no close button
        self.setMinimumWidth(420)
        self._finished = False
        self._started = time.monotonic()

        self.heading = QLabel("Updating TORCA...")
        self.heading.setStyleSheet("font-weight: bold;")
        self.status_label = QLabel("Starting...")
        self.status_label.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)       # indeterminate: animates for as long as the dialog is up
        self.bar.setTextVisible(False)
        self.elapsed_label = QLabel(f"Elapsed: {format_elapsed(0)}")
        self.elapsed_label.setStyleSheet("color: #aaa;")
        hint = QLabel("You can keep using TORCA while this runs. Please don't close it until the update finishes.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #aaa;")

        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(self.status_label)
        layout.addWidget(self.bar)
        layout.addWidget(self.elapsed_label)
        layout.addWidget(hint)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_status(self, message):
        self.status_label.setText(message)

    def _tick(self):
        self.elapsed_label.setText(f"Elapsed: {format_elapsed(time.monotonic() - self._started)}")

    def finish(self):
        """The update has ended (either way): stop the clock and close."""
        self._finished = True
        self._timer.stop()
        self.accept()

    def reject(self):
        if self._finished:
            super().reject()  # Esc / window-manager close are ignored while the update is running

    def closeEvent(self, event):
        if self._finished:
            event.accept()
        else:
            event.ignore()
