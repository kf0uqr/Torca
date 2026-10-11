"""The Audio tab: one row per connected radio showing what its audio is doing
and where it is heard.

Each radio's audio arrives at an audio hub (audio.AudioBridge) that feeds every
decoder, the recorder and the web remote at full level, whatever is chosen
here. This tab only controls the MONITOR -- what is played on the computer's
real audio devices: per radio a level meter (the audio the decoders are being
fed), volume, mute, and the output device (the system default, a particular
sound card or virtual cable, or none -- silent, decoders unaffected).

Choices are remembered per radio (matched by model + where it connects) and
applied again when that radio reconnects.
"""

import time

from PySide6.QtCore import Qt, QSettings, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QLabel,
    QProgressBar,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import workspace_state
from constants import AUDIO_DEVICE_SYSTEM_DEFAULT

try:
    import sounddevice as sd
except ImportError:   # no audio library: only the system default / silent choices
    sd = None

_SETTINGS_ORG = "IcomRadioApp"
_SETTINGS_APP = "RadioControl"

_NO_AUDIO_SECONDS = 3.0         # silent for this long while "receiving": say so
_SYSTEM_DEFAULT = "__default__"
_SILENT = "__silent__"


def output_devices():
    """[(name, index)] of the computer's output devices, one per name."""
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
    except Exception:
        return []
    seen, found = set(), []
    for index, device in enumerate(devices):
        name = device.get("name", "")
        if device.get("max_output_channels", 0) > 0 and name not in seen:
            seen.add(name)
            found.append((name, index))
    return found


def _radio_key(window):
    return workspace_state.identity_key(workspace_state.radio_identity(window._details))


class _RadioRow(QFrame):
    def __init__(self, window, settings_group):
        super().__init__()
        self.window = window
        self._group = settings_group
        self._applied = False
        self._smoothed = 0.0
        self.setFrameShape(QFrame.StyledPanel)

        self.title = QLabel(f"<b>{window.windowTitle()}</b>")
        self.status = QLabel("Connecting...")
        self.status.setStyleSheet("color: #aaa;")
        self.meter = QProgressBar()
        self.meter.setRange(0, 1000)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(10)
        self.meter.setToolTip("The level of this radio's audio -- what its decoders are being fed.")
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(100)
        self.volume.setToolTip("Volume of what you hear. Decoders always get the full-level audio.")
        self.mute = QCheckBox("Mute")
        self.output = QComboBox()
        self.output.setToolTip("Where this radio's audio is played. 'Silent' plays nothing; decoders still work.")
        self.note = QLabel("")
        self.note.setStyleSheet("color: #e8a33d;")

        grid = QGridLayout(self)
        grid.addWidget(self.title, 0, 0, 1, 3)
        grid.addWidget(self.status, 0, 3, 1, 2, Qt.AlignRight)
        grid.addWidget(self.meter, 1, 0, 1, 5)
        grid.addWidget(QLabel("Play on:"), 2, 0)
        grid.addWidget(self.output, 2, 1)
        grid.addWidget(QLabel("Volume:"), 2, 2)
        grid.addWidget(self.volume, 2, 3)
        grid.addWidget(self.mute, 2, 4)
        grid.addWidget(self.note, 3, 0, 1, 5)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(3, 1)

        self.refresh_devices()
        self.output.activated.connect(self._on_output_chosen)
        self.volume.valueChanged.connect(self._on_volume_changed)
        self.mute.toggled.connect(self._on_mute_toggled)

    # ---- the device list ----

    def refresh_devices(self):
        current = self.output.currentData()
        self.output.blockSignals(True)
        self.output.clear()
        self.output.addItem("System default", _SYSTEM_DEFAULT)
        self.output.addItem("Silent (decoders only)", _SILENT)
        for name, _index in output_devices():
            self.output.addItem(name, name)
        i = self.output.findData(current) if current is not None else 0
        self.output.setCurrentIndex(max(i, 0))
        self.output.blockSignals(False)

    @staticmethod
    def _device_for(choice):
        """The bridge's device value for a combo choice."""
        if choice == _SILENT:
            return None
        if choice == _SYSTEM_DEFAULT:
            return AUDIO_DEVICE_SYSTEM_DEFAULT
        for name, index in output_devices():
            if name == choice:
                return index
        return AUDIO_DEVICE_SYSTEM_DEFAULT   # that device is gone: fall back to the default

    # ---- controls ----

    def _on_output_chosen(self, _index):
        choice = self.output.currentData()
        ok = self.window.worker.set_monitor_output(self._device_for(choice))
        self.note.setText("" if ok else "Couldn't open that device -- nothing is being played.")
        self._group.setValue("output", choice)

    def _on_volume_changed(self, value):
        self.window.worker.set_monitor(volume=value / 100.0)
        self._group.setValue("volume", value)

    def _on_mute_toggled(self, muted):
        self.window.worker.set_monitor(muted=muted)
        self._group.setValue("muted", bool(muted))

    def _apply_saved(self):
        """Once the radio's audio hub exists: put its monitor back as it was last time."""
        group = self._group
        choice = group.value("output", None)
        volume = int(group.value("volume", 100) or 100) if group.contains("volume") else 100
        muted = str(group.value("muted", "false")).lower() in ("true", "1")
        self.volume.blockSignals(True); self.mute.blockSignals(True)
        self.volume.setValue(volume); self.mute.setChecked(muted)
        self.volume.blockSignals(False); self.mute.blockSignals(False)
        self.window.worker.set_monitor(volume=volume / 100.0, muted=muted)
        if choice is not None:
            i = self.output.findData(choice)
            if i >= 0:
                self.output.setCurrentIndex(i)
                self.note.setText("" if self.window.worker.set_monitor_output(self._device_for(choice))
                                  else "Couldn't open that device -- nothing is being played.")
        self._applied = True

    # ---- updating ----

    def refresh(self):
        worker = self.window.worker
        info = worker.audio_hub_status()
        self.title.setText(f"<b>{self.window.windowTitle()}</b>")
        if info is None:
            self.status.setText("No audio yet" )
            self.meter.setValue(0)
            self._set_controls_enabled(False)
            return
        if not self._applied:
            self._apply_saved()
        self._set_controls_enabled(info["monitor_supported"])
        level = worker.audio_level()
        self._smoothed = max(level * 1000.0, self._smoothed * 0.85)
        self.meter.setValue(min(1000, int(self._smoothed)))
        age = info["audio_age"]
        consumers = info["consumers"]
        feeds = f"feeding {consumers} tool{'s' if consumers != 1 else ''}" if consumers else "no tools using it"
        if not info["receiving"]:
            text = "not receiving"
        elif age is None or age > _NO_AUDIO_SECONDS:
            text = "no audio arriving" if age is None else f"no audio for {int(age)} s"
        else:
            text = f"{info['sample_rate']} Hz, {feeds}"
        self.status.setText(text)
        if not info["monitor_supported"]:
            self.note.setText("This radio's audio comes from a sound card; it is not played by TORCA.")

    def _set_controls_enabled(self, enabled):
        for widget in (self.output, self.volume, self.mute):
            widget.setEnabled(enabled)


class AudioTab(QWidget):
    def __init__(self, dashboard):
        super().__init__()
        self._dashboard = dashboard
        self.setWindowTitle("Audio")
        self._rows = {}      # RadioWindow -> _RadioRow

        intro = QLabel(
            "Each radio's audio goes to its decoders, recorder and web remote at full level no matter what is "
            "set here. These controls only decide what you hear on this computer."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #aaa;")
        self.empty_label = QLabel("No radio is connected.")
        self._rows_layout = QVBoxLayout()
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.empty_label)
        layout.addLayout(self._rows_layout)
        layout.addStretch()

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.refresh)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def refresh(self):
        radios = list(getattr(self._dashboard, "_connected_radios", []))
        for window in [w for w in self._rows if w not in radios]:
            row = self._rows.pop(window)
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        for window in radios:
            if window not in self._rows:
                group = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
                group.beginGroup(f"audio/{_radio_key(window)}")
                row = _RadioRow(window, group)
                self._rows[window] = row
                self._rows_layout.addWidget(row)
        self.empty_label.setVisible(not self._rows)
        for row in self._rows.values():
            row.refresh()
