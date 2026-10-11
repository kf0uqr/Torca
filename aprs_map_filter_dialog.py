"""Filter dialog for the dashboard's APRS map overlay (opened by
right-clicking the APRS button, see ham_dashboard.py's
_on_aprs_menu_requested).

Pure input form: exec() it, then read .filter() -- it never touches the
map or the database itself. The filter is a plain dict
{"since": epoch|None, "until": epoch|None, "callsign": str} (see
aprs_db.AprsDatabase.latest_positions for the exact matching rules);
an all-empty filter means "show everything"."""

import datetime

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (
    QCheckBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

_DATETIME_FORMAT = "yyyy-MM-dd HH:mm"

EMPTY_FILTER = {"since": None, "until": None, "callsign": ""}


def filter_is_active(flt):
    return bool(flt and (flt.get("since") is not None or flt.get("until") is not None or flt.get("callsign")))


class AprsMapFilterDialog(QDialog):
    def __init__(self, current=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("APRS Map Filter")
        current = current or EMPTY_FILTER

        now = QDateTime.currentDateTime()
        self.since_check = QCheckBox("Heard since:")
        self.since_edit = QDateTimeEdit(now.addDays(-1))
        self.until_check = QCheckBox("Heard until:")
        self.until_edit = QDateTimeEdit(now)
        for check, edit, epoch in (
            (self.since_check, self.since_edit, current.get("since")),
            (self.until_check, self.until_edit, current.get("until")),
        ):
            edit.setDisplayFormat(_DATETIME_FORMAT)
            edit.setCalendarPopup(True)
            if epoch is not None:
                edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(epoch)))
                check.setChecked(True)
            edit.setEnabled(check.isChecked())
            check.toggled.connect(edit.setEnabled)

        self.callsign_edit = QLineEdit(current.get("callsign") or "")
        self.callsign_edit.setPlaceholderText("e.g. N0CALL or N0CALL-9 (blank = all)")
        self.callsign_edit.setToolTip(
            "Without an SSID (N0CALL) matches every SSID of that callsign; "
            "with one (N0CALL-9) matches exactly that station."
        )

        hint = QLabel(
            "Shows each station's most recent position within the time range "
            "(local time). Leave everything unchecked/blank to show all stations."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #aaa;")

        form = QFormLayout()
        form.addRow(self.since_check, self.since_edit)
        form.addRow(self.until_check, self.until_edit)
        form.addRow("Callsign:", self.callsign_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        reset_button = QPushButton("Clear Filter")
        reset_button.clicked.connect(self._on_reset)
        buttons.addButton(reset_button, QDialogButtonBox.ResetRole)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #c66;")

        layout = QVBoxLayout()
        layout.addWidget(hint)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def _on_accept(self):
        if self.since_check.isChecked() and self.until_check.isChecked() \
                and self.since_edit.dateTime() > self.until_edit.dateTime():
            self.status_label.setText("\"Since\" must be before \"until\".")
            return
        self.accept()

    def _on_reset(self):
        self.since_check.setChecked(False)
        self.until_check.setChecked(False)
        self.callsign_edit.clear()
        self.accept()

    def filter(self):
        def epoch(check, edit):
            return float(edit.dateTime().toSecsSinceEpoch()) if check.isChecked() else None

        until = epoch(self.until_check, self.until_edit)
        if until is not None:
            until += 59.999  # picker has minute resolution; include the whole chosen minute
        return {
            "since": epoch(self.since_check, self.since_edit),
            "until": until,
            "callsign": self.callsign_edit.text().strip().upper(),
        }
