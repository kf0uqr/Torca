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

Every completed image is also auto-saved into a persistent gallery
(sstv_images.py) -- the one-image-only live preview (see sstv.py's own
SstvDecoder._complete_image docstring: it never clears .image, so the
finished image would otherwise just sit there until silently
overwritten the instant the next transmission's VIS header arrives,
with no save prompt and no way to get it back). Completion is detected
on the audio-callback thread, right after feed() (see
_on_decode_audio_frame) rather than from the 500ms UI-timer tick, so a
completed image can never be missed just because the operator stops
decoding before the next tick fires. The actual file write/index
update is marshaled onto the GUI thread via a Qt signal (Qt's
cross-thread auto-connection queues it) since it touches the gallery
table widget.
"""

import datetime
import uuid

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QFileDialog,
)

import sstv_images
from constants import AUDIO_DEFAULT_SAMPLE_RATE
from sstv import SstvDecoder
from rx_radio import ReceiveRadioSelector


def _array_to_qimage(image_array):
    height, width, _ = image_array.shape
    return QImage(image_array.data, width, height, width * 3, QImage.Format_RGB888).copy()


class SstvToolWindow(QWidget):
    # (image_array copy, mode_name) -- emitted from RadioWorker's
    # asyncio-loop thread (see _on_decode_audio_frame); Qt auto-connects
    # this as a queued connection since the receiving slot lives on the
    # GUI thread, so _on_image_completed below always runs there.
    _image_completed = Signal(object, str)

    def __init__(self, radio_window):
        super().__init__()
        self._radio_window = radio_window
        self.setWindowTitle(radio_window.tool_title("SSTV Tool"))
        self.resize(420, 760)

        self._decoder = None  # sstv.SstvDecoder, constructed fresh each time decoding starts
        self._pending_image_saved = False  # see _on_decode_audio_frame

        self._gallery = sstv_images.load_images()
        self._populating_gallery = False  # guards against itemChanged firing during _refresh_gallery_table
        self._viewed_gallery_id = None  # id of the saved image currently shown in the preview, or None if it's the live decode

        self._image_completed.connect(self._on_image_completed)

        self.decode_toggle_button = QPushButton("Start Decoding")
        self.decode_toggle_button.setCheckable(True)
        self.decode_toggle_button.toggled.connect(self._on_decode_toggled)
        self.rx_selector = ReceiveRadioSelector(radio_window, "SSTV Tool")
        self.decode_toggle_button.toggled.connect(self.rx_selector.set_locked)   # fixed while decoding
        self.rx_selector.lost.connect(self._on_receive_radio_lost)

        self.save_button = QPushButton("Save Image...")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._on_save_clicked)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.decode_toggle_button)
        buttons_row.addWidget(self.rx_selector)
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

        # ---- Saved image gallery ----
        self.gallery_label = QLabel("Saved Images:")
        self.gallery_table = QTableWidget(0, 4)
        self.gallery_table.setHorizontalHeaderLabels(["Date", "Time", "Mode", "Name"])
        self.gallery_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.gallery_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.gallery_table.itemChanged.connect(self._on_gallery_item_changed)
        self.gallery_table.cellDoubleClicked.connect(lambda *_: self._on_view_clicked())

        gallery_buttons_row = QHBoxLayout()
        self.view_button = QPushButton("View")
        self.view_button.clicked.connect(self._on_view_clicked)
        self.export_button = QPushButton("Save to...")
        self.export_button.clicked.connect(self._on_export_clicked)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self._on_delete_clicked)
        self.live_view_button = QPushButton("Back to Live")
        self.live_view_button.clicked.connect(self._on_live_view_clicked)
        gallery_buttons_row.addWidget(self.view_button)
        gallery_buttons_row.addWidget(self.export_button)
        gallery_buttons_row.addWidget(self.delete_button)
        gallery_buttons_row.addStretch()
        gallery_buttons_row.addWidget(self.live_view_button)

        layout = QVBoxLayout()
        layout.addLayout(buttons_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.image_label, stretch=2)
        layout.addWidget(self.gallery_label)
        layout.addWidget(self.gallery_table, stretch=1)
        layout.addLayout(gallery_buttons_row)
        self.setLayout(layout)

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(500)
        self._ui_timer.timeout.connect(self._update_ui)

        self._refresh_gallery_table()

    # ---- Decode ----

    def _on_receive_radio_lost(self):
        """The radio this tool was decoding was closed: stop decoding it (the choice is back to 'this radio')."""
        if self.decode_toggle_button.isChecked():
            self.decode_toggle_button.setChecked(False)

    def _on_decode_toggled(self, checked):
        worker = self.rx_selector.worker()   # the radio whose audio is decoded
        if checked:
            sample_rate = getattr(worker.radio, "audio_sample_rate", None) or AUDIO_DEFAULT_SAMPLE_RATE
            self._decoder = SstvDecoder(sample_rate)
            self._pending_image_saved = False
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
            self._viewed_gallery_id = None
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
        # Detect completion right here rather than waiting for the UI
        # timer, so a completed image can never be lost just because
        # the operator stops decoding (or the next transmission's VIS
        # header arrives and _start_image() overwrites .image) before
        # the next 500ms tick. _pending_image_saved guards against
        # re-emitting every single remaining chunk of this same
        # completed image -- it's reset False as soon as the decoder
        # moves on to receiving a new one (is_image_complete goes back
        # to False then), so exactly one emission happens per image.
        if self._decoder.is_image_complete:
            if not self._pending_image_saved:
                self._pending_image_saved = True
                self._image_completed.emit(self._decoder.image.copy(), self._decoder.detected_mode_name or "sstv")
        else:
            self._pending_image_saved = False

    def _on_image_completed(self, image_array, mode_name):
        """Runs on the GUI thread (queued cross-thread connection from
        _on_decode_audio_frame). Writes the completed image into the
        persistent gallery, unconditionally -- every completed image is
        saved, no operator action required, exactly the safety net
        sstv.py's own docstring investigation identified as missing."""
        path, filename = sstv_images.new_image_path()
        qimage = _array_to_qimage(image_array)
        qimage.save(str(path), "PNG")
        received_at = datetime.datetime.now()
        entry = {
            "id": uuid.uuid4().hex,
            "filename": filename,
            "name": f"{mode_name} {received_at:%Y-%m-%d %H:%M:%S}",
            "mode": mode_name,
            "received_at": received_at.isoformat(),
            "connection_label": self._radio_window._connection_label,
        }
        self._gallery.append(entry)
        sstv_images.save_images(self._gallery)
        self._refresh_gallery_table()

    def _update_ui(self):
        decoder = self._decoder
        if decoder is None:
            return
        self.status_label.setText(decoder.status)
        self.progress_bar.setValue(int(decoder.progress_fraction * 1000))
        if decoder.image is not None:
            self.save_button.setEnabled(True)
            # Only redraw the live decode over whatever's in the preview
            # if the operator isn't currently looking at a saved gallery
            # image -- otherwise the next line of a freshly-started
            # transmission would yank the view away from what they
            # clicked "View" on.
            if self._viewed_gallery_id is None:
                self._refresh_image_preview(decoder.image)

    def _refresh_image_preview(self, image_array):
        pixmap = QPixmap.fromImage(_array_to_qimage(image_array))
        self.image_label.setPixmap(pixmap)

    # ---- Save (current live/in-progress decode) ----

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
        qimage = _array_to_qimage(self._decoder.image)
        qimage.save(path, "PNG")

    # ---- Gallery (saved images) ----

    def _refresh_gallery_table(self):
        self._populating_gallery = True
        try:
            self.gallery_table.setRowCount(0)
            for entry in self._gallery:
                self._append_gallery_row(entry)
        finally:
            self._populating_gallery = False

    def _append_gallery_row(self, entry):
        row = self.gallery_table.rowCount()
        self.gallery_table.insertRow(row)
        try:
            received = datetime.datetime.fromisoformat(entry["received_at"])
            date_text = received.strftime("%Y-%m-%d")
            time_text = received.strftime("%H:%M:%S")
        except (KeyError, ValueError):
            date_text, time_text = "", ""

        date_item = QTableWidgetItem(date_text)
        date_item.setFlags(date_item.flags() & ~Qt.ItemIsEditable)
        date_item.setData(Qt.UserRole, entry["id"])
        time_item = QTableWidgetItem(time_text)
        time_item.setFlags(time_item.flags() & ~Qt.ItemIsEditable)
        mode_item = QTableWidgetItem(entry.get("mode", ""))
        mode_item.setFlags(mode_item.flags() & ~Qt.ItemIsEditable)
        name_item = QTableWidgetItem(entry.get("name", ""))

        self.gallery_table.setItem(row, 0, date_item)
        self.gallery_table.setItem(row, 1, time_item)
        self.gallery_table.setItem(row, 2, mode_item)
        self.gallery_table.setItem(row, 3, name_item)

    def _selected_gallery_entry(self):
        row = self.gallery_table.currentRow()
        if row < 0:
            return None
        item = self.gallery_table.item(row, 0)
        if item is None:
            return None
        entry_id = item.data(Qt.UserRole)
        for entry in self._gallery:
            if entry["id"] == entry_id:
                return entry
        return None

    def _on_gallery_item_changed(self, item):
        if self._populating_gallery or item.column() != 3:
            return
        row = item.row()
        entry_id = self.gallery_table.item(row, 0).data(Qt.UserRole)
        for entry in self._gallery:
            if entry["id"] == entry_id:
                entry["name"] = item.text()
                sstv_images.save_images(self._gallery)
                break

    def _on_view_clicked(self):
        entry = self._selected_gallery_entry()
        if entry is None:
            self.status_label.setText("Select a saved image to view first.")
            return
        path = sstv_images.IMAGES_DIR / entry["filename"]
        qimage = QImage(str(path))
        if qimage.isNull():
            self.status_label.setText(f"Saved image file missing or unreadable: {path}")
            return
        self.image_label.setPixmap(QPixmap.fromImage(qimage))
        self._viewed_gallery_id = entry["id"]
        self.status_label.setText(f"Viewing saved image: {entry.get('name', entry['filename'])}")

    def _on_live_view_clicked(self):
        self._viewed_gallery_id = None
        if self._decoder is not None and self._decoder.image is not None:
            self._refresh_image_preview(self._decoder.image)
            self.status_label.setText(self._decoder.status)
        else:
            self.image_label.clear()

    def _on_export_clicked(self):
        entry = self._selected_gallery_entry()
        if entry is None:
            self.status_label.setText("Select a saved image to save elsewhere first.")
            return
        src_path = sstv_images.IMAGES_DIR / entry["filename"]
        if not src_path.exists():
            self.status_label.setText(f"Saved image file missing: {src_path}")
            return
        suggested = entry.get("name") or "sstv_image"
        dest_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save Image To...",
            f"{suggested}.png",
            "PNG Images (*.png);;JPEG Images (*.jpg);;Bitmap Images (*.bmp)",
        )
        if not dest_path:
            return
        qimage = QImage(str(src_path))
        if qimage.isNull() or not qimage.save(dest_path):
            self.status_label.setText(f"Failed to save image to {dest_path}")
            return
        self.status_label.setText(f"Saved to {dest_path}")

    def _on_delete_clicked(self):
        entry = self._selected_gallery_entry()
        if entry is None:
            return
        if self._viewed_gallery_id == entry["id"]:
            self._viewed_gallery_id = None
        path = sstv_images.IMAGES_DIR / entry["filename"]
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self._gallery = [e for e in self._gallery if e["id"] != entry["id"]]
        sstv_images.save_images(self._gallery)
        self._refresh_gallery_table()

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
