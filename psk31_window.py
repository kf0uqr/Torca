"""
Psk31ToolWindow: the per-radio PSK31 send/decode tool. Opened as a
singleton from its owning RadioWindow (main_window.py), same show-or-
raise / hide-not-destroy pattern as CwToolWindow/RttyToolWindow/
SstvToolWindow/AprsToolWindow -- closeEvent stops any active decode
tap or in-progress send before hiding, so the same instance can be
reopened freely without losing its transcript.

Send: no radio-side keyer for PSK31 either (like RTTY, unlike CW) --
psk31.py's build_psk31_pcm() synthesizes the audio locally, and
RadioWorker.send_tx_audio_pcm() (the same generic PTT-key/push-PCM/
unkey primitive RTTY/APRS send already use) keys PTT, streams it out,
and unkeys.

Macros: structurally identical to CwToolWindow/RttyToolWindow's own
(same {MYCALL}/{CALL} token substitution, left-click-sends/right-
click-edits convention), persisted separately to psk31_macros.json.

Decode: psk31.Psk31Decoder fed live PCM via RadioWorker.start_psk31_
decode(). Works alongside normal listening audio or an active Virtual
Cable on the same radio, exactly like CW/SSTV/RTTY/APRS decode.

Tone: unlike RTTY's fixed 2125/2295 Hz mark/space pair, PSK31 has no
fixed on-air audio frequency -- operators tune so the signal lands
wherever they like within the SSB passband (a "Tone (Hz)" spin box
here, matching whatever the operator sees on the waterfall/spectrum),
shared between send (encoded at that exact audio frequency) and decode
(psk31.Psk31Decoder's center_hz, the frequency it mixes down against).
"""

import json
import pathlib

from PySide6.QtCore import Qt, QSettings, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget,
    QDialog,
    QDialogButtonBox,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QDoubleSpinBox,
    QTextEdit,
)

from constants import AUDIO_DEFAULT_SAMPLE_RATE, AUDIO_TX_PCM_SAMPLE_RATE
from psk31 import Psk31Decoder, build_psk31_pcm, DEFAULT_CENTER_HZ

_MACROS_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "psk31_macros.json"

# Same shape/count as cw_window.py/rtty_window.py's own default macro
# banks -- same common QSO-shape phrases.
_DEFAULT_MACROS = [
    {"label": "CQ", "text": "CQ CQ CQ DE {MYCALL} {MYCALL} {MYCALL} PSE K"},
    {"label": "Answer", "text": "{CALL} DE {MYCALL} {MYCALL} K"},
    {"label": "Report", "text": "UR RST 599 599 BTU"},
    {"label": "TU 73", "text": "TU 73 GL DE {MYCALL} SK"},
    {"label": "", "text": ""},
    {"label": "", "text": ""},
    {"label": "QRZ?", "text": "QRZ? DE {MYCALL}"},
    {"label": "AGN", "text": "PSE AGN AGN"},
    {"label": "R R", "text": "R R"},
    {"label": "", "text": ""},
]


def _load_macros():
    if _MACROS_PATH.exists():
        try:
            data = json.loads(_MACROS_PATH.read_text())
            if isinstance(data, list) and len(data) == len(_DEFAULT_MACROS):
                return data
        except (OSError, ValueError):
            pass
    return [dict(macro) for macro in _DEFAULT_MACROS]


def _save_macros(macros):
    _MACROS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MACROS_PATH.write_text(json.dumps(macros, indent=2))


class MacroEditDialog(QDialog):
    """Edit one macro's button label and message template -- same
    {MYCALL}/{CALL} substitution tokens as cw_window.py/rtty_window.
    py's own MacroEditDialog (see Psk31ToolWindow._resolve_macro_text)."""

    def __init__(self, label, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit PSK31 Macro")
        self.label_edit = QLineEdit(label)
        self.label_edit.setPlaceholderText("Button label")
        self.text_edit = QLineEdit(text)
        self.text_edit.setPlaceholderText("Message -- {MYCALL} and {CALL} are substituted before sending")
        form = QFormLayout()
        form.addRow("Button label:", self.label_edit)
        form.addRow("Message:", self.text_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def result_label(self):
        return self.label_edit.text().strip()

    def result_text(self):
        return self.text_edit.text().strip()


class Psk31ToolWindow(QWidget):
    # Internal cross-thread hop only: _on_decode_audio_frame runs on
    # RadioWorker's asyncio-loop thread (same requirement as every
    # other RadioWorker callback in this app) and must not touch any
    # widget directly.
    _decoded_text_ready = Signal(str)

    def __init__(self, radio_window):
        super().__init__()
        self._radio_window = radio_window
        self.setWindowTitle(f"PSK31 Tool -- {radio_window._connection_label}")
        self.resize(560, 500)

        self._decoder = None  # psk31.Psk31Decoder, constructed fresh each time decoding starts
        self._send_active = False  # True while a send_tx_audio_pcm() send is in flight

        # ---- Tone ----

        self.tone_spin = QDoubleSpinBox()
        self.tone_spin.setRange(200.0, 2800.0)
        self.tone_spin.setSingleStep(10.0)
        self.tone_spin.setDecimals(0)
        self.tone_spin.setSuffix(" Hz")
        self.tone_spin.setValue(DEFAULT_CENTER_HZ)
        self.tone_spin.setToolTip(
            "Audio frequency the PSK31 signal is sent/decoded at -- match this to "
            "wherever you're actually tuned in the passband (e.g. via the waterfall)."
        )
        tone_row = QHBoxLayout()
        tone_row.addWidget(QLabel("Tone:"))
        tone_row.addWidget(self.tone_spin)
        tone_row.addStretch()

        # ---- Macros ----

        self._macros = _load_macros()  # list of {"label", "text"} dicts, see _load_macros/_save_macros
        self.macro_buttons = []

        self.contact_call_edit = QLineEdit()
        self.contact_call_edit.setPlaceholderText("Other station's callsign (for {CALL} in macros)")
        contact_row = QHBoxLayout()
        contact_row.addWidget(QLabel("Contact:"))
        contact_row.addWidget(self.contact_call_edit)

        macro_row_1 = QHBoxLayout()
        macro_row_2 = QHBoxLayout()
        half = len(self._macros) // 2
        for index, macro in enumerate(self._macros):
            button = QPushButton(macro["label"] or "(empty)")
            button.setMinimumWidth(70)
            button.setContextMenuPolicy(Qt.CustomContextMenu)
            button.clicked.connect(lambda checked=False, i=index: self._on_macro_clicked(i))
            button.customContextMenuRequested.connect(
                lambda pos, i=index: self._on_macro_right_clicked(i)
            )
            self.macro_buttons.append(button)
            (macro_row_1 if index < half else macro_row_2).addWidget(button)
        self._refresh_macro_tooltips()

        macro_group = QVBoxLayout()
        macro_group.addWidget(QLabel("<b>Macros</b> (left-click: send, right-click: edit)"))
        macro_group.addLayout(macro_row_1)
        macro_group.addLayout(macro_row_2)

        # ---- Send ----

        self.send_text_edit = QLineEdit()
        self.send_text_edit.setPlaceholderText("Text to send as PSK31...")
        self.send_text_edit.returnPressed.connect(self._on_send_clicked)

        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._on_send_clicked)
        self.stop_send_button = QPushButton("Stop")
        self.stop_send_button.clicked.connect(self._on_stop_send_clicked)

        self.send_status_label = QLabel("")
        self.send_status_label.setStyleSheet("color: #aaa;")

        send_buttons_row = QHBoxLayout()
        send_buttons_row.addWidget(self.send_button)
        send_buttons_row.addWidget(self.stop_send_button)
        send_buttons_row.addStretch()

        send_group = QVBoxLayout()
        send_group.addWidget(QLabel("<b>Send</b>"))
        send_group.addWidget(self.send_text_edit)
        send_group.addLayout(send_buttons_row)
        send_group.addWidget(self.send_status_label)

        # ---- Decode ----

        self.decode_toggle_button = QPushButton("Start Decoding")
        self.decode_toggle_button.setCheckable(True)
        self.decode_toggle_button.toggled.connect(self._on_decode_toggled)

        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._on_clear_clicked)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.decode_toggle_button)
        buttons_row.addWidget(clear_button)
        buttons_row.addStretch()

        self.status_label = QLabel("Not decoding.")
        self.status_label.setStyleSheet("color: #aaa;")

        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setStyleSheet("font-family: monospace;")

        decode_group = QVBoxLayout()
        decode_group.addWidget(QLabel("<b>Decode</b>"))
        decode_group.addLayout(buttons_row)
        decode_group.addWidget(self.status_label)
        decode_group.addWidget(self.transcript, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(tone_row)
        layout.addLayout(macro_group)
        layout.addLayout(contact_row)
        layout.addLayout(send_group)
        layout.addLayout(decode_group)
        self.setLayout(layout)

        self._decoded_text_ready.connect(self._on_decoded_text_ready)

    # ---- Send ----

    def _on_send_clicked(self):
        text = self.send_text_edit.text().strip()
        if not text:
            return
        self._start_send(text)

    def _start_send(self, text):
        if self._send_active:
            return
        try:
            pcm = build_psk31_pcm(text, sample_rate=AUDIO_TX_PCM_SAMPLE_RATE, center_hz=self.tone_spin.value())
        except ValueError as exc:
            self.send_status_label.setText(str(exc))
            return
        self._send_active = True
        self.send_button.setEnabled(False)
        self._radio_window.worker.send_tx_audio_pcm(pcm)
        # Fire-and-forget, same as rtty_window.py's own Send -- there's
        # no live "done" signal for a TX-audio send, so this re-enables
        # the Send button after the estimated duration rather than
        # waiting on one. A real failure surfaces via the owning
        # RadioWindow's existing audio_status/error handling.
        duration_s = len(pcm) / (AUDIO_TX_PCM_SAMPLE_RATE * 2)
        self.send_status_label.setText(f"Sending (~{duration_s:.1f}s)...")
        QTimer.singleShot(int(duration_s * 1000) + 200, self._on_send_finished)

    def _on_send_finished(self):
        if not self._send_active:
            return  # already stopped via _on_stop_send_clicked
        self._send_active = False
        self.send_button.setEnabled(True)
        self.send_status_label.setText("")

    def _on_stop_send_clicked(self):
        if not self._send_active:
            return
        self._radio_window.worker.stop_tx_audio_send()
        self._send_active = False
        self.send_button.setEnabled(True)
        self.send_status_label.setText("Stopped.")

    # ---- Macros ----

    def _resolve_macro_text(self, text):
        mycall = QSettings("IcomRadioApp", "RadioControl").value("operator_callsign", "") or ""
        contact_call = self.contact_call_edit.text().strip().upper()
        return text.replace("{MYCALL}", mycall).replace("{CALL}", contact_call)

    def _on_macro_clicked(self, index):
        macro = self._macros[index]
        if not macro["text"]:
            self._on_macro_right_clicked(index)
            return
        resolved = self._resolve_macro_text(macro["text"])
        self.send_text_edit.setText(resolved)
        self._start_send(resolved)

    def _on_macro_right_clicked(self, index):
        macro = self._macros[index]
        dialog = MacroEditDialog(macro["label"], macro["text"], self)
        if dialog.exec() != QDialog.Accepted:
            return
        macro["label"] = dialog.result_label()
        macro["text"] = dialog.result_text()
        self.macro_buttons[index].setText(macro["label"] or "(empty)")
        self._refresh_macro_tooltips()
        _save_macros(self._macros)

    def _refresh_macro_tooltips(self):
        for button, macro in zip(self.macro_buttons, self._macros):
            button.setToolTip(macro["text"] or "Click to configure this macro")

    # ---- Decode ----

    def _on_decode_toggled(self, checked):
        worker = self._radio_window.worker
        if checked:
            sample_rate = getattr(worker.radio, "audio_sample_rate", None) or AUDIO_DEFAULT_SAMPLE_RATE
            self._decoder = Psk31Decoder(sample_rate, center_hz=self.tone_spin.value())
            try:
                worker.start_psk31_decode(self._on_decode_audio_frame)
            except RuntimeError as exc:
                # Defensive backstop only -- see rtty_window.py's own
                # _on_decode_toggled for why this shouldn't actually be
                # reachable in practice.
                self._decoder = None
                self.status_label.setText(str(exc))
                self.decode_toggle_button.blockSignals(True)
                self.decode_toggle_button.setChecked(False)
                self.decode_toggle_button.blockSignals(False)
                return
            self.tone_spin.setEnabled(False)  # changing it mid-decode would desync the already-running decoder's LO
            self.decode_toggle_button.setText("Stop Decoding")
            self.status_label.setText("Decoding -- searching for a BPSK31 carrier...")
        else:
            worker.stop_psk31_decode()
            self._decoder = None
            self.tone_spin.setEnabled(True)
            self.decode_toggle_button.setText("Start Decoding")
            self.status_label.setText("Not decoding.")

    def _on_decode_audio_frame(self, pcm_bytes):
        # Runs on RadioWorker's asyncio-loop thread. psk31.Psk31Decoder
        # has no thread-safety of its own, but it's only ever touched
        # here (RadioWorker delivers its own audio callbacks serially)
        # and from _on_decode_toggled's start/stop on the GUI thread,
        # which only replace/clear self._decoder while decoding is NOT
        # active -- so there's never genuine concurrent access to the
        # same Psk31Decoder instance.
        if self._decoder is None:
            return
        text = self._decoder.feed(pcm_bytes)
        if text:
            self._decoded_text_ready.emit(text)

    def _on_decoded_text_ready(self, text):
        self._append_transcript(text)

    def _append_transcript(self, text):
        cursor = self.transcript.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.transcript.setTextCursor(cursor)
        self.transcript.ensureCursorVisible()

    def _on_clear_clicked(self):
        self.transcript.clear()

    # ---- Lifecycle ----

    def closing_for_real(self):
        """Called by RadioWindow.closeEvent when the OWNING radio
        window is actually going away for good -- see CwToolWindow.
        closing_for_real for the full rationale."""
        self._closing_for_real = True
        self.close()

    def closeEvent(self, event):
        # Singleton -- hidden, not destroyed -- but an active decode
        # tap or an in-progress send is a real hardware side effect
        # (an abandoned radio.start_rx() tap would keep running with
        # nothing consuming it, and leaving a send mid-message would
        # leave PTT keyed) that must be stopped first, same as CW/
        # SSTV/RTTY/APRS Tool.
        if self.decode_toggle_button.isChecked():
            self.decode_toggle_button.setChecked(False)  # synchronously runs _on_decode_toggled(False)
        if self._send_active:
            self._radio_window.worker.stop_tx_audio_send()
            self._send_active = False
        if getattr(self, "_closing_for_real", False):
            event.accept()
            return
        event.ignore()
        self.hide()
