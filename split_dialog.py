"""SplitSettingsDialog: right-click menu on the "Split" control button
(main_window.py) opens this -- a dedicated panel for configuring split
operation, modeled on wfview's "Rpt/Split" window
(https://wfview.org/wfview-user-manual/repeater-and-split-operation/).

Scope deliberately narrower than wfview's own dialog: wfview also has a
"Repeater Duplex" section (simplex/+/-/auto shift direction, offset-per-
band) and a "Repeater Tone Type"/"Tone Selection" section (CTCSS/DTCS).
Neither is included here -- rigplane has no repeater-duplex-offset
command at all (confirmed by reading its command modules directly, only
tone/CTCSS commands exist, no duplex-shift ones), and tone/CTCSS is a
distinct FM-repeater-access feature, not "split mode operation" as
asked for. wfview's "AutoTrack" (continuous Sub-follows-Main-with-
offset) is also left out -- wfview's own docs flag it as fragile/
experimental ("do not use both [AutoTrack and Quick Split] at the same
time"), and there's no live radio available this session to verify a
from-scratch implementation actually behaves -- consistent with this
project's standing rule to not guess at unverifiable radio behavior.

Live-apply, not OK/Cancel: every button/checkbox here takes effect on
the radio immediately when clicked, same as RigctldDialog/
VirtualCableDialog (ham_dashboard.py) -- "Close" just dismisses the
window, it doesn't commit or discard anything.

Dual-receiver (9700/7610) note: split only ever operates against Main's
own VFO A/B pair here, never Sub's -- PTT always transmits from Main
regardless of which receiver is "active" (a confirmed real hardware
limitation, documented at length in main_window.py's _on_ptt_toggled),
so Main's A/B pair is the only one where a "split" TX/RX split actually
means anything.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from radio_worker import RECEIVER_MAIN, RECEIVER_SUB


class SplitSettingsDialog(QDialog):
    def __init__(self, radio_window):
        super().__init__(radio_window)
        self.radio_window = radio_window
        self.worker = radio_window.worker
        self.setWindowTitle(f"Split Settings -- {radio_window._connection_label}")

        # Best-effort starting point for the RX/TX fields -- the last
        # frequency this window is tracking (whichever VFO/receiver is
        # currently active), same as the tuning knob's own baseline.
        # There's no live one-shot "read Main's frequency" plumbing
        # wired up from the worker to the GUI thread (only the
        # continuous poll loop, which reports whichever receiver is
        # currently active/selected) -- so this is a convenience
        # starting value, not a guaranteed-accurate read of Main's real
        # current frequency; the operator can always retype it before
        # clicking Set.
        start_mhz = (radio_window._current_freq_hz or 0) / 1e6

        split_group = QGroupBox("Split Mode")
        self.split_checkbox = QPushButton("Split: OFF")
        self.split_checkbox.setCheckable(True)
        self.split_checkbox.setChecked(radio_window.control_widgets["split"].isChecked())
        self.split_checkbox.setStyleSheet(
            "QPushButton:checked { background-color: #2a6; color: white; font-weight: bold; }"
        )
        self.split_checkbox.toggled.connect(self._on_split_toggled)
        self._update_split_button_text(self.split_checkbox.isChecked())

        quick_split_button = QPushButton("Quick Split")
        quick_split_button.setToolTip(
            "One-shot: syncs the TX VFO's frequency/mode/tone to the RX VFO right now "
            "(radio-side feature -- exact behavior/availability varies by model)."
        )
        quick_split_button.clicked.connect(self.worker.quick_split)

        split_top_row = QHBoxLayout()
        split_top_row.addWidget(self.split_checkbox)
        split_top_row.addWidget(quick_split_button)
        split_top_row.addStretch()

        self.rx_freq_spin = QDoubleSpinBox()
        self.rx_freq_spin.setDecimals(6)
        self.rx_freq_spin.setRange(0, 10000)
        self.rx_freq_spin.setSuffix(" MHz")
        self.rx_freq_spin.setValue(start_mhz)
        set_rx_button = QPushButton("Set RX")
        set_rx_button.clicked.connect(self._on_set_rx_clicked)

        self.tx_freq_spin = QDoubleSpinBox()
        self.tx_freq_spin.setDecimals(6)
        self.tx_freq_spin.setRange(0, 10000)
        self.tx_freq_spin.setSuffix(" MHz")
        self.tx_freq_spin.setValue(start_mhz)
        set_tx_button = QPushButton("Set TX")
        set_tx_button.clicked.connect(self._on_set_tx_clicked)

        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setDecimals(3)
        self.offset_spin.setRange(0, 100000)
        self.offset_spin.setSuffix(" kHz")
        split_plus_button = QPushButton("Split+ (TX = RX + Offset)")
        split_plus_button.clicked.connect(lambda: self._on_split_offset_clicked(+1))
        split_minus_button = QPushButton("Split- (TX = RX - Offset)")
        split_minus_button.clicked.connect(lambda: self._on_split_offset_clicked(-1))

        freq_form = QFormLayout()
        rx_row = QHBoxLayout()
        rx_row.addWidget(self.rx_freq_spin)
        rx_row.addWidget(set_rx_button)
        freq_form.addRow("RX Freq (VFO A):", rx_row)
        tx_row = QHBoxLayout()
        tx_row.addWidget(self.tx_freq_spin)
        tx_row.addWidget(set_tx_button)
        freq_form.addRow("TX Freq (VFO B):", tx_row)
        freq_form.addRow("Offset:", self.offset_spin)
        offset_row = QHBoxLayout()
        offset_row.addWidget(split_plus_button)
        offset_row.addWidget(split_minus_button)
        freq_form.addRow("", offset_row)

        split_layout = QVBoxLayout()
        split_layout.addLayout(split_top_row)
        split_layout.addLayout(freq_form)
        split_group.setLayout(split_layout)

        vfo_group = QGroupBox("VFO")
        vfo_layout = QVBoxLayout()
        if radio_window.worker.is_dual_receiver:
            vfo_layout.addWidget(QLabel(
                "PTT always transmits from Main regardless of the active receiver "
                "(hardware limitation) -- these act on Main/Sub as receivers, not VFO A/B."
            ))
            sel_row = QHBoxLayout()
            sel_main_button = QPushButton("Sel Main")
            sel_main_button.clicked.connect(lambda: self._on_select_receiver_clicked(RECEIVER_MAIN))
            sel_sub_button = QPushButton("Sel Sub")
            sel_sub_button.clicked.connect(lambda: self._on_select_receiver_clicked(RECEIVER_SUB))
            sel_row.addWidget(sel_main_button)
            sel_row.addWidget(sel_sub_button)
            vfo_layout.addLayout(sel_row)

            copy_row = QHBoxLayout()
            equalize_button = QPushButton("M=>S")
            equalize_button.setToolTip("Copy Main's frequency/mode onto Sub.")
            equalize_button.clicked.connect(self.worker.equalize_main_sub)
            swap_button = QPushButton("Swap M/S")
            swap_button.setToolTip("Swap Main and Sub's frequencies.")
            swap_button.clicked.connect(self.worker.swap_main_sub)
            copy_row.addWidget(equalize_button)
            copy_row.addWidget(swap_button)
            vfo_layout.addLayout(copy_row)
        else:
            vfo_layout.addWidget(QLabel("Selects which VFO the tuning knob/RX Freq-TX Freq fields address next."))
            sel_row = QHBoxLayout()
            sel_a_button = QPushButton("Sel A")
            sel_a_button.clicked.connect(lambda: self._on_select_vfo_clicked("A"))
            sel_b_button = QPushButton("Sel B")
            sel_b_button.clicked.connect(lambda: self._on_select_vfo_clicked("B"))
            sel_row.addWidget(sel_a_button)
            sel_row.addWidget(sel_b_button)
            vfo_layout.addLayout(sel_row)
        vfo_group.setLayout(vfo_layout)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)

        layout = QVBoxLayout()
        layout.addWidget(split_group)
        layout.addWidget(vfo_group)
        layout.addWidget(close_button)
        self.setLayout(layout)

    def _update_split_button_text(self, checked):
        self.split_checkbox.setText("Split: ON" if checked else "Split: OFF")

    def _on_split_toggled(self, checked):
        self._update_split_button_text(checked)
        # Proxies the real Split control button rather than calling
        # worker.set_control_value("split", ...) directly -- keeps this
        # dialog, the button's own pressed-state styling, and the
        # existing poll-loop reconciliation (radio_worker.py) all
        # agreeing on one single source of truth instead of two
        # independently-tracked "is split on" states that could drift
        # apart.
        real_button = self.radio_window.control_widgets["split"]
        if real_button.isChecked() != checked:
            real_button.setChecked(checked)

    def _target_receiver_kwargs(self, vfo_slot):
        if self.worker.is_dual_receiver:
            return {"receiver": RECEIVER_MAIN, "vfo_slot": vfo_slot}
        return {"vfo_slot": vfo_slot}

    def _on_set_rx_clicked(self):
        freq_hz = round(self.rx_freq_spin.value() * 1e6)
        if self.worker.is_dual_receiver:
            self.worker.select_receiver_vfo_and_set_frequency(RECEIVER_MAIN, freq_hz, "A")
        else:
            self.worker.select_vfo_and_set_frequency("A", freq_hz)

    def _on_set_tx_clicked(self):
        freq_hz = round(self.tx_freq_spin.value() * 1e6)
        if self.worker.is_dual_receiver:
            self.worker.select_receiver_vfo_and_set_frequency(RECEIVER_MAIN, freq_hz, "B")
        else:
            self.worker.select_vfo_and_set_frequency("B", freq_hz)

    def _on_split_offset_clicked(self, sign):
        tx_mhz = self.rx_freq_spin.value() + sign * (self.offset_spin.value() / 1000.0)
        self.tx_freq_spin.setValue(tx_mhz)
        self._on_set_tx_clicked()

    def _on_select_receiver_clicked(self, receiver):
        self.worker.select_receiver(receiver)
        self.worker.set_scope_receiver(receiver)
        self.radio_window._update_active_receiver_ui(receiver)

    def _on_select_vfo_clicked(self, vfo_value):
        self.worker.set_control_value("vfo", vfo_value)
