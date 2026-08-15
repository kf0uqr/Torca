"""
RadioWindow: the main application window. Ties together the radio
connection (via RadioWorker), all the live controls/meters/sliders, the
spectrum scope and waterfall, band buttons, and the "extra" action
buttons (Ham Dashboard, Launch WSJT-X, Rigctld, Virtual Cables).
"""

import os
import platform
import sys

from PySide6.QtCore import Qt, Slot, QSettings
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QSlider,
    QComboBox,
    QPushButton,
    QMessageBox,
    QFileDialog,
)

from constants import (
    RADIO_BANDS,
    LEVEL_DEFINITIONS,
    CONTROL_DEFINITIONS,
    CONTROL_OPTION_EXCLUDED,
    TUNING_STEPS,
)
from radio_worker import RadioWorker
from widgets import SpectrumWidget, WaterfallWidget, MeterWidget, TuningKnobWidget
from wsjtx_rigctld import RigctldServer, RIGCTLD_DEFAULT_PORT, find_wsjtx_executable, launch_wsjtx, WSJTX_RIG_NAME
from ham_dashboard import HamClockWindow

class RadioWindow(QWidget):
    def __init__(self, details):
        super().__init__()
        self._details = details
        if details["connection_type"] == "network":
            self._connection_label = f"{details['host']} (LAN)"
        else:
            self._connection_label = f"{details['serial_port']} (USB)"
        self.setWindowTitle(f"Icom Radio Control -- {details['radio_model']}")

        self.freq_display = QLabel("-- MHz")
        self.freq_display.setStyleSheet(
            "font-size: 28px; font-weight: bold; color: white; "
            # Matches SpectrumWidget's own background fill (QColor(10, 10,
            # 20)) exactly, rather than a semi-transparent black, since
            # this label sits directly on top of that scope.
            "background-color: rgb(10, 10, 20); padding: 4px 10px; border-radius: 4px;"
        )
        self.freq_display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # Fixed width sized for the longest realistic reading ("999.999999
        # MHz") so the overlay box doesn't resize/jump as digits change --
        # only its position (top-right of the scope) needs recalculating,
        # and only on resize, not on every frequency update. 230px was too
        # narrow at this font size/padding -- right-aligned text overflowing
        # a too-narrow fixed-width label clips off the LEFT edge (the
        # visible symptom), so this needs headroom rather than an exact fit.
        self.freq_display.setFixedWidth(280)

        # Three independently double-click-switchable meters side by side,
        # like a radio's multi-function meter that can show several
        # readings at once. Different defaults so they're not all showing
        # S-Meter out of the box; each is free to be switched to any of
        # METER_DEFINITIONS regardless of what the others show.
        self.meter_widget = MeterWidget(meter_type="s_meter")
        self.meter_widget_2 = MeterWidget(meter_type="power")
        self.meter_widget_3 = MeterWidget(meter_type="swr")
        self.meter_widgets = [self.meter_widget, self.meter_widget_2, self.meter_widget_3]
        self.meters_row = QHBoxLayout()
        for widget in self.meter_widgets:
            self.meters_row.addWidget(widget)

        # A second row of three more, same idea, covering the remaining
        # meter types with useful defaults so all six show something
        # different out of the box (all nine are still reachable from any
        # of the six via double-click).
        self.meter_widget_4 = MeterWidget(meter_type="alc")
        self.meter_widget_5 = MeterWidget(meter_type="voltage")
        self.meter_widget_6 = MeterWidget(meter_type="comp")
        self.meter_widgets.extend([self.meter_widget_4, self.meter_widget_5, self.meter_widget_6])
        self.meters_row_2 = QHBoxLayout()
        for widget in (self.meter_widget_4, self.meter_widget_5, self.meter_widget_6):
            self.meters_row_2.addWidget(widget)

        # One button per band this radio supports, rather than a dropdown --
        # built now since the radio model (and therefore band list) is
        # already known from the connection dialog, before the radio itself
        # connects. Buttons stay disabled until _on_connected() enables them.
        self.band_buttons = []
        self.band_button_ranges = []  # parallel list: (button, low_hz, high_hz), for highlighting the active band
        self.band_buttons_row = QHBoxLayout()
        for label, low_hz, high_hz in RADIO_BANDS.get(details["radio_model"], []):
            button = QPushButton(label)
            button.setEnabled(False)
            button.setToolTip(f"{low_hz / 1e6:.3f}\u2013{high_hz / 1e6:.3f} MHz")
            button.clicked.connect(
                lambda checked=False, lbl=label, f=low_hz: self._on_band_selected(lbl, f)
            )
            self.band_buttons.append(button)
            self.band_button_ranges.append((button, low_hz, high_hz))
            self.band_buttons_row.addWidget(button)

        self.spectrum_widget = SpectrumWidget()
        self.spectrum_widget.set_overlay_widget(self.freq_display)
        self.waterfall_widget = WaterfallWidget()

        self.tuning_knob = TuningKnobWidget()
        self.tuning_knob.setEnabled(False)
        self.tuning_knob.steps_changed.connect(self._on_knob_steps)

        self.step_combo = QComboBox()
        for label, step_hz in TUNING_STEPS:
            self.step_combo.addItem(label, step_hz)
        self.step_combo.setCurrentIndex(2)  # default to 1 kHz steps

        self._current_freq_hz = None  # last known frequency, used as the knob's baseline

        self.ptt_button = QPushButton("PTT")
        self.ptt_button.setFixedHeight(40)
        self.ptt_button.setCheckable(True)
        self.ptt_button.setStyleSheet(
            "QPushButton { background-color: #a33; color: white; font-weight: bold; border-radius: 4px; }"
            "QPushButton:checked { background-color: #f22; }"
            "QPushButton:disabled { background-color: #555; color: #999; }"
        )
        self.ptt_button.setEnabled(False)
        self.ptt_button.setToolTip("Click to transmit, click again to release")
        self.ptt_button.toggled.connect(self._on_ptt_toggled)

        self.hamclock_button = QPushButton("Ham Dashboard")
        self.hamclock_button.setToolTip(
            "Opens a HamClock-inspired dashboard: day/night world map, UTC/"
            "local clocks, live solar-terrestrial data (SFI/SSN/K-index) from "
            "NOAA's public feeds, and satellite tracking. Doesn't need the "
            "radio connected, except to double-click a satellite and start "
            "Doppler correction."
        )
        self.hamclock_button.clicked.connect(self._on_hamclock_button_clicked)
        self.hamclock_window = None  # created lazily on first click

        self.wsjtx_button = QPushButton("Launch WSJT-X")
        self.wsjtx_button.setToolTip(
            f"Launches WSJT-X in its own isolated profile (--rig-name={WSJTX_RIG_NAME}), "
            "separate from your main WSJT-X settings. Use the Rigctld button below to "
            "let it control this radio."
        )
        self.wsjtx_button.clicked.connect(self._on_wsjtx_button_clicked)

        self.rigctld_port_input = QSpinBox()
        self.rigctld_port_input.setRange(1, 65535)
        self.rigctld_port_input.setValue(RIGCTLD_DEFAULT_PORT)
        self.rigctld_port_input.setToolTip("TCP port for the rigctld server to listen on.")

        self.rigctld_button = QPushButton(f"Rigctld: OFF (port {RIGCTLD_DEFAULT_PORT})")
        self.rigctld_button.setCheckable(True)
        self.rigctld_button.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.rigctld_button.setToolTip(
            "Lets other CAT-aware apps (WSJT-X, JTDX, fldigi, ...) control this "
            "radio through this app's connection -- select \"Hamlib NET rigctl\" "
            "(rig model 2) and 127.0.0.1:<port above> in that app."
        )
        self.rigctld_button.toggled.connect(self._on_rigctld_toggled)
        self.rigctld_server = None  # created lazily on first enable

        self.virtual_cable_button = QPushButton("Virtual Cables: OFF")
        self.virtual_cable_button.setCheckable(True)
        self.virtual_cable_button.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.virtual_cable_button.setEnabled(False)
        self.virtual_cable_button.setToolTip(
            "Creates two virtual audio devices (Linux/PulseAudio or PipeWire "
            "only) so an external app (WSJT-X, etc.) can send/receive audio "
            "through this app's radio connection, in place of the physical "
            "devices chosen in the connection dialog. Click again to switch "
            "back to those original devices."
        )
        self.virtual_cable_button.toggled.connect(self._on_virtual_cable_toggled)

        self.status_label = QLabel("Connecting...")

        # AF Gain/Squelch/Monitor/TX Level/RF Level -- built generically
        # from LEVEL_DEFINITIONS, all disabled until _on_connected().
        self.level_sliders = {}
        self.level_labels = {}
        levels_row = QVBoxLayout()
        for key, definition in LEVEL_DEFINITIONS.items():
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 100)
            slider.setEnabled(False)
            slider.valueChanged.connect(lambda value, k=key: self._on_level_changed(k, value))
            label = QLabel(f"{definition['label']}: --")
            row = QHBoxLayout()
            row.addWidget(label)
            row.addWidget(slider)
            levels_row.addLayout(row)
            self.level_sliders[key] = slider
            self.level_labels[key] = label

        # Mode/Digital/NR/NB/AGC/Preamp/Filter/VFO -- built generically
        # from CONTROL_DEFINITIONS: a combo box ("combo" type), a
        # checkable toggle button ("toggle" type), or a plain click-to-
        # swap button ("vfo_toggle" type). All disabled until
        # _on_connected() enables them.
        self.control_widgets = {}
        controls_row = QHBoxLayout()
        vfo_toggle_column = None  # lazily created; all vfo_toggle-type buttons stack in this one column
        for key, definition in CONTROL_DEFINITIONS.items():
            if definition["type"] == "combo":
                widget = QComboBox()
                excluded_labels = CONTROL_OPTION_EXCLUDED.get((details["radio_model"], key), set())
                for option_label, option_value in definition["options"]:
                    if option_label in excluded_labels:
                        continue
                    widget.addItem(option_label, option_value)
                widget.setEnabled(False)
                widget.currentIndexChanged.connect(
                    lambda index, k=key, w=widget: self._on_control_combo_changed(k, w)
                )
                label = QLabel(definition["label"])
                col = QVBoxLayout()
                col.addWidget(label, alignment=Qt.AlignHCenter)
                col.addWidget(widget)
                # controls_row gets stretched to match the much taller knob
                # column next to it in tuning_row -- without this stretch,
                # Qt distributes that extra height by pushing the combo
                # down away from its label instead of leaving it flush.
                col.addStretch()
                controls_row.addLayout(col)
                controls_row.setAlignment(col, Qt.AlignTop)
            elif definition["type"] == "vfo_toggle":
                # A single button showing the current state, like the real
                # A/B or V/M button -- click swaps to the other option,
                # rather than a dropdown to pick from. Starts on the first
                # option's label; _on_control_updated() corrects it to
                # the radio's actual state once connected (except for
                # write_only entries, which never get corrected -- see
                # the tooltip below). All buttons of this type share one
                # vertical column (e.g. VFO A/B with VFO/MEM stacked
                # directly under it), rather than each getting its own
                # flat slot in the row.
                widget = QPushButton(definition["options"][0][0])
                widget.setEnabled(False)
                if definition.get("write_only"):
                    widget.setToolTip(
                        "This radio can't report its current state for this control "
                        "(CI-V read-only limitation) -- this button shows the last "
                        "command sent, not a live-confirmed value."
                    )
                widget.clicked.connect(lambda checked=False, k=key: self._on_vfo_toggle_clicked(k))
                if vfo_toggle_column is None:
                    vfo_toggle_column = QVBoxLayout()
                    controls_row.addLayout(vfo_toggle_column)
                    controls_row.setAlignment(vfo_toggle_column, Qt.AlignTop)
                vfo_toggle_column.addWidget(widget)
            else:  # "toggle"
                widget = QPushButton(definition["label"])
                widget.setCheckable(True)
                widget.setEnabled(False)
                widget.setStyleSheet(
                    "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
                )
                widget.toggled.connect(lambda checked, k=key: self._on_control_toggled(k, checked))
                controls_row.addWidget(widget)
                controls_row.setAlignment(widget, Qt.AlignTop)
            self.control_widgets[key] = widget

        # Extra action buttons (WSJT-X, Rigctld, and whatever gets added
        # later) -- a horizontal row of its own, kept as an attribute so
        # future buttons can just be appended to it directly.
        rigctld_column = QVBoxLayout()
        rigctld_column.addWidget(self.rigctld_port_input)
        rigctld_column.addWidget(self.rigctld_button)

        self.extra_buttons_row = QHBoxLayout()
        self.extra_buttons_row.addWidget(self.hamclock_button)
        self.extra_buttons_row.addWidget(self.wsjtx_button)
        self.extra_buttons_row.addLayout(rigctld_column)
        self.extra_buttons_row.addWidget(self.virtual_cable_button)
        self.extra_buttons_row.addStretch()

        # controls_row (Mode/Digital/NR/NB/AGC/Preamp/Filter/VFO) and
        # extra_buttons_row stack vertically together, to the left of the
        # tuning knob -- not mixed into knob_row, which is the knob's own
        # column on the right.
        left_column = QVBoxLayout()
        left_column.addLayout(controls_row)
        left_column.addLayout(self.extra_buttons_row)

        knob_row = QVBoxLayout()
        knob_row.addWidget(self.tuning_knob, alignment=Qt.AlignHCenter)
        knob_row.addWidget(self.step_combo, alignment=Qt.AlignHCenter)
        knob_row.addWidget(self.ptt_button)

        tuning_row = QHBoxLayout()
        tuning_row.addLayout(left_column)
        tuning_row.addStretch()
        tuning_row.addLayout(knob_row)

        layout = QVBoxLayout()
        layout.addWidget(self.spectrum_widget)
        layout.addWidget(self.waterfall_widget)
        layout.addLayout(self.meters_row)
        layout.addLayout(self.meters_row_2)
        layout.addLayout(self.band_buttons_row)
        layout.addLayout(tuning_row)
        layout.addLayout(levels_row)
        layout.addWidget(self.status_label)
        self.setLayout(layout)

        self.worker = RadioWorker(details)
        self.worker.connected.connect(self._on_connected)
        self.worker.connection_failed.connect(self._on_connection_failed)
        self.worker.frequency_updated.connect(self._on_frequency_updated)
        self.worker.meter_updated.connect(self._on_meter_updated)
        self.worker.scope_frame_received.connect(self._on_scope_frame)
        self.worker.error.connect(self._on_error)
        self.worker.audio_status.connect(self._on_audio_status)
        self.worker.level_updated.connect(self._on_level_updated)
        self.worker.control_updated.connect(self._on_control_updated)
        self.worker.start()

    @Slot()
    def _on_connected(self):
        self.status_label.setText(f"Connected to {self._connection_label}")
        self.tuning_knob.setEnabled(True)
        self.ptt_button.setEnabled(True)
        for button in self.band_buttons:
            button.setEnabled(True)
        for slider in self.level_sliders.values():
            slider.setEnabled(True)
        for widget in self.control_widgets.values():
            widget.setEnabled(True)
        self.virtual_cable_button.setEnabled(True)

    @Slot(str)
    def _on_connection_failed(self, message):
        self.status_label.setText("Connection failed")
        QMessageBox.critical(self, "Connection Error", message)

    @Slot(int)
    def _on_frequency_updated(self, freq_hz):
        self._current_freq_hz = freq_hz
        self.freq_display.setText(f"{freq_hz / 1e6:.6f} MHz")
        self._update_band_button_highlight()

    @Slot(str, int)
    def _on_meter_updated(self, meter_type, level):
        # Each meter widget independently filters for whichever type it's
        # currently showing -- guards against a stale reading landing
        # just after the user double-clicked to a different meter type
        # mid-poll-cycle, same as before, just applied per-widget now.
        for widget in self.meter_widgets:
            if widget.meter_type == meter_type:
                widget.set_value(level)

    @Slot(object)
    def _on_scope_frame(self, frame):
        self.spectrum_widget.set_frame(frame)
        self.waterfall_widget.set_frame(frame)

    @Slot(str)
    def _on_error(self, message):
        print(f"[ERROR] {message}", file=sys.stderr)

    @Slot(str)
    def _on_audio_status(self, message):
        print(f"[AUDIO] {message}")

    @Slot(str, float)
    def _on_level_updated(self, key, value):
        slider = self.level_sliders.get(key)
        label = self.level_labels.get(key)
        if slider is None:
            return
        percent = round(value * 100)
        # Block signals while reflecting the radio's actual value so this
        # doesn't immediately fire _on_level_changed and write it straight
        # back to the radio.
        if slider.value() != percent:
            slider.blockSignals(True)
            slider.setValue(percent)
            slider.blockSignals(False)
        label.setText(f"{LEVEL_DEFINITIONS[key]['label']}: {percent}%")

    def _on_level_changed(self, key, value):
        self.level_labels[key].setText(f"{LEVEL_DEFINITIONS[key]['label']}: {value}%")
        self.worker.set_level_value(key, value / 100.0)

    def _on_control_combo_changed(self, key, widget):
        value = widget.currentData()
        self.worker.set_control_value(key, value)

    def _on_control_toggled(self, key, checked):
        self.worker.set_control_value(key, checked)

    def _on_ptt_toggled(self, checked):
        self.ptt_button.setText("TRANSMITTING" if checked else "PTT")
        if checked:
            self.worker.start_ptt()
        else:
            self.worker.stop_ptt()

    def _on_hamclock_button_clicked(self):
        if self.hamclock_window is None:
            self.hamclock_window = HamClockWindow(
                worker=self.worker,
                observer_lat=self._details.get("observer_lat"),
                observer_lon=self._details.get("observer_lon"),
                observer_elevation_m=self._details.get("observer_elevation_m", 0.0),
            )
        self.hamclock_window.show()
        self.hamclock_window.raise_()
        self.hamclock_window.activateWindow()

    def _on_wsjtx_button_clicked(self):
        settings = QSettings("IcomRadioApp", "RadioControl")
        path = settings.value("wsjtx_executable_path", "")

        if not path or not os.path.isfile(path):
            path = find_wsjtx_executable()

        if not path:
            path, _filter = QFileDialog.getOpenFileName(
                self, "Locate the WSJT-X executable",
                "", "Executable (*.exe);;All files (*)" if platform.system() == "Windows" else "All files (*)",
            )
            if not path:
                return  # user cancelled the browse dialog

        try:
            launch_wsjtx(path)
        except OSError as exc:
            QMessageBox.critical(self, "WSJT-X", f"Couldn't launch WSJT-X at:\n{path}\n\n{exc}")
            return

        # Only remember the path once it's actually confirmed to work
        # (subprocess.Popen not raising means the OS accepted it).
        settings.setValue("wsjtx_executable_path", path)

    def _on_rigctld_toggled(self, checked):
        if checked:
            port = self.rigctld_port_input.value()
            self.rigctld_server = RigctldServer(
                get_freq=lambda: self._current_freq_hz or 0,
                set_freq=lambda hz: self.worker.set_frequency(int(hz)),
                get_mode=lambda: (self.control_widgets["mode"].currentData() or "USB", 0),
                set_mode=lambda mode: self.worker.set_control_value("mode", mode),
                get_ptt=lambda: self.ptt_button.isChecked(),
                set_ptt=lambda on: self.ptt_button.setChecked(on),  # reuses the normal PTT path via its toggled signal
                port=port,
            )
            try:
                self.rigctld_server.start()
            except RuntimeError as exc:
                QMessageBox.critical(self, "Rigctld", str(exc))
                self.rigctld_server = None
                self.rigctld_button.setChecked(False)
                return
            self.rigctld_button.setText(f"Rigctld: ON (port {port})")
            self.rigctld_port_input.setEnabled(False)  # changing port on a live server needs a stop/restart first
        else:
            port = self.rigctld_port_input.value()
            if self.rigctld_server is not None:
                self.rigctld_server.stop()
                self.rigctld_server = None
            self.rigctld_button.setText(f"Rigctld: OFF (port {port})")
            self.rigctld_port_input.setEnabled(True)

    def _on_virtual_cable_toggled(self, checked):
        if checked:
            self.virtual_cable_button.setText("Virtual Cables: ON")
            self.worker.enable_virtual_cables()
        else:
            self.virtual_cable_button.setText("Virtual Cables: OFF")
            self.worker.disable_virtual_cables()

    def _on_vfo_toggle_clicked(self, key):
        definition = CONTROL_DEFINITIONS[key]
        options = definition["options"]
        current_label = self.control_widgets[key].text()
        labels = [label for label, _value in options]
        try:
            index = labels.index(current_label)
        except ValueError:
            index = 0
        # Swap to the OTHER option (cycles through all if there were more
        # than two, but VFO A/B only ever has two).
        target_label, target_value = options[(index + 1) % len(options)]
        self.worker.set_control_value(key, target_value)

    @Slot(str, object)
    def _on_control_updated(self, key, value):
        widget = self.control_widgets.get(key)
        if widget is None:
            return
        definition = CONTROL_DEFINITIONS[key]
        # blockSignals while reflecting the radio's actual state so this
        # doesn't immediately fire _on_control_combo_changed/_on_control_
        # toggled and write the value straight back to the radio.
        if definition["type"] == "combo":
            index = widget.findData(value)
            if index != -1 and widget.currentIndex() != index:
                widget.blockSignals(True)
                widget.setCurrentIndex(index)
                widget.blockSignals(False)
        elif definition["type"] == "vfo_toggle":
            label = next((lbl for lbl, val in definition["options"] if val == value), None)
            if label and widget.text() != label:
                widget.setText(label)
        else:  # "toggle"
            checked = bool(value)
            if widget.isChecked() != checked:
                widget.blockSignals(True)
                widget.setChecked(checked)
                widget.blockSignals(False)

    def _on_band_selected(self, band_label, low_edge_hz):
        self.worker.select_band(band_label, low_edge_hz)
        # Update optimistically using the band's low edge -- if the IC-7300
        # band-stacking register recalls a different frequency within the
        # band, the next poll cycle corrects this to the real value.
        self._current_freq_hz = low_edge_hz
        self.freq_display.setText(f"{low_edge_hz / 1e6:.6f} MHz")
        self._update_band_button_highlight()

    def _on_knob_steps(self, steps):
        if self._current_freq_hz is None:
            return  # haven't heard a frequency from the radio yet
        step_hz = self.step_combo.currentData()
        new_freq_hz = max(0, self._current_freq_hz + steps * step_hz)
        self.worker.set_frequency(new_freq_hz)
        # Update optimistically so the readout feels responsive while
        # spinning the knob; the next poll cycle will confirm/correct it.
        self._current_freq_hz = new_freq_hz
        self.freq_display.setText(f"{new_freq_hz / 1e6:.6f} MHz")
        self._update_band_button_highlight()

    def _update_band_button_highlight(self):
        """Turns the button for whichever band the current frequency
        falls in green, matching the toggle-button convention used
        elsewhere in this app -- and clears it for every other band."""
        freq = self._current_freq_hz
        for button, low_hz, high_hz in self.band_button_ranges:
            active = freq is not None and low_hz <= freq <= high_hz
            button.setStyleSheet(
                "QPushButton { background-color: #2a6; color: white; font-weight: bold; }" if active else ""
            )

    def closeEvent(self, event):
        if self.hamclock_window is not None:
            self.hamclock_window.close()
        if self.rigctld_server is not None:
            self.rigctld_server.stop()
        if self.virtual_cable_button.isChecked():
            self.worker.disable_virtual_cables()  # best-effort; worker.stop() below may cut this off before it finishes
        self.worker.stop()
        self.worker.wait(2000)  # give the polling loop a moment to exit cleanly
        event.accept()
