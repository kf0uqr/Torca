"""
CwToolWindow: the per-radio CW (Morse code) send/decode tool. Opened
as a singleton from its owning RadioWindow (main_window.py), same
show-or-raise pattern as ham_dashboard.py's LogBookWindow -- closeEvent
stops any active decode tap or in-progress CW send (real hardware side
effects, unlike Log Book) before hiding rather than destroying, so the
same instance can be reopened freely without losing its transcript.

Send: the radio's own built-in keyer over CI-V (RadioWorker.
send_cw_text/stop_cw_text) -- no local audio/DSP involved at all. PTT
is keyed explicitly around the send (half a second before it starts,
half a second after it ends) rather than left to the radio's own
keyer alone -- see _on_send_clicked's docstring for why.

Decode: an auto-adaptive tone decoder (cw.CwDecoder) fed live PCM via
RadioWorker.start_cw_decode(). Works alongside normal listening audio
or an active Virtual Cable on the same radio -- RadioWorker shares
whichever RX stream is already running (rigplane only allows one
radio.start_rx() registration at a time) rather than requiring
exclusive access, so this window never needs to gate the toggle on
what else the radio's audio is doing.

Macros: a bank of one-click common CW phrases across the top (CQ,
signal report, TU 73, etc, plus a few blank slots for the operator's
own) -- left-click sends it through the exact same PTT-sequenced path
as the Send button (a blank one opens the edit dialog instead, since
there's nothing useful to send); right-click always opens the edit
dialog. {MYCALL}/{CALL} tokens are substituted before sending --
{MYCALL} from the operator_callsign QSettings value used elsewhere in
this app, {CALL} from this window's own Contact field. Persisted to a
shared cw_macros.json (same ~/.icom_radio_app_cache directory
connection_dialog.py already uses for structured, non-QSettings data)
so edits carry over across radios and app restarts.
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
    QSpinBox,
    QTextEdit,
)

from constants import AUDIO_DEFAULT_SAMPLE_RATE
from cw import CwDecoder, estimate_cw_send_duration_ms, MIN_ACCEPTED_WPM, MAX_ACCEPTED_WPM

_MACROS_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "cw_macros.json"

# 8 preset phrases covering the common shape of a CW QSO (calling CQ,
# answering a call, exchanging a signal report, requesting a repeat,
# asking who's there, breaking in, acknowledging, and signing off) plus
# 2 blank slots per row for the operator's own. {MYCALL}/{CALL} are
# substituted at send time (see _resolve_macro_text) -- deliberately no
# other placeholder text (e.g. a literal "NAME"/"QTH" reminder) since a
# macro click sends immediately, and literal placeholder text would be
# sent as CW exactly as typed.
_DEFAULT_MACROS = [
    {"label": "CQ", "text": "CQ CQ CQ DE {MYCALL} {MYCALL} {MYCALL} K"},
    {"label": "Answer", "text": "{CALL} DE {MYCALL} {MYCALL} K"},
    {"label": "Report", "text": "UR RST 599 599 BT"},
    {"label": "TU 73", "text": "TU 73 GL DE {MYCALL} SK"},
    {"label": "", "text": ""},
    {"label": "", "text": ""},
    {"label": "QRZ?", "text": "QRZ? DE {MYCALL}"},
    {"label": "AGN", "text": "PSE AGN AGN"},
    {"label": "BK", "text": "BK"},
    {"label": "R R", "text": "R R"},
    {"label": "", "text": ""},
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
    """Edit one macro's button label and message template. {MYCALL}
    and {CALL} are the only recognized substitution tokens -- see
    CwToolWindow._resolve_macro_text."""

    def __init__(self, label, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit CW Macro")
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

# Delay between keying PTT and actually starting to send code, and
# between the estimated end of sending and releasing PTT -- lets
# external relays/an amplifier settle before/after the CW keying
# itself, avoiding hot-switching and a clipped first or last
# character. Per explicit instruction, not (yet) user-configurable.
_PTT_LEAD_MS = 500
_PTT_TRAIL_MS = 500
# There's no live "done" signal for a CW send (see cw.
# estimate_cw_send_duration_ms's docstring) -- this pads the estimate
# a little so PTT doesn't release a moment before the radio actually
# finishes keying the last character.
_DURATION_SAFETY_MARGIN = 1.08


class CwToolWindow(QWidget):
    # Internal cross-thread hop only: _on_decode_audio_frame runs on
    # RadioWorker's asyncio-loop thread (same requirement as every
    # other RadioWorker callback in this app) and must not touch any
    # widget directly -- emitting this signal is the hand-off back to
    # the GUI thread, same pattern as RadioWorker's own signals.
    _decoded_text_ready = Signal(str)

    def __init__(self, radio_window):
        super().__init__()
        self._radio_window = radio_window
        self.setWindowTitle(f"CW Tool -- {radio_window._connection_label}")
        self.resize(560, 480)

        self._decoder = None  # cw.CwDecoder, constructed fresh each time decoding starts

        # True from the moment Send keys PTT until PTT is released
        # again (the trail timer firing, Stop being clicked, or this
        # window closing) -- guards against a second Send while one is
        # already in flight, and tells closeEvent whether IT (not some
        # unrelated manual PTT use) is holding PTT and needs to release
        # it. See _on_send_clicked's docstring for the full sequence.
        self._send_sequence_active = False
        self._send_pending_text = None

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
        self.send_text_edit.setPlaceholderText("Text to send as CW...")
        self.send_text_edit.returnPressed.connect(self._on_send_clicked)

        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._on_send_clicked)
        self.stop_send_button = QPushButton("Stop")
        self.stop_send_button.clicked.connect(self._on_stop_send_clicked)

        send_buttons_row = QHBoxLayout()
        send_buttons_row.addWidget(self.send_button)
        send_buttons_row.addWidget(self.stop_send_button)
        send_buttons_row.addStretch()

        self.wpm_spin = QSpinBox()
        self.wpm_spin.setRange(6, 48)
        self.wpm_spin.setSuffix(" WPM")
        self.wpm_spin.valueChanged.connect(self._on_wpm_spin_changed)

        self.pitch_spin = QSpinBox()
        self.pitch_spin.setRange(300, 900)
        self.pitch_spin.setSingleStep(5)
        self.pitch_spin.setSuffix(" Hz")
        self.pitch_spin.valueChanged.connect(self._on_pitch_spin_changed)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Speed:"))
        speed_row.addWidget(self.wpm_spin)
        speed_row.addWidget(QLabel("Pitch:"))
        speed_row.addWidget(self.pitch_spin)
        speed_row.addStretch()

        send_group = QVBoxLayout()
        send_group.addWidget(QLabel("<b>Send</b>"))
        send_group.addWidget(self.send_text_edit)
        send_group.addLayout(send_buttons_row)
        send_group.addLayout(speed_row)

        # See _on_send_clicked's docstring for the PTT-around-the-send
        # sequence these two drive.
        self._send_ptt_lead_timer = QTimer(self)
        self._send_ptt_lead_timer.setSingleShot(True)
        self._send_ptt_lead_timer.timeout.connect(self._on_send_ptt_lead_elapsed)

        self._send_ptt_trail_timer = QTimer(self)
        self._send_ptt_trail_timer.setSingleShot(True)
        self._send_ptt_trail_timer.timeout.connect(self._on_send_ptt_trail_elapsed)

        # ---- Decode ----

        self.decode_toggle_button = QPushButton("Start Decoding")
        self.decode_toggle_button.setCheckable(True)
        self.decode_toggle_button.toggled.connect(self._on_decode_toggled)

        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self._on_clear_clicked)

        decode_buttons_row = QHBoxLayout()
        decode_buttons_row.addWidget(self.decode_toggle_button)
        decode_buttons_row.addWidget(self.clear_button)
        decode_buttons_row.addStretch()

        self.decode_status_label = QLabel("Not decoding.")
        self.decode_status_label.setStyleSheet("color: #aaa;")

        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)

        decode_group = QVBoxLayout()
        decode_group.addWidget(QLabel("<b>Decode</b>"))
        decode_group.addLayout(decode_buttons_row)
        decode_group.addWidget(self.decode_status_label)
        decode_group.addWidget(self.transcript)

        layout = QVBoxLayout()
        layout.addLayout(macro_group)
        layout.addLayout(contact_row)
        layout.addLayout(send_group)
        layout.addLayout(decode_group)
        self.setLayout(layout)

        self._decoded_text_ready.connect(self._on_decoded_text_ready)

        worker = radio_window.worker
        worker.key_speed_changed.connect(self._on_key_speed_changed)
        worker.cw_pitch_changed.connect(self._on_cw_pitch_changed)

        # One-shot initial sync from RadioWorker's own already-polled
        # cache, not a fresh read: unlike every other synced control in
        # this app (built inside RadioWindow.__init__, before the
        # worker's poll loop has ever run once), this window is opened
        # on demand, potentially long after polling started -- the
        # poll loop only emits key_speed_changed/cw_pitch_changed on a
        # genuine CHANGE from its last-observed value, so relying on a
        # future emission to arrive would risk waiting forever if
        # nothing happens to change after this window opens.
        if worker._last_observed_key_speed is not None:
            self.wpm_spin.blockSignals(True)
            self.wpm_spin.setValue(worker._last_observed_key_speed)
            self.wpm_spin.blockSignals(False)
        if worker._last_observed_cw_pitch is not None:
            self.pitch_spin.blockSignals(True)
            self.pitch_spin.setValue(worker._last_observed_cw_pitch)
            self.pitch_spin.blockSignals(False)

        # Decoder's WPM estimate/tone-detected state changes
        # continuously as marks arrive, not on a discrete event -- this
        # just refreshes the status label periodically while decoding,
        # rather than needing cw.CwDecoder to push its own updates.
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._update_decode_status)

    # ---- Send ----

    def _on_send_clicked(self):
        """Keys PTT, waits _PTT_LEAD_MS, sends the CW text, waits for
        it to (an estimated) finish plus _PTT_TRAIL_MS, then releases
        PTT -- rather than relying on the radio's own keyer to key
        PTT/TX itself for exactly the duration of the code (which it
        does do on its own, but with no lead-in/lead-out margin). The
        lead/trail delays give external relays or an amplifier time to
        settle before/after the actual keying, avoiding hot-switching
        and a clipped first or last character -- explicit instruction,
        not a guess. Ignored while a previous send is still in flight
        (guarded by _send_sequence_active, not just the disabled Send
        button -- returnPressed on the text field can still fire while
        the button itself is disabled). Macro clicks (_on_macro_clicked)
        go through the same _start_send_sequence path."""
        text = self.send_text_edit.text().strip()
        if not text:
            return
        self._start_send_sequence(text)

    def _start_send_sequence(self, text):
        if self._send_sequence_active:
            return
        self._send_sequence_active = True
        self.send_button.setEnabled(False)
        self._send_pending_text = text
        self._radio_window.worker.start_ptt()
        self._send_ptt_lead_timer.start(_PTT_LEAD_MS)

    def _on_send_ptt_lead_elapsed(self):
        text = self._send_pending_text
        self._send_pending_text = None
        if text is None:
            return
        self._radio_window.worker.send_cw_text(text)
        duration_ms = estimate_cw_send_duration_ms(text, self.wpm_spin.value())
        self._send_ptt_trail_timer.start(int(duration_ms * _DURATION_SAFETY_MARGIN) + _PTT_TRAIL_MS)

    def _on_send_ptt_trail_elapsed(self):
        self._radio_window.worker.stop_ptt()
        self._send_sequence_active = False
        self.send_button.setEnabled(True)

    def _on_stop_send_clicked(self):
        # Immediate, regardless of which phase of the send sequence
        # (still in the pre-send PTT lead delay, actively sending, or
        # waiting out the post-send trail delay) this interrupts --
        # cancelling the timers prevents a pending send_cw_text() or
        # stop_ptt() call from firing later on top of this.
        self._cancel_send_sequence_timers()
        worker = self._radio_window.worker
        worker.stop_cw_text()
        if self._send_sequence_active:
            worker.stop_ptt()
            self._send_sequence_active = False
        self.send_button.setEnabled(True)

    def _cancel_send_sequence_timers(self):
        self._send_ptt_lead_timer.stop()
        self._send_ptt_trail_timer.stop()
        self._send_pending_text = None

    # ---- Macros ----

    def _resolve_macro_text(self, text):
        """Substitutes {MYCALL} (this app's shared operator_callsign
        QSettings value, same one PSKReporter/QSO logging already use)
        and {CALL} (this window's own Contact field) into a macro's
        stored template. Read fresh each call rather than cached, in
        case the operator's callsign is changed in settings, or the
        Contact field is edited, while this window stays open."""
        mycall = QSettings("IcomRadioApp", "RadioControl").value("operator_callsign", "") or ""
        contact_call = self.contact_call_edit.text().strip().upper()
        return text.replace("{MYCALL}", mycall).replace("{CALL}", contact_call)

    def _on_macro_clicked(self, index):
        macro = self._macros[index]
        if not macro["text"]:
            # Nothing configured yet -- editing it is more useful than
            # silently sending nothing.
            self._on_macro_right_clicked(index)
            return
        resolved = self._resolve_macro_text(macro["text"])
        self.send_text_edit.setText(resolved)
        self._start_send_sequence(resolved)

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

    def _on_wpm_spin_changed(self, value):
        self._radio_window.worker.set_key_speed(value)

    def _on_pitch_spin_changed(self, value):
        self._radio_window.worker.set_cw_pitch(value)

    def _on_key_speed_changed(self, wpm):
        if self.wpm_spin.value() != wpm:
            self.wpm_spin.blockSignals(True)
            self.wpm_spin.setValue(wpm)
            self.wpm_spin.blockSignals(False)

    def _on_cw_pitch_changed(self, hz):
        if self.pitch_spin.value() != hz:
            self.pitch_spin.blockSignals(True)
            self.pitch_spin.setValue(hz)
            self.pitch_spin.blockSignals(False)

    # ---- Decode ----

    def _on_decode_toggled(self, checked):
        worker = self._radio_window.worker
        if checked:
            sample_rate = getattr(worker.radio, "audio_sample_rate", None) or AUDIO_DEFAULT_SAMPLE_RATE
            self._decoder = CwDecoder(sample_rate, self.pitch_spin.value())
            try:
                worker.start_cw_decode(self._on_decode_audio_frame)
            except RuntimeError as exc:
                # Defensive backstop only -- with cw_tool_button only
                # ever enabled once the radio is connected (main_
                # window.py's _on_connected), start_cw_decode() raising
                # synchronously (not-connected-yet) shouldn't actually
                # be reachable from here. A failure to actually attach
                # (e.g. the radio doesn't support RX audio at all)
                # happens asynchronously instead and is reported via
                # worker.error, not this exception.
                self._decoder = None
                self.decode_status_label.setText(str(exc))
                # Nothing was actually started (start_cw_decode raised
                # before that), so this is just a UI correction, not a
                # real stop -- block signals so it doesn't re-enter
                # this method via toggled(False) and overwrite the
                # error message above with "Not decoding."
                self.decode_toggle_button.blockSignals(True)
                self.decode_toggle_button.setChecked(False)
                self.decode_toggle_button.blockSignals(False)
                return
            self.decode_toggle_button.setText("Stop Decoding")
            self.decode_status_label.setText("Decoding -- listening for tone...")
            self._status_timer.start()
        else:
            self._status_timer.stop()
            worker.stop_cw_decode()
            if self._decoder is not None:
                trailing = self._decoder.flush()
                if trailing:
                    self._append_transcript(trailing)
            self._decoder = None
            self.decode_toggle_button.setText("Start Decoding")
            self.decode_status_label.setText("Not decoding.")

    def _on_decode_audio_frame(self, pcm_bytes):
        # Runs on RadioWorker's asyncio-loop thread. cw.CwDecoder has
        # no thread-safety of its own, but it's only ever touched here
        # (RadioWorker delivers its own audio callbacks serially) and
        # from _on_decode_toggled's start/stop on the GUI thread, which
        # only replace/clear self._decoder while decoding is NOT
        # active -- so there's never genuine concurrent access to the
        # same CwDecoder instance.
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

    def _update_decode_status(self):
        if self._decoder is None:
            return
        if self._decoder.has_signal:
            wpm = self._decoder.current_wpm
            text = f"Decoding -- tone detected, ~{wpm:.0f} WPM"
            if not (MIN_ACCEPTED_WPM <= wpm <= MAX_ACCEPTED_WPM):
                # cw.CwDecoder silently drops decoded characters outside
                # this range (see its own MIN_ACCEPTED_WPM comment) --
                # surfaced here so an operator watching an empty
                # transcript with a tone clearly detected knows why,
                # rather than assuming decode is just broken.
                text += " (ignoring -- outside accepted 5-60 WPM range)"
            self.decode_status_label.setText(text)
        else:
            self.decode_status_label.setText("Decoding -- listening for tone...")

    def _on_clear_clicked(self):
        self.transcript.clear()

    # ---- Lifecycle ----

    def closing_for_real(self):
        """Called by RadioWindow.closeEvent when the OWNING radio
        window is actually going away for good (not just this window
        being closed by the operator) -- lets closeEvent below really
        close instead of its usual hide-and-keep-the-singleton-alive
        behavior. Without this, closing the radio connection would
        leave an orphaned CW Tool window on screen, still wired to a
        RadioWorker that just stopped."""
        self._closing_for_real = True
        self.close()

    def closeEvent(self, event):
        # Singleton -- hidden, not destroyed (main_window.py reopens
        # this same instance) -- but an active decode tap or an
        # in-progress CW send is a real hardware side effect that must
        # be stopped first: an abandoned radio.start_rx() tap would
        # otherwise keep running with nothing consuming it, and leaving
        # CW sending mid-message is worse than leaving PTT keyed (same
        # "always attempt to clean up" reasoning as RadioWorker.
        # _stop_ptt).
        if self.decode_toggle_button.isChecked():
            self.decode_toggle_button.setChecked(False)  # synchronously runs _on_decode_toggled(False)
        self._cancel_send_sequence_timers()
        worker = self._radio_window.worker
        worker.stop_cw_text()
        if self._send_sequence_active:
            # Only release PTT if THIS window's own Send sequence is
            # what's holding it -- an unrelated manual PTT press (the
            # radio window's own PTT button) must not be cut off just
            # because this window happened to close at the same time.
            worker.stop_ptt()
            self._send_sequence_active = False
        self.send_button.setEnabled(True)
        if getattr(self, "_closing_for_real", False):
            event.accept()
            return
        event.ignore()
        self.hide()
