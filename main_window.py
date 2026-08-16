"""
RadioWindow: a per-radio-connection window. Ties together the radio
connection (via RadioWorker), all the live controls/meters/sliders, the
spectrum scope and waterfall, band buttons, and PTT. Constructed by
HamClockWindow (ham_dashboard.py) -- one per connected radio, each with
a "satellite role" (RADIO_ROLES in constants.py) chosen in
ConnectionDialog and passed in via `satellite_session`, which is the
single shared source of truth for satellite/transponder selection and
the periodic Doppler-tracking tick (satellite_session.py). This window
has no satellite/transponder UI of its own -- role dispatch
(apply_satellite_tick/apply_satellite_mode, called by SatelliteSession)
is the only satellite-related logic that lives here, deciding what THIS
radio does with the shared Doppler state depending on its role.
"""

import sys

from PySide6.QtCore import Qt, Slot, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QSlider,
    QComboBox,
    QPushButton,
    QMessageBox,
)

from constants import (
    RADIO_BANDS,
    RADIO_ROLES,
    LEVEL_DEFINITIONS,
    DUAL_RECEIVER_LEVEL_KEYS,
    CONTROL_DEFINITIONS,
    CONTROL_OPTION_EXCLUDED,
    TUNING_STEPS,
)
from radio_worker import RadioWorker, RECEIVER_MAIN, RECEIVER_SUB
from widgets import SpectrumWidget, WaterfallWidget, MeterWidget, TuningKnobWidget
from satellite_tracking import radio_mode_for_transponder

_ROLE_LABELS = {value: label for label, value in RADIO_ROLES}  # "full_duplex" -> "Satellite Full Duplex", etc.


class RadioWindow(QWidget):
    closed = Signal(object)  # emitted at the end of closeEvent, carrying self -- HamClockWindow listens

    def __init__(self, details, satellite_session):
        super().__init__()
        self._details = details
        self._role = details.get("role", "non_sat")
        self._satellite_session = satellite_session
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
        self.ptt_button.setToolTip(
            "Click to transmit, click again to release. While satellite "
            "tracking is running, this also swaps VFO B (uplink, "
            "Doppler-corrected) in for the transmission and back to VFO A "
            "(downlink) on release."
        )
        self.ptt_button.toggled.connect(self._on_ptt_toggled)
        # "downlink" role radios never transmit as part of satellite
        # tracking (they're the RX half of a "poor man's full duplex"
        # pair) -- hidden entirely rather than just disabled, since
        # there's no sensible manual PTT action for this role either.
        self.ptt_button.setVisible(self._role != "downlink")

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

        # Purely informational -- satellite tracking is now controlled
        # centrally from the Ham Dashboard (satellite/transponder
        # selection, Start/Stop Tracking), not per-radio-window, but the
        # operator still needs to see at a glance what each connected
        # radio is doing.
        self.role_label = QLabel(f"Role: {_ROLE_LABELS.get(self._role, self._role)}")
        self.role_label.setStyleSheet("color: #aaa; font-size: 11px;")

        # Dual-receiver (9700/7610) only, "full_duplex" role's periodic
        # tracking tick specifically: whether Sub's own VFO A has
        # already been explicitly selected for the current tracking
        # session (via select_receiver_vfo_and_set_frequency). Once
        # selected, Sub's direction never changes on its own (downlink
        # only, tracked continuously while receiving -- see
        # apply_satellite_tick), so subsequent ticks can just use a bare
        # set_receiver_frequency instead of reselecting every time --
        # confirmed working for this specific, tightly-looped (2-second)
        # use. Manual Sub frequency adjustment (the tuning knob, band
        # selection) does NOT reuse this flag -- confirmed live on a
        # real 9700 that a one-time-select-then-bare-writes approach for
        # those, mirroring this, did NOT reliably work (nothing on Sub
        # moved at all); they instead bundle the VFO-A select into every
        # single write, unconditionally. Reset to False whenever
        # SatelliteSession (re)starts tracking (_on_tracking_changed) so
        # a fresh tracking session always reselects it.
        self._sub_vfo_a_selected = False
        self._satellite_session.tracking_changed.connect(self._on_tracking_changed)


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

        # controls_row (Mode/Digital/NR/NB/AGC/Preamp/Filter/VFO) and the
        # role label stack vertically together, to the left of the tuning
        # knob -- not mixed into knob_row, which is the knob's own column
        # on the right. WSJT-X/Rigctld/Virtual Cables all moved to the
        # Ham Dashboard, which is the one place that makes sense once
        # multiple radios can be connected at once.
        left_column = QVBoxLayout()
        left_column.addLayout(controls_row)
        left_column.addWidget(self.role_label)

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

        # Registers with the shared satellite session so this radio
        # starts participating in Doppler tracking per its role -- see
        # satellite_session.py. "non_sat" radios are never registered at
        # all (SatelliteSession's dispatch never needs to know they
        # exist). If a satellite/transponder is already selected
        # (joining mid-session, e.g. connecting a second radio partway
        # through a pass), register() applies the current mode
        # immediately rather than waiting for the next transponder change.
        if self._role != "non_sat":
            self._satellite_session.register(self, self._role)

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

    @Slot(str)
    def _on_connection_failed(self, message):
        self.status_label.setText("Connection failed")
        QMessageBox.critical(self, "Connection Error", message)

    @Slot(int)
    def _on_frequency_updated(self, freq_hz):
        self._current_freq_hz = freq_hz
        self.freq_display.setText(f"{freq_hz / 1e6:.6f} MHz")
        self._update_band_button_highlight()
        self.spectrum_widget.set_tuned_frequency(freq_hz)

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
        # either way).
        #
        # The retune and the PTT command are bundled into ONE atomic
        # worker call (start_ptt_after_vfo -- every radio type -- and
        # stop_ptt_then_vfo/stop_ptt_and_select_receiver depending on
        # the radio type, see below) instead of calling
        # apply_satellite_tick() and then separately
        # self.worker.start_ptt()/stop_ptt() -- confirmed live on a real
        # 9700 that those two, dispatched as independently-scheduled
        # commands, don't actually guarantee the retune completes before
        # PTT does. If start_ptt() ever won that race, the radio (which
        # appears to refuse VFO/frequency changes once already
        # transmitting) would just ignore the retune, and the whole
        # transmission would hold the RX frequency instead of switching
        # to the uplink at all -- exactly what was reported.
        #
        # current_state() (not the last periodic tick's cached numbers)
        # is used deliberately -- same reasoning, the freshest possible
        # Doppler-corrected uplink frequency right at the moment of
        # keying. "downlink"-role radios never reach this at all (PTT
        # button hidden); "non_sat" never has tracking_active True (not
        # registered with the session).
        tracking_active = self._role != "non_sat" and self._satellite_session.is_tracking()
        if not (tracking_active and self.worker.is_connected()):
            if checked:
                self.worker.start_ptt()
            else:
                self.worker.stop_ptt()
            return

        state = self._satellite_session.current_state()
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
        if freq_hz is None:
            # No usable frequency for this direction (e.g. no uplink
            # stored for this transponder) -- key/unkey without a
            # bundled retune, same graceful-degradation as the tick.
            if checked:
                self.worker.start_ptt()
            else:
                self.worker.stop_ptt()
        elif not self.worker.control_available("vfo"):
            if checked:
                self.worker.start_ptt()
            else:
                self.worker.stop_ptt()
        else:
            # Same VFO A(RX)/B(TX) swap on Main for BOTH dual- and
            # single-receiver radios -- confirmed live on a real 9700,
            # with a controlled test that gave each VFO its own distinct
            # mode to tell them apart directly, that PTT-driven
            # transmission always follows Main's own current VFO A/B
            # context and NEVER Sub, regardless of what's written to
            # Sub's frequency/VFO slot. (The radio's own split feature
            # doesn't need to be on for this at all -- this explicitly
            # commands the VFO/frequency itself either way.) Sub carries
            # the downlink, continuously re-tuned while receiving -- not
            # touched at all during a transmission (the scope stays on
            # it regardless, so it's still worth watching even though
            # it's not being re-tuned) -- see apply_satellite_tick.
            #
            # A standalone "uplink"-role radio (is_dual_receiver False,
            # receiver=None throughout) takes exactly this same single-
            # receiver path -- it's already precisely what that role
            # needs: VFO B on press, VFO A on release, no receiver
            # concept at all.
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
            # -- that stays on Sub throughout a transmission so you can
            # see yourself on the downlink while transmitting.
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
                # (apply_satellite_tick never touches it during TX).
                # Main just stays wherever the transmission left it (the
                # uplink band, VFO B) until the next PTT press retunes it
                # fresh -- nothing needs Main in between, since Sub is
                # what's actually active/watched during RX.
                self.worker.stop_ptt_and_select_receiver(receiver)
            else:
                self.worker.stop_ptt_then_vfo(target_vfo, freq_hz)
            if receiver is not None:
                self._update_active_receiver_ui(receiver)
            self._current_freq_hz = freq_hz
            self.freq_display.setText(f"{freq_hz / 1e6:.6f} MHz")
            self._update_band_button_highlight()

    def _on_tracking_changed(self, tracking):
        self._sub_vfo_a_selected = False

    def apply_satellite_tick(self, downlink_hz, downlink_doppler_hz, uplink_hz, uplink_doppler_hz):
        """Called by SatelliteSession every tracking tick (2s) and
        immediately on transponder/offset change. Role dispatch -- what
        THIS radio does with the shared Doppler state depends entirely
        on self._role; mechanics themselves are unchanged from the
        original single-radio implementation, just relocated from what
        used to be _on_satellite_tracking_tick. Returns a warning string
        (possibly empty) for SatelliteSession to aggregate and surface.

        "full_duplex": today's original dual-receiver logic, verbatim --
        Sub continuously re-tuned to the downlink while receiving, Main
        re-tuned to the uplink only while transmitting (never both the
        same tick -- confirmed live on a real 9700 that writing both
        every tick fights over receiver focus and makes the active
        receiver flip-flop continuously). Main sits wherever PTT last
        left it between transmissions -- see _on_ptt_toggled.

        "downlink": always re-tuned to the downlink, VFO A, every tick,
        unconditionally -- this role never transmits, so there's no
        "while receiving" gate needed at all.

        "uplink": does nothing on the periodic tick at all -- mirrors
        how full_duplex's Main is left alone between transmissions;
        PTT press (_on_ptt_toggled) computes a fresh uplink frequency
        and retunes right then instead.

        Not called for "non_sat" radios (never registered with the
        session)."""
        if not self.worker.is_connected():
            return ""
        warning_text = ""
        if self._role == "full_duplex":
            transmitting = self.ptt_button.isChecked()
            freq_hz = None
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
                self._current_freq_hz = freq_hz
                self.freq_display.setText(f"{freq_hz / 1e6:.6f} MHz")
                self._update_band_button_highlight()
        elif self._role == "downlink":
            freq_hz = downlink_hz
            if not self.worker.control_available("vfo"):
                warning_text = "VFO control unavailable -- can't switch bands"
            elif freq_hz is not None:
                self.worker.select_vfo_and_set_frequency("A", freq_hz)
                self._current_freq_hz = freq_hz
                self.freq_display.setText(f"{freq_hz / 1e6:.6f} MHz")
                self._update_band_button_highlight()
        # "uplink": deliberately nothing here -- see docstring.
        return warning_text

    def apply_satellite_mode(self, transponder):
        """Called by SatelliteSession whenever the transponder selection
        changes (and once immediately on register(), if a transponder is
        already selected -- e.g. connecting a second radio mid-pass).
        Role dispatch, reusing radio_mode_for_transponder's confirmed-
        unambiguous mapping (see satellite_tracking.py) -- relocated
        near-verbatim from the original single-radio _apply_transponder_
        mode.

        "full_duplex": downlink mode applied via set_control_value
        (targets whichever receiver is currently active -- Sub, since
        that's who's listening). Uplink mode applied to Main
        specifically via set_receiver_control_value, preferring the
        transponder's own recorded uplink_mode (correctly handles
        inverting linear transponders, e.g. AO-7's LSB-up/USB-down) and
        falling back to mirroring the downlink mode only when it's FM/
        WFM (safe -- FM transponders don't invert) and no uplink_mode
        was recorded. Any other case leaves Main's mode alone rather
        than guess.

        "downlink": downlink mode only, via set_control_value (this
        radio has no Main to speak of).

        "uplink": same uplink-mode preference logic as full_duplex's
        Main half, applied via the plain (non-receiver-specific)
        set_control_value -- this radio has no Sub/downlink concept, so
        there's no separate "which receiver" question here at all."""
        if transponder is None or not self.worker.is_connected():
            return
        if "mode" not in self.control_widgets:
            return
        downlink_mode_value = radio_mode_for_transponder(transponder.get("mode"))
        uplink_mode_value = radio_mode_for_transponder(transponder.get("uplink_mode"))
        if uplink_mode_value is None and downlink_mode_value in ("FM", "WFM"):
            uplink_mode_value = downlink_mode_value

        if self._role == "full_duplex":
            if downlink_mode_value is not None:
                self.worker.set_control_value("mode", downlink_mode_value)
            if uplink_mode_value is not None:
                self.worker.set_receiver_control_value(RECEIVER_MAIN, "mode", uplink_mode_value)
        elif self._role == "downlink":
            if downlink_mode_value is not None:
                self.worker.set_control_value("mode", downlink_mode_value)
        elif self._role == "uplink":
            if uplink_mode_value is not None:
                self.worker.set_control_value("mode", uplink_mode_value)

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
        if key == "mode":
            self.spectrum_widget.set_mode(value)
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
        # the SHARED offset (SatelliteSession) applied to the
        # transponder's nominal downlink instead of commanding the radio
        # directly -- otherwise the next tracking tick (up to 2s later)
        # would just overwrite any manual adjustment with a fresh
        # nominal+Doppler computation. This is how an operator tunes
        # within a linear satellite's passband, or nudges to better
        # match where it's actually transmitting. Only meaningful for
        # roles that actually watch the downlink continuously
        # (full_duplex/downlink) -- "uplink" has no receive duty as part
        # of satellite tracking at all (see apply_satellite_tick), and
        # "non_sat" never has tracking active in the first place.
        if self._role in ("full_duplex", "downlink") and self._satellite_session.is_tracking():
            self._satellite_session.adjust_offset(steps * step_hz)
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
        self._satellite_session.unregister(self)
        # Emitted BEFORE worker.stop() -- HamClockWindow's handler may
        # still need to call something on this worker (e.g.
        # set_virtual_cable_bridge(False, False), if this radio was a
        # Virtual Cables RX/TX target), which needs the worker's asyncio
        # loop still alive to have any chance of actually running (best-
        # effort either way -- worker.stop() right after this may still
        # cut it off before it finishes).
        self.closed.emit(self)
        self.worker.stop()
        self.worker.wait(2000)  # give the polling loop a moment to exit cleanly
        event.accept()
