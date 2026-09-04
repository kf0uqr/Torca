"""Recording Tool: manual/satellite-synced audio recording, a
persistent list of past recordings, playback with a seek bar, export,
and delete. See recording.py for the actual capture/storage logic --
this file is UI + lifecycle only, following the same singleton-tool-
window pattern as memories_window.py's MemoriesWindow.
"""

import datetime
import os
import shutil
import uuid

from PySide6.QtCore import Qt, QTimer, QUrl, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import recording

_SOURCE_LABELS = {"rx": "RX", "tx": "TX", "both": "Both (RX+TX)"}
_SOURCE_VALUES = {v: k for k, v in _SOURCE_LABELS.items()}
_RX_CHANNEL_LABELS = {"main": "Main", "sub": "Sub", "both": "Both (stereo)"}
_RX_CHANNEL_VALUES = {v: k for k, v in _RX_CHANNEL_LABELS.items()}


def _format_duration(seconds):
    seconds = int(round(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_ms(ms):
    return _format_duration((ms or 0) / 1000.0)


class RecordingToolWindow(QWidget):
    def __init__(self, radio_window):
        super().__init__()
        self._radio_window = radio_window
        self.setWindowTitle(f"Recording Tool -- {radio_window._connection_label}")
        self.resize(640, 520)
        self._closing_for_real = False

        self._recordings = recording.load_recordings()
        self._recorder = None            # active recording.Recorder, or None
        self._recording_entry = None     # dict being built for the in-progress recording
        self._auto_started = False       # True if the CURRENT recording was satellite-sync-started
        self._last_elevation_up = None   # None until the first satellite tick with a known state
        self._last_satellite_name = None  # name from the most recent satellite tick, used to resume after a reconnect
        self._populating_table = False   # guards against itemChanged firing during _refresh_table
        self._playing_id = None          # id of the recording currently loaded in the player

        # ---- Controls ----
        self.source_combo = QComboBox()
        for label in ("RX", "TX", "Both (RX+TX)"):
            self.source_combo.addItem(label)
        self.source_combo.currentTextChanged.connect(self._on_source_changed)

        self.rx_channel_combo = QComboBox()
        self._is_dual_receiver = bool(getattr(radio_window.worker, "is_dual_receiver", False))
        self.rx_channel_label = QLabel("RX channel:")
        if not self._is_dual_receiver:
            self.rx_channel_combo.setVisible(False)
            self.rx_channel_label.setVisible(False)

        self.record_button = QPushButton("● Record")
        self.record_button.setCheckable(True)
        self.record_button.setStyleSheet(
            "QPushButton:checked { background-color: #c33; color: white; font-weight: bold; }"
        )
        self.record_button.toggled.connect(self._on_record_toggled)

        self.elapsed_label = QLabel("")

        self.sync_checkbox = QCheckBox("Sync with selected satellite (start at AOS, stop at LOS)")
        if radio_window._role == "non_sat":
            self.sync_checkbox.setVisible(False)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #c33;")

        controls_row = QHBoxLayout()
        controls_row.addWidget(QLabel("Source:"))
        controls_row.addWidget(self.source_combo)
        controls_row.addWidget(self.rx_channel_label)
        controls_row.addWidget(self.rx_channel_combo)
        controls_row.addWidget(self.record_button)
        controls_row.addWidget(self.elapsed_label)
        controls_row.addStretch()

        self._refresh_rx_channel_options()

        # ---- Recordings list ----
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Date", "Time", "Length", "Name"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.table.cellDoubleClicked.connect(lambda *_: self._on_play_clicked())

        list_buttons_row = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self._on_play_clicked)
        self.export_button = QPushButton("Export...")
        self.export_button.clicked.connect(self._on_export_clicked)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self._on_delete_clicked)
        list_buttons_row.addWidget(self.play_button)
        list_buttons_row.addWidget(self.export_button)
        list_buttons_row.addWidget(self.delete_button)
        list_buttons_row.addStretch()

        # ---- Playback ----
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.durationChanged.connect(self._on_player_duration_changed)
        self._player.positionChanged.connect(self._on_player_position_changed)
        self._player.playbackStateChanged.connect(self._on_player_state_changed)

        self.play_pause_button = QPushButton("▶")
        self.play_pause_button.setEnabled(False)
        self.play_pause_button.clicked.connect(self._on_play_pause_clicked)
        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setEnabled(False)
        self.seek_slider.sliderMoved.connect(self._player.setPosition)
        self.position_label = QLabel("0:00 / 0:00")

        playback_row = QHBoxLayout()
        playback_row.addWidget(self.play_pause_button)
        playback_row.addWidget(self.seek_slider)
        playback_row.addWidget(self.position_label)

        layout = QVBoxLayout()
        layout.addLayout(controls_row)
        layout.addWidget(self.sync_checkbox)
        layout.addWidget(self.status_label)
        layout.addWidget(self.table)
        layout.addLayout(list_buttons_row)
        layout.addLayout(playback_row)
        self.setLayout(layout)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_elapsed_label)

        radio_window.ptt_button.toggled.connect(self._on_ptt_toggled)
        if radio_window._role != "non_sat":
            radio_window._satellite_session.state_updated.connect(self._on_satellite_state_updated)
        radio_window.worker.reconnecting.connect(self._on_worker_reconnecting)
        radio_window.worker.reconnected.connect(self._on_worker_reconnected)

        self._refresh_table()

    # ---- Source/RX-channel option handling ----

    def _on_source_changed(self, _label):
        self._refresh_rx_channel_options()

    def _refresh_rx_channel_options(self):
        """Repopulates rx_channel_combo's available options for the
        currently-selected source -- "Both" (stereo Main+Sub) only
        makes sense when source is plain RX (a stereo file); RX+TX
        combined is always a single mono track (per design), so only
        Main/Sub (not stereo Both) are offered there. Preserves the
        current selection if it's still valid, otherwise defaults to
        the first option."""
        if not self._is_dual_receiver:
            self._rx_channel_relevant = False
            return
        source = _SOURCE_VALUES[self.source_combo.currentText()]
        if source == "tx":
            self._rx_channel_relevant = False
            self.rx_channel_combo.setVisible(False)
            self.rx_channel_label.setVisible(False)
            return
        self._rx_channel_relevant = True
        self.rx_channel_combo.setVisible(True)
        self.rx_channel_label.setVisible(True)
        previous = self.rx_channel_combo.currentText()
        allowed = ["main", "sub"] if source == "both" else ["main", "sub", "both"]
        self.rx_channel_combo.blockSignals(True)
        self.rx_channel_combo.clear()
        for value in allowed:
            self.rx_channel_combo.addItem(_RX_CHANNEL_LABELS[value])
        index = self.rx_channel_combo.findText(previous)
        self.rx_channel_combo.setCurrentIndex(index if index >= 0 else 0)
        self.rx_channel_combo.blockSignals(False)

    # ---- Recording start/stop ----

    def _on_record_toggled(self, checked):
        if checked:
            self._start_recording()
            if self._recorder is None:
                # _start_recording already reported why and left the
                # button unchecked; nothing else to do.
                return
            self.record_button.setText("■ Stop")
        else:
            self._stop_recording()
            self.record_button.setText("● Record")

    def _start_recording(self, satellite=None, auto=False):
        worker = self._radio_window.worker
        bridge = getattr(worker, "audio_bridge", None)
        if bridge is None:
            self.status_label.setText("Can't record: no audio stream is active for this radio.")
            self.record_button.blockSignals(True)
            self.record_button.setChecked(False)
            self.record_button.blockSignals(False)
            return
        self.status_label.setText("")

        source = _SOURCE_VALUES[self.source_combo.currentText()]
        rx_channel = None
        if self._rx_channel_relevant:
            rx_channel = _RX_CHANNEL_VALUES[self.rx_channel_combo.currentText()]

        path, filename = recording.new_recording_path()
        self._recorder = recording.Recorder(
            path,
            source=source,
            rx_channel=rx_channel,
            rx_sample_rate=bridge.sample_rate,
            rx_is_stereo=bridge.is_rx_stereo(),
        )
        bridge.add_extra_rx_raw_callback(self._recorder.on_rx_raw)
        bridge.add_extra_tx_callback(self._recorder.on_tx)
        if self._radio_window.ptt_button.isChecked():
            self._recorder.set_tx_active(True)

        started_at = datetime.datetime.now()
        if satellite:
            default_name = f"{satellite} pass {started_at:%Y-%m-%d %H:%M}"
        else:
            default_name = started_at.strftime("%Y-%m-%d %H:%M:%S")
        self._recording_entry = {
            "id": uuid.uuid4().hex,
            "filename": filename,
            "name": default_name,
            "started_at": started_at.isoformat(),
            "duration_sec": 0.0,
            "source": source,
            "rx_channel": rx_channel,
            "satellite": satellite,
            "connection_label": self._radio_window._connection_label,
        }
        self._recording_started_at = started_at
        self._auto_started = auto
        self._elapsed_timer.start()
        self._update_elapsed_label()
        if not self.record_button.isChecked():
            self.record_button.blockSignals(True)
            self.record_button.setChecked(True)
            self.record_button.blockSignals(False)
            self.record_button.setText("■ Stop")

    def _stop_recording(self):
        if self._recorder is None:
            return
        bridge = getattr(self._radio_window.worker, "audio_bridge", None)
        if bridge is not None:
            bridge.remove_extra_rx_raw_callback(self._recorder.on_rx_raw)
            bridge.remove_extra_tx_callback(self._recorder.on_tx)
        duration = self._recorder.close()
        entry = self._recording_entry
        entry["duration_sec"] = duration
        self._recordings.append(entry)
        recording.save_recordings(self._recordings)
        self._recorder = None
        self._recording_entry = None
        self._auto_started = False
        self._elapsed_timer.stop()
        self.elapsed_label.setText("")
        if self.record_button.isChecked():
            self.record_button.blockSignals(True)
            self.record_button.setChecked(False)
            self.record_button.blockSignals(False)
        self._refresh_table()

    def _update_elapsed_label(self):
        if self._recorder is None:
            return
        elapsed = (datetime.datetime.now() - self._recording_started_at).total_seconds()
        self.elapsed_label.setText(f"Recording... {_format_duration(elapsed)}")

    def _on_ptt_toggled(self, checked):
        if self._recorder is not None:
            self._recorder.set_tx_active(checked)

    # ---- Reconnect handling ----

    def _on_worker_reconnecting(self, _attempt, _retry_in):
        """Fires once per reconnect attempt, repeatedly, while the
        connection is down -- only the FIRST one (while self._recorder
        is still set) matters here; stopping the recording makes every
        later firing during the same drop a no-op via the same guard,
        so there's no separate "just went down" edge to track."""
        if self._recorder is None:
            return
        # Tag the entry as interrupted before _stop_recording() appends
        # it to the index, so it's visibly distinct in the Name column
        # -- otherwise a recording truncated by a dropped connection
        # looks identical in the list to one that ended normally, the
        # exact ambiguity that made the original "0 second recording"
        # report hard to diagnose.
        self._recording_entry["name"] += " (interrupted -- connection lost)"
        self._stop_recording()

    def _on_worker_reconnected(self):
        """Resumes satellite-sync recording (as a new file covering the
        remainder of the pass) if sync is enabled, nothing is currently
        recording, and the tracked satellite was still above the
        horizon when the connection dropped -- mirrors a fresh AOS."""
        if not self.sync_checkbox.isChecked():
            return
        if self._recorder is not None:
            return
        if self._last_elevation_up:
            self._start_recording(satellite=self._last_satellite_name, auto=True)

    # ---- Satellite sync ----

    @Slot(object, object, str, object, object, str)
    def _on_satellite_state_updated(self, satellite, look, _crossing_text, _downlink_doppler_hz, _uplink_doppler_hz, _warning_text):
        if not self.sync_checkbox.isChecked():
            self._last_elevation_up = None
            return
        elevation_deg = look.get("elevation_deg") if isinstance(look, dict) else None
        if elevation_deg is None:
            return
        self._last_satellite_name = satellite.get("name") if isinstance(satellite, dict) else str(satellite)
        up = elevation_deg >= 0
        if self._last_elevation_up is None:
            self._last_elevation_up = up
            return
        if up and not self._last_elevation_up:
            # AOS -- only auto-start if nothing is already recording
            # (a manual recording already in progress takes priority;
            # don't interrupt or duplicate it).
            if self._recorder is None:
                name = satellite.get("name") if isinstance(satellite, dict) else str(satellite)
                self._start_recording(satellite=name, auto=True)
        elif not up and self._last_elevation_up:
            # LOS -- only auto-stop a recording THIS feature started.
            # A manually-started recording stays running under manual
            # control even if the tracked satellite happens to set,
            # matching this app's existing "don't surprise the
            # operator" convention (see _on_active_receiver_changed's
            # own docstring in main_window.py).
            if self._recorder is not None and self._auto_started:
                self._stop_recording()
        self._last_elevation_up = up

    # ---- List / table ----

    def _refresh_table(self):
        self._populating_table = True
        try:
            self.table.setRowCount(0)
            for entry in self._recordings:
                self._append_row(entry)
        finally:
            self._populating_table = False

    def _append_row(self, entry):
        row = self.table.rowCount()
        self.table.insertRow(row)
        try:
            started = datetime.datetime.fromisoformat(entry["started_at"])
            date_text = started.strftime("%Y-%m-%d")
            time_text = started.strftime("%H:%M:%S")
        except (KeyError, ValueError):
            date_text, time_text = "", ""
        length_text = _format_duration(entry.get("duration_sec"))

        date_item = QTableWidgetItem(date_text)
        date_item.setFlags(date_item.flags() & ~Qt.ItemIsEditable)
        date_item.setData(Qt.UserRole, entry["id"])
        time_item = QTableWidgetItem(time_text)
        time_item.setFlags(time_item.flags() & ~Qt.ItemIsEditable)
        length_item = QTableWidgetItem(length_text)
        length_item.setFlags(length_item.flags() & ~Qt.ItemIsEditable)
        name_item = QTableWidgetItem(entry.get("name", ""))

        self.table.setItem(row, 0, date_item)
        self.table.setItem(row, 1, time_item)
        self.table.setItem(row, 2, length_item)
        self.table.setItem(row, 3, name_item)

    def _selected_entry(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        entry_id = item.data(Qt.UserRole)
        for entry in self._recordings:
            if entry["id"] == entry_id:
                return entry
        return None

    def _on_table_item_changed(self, item):
        if self._populating_table or item.column() != 3:
            return
        row = item.row()
        entry_id = self.table.item(row, 0).data(Qt.UserRole)
        for entry in self._recordings:
            if entry["id"] == entry_id:
                entry["name"] = item.text()
                recording.save_recordings(self._recordings)
                break

    # ---- Play / export / delete ----

    def _on_play_clicked(self):
        entry = self._selected_entry()
        if entry is None:
            return
        path = recording.RECORDINGS_DIR / entry["filename"]
        if not path.exists():
            self.status_label.setText(f"Recording file missing: {path}")
            return
        self.status_label.setText("")
        self._playing_id = entry["id"]
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()
        self.play_pause_button.setEnabled(True)
        self.seek_slider.setEnabled(True)

    def _on_play_pause_clicked(self):
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_player_state_changed(self, state):
        self.play_pause_button.setText(
            "⏸" if state == QMediaPlayer.PlaybackState.PlayingState else "▶"
        )

    def _on_player_duration_changed(self, duration_ms):
        self.seek_slider.setRange(0, duration_ms)
        self._update_position_label()

    def _on_player_position_changed(self, position_ms):
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(position_ms)
        self._update_position_label()

    def _update_position_label(self):
        self.position_label.setText(
            f"{_format_ms(self._player.position())} / {_format_ms(self._player.duration())}"
        )

    def _on_export_clicked(self):
        entry = self._selected_entry()
        if entry is None:
            self.status_label.setText("Select a recording to export first.")
            return
        src_path = recording.RECORDINGS_DIR / entry["filename"]
        if not src_path.exists():
            self.status_label.setText(f"Recording file missing: {src_path}")
            return
        filters = ["WAV files (*.wav)"]
        if shutil.which("ffmpeg"):
            filters += ["MP3 files (*.mp3)", "FLAC files (*.flac)", "OGG files (*.ogg)"]
        suggested = entry.get("name") or "recording"
        dest_path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export Recording", f"{suggested}.wav", ";;".join(filters)
        )
        if not dest_path:
            return
        try:
            recording.export_recording(src_path, dest_path)
            self.status_label.setStyleSheet("color: #2a6;")
            self.status_label.setText(f"Exported to {dest_path}")
        except Exception as exc:
            self.status_label.setStyleSheet("color: #c33;")
            self.status_label.setText(f"Export failed: {exc}")

    def _on_delete_clicked(self):
        entry = self._selected_entry()
        if entry is None:
            return
        if self._playing_id == entry["id"]:
            self._player.stop()
            self._player.setSource(QUrl())
            self.play_pause_button.setEnabled(False)
            self.seek_slider.setEnabled(False)
            self._playing_id = None
        path = recording.RECORDINGS_DIR / entry["filename"]
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        self._recordings = [e for e in self._recordings if e["id"] != entry["id"]]
        recording.save_recordings(self._recordings)
        self._refresh_table()

    # ---- Lifecycle ----

    def closing_for_real(self):
        """Called by RadioWindow.closeEvent when the OWNING radio
        window is actually going away for good -- lets closeEvent below
        really close instead of its usual hide-and-keep-the-singleton-
        alive behavior. Also cleanly stops any in-progress recording
        rather than leaving an open, never-finalized wave file behind."""
        self._closing_for_real = True
        if self._recorder is not None:
            self._stop_recording()
        self.close()

    def closeEvent(self, event):
        if self._closing_for_real:
            event.accept()
        else:
            event.ignore()
            self.hide()
