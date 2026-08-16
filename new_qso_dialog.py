"""
NewQsoDialog: collects one QSO's fields, auto-filling from a chosen
connected radio's live frequency/mode and (if that radio's role
participates in satellite tracking) the dashboard's currently active
satellite/transponder -- so logging a contact needs as little manual
typing as possible, while every auto-filled field stays a normal,
overridable widget. Also doubles as the "Edit Selected" form
(log_book_window.py) when constructed with existing_qso: no radio
picker/autofill in that mode, just a pre-filled form.

Shown non-modally (caller calls .show(), not .exec()) -- confirmed
live that .exec()'s default application-modal blocking meant the
operator couldn't touch a RadioWindow (e.g. retune) while this was
open, defeating the point of its live radio autofill entirely. Emits
submitted(dict) with the collected ADIF fields on a successful Log
QSO/Save Changes click instead of returning a value from exec();
persistence and any QRZ upload is the caller's job (log_book_window.py),
same division of responsibility as ConnectionDialog.get_details() had
for the (still modal, still a real one-shot picker) connection flow.
"""

import datetime

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QComboBox,
    QMessageBox,
)

from constants import RADIO_ROLES
import adif
import qso_log

# Independently computed from RADIO_ROLES rather than imported from
# ham_dashboard.py/main_window.py (each of which already does this
# same one-liner itself) -- avoids a circular import, since
# ham_dashboard.py -> log_book_window.py -> new_qso_dialog.py already
# forms one direction of dependency.
_ROLE_LABELS = {value: label for label, value in RADIO_ROLES}

_POLL_INTERVAL_MS = 1000


class NewQsoDialog(QDialog):
    submitted = Signal(dict)  # collected ADIF fields, emitted right before the window closes

    def __init__(self, dashboard, existing_qso=None, parent=None):
        super().__init__(parent)
        self._dashboard = dashboard
        self._existing_qso = existing_qso
        self.result_fields = None
        self.setWindowTitle("Edit QSO" if existing_qso is not None else "New QSO")

        self.radio_combo = QComboBox()
        self.radio_combo.addItem("-- Manual entry only --", None)
        for window in getattr(dashboard, "_connected_radios", []):
            role_label = _ROLE_LABELS.get(window._role, window._role)
            self.radio_combo.addItem(
                f"{window._details['radio_model']} ({role_label}) -- {window._connection_label}", window
            )

        now = datetime.datetime.now(datetime.timezone.utc)
        self.date_input = QLineEdit(now.strftime("%Y%m%d"))
        self.date_input.setToolTip("ADIF date, YYYYMMDD, UTC.")
        self.time_input = QLineEdit(now.strftime("%H%M"))
        self.time_input.setToolTip("ADIF time, HHMM or HHMMSS, UTC.")
        self.call_input = QLineEdit()
        self.band_input = QLineEdit()
        self.freq_input = QLineEdit()
        self.freq_input.setToolTip("MHz")
        self.mode_input = QLineEdit()
        self.submode_input = QLineEdit()
        self.rst_sent_input = QLineEdit()
        self.rst_rcvd_input = QLineEdit()
        self.name_input = QLineEdit()
        self.gridsquare_input = QLineEdit()
        self.sat_name_input = QLineEdit()
        self.prop_mode_input = QLineEdit()
        self.comment_input = QLineEdit()

        form = QFormLayout()
        if existing_qso is None:
            form.addRow("Radio:", self.radio_combo)
        form.addRow("Date:", self.date_input)
        form.addRow("Time On:", self.time_input)
        form.addRow("Call:", self.call_input)
        form.addRow("Band:", self.band_input)
        form.addRow("Freq (MHz):", self.freq_input)
        form.addRow("Mode:", self.mode_input)
        form.addRow("Submode:", self.submode_input)
        form.addRow("RST Sent:", self.rst_sent_input)
        form.addRow("RST Rcvd:", self.rst_rcvd_input)
        form.addRow("Name:", self.name_input)
        form.addRow("Grid Square:", self.gridsquare_input)
        form.addRow("Satellite:", self.sat_name_input)
        form.addRow("Prop Mode:", self.prop_mode_input)
        form.addRow("Comment:", self.comment_input)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        if existing_qso is not None:
            button_box.button(QDialogButtonBox.Ok).setText("Save Changes")
        else:
            button_box.button(QDialogButtonBox.Ok).setText("Log QSO")

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(button_box)
        self.setLayout(layout)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_radio_state)
        if existing_qso is not None:
            self._load_existing(existing_qso)
        else:
            self.radio_combo.currentIndexChanged.connect(self._poll_radio_state)
            self._poll_timer.start(_POLL_INTERVAL_MS)
            self._poll_radio_state()  # fill immediately rather than waiting for the first tick

    def _load_existing(self, qso):
        self.date_input.setText(qso.get("QSO_DATE", ""))
        self.time_input.setText(qso.get("TIME_ON", ""))
        self.call_input.setText(qso.get("CALL", ""))
        self.band_input.setText(qso.get("BAND", ""))
        self.freq_input.setText(qso.get("FREQ", ""))
        self.mode_input.setText(qso.get("MODE", ""))
        self.submode_input.setText(qso.get("SUBMODE", ""))
        self.rst_sent_input.setText(qso.get("RST_SENT", ""))
        self.rst_rcvd_input.setText(qso.get("RST_RCVD", ""))
        self.name_input.setText(qso.get("NAME", ""))
        self.gridsquare_input.setText(qso.get("GRIDSQUARE", ""))
        self.sat_name_input.setText(qso.get("SAT_NAME", ""))
        self.prop_mode_input.setText(qso.get("PROP_MODE", ""))
        self.comment_input.setText(qso.get("COMMENT", ""))

    def _poll_radio_state(self):
        """Reads the picked radio's live state straight off its
        RadioWindow -- same underscore-prefixed attributes
        ham_dashboard.py's own RigctldDialog callbacks already read
        directly (_current_freq_hz, control_widgets["mode"]), an
        established pattern in this app rather than something new
        here. Every field this touches stays a plain editable QLineEdit
        -- the operator can type over any of them before submitting."""
        radio_window = self.radio_combo.currentData()
        if radio_window is None:
            return

        freq_hz = radio_window._current_freq_hz
        if freq_hz is not None:
            self.freq_input.setText(f"{freq_hz / 1e6:.6f}")
            band = adif.band_for_freq_hz(freq_hz)
            if band:
                self.band_input.setText(band)

        mode_widget = radio_window.control_widgets.get("mode")
        if mode_widget is not None:
            mode_fields = adif.adif_mode_fields(mode_widget.currentData())
            if mode_fields.get("MODE"):
                self.mode_input.setText(mode_fields["MODE"])
            self.submode_input.setText(mode_fields.get("SUBMODE", ""))

        # Satellite autofill only makes sense if the CHOSEN radio
        # actually participates in satellite tracking (its role isn't
        # "non_sat") -- satellite/transponder selection itself is
        # centralized on the dashboard (HamClockWindow._active_satellite/
        # .transponder_combo), not per-radio.
        active_satellite = getattr(self._dashboard, "_active_satellite", None)
        if radio_window._role != "non_sat" and active_satellite is not None:
            self.sat_name_input.setText(active_satellite.get("name", ""))
            self.prop_mode_input.setText("SAT")
        else:
            self.sat_name_input.clear()
            self.prop_mode_input.clear()

    def _on_accept(self):
        call = self.call_input.text().strip().upper()
        if not call:
            QMessageBox.warning(self, "New QSO", "Callsign is required.")
            return
        self._poll_timer.stop()

        fields = {
            "QSO_DATE": self.date_input.text().strip(),
            "TIME_ON": self.time_input.text().strip(),
            "CALL": call,
            "BAND": self.band_input.text().strip(),
            "FREQ": self.freq_input.text().strip(),
            "MODE": self.mode_input.text().strip(),
            "SUBMODE": self.submode_input.text().strip(),
            "RST_SENT": self.rst_sent_input.text().strip(),
            "RST_RCVD": self.rst_rcvd_input.text().strip(),
            "NAME": self.name_input.text().strip(),
            "GRIDSQUARE": self.gridsquare_input.text().strip(),
            "SAT_NAME": self.sat_name_input.text().strip(),
            "PROP_MODE": self.prop_mode_input.text().strip(),
            "COMMENT": self.comment_input.text().strip(),
        }
        if self._existing_qso is not None:
            # Preserve local-only QRZ sync metadata AND identity across
            # an edit -- log_book_window.py decides what to do with the
            # sync fields (a synced record gets DELETE+re-INSERT'd; an
            # unsynced one just stays that way until the next sync);
            # UUID_FIELD is how it finds this exact record again
            # afterward (not a list index, which can go stale now that
            # this window is non-modal -- see the module docstring).
            for key in (qso_log.LOGID_FIELD, qso_log.SYNCED_FIELD, qso_log.UUID_FIELD):
                if key in self._existing_qso:
                    fields[key] = self._existing_qso[key]

        self.result_fields = fields
        self.submitted.emit(fields)
        self.accept()
