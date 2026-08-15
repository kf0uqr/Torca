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
    DUAL_RECEIVER_LEVEL_KEYS,
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
            "tracking is running, this also swaps VFO B (uplink, "
            "Doppler-corrected) in for the transmission and back to VFO A "
            "(downlink) on release."
        )
        self.ptt_button.toggled.connect(self._on_ptt_toggled)

        # Dual-receiver (9700/7610) only -- rigplane's select_receiver(),
        # confirmed via its own docstring to issue the real main_select/
        # sub_select CI-V opcode (0x07 0xD0/0xD1) and update RadioState.
        # active. Originally added to test whether this is what actually
        # routes PTT/TX to Sub -- confirmed live (plus independently by
        # Icom's own documented behavior) that it isn't: PTT always
        # transmits from Main, full stop, a real hardware limitation.
        # Kept as a manual control since it does drive something useful
        # -- the scope/waterfall (set_scope_receiver, called alongside
        # it) -- so you can look at Sub's downlink (e.g. to see your own
        # signal) while Main handles TX. Hidden for single-receiver
        # radios (_on_connected).
        self.active_receiver_button = QPushButton("Active: MAIN")
        self.active_receiver_button.setEnabled(False)
        self.active_receiver_button.setVisible(False)
        self.active_receiver_button.setToolTip(
            "Dual-receiver only: makes Main or Sub the radio's active "
            "receiver and switches the scope/waterfall to match -- e.g. "
            "switch to Sub to see your own signal on the downlink while "
            "Main handles TX. PTT always transmits from Main regardless "
            "of this (confirmed hardware behavior on the 9700/7610, not "
            "something this switches)."
        )
        self.active_receiver_button.clicked.connect(self._on_active_receiver_toggle_clicked)

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
        # Dual-receiver (9700/7610) only, satellite mode's periodic
        # tracking tick specifically: whether Sub's own VFO A has
        # already been explicitly selected for the current tracking
        # session (via select_receiver_vfo_and_set_frequency). Once
        # selected, Sub's direction never changes on its own (downlink
        # only, tracked continuously while receiving -- see
        # _on_satellite_tracking_tick), so subsequent ticks can just use
        # a bare set_receiver_frequency instead of reselecting every
        # time -- confirmed working for this specific, tightly-looped
        # (2-second) use. Manual Sub frequency adjustment (the tuning
        # knob, band selection) does NOT reuse this flag -- confirmed
        # live on a real 9700 that a one-time-select-then-bare-writes
        # approach for those, mirroring this, did NOT reliably work
        # (nothing on Sub moved at all); they instead bundle the VFO-A
        # select into every single write, unconditionally. Reset to
        # False in _start_satellite_tracking so a fresh tracking session
        # always reselects it.
        self._sub_vfo_a_selected = False
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
        #
        # On a dual-receiver radio, AF Gain/Squelch/RF Level (the
        # DUAL_RECEIVER_LEVEL_KEYS entries) turned out NOT to be
        # addressable per-receiver via receiver= at all -- confirmed
        # live on a real 9700 that moving the (Main-targeting, as
        # always) AF Gain slider actually controlled SUB once Sub was
        # made the active receiver via active_receiver_button, i.e.
        # these commands just follow whichever receiver is active
        # (select_receiver()) regardless of what receiver= is passed.
        # (An earlier version of this added a second, dedicated "(Sub)"
        # slider using explicit receiver= addressing -- confirmed live
        # that one never worked at all, exactly consistent with this.)
        # So there's only ever ONE slider per level now; its label gets
        # a live " (Sub)" suffix from _level_receiver_suffix whenever
        # the active receiver isn't Main, so it's clear which receiver
        # it's currently actually affecting -- see
        # _on_active_receiver_toggle_clicked.
        self._level_receiver_suffix = {key: "" for key in DUAL_RECEIVER_LEVEL_KEYS}
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
        knob_row.addWidget(self.active_receiver_button)

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
        self.worker.active_receiver_changed.connect(self._on_active_receiver_changed)
        self.worker.start()

    @Slot()
    def _on_connected(self):
        self.status_label.setText(f"Connected to {self._connection_label}")
        self.tuning_knob.setEnabled(True)
        self.ptt_button.setEnabled(True)
        if self.worker.is_dual_receiver:
            self.active_receiver_button.setVisible(True)
            self.active_receiver_button.setEnabled(True)
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
        suffix = self._level_receiver_suffix.get(key, "")
        label.setText(f"{LEVEL_DEFINITIONS[key]['label']}{suffix}: {percent}%")

    def _on_level_changed(self, key, value):
        suffix = self._level_receiver_suffix.get(key, "")
        self.level_labels[key].setText(f"{LEVEL_DEFINITIONS[key]['label']}{suffix}: {value}%")
        self.worker.set_level_value(key, value / 100.0)

    def _on_control_combo_changed(self, key, widget):
        value = widget.currentData()
        self.worker.set_control_value(key, value)

    def _on_control_toggled(self, key, checked):
        self.worker.set_control_value(key, checked)

    def _on_ptt_toggled(self, checked):
        self.ptt_button.setText("TRANSMITTING" if checked else "PTT")
        # While actively Doppler-tracking a satellite, PTT also drives the
        # VFO swap: B (uplink)/A (downlink) around the transmission, not
        # just start/stop_ptt() on their own -- explicitly commanding
        # the VFO/frequency itself rather than relying on the radio's
        # own split feature to do it, which turns out to not need to be
        # on at all (confirmed live on a real 705: identical behavior
        # either way) -- see _start_satellite_tracking.
        #
        # The retune and the PTT command are bundled into ONE atomic
        # worker call (start_ptt_after_vfo -- every radio type -- and
        # stop_ptt_then_vfo/stop_ptt_and_select_receiver depending on
        # the radio type, see below) instead of calling
        # _on_satellite_tracking_tick() and then separately
        # self.worker.start_ptt()/stop_ptt() -- confirmed live on a real
        # 9700 that those two, dispatched as independently-scheduled
        # commands, don't actually guarantee the retune completes before
        # PTT does. If start_ptt() ever won that race, the radio (which
        # appears to refuse VFO/frequency changes once already
        # transmitting) would just ignore the retune, and the whole
        # transmission would hold the RX frequency instead of switching
        # to the uplink at all -- exactly what was reported.
        tracking_active = self._active_satellite is not None and self._satellite_tracking_timer.isActive()
        if not (tracking_active and self.worker.is_connected()):
            if checked:
                self.worker.start_ptt()
            else:
                self.worker.stop_ptt()
            return

        satellite = self._active_satellite
        state = self._compute_satellite_state(satellite)
        if state is None:
            # Propagation failed -- nothing to retune to, but PTT should
            # still actually transmit/release rather than silently do
            # nothing.
            if checked:
                self.worker.start_ptt()
            else:
                self.worker.stop_ptt()
            return
        look, crossing_text, downlink_hz, downlink_doppler_hz, uplink_hz, uplink_doppler_hz = state

        freq_hz = uplink_hz if checked else downlink_hz
        warning_text = ""
        if freq_hz is None:
            # No usable frequency for this direction (e.g. no uplink
            # stored for this transponder) -- key/unkey without a
            # bundled retune, same graceful-degradation as the tick.
            if checked:
                self.worker.start_ptt()
            else:
                self.worker.stop_ptt()
        elif not self.worker.control_available("vfo"):
            warning_text = "  [VFO control unavailable -- can't switch bands]"
            if checked:
                self.worker.start_ptt()
            else:
                self.worker.stop_ptt()
        else:
            # Same VFO A(RX)/B(TX) swap on Main for BOTH radio types now
            # -- confirmed live on a real 9700, with a controlled test
            # that gave each VFO its own distinct mode to tell them
            # apart directly, that PTT-driven transmission always
            # follows Main's own current VFO A/B context and NEVER Sub,
            # regardless of what's written to Sub's frequency/VFO slot.
            # (The radio's own split feature doesn't need to be on for
            # this at all -- see _start_satellite_tracking -- this
            # explicitly commands the VFO/frequency itself either way.)
            # Sub carries the downlink, continuously re-tuned while
            # receiving -- not touched at all during a transmission (the
            # scope stays on it regardless, so it's still worth watching
            # even though it's not being re-tuned) -- see
            # _on_satellite_tracking_tick.
            target_vfo = "B" if checked else "A"
            # Satellite mode, dual-receiver only: PTT also switches the
            # active receiver -- Main for the transmission (so the
            # tuning knob/AF/RF/squelch controls follow it, since Main
            # is what's actually being adjusted while transmitting),
            # back to Sub on release. Bundled into the same atomic
            # worker call as the VFO retune and PTT keying (receiver
            # param on start_ptt_after_vfo) rather than a separately-
            # dispatched select_receiver(), for the same ordering
            # reasons as everything else here. Never touches the scope
            # -- that stays on Sub throughout a transmission
            # (_start_satellite_tracking) so you can see yourself on the
            # downlink while transmitting.
            receiver = None
            if self.worker.is_dual_receiver:
                receiver = RECEIVER_MAIN if checked else RECEIVER_SUB
            if checked:
                self.worker.start_ptt_after_vfo(target_vfo, freq_hz, receiver)
            elif self.worker.is_dual_receiver:
                # Release, dual-receiver only: does NOT retune Main to
                # VFO A/downlink_hz -- confirmed live on a real 9700,
                # Main can't switch to a band Sub currently occupies,
                # and Sub is sitting on exactly that downlink band
                # continuously throughout the transmission
                # (_on_satellite_tracking_tick never touches it during
                # TX). Main just stays wherever the transmission left it
                # (the uplink band, VFO B) until the next PTT press
                # retunes it fresh -- nothing needs Main in between,
                # since Sub is what's actually active/watched during RX.
                self.worker.stop_ptt_and_select_receiver(receiver)
            else:
                self.worker.stop_ptt_then_vfo(target_vfo, freq_hz)
            if receiver is not None:
                self._update_active_receiver_ui(receiver)
            self._current_freq_hz = freq_hz
            self.freq_display.setText(f"{freq_hz / 1e6:.6f} MHz")
            self._update_band_button_highlight()

        self._update_satellite_overlay(satellite, look, crossing_text, downlink_doppler_hz, uplink_doppler_hz, warning_text)

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
        # Split doesn't need to be turned on for this -- confirmed live
        # on a real 705 that PTT switches to the Doppler-corrected
        # uplink and back the same way whether split is on or off,
        # since select_vfo_and_set_frequency()/start_ptt_after_vfo()
        # already explicitly command the VFO A/B swap themselves rather
        # than relying on split to do it. (An earlier version of this
        # enabled split here, on the theory a 9700 needed it for PTT to
        # follow Main's VFO B at all -- that turned out to be wrong too;
        # PTT already follows Main's VFO A/B regardless of split state.)
        self._sub_vfo_a_selected = False

        # Satellite mode, dual-receiver only: Sub is the active receiver
        # (tuning knob/AF/RF/squelch controls) and the scope both default
        # to Sub -- receiving, that's the Doppler-corrected downlink
        # Sub continuously tracks (_on_satellite_tracking_tick), so
        # that's what's worth looking at/listening to and adjusting.
        # PTT itself switches the active receiver to Main for the
        # duration of each transmission (_on_ptt_toggled) but
        # deliberately leaves the scope on Sub throughout, to see
        # yourself on the downlink while transmitting.
        if self.worker.is_connected() and self.worker.is_dual_receiver:
            # Radio-side dual watch / Main-Sub tracking (real Icom
            # features -- alternating-listen and linked-tuning,
            # respectively) directly fight this app's own from-scratch
            # Main=uplink/Sub=downlink management -- confirmed live on
            # a real 9700 that the active receiver kept oscillating
            # between Main and Sub no matter how the software-side
            # select_receiver() calls were adjusted (redundant every
            # tick, or only once), strongly suggesting the radio itself
            # was doing the switching regardless of what this app
            # commanded. Explicitly disabled here so satellite mode
            # starts from a clean, from-scratch state.
            self.worker.set_dual_receiver_linking(False)
            self.worker.select_receiver(RECEIVER_SUB)
            self.worker.set_scope_receiver(RECEIVER_SUB)
            self._update_active_receiver_ui(RECEIVER_SUB)

        self._on_satellite_tracking_tick()
        self._satellite_tracking_timer.start(SATELLITE_TRACKING_INTERVAL_MS)

    def _stop_satellite_tracking(self):
        """Pauses re-tuning -- the satellite stays selected (name,
        transponder list, last-known overlay reading) until either
        resumed or replaced by double-clicking another one. Also
        restores Main as the active receiver/scope (dual-receiver only),
        undoing _start_satellite_tracking's switch to Sub -- otherwise
        normal (non-satellite) operation would silently be left
        controlling/watching Sub instead of Main. Deliberately does NOT
        re-enable dual watch/Main-Sub tracking (disabled on start) --
        this app never reads their original state before disabling
        them, so there's nothing to correctly restore; if wanted for
        normal (non-satellite) operation, they can be turned back on
        directly on the radio itself."""
        self._satellite_tracking_timer.stop()
        if self.worker.is_connected() and self.worker.is_dual_receiver:
            self.worker.select_receiver(RECEIVER_MAIN)
            self.worker.set_scope_receiver(RECEIVER_MAIN)
            self._update_active_receiver_ui(RECEIVER_MAIN)

    def _compute_satellite_state(self, satellite):
        """Pure computation, no radio I/O -- look angles, next AOS/LOS,
        and both directions' Doppler-corrected frequencies for the given
        satellite right now. Shared between the periodic tracking tick
        (which dispatches per-tick, unbundled with anything else) and
        PTT press/release (which needs the exact same numbers but has to
        bundle sending them to the radio together with the PTT command
        itself -- see _on_ptt_toggled). Returns None if propagation
        fails (invalid/missing TLE)."""
        line1, line2 = satellite.get("line1", ""), satellite.get("line2", "")
        observer_lat = self._details.get("observer_lat")
        observer_lon = self._details.get("observer_lon")
        observer_elevation_km = (self._details.get("observer_elevation_m") or 0.0) / 1000.0
        now = datetime.datetime.now(datetime.timezone.utc)

        look = satellite_look_angles(line1, line2, now, observer_lat, observer_lon, observer_elevation_km)
        if look is None:
            return None

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

        return look, crossing_text, downlink_hz, downlink_doppler_hz, uplink_hz, uplink_doppler_hz

    def _update_satellite_overlay(self, satellite, look, crossing_text,
                                   downlink_doppler_hz, uplink_doppler_hz, warning_text=""):
        doppler_text = ""
        if downlink_doppler_hz is not None:
            doppler_text += f"  RX Doppler {downlink_doppler_hz:+.0f} Hz"
            if self._satellite_freq_offset_hz:
                doppler_text += f"  Offset {self._satellite_freq_offset_hz:+.0f} Hz"
        if uplink_doppler_hz is not None:
            doppler_text += f"  TX Doppler {uplink_doppler_hz:+.0f} Hz"
        visibility = "up" if look["elevation_deg"] >= 0 else "down"
        self.satellite_overlay_label.setText(
            f"{satellite.get('name', '?')} ({visibility})\n"
            f"El {look['elevation_deg']:.1f}°  Az {look['azimuth_deg']:.1f}°{doppler_text}{warning_text}\n"
            f"{crossing_text}"
        )
        self.spectrum_widget.reposition_overlays()

    def _on_satellite_tracking_tick(self):
        """Called every SATELLITE_TRACKING_INTERVAL_MS by the tracking
        timer, and also immediately (for responsiveness) on satellite/
        transponder selection and knob input. NOT called for PTT press/
        release any more -- see _on_ptt_toggled, which needs the retune
        bundled atomically with the PTT command itself, not dispatched
        here as a separate, independently-scheduled one.

        Single-receiver radios (7300/705): one shared VFO context, A for
        RX / B for TX. Which VFO to target is derived fresh from
        ptt_button.isChecked() on EVERY call, not just remembered from
        the last PTT edge, and reasserted via select_vfo_and_set_
        frequency() (VFO select + frequency, atomically) every single
        tick -- confirmed working correctly on a real 705.

        Dual-receiver radios (9700/7610): Sub is the downlink, Main is
        the uplink (confirmed live on a real 9700: PTT-driven
        transmission always follows Main's own VFO A/B context, never
        Sub, so Main has to be what carries the uplink). Only whichever
        receiver is actually ACTIVE right now gets Doppler-tuned this
        tick -- Sub while receiving, Main while transmitting, never
        both:

        - Sub, while receiving: continuously re-tuned to the live
          Doppler-corrected downlink, same as before. Its VFO slot only
          needs selecting once per tracking session (_sub_vfo_a_selected,
          reset in _start_satellite_tracking) since its direction never
          changes; subsequent ticks just update its frequency.
        - Main, while transmitting: continuously re-tuned to the live
          Doppler-corrected uplink, same select_vfo_and_set_frequency()
          pattern as the single-receiver case above, just gated to only
          run while transmitting.
        - The receiver NOT currently active isn't touched at all by the
          tick. Main sits wherever PTT last left it until the next
          press (_on_ptt_toggled's bundled start_ptt_after_vfo() gives
          it a fresh, correct value right when it's actually needed,
          not continuously pre-tuned while nothing's watching it). Sub
          isn't re-tuned during a transmission -- deliberately: Doppler
          drift over the span of one transmission is small enough to
          stay within the scope's visible bandwidth (which stays on Sub
          throughout, see _start_satellite_tracking, specifically so
          you can see yourself on the downlink while transmitting), so
          there's nothing worth correcting for until PTT releases
          anyway. This also sidesteps a real hardware quirk, confirmed
          live on a real 9700 all the way back at the start of this
          dual-receiver investigation: writing a receiver's frequency
          also visibly focuses/activates it -- so writing both every
          tick (an earlier version of this did) fought over focus and
          made the active receiver flip-flop continuously regardless of
          RX/TX state. Only ever touching one receiver per tick avoids
          that entirely, without needing to explicitly reassert
          anything afterward."""
        satellite = self._active_satellite
        if satellite is None:
            return
        state = self._compute_satellite_state(satellite)
        if state is None:
            self.satellite_overlay_label.setText(f"{satellite.get('name', '?')}\nOrbit propagation failed (invalid TLE?)")
            self.spectrum_widget.reposition_overlays()
            return
        look, crossing_text, downlink_hz, downlink_doppler_hz, uplink_hz, uplink_doppler_hz = state

        warning_text = ""
        transmitting = self.ptt_button.isChecked()
        if self.worker.is_connected():
            if self.worker.is_dual_receiver:
                if transmitting:
                    freq_hz = uplink_hz
                    if freq_hz is not None:
                        self.worker.select_vfo_and_set_frequency("B", freq_hz)
                else:
                    freq_hz = downlink_hz
                    if freq_hz is not None:
                        if not self._sub_vfo_a_selected:
                            self.worker.select_receiver_vfo_and_set_frequency(RECEIVER_SUB, freq_hz)
                            self._sub_vfo_a_selected = True
                        else:
                            self.worker.set_receiver_frequency(RECEIVER_SUB, freq_hz)
                if freq_hz is not None:
                    # Update optimistically, same as _on_knob_steps/
                    # _on_band_selected -- otherwise the main frequency
                    # readout and band-button highlight just sit still
                    # until the next 0.5s poll cycle confirms them.
                    self._current_freq_hz = freq_hz
                    self.freq_display.setText(f"{freq_hz / 1e6:.6f} MHz")
                    self._update_band_button_highlight()
            else:
                target_vfo = "B" if transmitting else "A"
                freq_hz = uplink_hz if transmitting else downlink_hz
                if not self.worker.control_available("vfo"):
                    warning_text = "  [VFO control unavailable -- can't switch bands]"
                elif freq_hz is not None:
                    self.worker.select_vfo_and_set_frequency(target_vfo, freq_hz)
                else:
                    self.worker.set_control_value("vfo", target_vfo)

                if freq_hz is not None and not warning_text:
                    self._current_freq_hz = freq_hz
                    self.freq_display.setText(f"{freq_hz / 1e6:.6f} MHz")
                    self._update_band_button_highlight()

        self._update_satellite_overlay(satellite, look, crossing_text, downlink_doppler_hz, uplink_doppler_hz, warning_text)

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

    def _on_active_receiver_toggle_clicked(self):
        # Manual control -- switches which receiver is active AND which
        # one the scope/waterfall shows (set_scope_receiver -- confirmed
        # live it doesn't follow select_receiver() on its own), together.
        # During active satellite-mode tracking, PTT itself also
        # switches the active receiver automatically (_on_ptt_toggled)
        # but deliberately leaves the scope alone -- this button is the
        # only thing that moves the scope, so it stays under manual
        # control even while satellite mode is running.
        receiver = RECEIVER_MAIN if self.active_receiver_button.text() == "Active: SUB" else RECEIVER_SUB
        self.worker.select_receiver(receiver)
        self.worker.set_scope_receiver(receiver)
        self._update_active_receiver_ui(receiver)

    def _on_active_receiver_changed(self, receiver):
        """Connected to worker.active_receiver_changed -- real, polled
        ground truth from get_active_receiver() (radio_worker.py's
        _poll_loop), not something this app commanded. Only reflects it
        in the UI (label/level-suffix) -- deliberately does NOT also
        move the scope (set_scope_receiver), which stays under manual
        control (active_receiver_button) only. Fixes a reported
        startup mismatch: if the radio was already sitting on Sub when
        the app connected (e.g. left there from a previous session),
        the button used to keep showing "Active: MAIN" until manually
        clicked, since nothing reflected the radio's actual starting
        state."""
        self._update_active_receiver_ui(receiver)

    def _update_active_receiver_ui(self, receiver):
        """Reflects `receiver` as the active one in the UI -- the
        active_receiver_button label, and a live "(Sub)" suffix on the
        AF Gain/Squelch/RF Level labels (DUAL_RECEIVER_LEVEL_KEYS,
        confirmed live on a real 9700 to follow whichever receiver is
        active rather than any receiver= addressing) so it's clear
        which receiver they're actually affecting. The displayed
        percentage itself catches up within one poll cycle
        (_on_level_updated) since the sliders keep polling/writing the
        same way regardless. Optimistic, same as the vfo_toggle buttons
        -- no polling of get_active_receiver() wired up (yet). Shared by
        the manual toggle above and satellite mode's automatic
        PTT-driven switching (_on_ptt_toggled, _start_satellite_
        tracking, _stop_satellite_tracking).

        Also invalidates _current_freq_hz/freq_display (reasoned out
        live on a real 9700, from the confirmed same-band constraint:
        a number that appeared to keep changing within Main's own band
        while Sub was supposedly active couldn't actually be Sub at
        all, since Sub can never land on Main's band -- it had to be a
        stale value). _current_freq_hz was left holding whichever
        receiver was active BEFORE this switch until the next 0.5s poll
        happened to refresh it -- ordinarily harmless, but the tuning
        knob (_on_knob_steps) uses it as its baseline for the NEXT
        relative step, and set_receiver_frequency() addresses Sub
        explicitly regardless -- so a knob turn against a stale,
        wrong-receiver baseline computes a target that's actually still
        in the OLD receiver's band, gets silently rejected by the radio
        for the same reason, and the optimistic display update shows
        that never-applied value anyway, looking exactly like a phantom
        VFO drifting inside the other receiver's band. Every caller of
        this already supplies (or immediately follows up with) the
        correct value for the new receiver except the manual toggle,
        which now just shows "-- MHz" for up to one poll cycle instead
        of a stale, misleading number the knob could act on."""
        self.active_receiver_button.setText("Active: SUB" if receiver == RECEIVER_SUB else "Active: MAIN")
        suffix = " (Sub)" if receiver == RECEIVER_SUB else ""
        for key in DUAL_RECEIVER_LEVEL_KEYS:
            self._level_receiver_suffix[key] = suffix
            label = self.level_labels[key]
            percent = self.level_sliders[key].value()
            label.setText(f"{LEVEL_DEFINITIONS[key]['label']}{suffix}: {percent}%")
        self._current_freq_hz = None
        self.freq_display.setText("-- MHz")
        self._update_band_button_highlight()

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
        # Dual-receiver: target whichever receiver is actually active
        # right now (active_receiver_button's label -- there's no other
        # tracked state for this in main_window.py) rather than always
        # defaulting to Main -- confirmed live on a real 9700 that band
        # selection silently changed Main while Sub was the active/
        # displayed receiver, leaving Sub's real frequency untouched
        # but showing Main's just-selected band instead.
        receiver = None
        if self.worker.is_dual_receiver and self.active_receiver_button.text() == "Active: SUB":
            receiver = RECEIVER_SUB
        self.worker.select_band(band_label, low_edge_hz, receiver)
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
        # Dual-receiver: target whichever receiver is actually active
        # right now (same pattern as _on_band_selected) rather than
        # always defaulting to Main -- set_frequency() (used for single-
        # receiver/Main) has no receiver concept at all, so this would
        # otherwise silently retune Main while Sub was the active/
        # displayed receiver, same bug _on_band_selected had. Bundles
        # Sub's VFO A select with every single write (select_receiver_
        # vfo_and_set_frequency), not a bare set_receiver_frequency --
        # confirmed live on a real 9700 that establishing Sub's VFO slot
        # ONCE, separately, then relying on later bare writes (an
        # earlier version of this did, matching the pattern satellite
        # mode's periodic tick already uses successfully) did NOT
        # reliably work for the knob: nothing on Sub moved at all.
        # Bundling every time matches the ONE pattern confirmed to
        # actually work in every context so far (Main's own VFO A/B
        # every tick, Sub's very first downlink tick) -- the extra VFO-
        # slot-select is a no-op on the radio's side once already
        # selected, and the knob is a user-paced action, not a rapid
        # automatic timer, so the earlier "redundant reselect behaves
        # like a toggle" risk (confirmed for automatic 2-second ticks)
        # is far less likely to bite here.
        if self.worker.is_dual_receiver and self.active_receiver_button.text() == "Active: SUB":
            self.worker.select_receiver_vfo_and_set_frequency(RECEIVER_SUB, new_freq_hz, "A")
        else:
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
