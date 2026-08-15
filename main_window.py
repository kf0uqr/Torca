"""
RadioWindow: the main application window. Ties together the radio
connection (via RadioWorker), all the live controls/meters/sliders, the
spectrum scope and waterfall, band buttons, and the "extra" action
buttons (Ham Dashboard, Launch WSJT-X, Rigctld, Virtual Cables). Also
owns live satellite Doppler tracking -- double-clicking a satellite on
the Ham Dashboard map selects it here (via HamClockWindow's
satellite_selected signal) rather than opening a dialog of its own, so
tracking keeps running in the background (VFO A retuned every couple
seconds, elevation/azimuth/Doppler/AOS-LOS overlaid on the spectrum
scope, like the frequency readout) and switching to another satellite
is just another double-click away.
"""

import datetime
import os
import platform
import sys

from PySide6.QtCore import Qt, QTimer, Slot, QSettings
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
from radio_worker import RadioWorker, RECEIVER_MAIN, RECEIVER_SUB
from widgets import SpectrumWidget, WaterfallWidget, MeterWidget, TuningKnobWidget
from wsjtx_rigctld import RigctldServer, RIGCTLD_DEFAULT_PORT, find_wsjtx_executable, launch_wsjtx, WSJTX_RIG_NAME
from ham_dashboard import HamClockWindow
from satellite_tracking import doppler_correction, satellite_look_angles, next_aos_los, format_countdown

SATELLITE_TRACKING_INTERVAL_MS = 2000

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

        # Live satellite tracking overlay -- populated by double-clicking
        # a satellite on the Ham Dashboard map (see _on_satellite_selected
        # below). Hidden until a satellite is actually selected. No fixed
        # width, unlike freq_display -- satellite names and countdown
        # digits both vary enough in length that a fixed box would either
        # clip or float awkwardly; a little jitter on update is the
        # tradeoff, and this is informational rather than something a
        # user is reading a tuning offset off of.
        self.satellite_overlay_label = QLabel("")
        self.satellite_overlay_label.setStyleSheet(
            "font-size: 13px; color: white; "
            "background-color: rgb(10, 10, 20); padding: 4px 10px; border-radius: 4px;"
        )
        self.satellite_overlay_label.setVisible(False)
        self.spectrum_widget.set_overlay_widget(self.satellite_overlay_label, corner="top-left")

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
        self.ptt_button.setToolTip(
            "Click to transmit, click again to release. While satellite "
            "tracking is running, this also swaps split VFO B (uplink, "
            "Doppler-corrected) in for the transmission and back to VFO A "
            "(downlink) on release."
        )
        self.ptt_button.toggled.connect(self._on_ptt_toggled)

        self.hamclock_button = QPushButton("Ham Dashboard")
        self.hamclock_button.setToolTip(
            "Opens a HamClock-inspired dashboard: day/night world map, UTC/"
            "local clocks, live solar-terrestrial data (SFI/SSN/K-index) from "
            "NOAA's public feeds, and satellite tracking. Double-click a "
            "tracked satellite there to start live elevation/azimuth/AOS-LOS "
            "and Doppler-corrected VFO A tracking here in the main window."
        )
        self.hamclock_button.clicked.connect(self._on_hamclock_button_clicked)
        self.hamclock_window = None  # created lazily on first click

        # Satellite tracking: populated by double-clicking a satellite on
        # the Ham Dashboard map (see _on_satellite_selected). Disabled
        # until then; "Stop Tracking" clears it back to this state.
        self.satellite_label = QLabel("No satellite selected")
        self.satellite_transponder_combo = QComboBox()
        self.satellite_transponder_combo.setEnabled(False)
        self.satellite_transponder_combo.setToolTip(
            "Which of the selected satellite's stored transponders to Doppler-"
            "correct VFO A against."
        )
        self.satellite_transponder_combo.currentIndexChanged.connect(self._on_satellite_transponder_changed)
        # Toggle, not a one-way stop button -- pausing/resuming keeps the
        # satellite selected (only double-clicking a different one on the
        # map replaces it). Starts unchecked/disabled; _on_satellite_selected
        # enables it and checks it (tracking starts immediately on select,
        # same as before this was a toggle).
        self.satellite_tracking_button = QPushButton("Start Tracking")
        self.satellite_tracking_button.setCheckable(True)
        self.satellite_tracking_button.setEnabled(False)
        self.satellite_tracking_button.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.satellite_tracking_button.setToolTip(
            "Pause/resume Doppler re-tuning. The satellite stays selected "
            "either way -- double-click a different one on the map to switch."
        )
        self.satellite_tracking_button.toggled.connect(self._on_satellite_tracking_toggled)

        self.satellite_row = QHBoxLayout()
        self.satellite_row.addWidget(self.satellite_label)
        self.satellite_row.addWidget(self.satellite_transponder_combo, 1)
        self.satellite_row.addWidget(self.satellite_tracking_button)

        self._active_satellite = None
        self._next_satellite_crossing = None
        # Added to a transponder's nominal downlink before Doppler
        # correction, via the tuning knob while tracking is active (see
        # _on_knob_steps) -- lets the operator tune within a linear
        # satellite's passband, or nudge to better match where it's
        # actually transmitting, without the next tracking tick
        # overwriting the adjustment. Reset whenever the satellite or
        # transponder changes, since it's only meaningful relative to
        # whichever nominal frequency it was dialed in against.
        self._satellite_freq_offset_hz = 0
        self._satellite_tracking_timer = QTimer(self)
        self._satellite_tracking_timer.timeout.connect(self._on_satellite_tracking_tick)

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
        layout.addLayout(self.satellite_row)
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
        # While actively Doppler-tracking a satellite, PTT also drives the
        # split VFO swap: B (uplink)/A (downlink) around the transmission,
        # not just start/stop_ptt() on their own. Split itself was already
        # turned on when tracking started (_start_satellite_tracking), so
        # the radio's RX never leaves VFO A regardless of what VFO B does.
        tracking_active = self._active_satellite is not None and self._satellite_tracking_timer.isActive()
        if checked:
            if tracking_active and self.worker.is_connected():
                # ptt_button.isChecked() is already True by the time this
                # slot runs, so the tick sees target_vfo="B" and selects
                # it + sets the Doppler-corrected uplink atomically,
                # before keying up.
                self._on_satellite_tracking_tick()
            self.worker.start_ptt()
        else:
            self.worker.stop_ptt()
            if tracking_active and self.worker.is_connected():
                self._on_satellite_tracking_tick()  # now sees target_vfo="A", restores VFO A + downlink atomically

    def _on_hamclock_button_clicked(self):
        if self.hamclock_window is None:
            self.hamclock_window = HamClockWindow(
                observer_lat=self._details.get("observer_lat"),
                observer_lon=self._details.get("observer_lon"),
                observer_elevation_m=self._details.get("observer_elevation_m", 0.0),
            )
            self.hamclock_window.satellite_selected.connect(self._on_satellite_selected)
        self.hamclock_window.show()
        self.hamclock_window.raise_()
        self.hamclock_window.activateWindow()

    def _on_satellite_selected(self, satellite):
        """A satellite was double-clicked on the Ham Dashboard map --
        (re)start tracking it here, replacing whatever was active
        before (it stays selected/tracked until another double-click
        replaces it -- pausing via the toggle button doesn't clear it).
        Doesn't require the radio to be connected: elevation/azimuth/
        AOS-LOS are still useful on their own, only the actual VFO
        re-tuning is gated on that (checked every tick, not just here,
        so it picks up automatically if the radio connects mid-
        session)."""
        self._active_satellite = satellite
        self._next_satellite_crossing = None
        self._satellite_freq_offset_hz = 0
        self.satellite_label.setText(satellite.get("name", "?"))

        self.satellite_transponder_combo.blockSignals(True)
        self.satellite_transponder_combo.clear()
        transponders = satellite.get("transponders", [])
        if transponders:
            for transponder in transponders:
                downlink = transponder.get("downlink_mhz") or "?"
                mode = transponder.get("mode") or "?"
                description = transponder.get("description") or "Transponder"
                self.satellite_transponder_combo.addItem(f"{description} -- {downlink} MHz {mode}", transponder)
            self.satellite_transponder_combo.setEnabled(True)
        else:
            self.satellite_transponder_combo.addItem("No transponders stored -- Doppler correction unavailable", None)
            self.satellite_transponder_combo.setEnabled(False)
        self.satellite_transponder_combo.blockSignals(False)

        self.satellite_tracking_button.setEnabled(True)
        self.satellite_overlay_label.setVisible(True)

        # Tracking always (re)starts on selection, same as before this
        # button became a toggle -- if it's already checked (switching
        # straight from one satellite to another), setChecked(True) won't
        # re-emit toggled, so start explicitly rather than relying on it.
        self.satellite_tracking_button.blockSignals(True)
        self.satellite_tracking_button.setChecked(True)
        self.satellite_tracking_button.blockSignals(False)
        self.satellite_tracking_button.setText("Stop Tracking")
        self._start_satellite_tracking()

    def _on_satellite_transponder_changed(self, _index):
        self._satellite_freq_offset_hz = 0  # only meaningful relative to the transponder it was dialed in against
        if self._active_satellite is not None:
            self._on_satellite_tracking_tick()  # reflect the new choice immediately, don't wait for the timer

    def _on_satellite_tracking_toggled(self, checked):
        self.satellite_tracking_button.setText("Stop Tracking" if checked else "Start Tracking")
        if checked:
            self._start_satellite_tracking()
        else:
            self._stop_satellite_tracking()

    def _start_satellite_tracking(self):
        # Split (VFO A/B sharing one receiver, swapped around PTT) is
        # only the right mechanism on a single-receiver radio (7300/705).
        # A genuine dual-receiver radio (9700/7610 -- self.worker.
        # is_dual_receiver, confirmed at connect time) keeps Main tuned
        # to the downlink and Sub to the uplink continuously and
        # simultaneously instead (see _on_satellite_tracking_tick) --
        # true full-duplex, no split/VFO-switching involved at all, so
        # there's nothing to enable here for that radio type.
        if self.worker.is_connected() and not self.worker.is_dual_receiver:
            # Checked explicitly (not just left to set_control_value()'s
            # own console [ERROR] line) since split's setter, like vfo's,
            # was never confirmed against real hardware -- if it's
            # silently not working, PTT would key up without split
            # actually on, and better to say so up front than have that
            # show up as an unexplained "nothing happens" on the next
            # transmission.
            if self.worker.control_available("split"):
                self.worker.set_control_value("split", True)
            else:
                QMessageBox.warning(
                    self, "Satellite Tracking",
                    "Split mode control isn't available on this radio/install -- "
                    "PTT won't be able to switch to the Doppler-corrected uplink. "
                    "Downlink tracking will still work."
                )
        self._on_satellite_tracking_tick()
        self._satellite_tracking_timer.start(SATELLITE_TRACKING_INTERVAL_MS)

    def _stop_satellite_tracking(self):
        """Pauses re-tuning -- the satellite stays selected (name,
        transponder list, last-known overlay reading) until either
        resumed or replaced by double-clicking another one. Also drops
        split back off (single-receiver radios only -- see
        _start_satellite_tracking), since it was only turned on for this."""
        self._satellite_tracking_timer.stop()
        if self.worker.is_connected() and not self.worker.is_dual_receiver:
            self.worker.set_control_value("split", False)

    def _on_satellite_tracking_tick(self):
        """Called every SATELLITE_TRACKING_INTERVAL_MS by the tracking
        timer, and also immediately (for responsiveness) on satellite/
        transponder selection, PTT press/release, and knob input.

        Two genuinely different mechanisms depending on
        self.worker.is_dual_receiver (confirmed at connect time against
        rigplane's DualReceiverCapable protocol):

        - Single-receiver radios (7300/705): one shared VFO context,
          A for RX / B for TX, swapped around PTT via split mode. Which
          VFO to target is derived fresh from ptt_button.isChecked() on
          EVERY call, not just remembered from the last PTT edge, and
          reasserted via select_vfo_and_set_frequency() (VFO select +
          frequency, atomically) every single tick rather than only at
          transitions -- an earlier version only did the VFO switch at
          PTT press/release and left the VFO alone (plain
          set_frequency()) on the steady-state ticks in between, which
          let the radio's actual selected VFO drift back on its own
          while PTT was still held, producing a repeating band-then-
          back-then-band oscillation instead of a stable TX frequency.
          Confirmed working correctly on a real 705.

        - Dual-receiver radios (9700/7610): Main and Sub are independent
          RF chains, not a single switched VFO context -- but addressing
          either one to write its frequency (IcomRadio.set_frequency()'s
          receiver= kwarg) turned out, confirmed live on a real 9700, to
          also visibly focus/select it, the same way VFO selection does
          on a single-receiver radio. Writing both every tick regardless
          of PTT state (an earlier version of this) made the radio flip
          which one was active back and forth, exactly the oscillation
          this is trying to avoid. So despite Main/Sub genuinely being
          independent hardware, the fix ends up structurally similar to
          the single-receiver path: only the ONE currently relevant to
          RX or TX ever gets a set_receiver_frequency() call -- Main
          while receiving, Sub while transmitting, never both -- so the
          radio has no reason to move off whichever one it's already on.
          Both directions' Doppler are still computed and shown together
          in the overlay (pure math, no radio I/O) even though only one
          is actually being sent."""
        satellite = self._active_satellite
        if satellite is None:
            return
        line1, line2 = satellite.get("line1", ""), satellite.get("line2", "")
        observer_lat = self._details.get("observer_lat")
        observer_lon = self._details.get("observer_lon")
        observer_elevation_km = (self._details.get("observer_elevation_m") or 0.0) / 1000.0
        now = datetime.datetime.now(datetime.timezone.utc)

        look = satellite_look_angles(line1, line2, now, observer_lat, observer_lon, observer_elevation_km)
        if look is None:
            self.satellite_overlay_label.setText(f"{satellite.get('name', '?')}\nOrbit propagation failed (invalid TLE?)")
            self.spectrum_widget.reposition_overlays()
            return

        # The AOS/LOS search is a real (if cheap) computation -- only
        # re-run it once the previously-found crossing has actually
        # passed, not on every 2-second tick.
        if self._next_satellite_crossing is None or now >= self._next_satellite_crossing["time_utc"]:
            self._next_satellite_crossing = next_aos_los(
                line1, line2, now, observer_lat, observer_lon, observer_elevation_km
            )
        crossing_text = "AOS/LOS: unknown"
        if self._next_satellite_crossing:
            remaining = (self._next_satellite_crossing["time_utc"] - now).total_seconds()
            crossing_text = f"{self._next_satellite_crossing['event']} in {format_countdown(remaining)}"

        # Compute both directions' Doppler correction every tick,
        # regardless of PTT -- which one(s) actually get sent to the
        # radio, and how, depends on whether it's dual-receiver (below).
        transponder = self.satellite_transponder_combo.currentData()
        downlink_hz, downlink_doppler_hz = None, None
        uplink_hz, uplink_doppler_hz = None, None
        if transponder is not None:
            try:
                base_downlink_hz = round(float(transponder.get("downlink_mhz")) * 1e6)
            except (TypeError, ValueError):
                base_downlink_hz = None
            if base_downlink_hz is not None:
                # The tuning knob adjusts this offset instead of the
                # radio directly while tracking is active (_on_knob_
                # steps) -- so manual tuning within a linear satellite's
                # passband, or a nudge to line up with where it's
                # actually transmitting, survives the next tick instead
                # of being overwritten by a recompute from the
                # transponder's bare nominal downlink. RX only -- there's
                # no equivalent TX offset.
                base_downlink_hz += self._satellite_freq_offset_hz
                result = doppler_correction(
                    base_downlink_hz, line1, line2, now, observer_lat, observer_lon, observer_elevation_km,
                    uplink=False,
                )
                if result is not None:
                    downlink_hz = round(result["frequency_hz"])
                    downlink_doppler_hz = result["doppler_hz"]

            try:
                base_uplink_hz = round(float(transponder.get("uplink_mhz")) * 1e6)
            except (TypeError, ValueError):
                base_uplink_hz = None
            if base_uplink_hz is not None:
                result = doppler_correction(
                    base_uplink_hz, line1, line2, now, observer_lat, observer_lon, observer_elevation_km,
                    uplink=True,
                )
                if result is not None:
                    uplink_hz = round(result["frequency_hz"])
                    uplink_doppler_hz = result["doppler_hz"]

        doppler_text = ""
        warning_text = ""
        transmitting = self.ptt_button.isChecked()
        if self.worker.is_connected():
            if self.worker.is_dual_receiver:
                # Main = downlink (RX), Sub = uplink (TX) -- but only the
                # ONE that's actually relevant right now gets touched,
                # not both every tick. Confirmed live (a real 9700):
                # setting both regardless of PTT state makes the radio
                # visibly flip which receiver is active/selected back and
                # forth -- addressing a receiver to write its frequency
                # apparently also focuses it, even though Main/Sub are
                # independent RF chains at the hardware level. Only ever
                # commanding the one currently in use (Main while
                # receiving, Sub while transmitting) is what actually
                # keeps the radio sitting still on it -- same principle
                # as the single-receiver path below never touching the
                # inactive VFO either, just without a shared context to
                # explicitly switch.
                if transmitting:
                    if uplink_hz is not None:
                        self.worker.set_receiver_frequency(RECEIVER_SUB, uplink_hz)
                        self._current_freq_hz = uplink_hz
                        self.freq_display.setText(f"{uplink_hz / 1e6:.6f} MHz")
                        self._update_band_button_highlight()
                else:
                    if downlink_hz is not None:
                        self.worker.set_receiver_frequency(RECEIVER_MAIN, downlink_hz)
                        self._current_freq_hz = downlink_hz
                        self.freq_display.setText(f"{downlink_hz / 1e6:.6f} MHz")
                        self._update_band_button_highlight()

                # Doppler for both directions still shown together (pure
                # math, no radio I/O) so the operator can see what TX
                # Doppler will be before ever keying up, even though only
                # the active direction is actually being sent to the radio.
                if downlink_doppler_hz is not None:
                    doppler_text += f"  RX Doppler {downlink_doppler_hz:+.0f} Hz"
                    if not transmitting and self._satellite_freq_offset_hz:
                        doppler_text += f"  Offset {self._satellite_freq_offset_hz:+.0f} Hz"
                if uplink_doppler_hz is not None:
                    doppler_text += f"  TX Doppler {uplink_doppler_hz:+.0f} Hz"
            else:
                # Single-receiver radio (7300/705): one shared VFO
                # context -- A for RX, B for TX, swapped around PTT.
                target_vfo = "B" if transmitting else "A"
                freq_hz = uplink_hz if transmitting else downlink_hz
                if not self.worker.control_available("vfo"):
                    # Surfaced here, inline, rather than only as a
                    # console [ERROR] line from set_control_value()/
                    # select_vfo_and_set_frequency() themselves -- "vfo"'s
                    # setter was never confirmed against real hardware
                    # (see its CONTROL_DEFINITIONS entry), so if it's not
                    # actually working on this radio, split-mode PTT
                    # would otherwise fail completely silently from the
                    # operator's chair.
                    warning_text = "  [VFO control unavailable -- can't switch bands]"
                elif freq_hz is not None:
                    self.worker.select_vfo_and_set_frequency(target_vfo, freq_hz)
                else:
                    self.worker.set_control_value("vfo", target_vfo)

                if freq_hz is not None and not warning_text:
                    # Update optimistically, same as _on_knob_steps/
                    # _on_band_selected -- otherwise the main frequency
                    # readout and band-button highlight just sit still
                    # until the next 0.5s poll cycle confirms them, which
                    # reads as "frozen", especially right at a PTT-
                    # triggered VFO/band switch.
                    self._current_freq_hz = freq_hz
                    self.freq_display.setText(f"{freq_hz / 1e6:.6f} MHz")
                    self._update_band_button_highlight()

                direction = "TX" if transmitting else "RX"
                active_doppler_hz = uplink_doppler_hz if transmitting else downlink_doppler_hz
                if active_doppler_hz is not None:
                    doppler_text = f"  {direction} Doppler {active_doppler_hz:+.0f} Hz"
                    if not transmitting and self._satellite_freq_offset_hz:
                        doppler_text += f"  Offset {self._satellite_freq_offset_hz:+.0f} Hz"

        visibility = "up" if look["elevation_deg"] >= 0 else "down"
        self.satellite_overlay_label.setText(
            f"{satellite.get('name', '?')} ({visibility})\n"
            f"El {look['elevation_deg']:.1f}°  Az {look['azimuth_deg']:.1f}°{doppler_text}{warning_text}\n"
            f"{crossing_text}"
        )
        self.spectrum_widget.reposition_overlays()

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
        step_hz = self.step_combo.currentData()
        # While actively Doppler-tracking a transponder, the knob adjusts
        # the offset applied to that transponder's nominal downlink
        # instead of commanding the radio directly -- otherwise the next
        # tracking tick (up to 2s later) would just overwrite any manual
        # adjustment with a fresh nominal+Doppler computation. This is
        # how an operator tunes within a linear satellite's passband, or
        # nudges to better match where it's actually transmitting.
        if (self._active_satellite is not None and self._satellite_tracking_timer.isActive()
                and self.satellite_transponder_combo.currentData() is not None):
            self._satellite_freq_offset_hz += steps * step_hz
            self._on_satellite_tracking_tick()  # apply immediately, don't wait for the next timer tick
            return

        if self._current_freq_hz is None:
            return  # haven't heard a frequency from the radio yet
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
        self._satellite_tracking_timer.stop()
        if self.hamclock_window is not None:
            self.hamclock_window.close()
        if self.rigctld_server is not None:
            self.rigctld_server.stop()
        if self.virtual_cable_button.isChecked():
            self.worker.disable_virtual_cables()  # best-effort; worker.stop() below may cut this off before it finishes
        self.worker.stop()
        self.worker.wait(2000)  # give the polling loop a moment to exit cleanly
        event.accept()
