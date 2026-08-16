"""
LogBookWindow: the QSO Log Book's main window. Lists every QSO from
the local ADIF log (qso_log.py's qsolog.adi -- the source of truth,
works with zero QRZ configuration), sortable and with configurable
columns (qso_log.QSO_FIELD_DEFINITIONS). QRZ.com sync ("Sync with
QRZ") is an optional, periodic, bidirectional reconciliation layered
on top -- see qso_log.sync_with_qrz's own docstring for the push/pull
design.

Opened as a singleton from HamClockWindow (ham_dashboard.py), same
show-or-raise pattern as its "Connect New Radio..." flow -- closeEvent
hides rather than destroys it, so the same instance can always be
re-shown without needing a "closed" signal to know when to build a
fresh one.
"""

from PySide6.QtCore import Qt, QSettings
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
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMessageBox,
)

import qso_log
from new_qso_dialog import NewQsoDialog

_SETTINGS_ORG = "IcomRadioApp"
_SETTINGS_APP = "RadioControl"
_QRZ_API_KEY_SETTING = "qrz_api_key"
_QRZ_SYNC_CURSOR_SETTING = "qrz_sync_cursor"
_VISIBLE_COLUMNS_SETTING = "log_book_visible_columns"


def _settings():
    return QSettings(_SETTINGS_ORG, _SETTINGS_APP)


class ManageColumnsDialog(QDialog):
    """Checkbox table over QSO_FIELD_DEFINITIONS -- same Show/Name
    QTableWidget shape as satellite_tracking.py's SatelliteConfigDialog."""

    def __init__(self, visible_keys, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Columns")
        self.resize(360, 420)

        self._keys = list(qso_log.QSO_FIELD_DEFINITIONS.keys())
        self.table = QTableWidget(len(self._keys), 2)
        self.table.setHorizontalHeaderLabels(["Show", "Field"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for row, key in enumerate(self._keys):
            show_item = QTableWidgetItem()
            show_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            show_item.setCheckState(Qt.Checked if key in visible_keys else Qt.Unchecked)
            self.table.setItem(row, 0, show_item)
            name_item = QTableWidgetItem(qso_log.QSO_FIELD_DEFINITIONS[key]["label"])
            name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 1, name_item)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(self.table)
        layout.addWidget(button_box)
        self.setLayout(layout)

    def result_visible_keys(self):
        return [
            key for row, key in enumerate(self._keys)
            if self.table.item(row, 0).checkState() == Qt.Checked
        ]


class QrzSettingsDialog(QDialog):
    """One field: the QRZ Logbook API access key -- optional, from the
    user's QRZ.com account's Logbook Data subscription. Skippable; the
    Log Book works fully without it (local-only)."""

    def __init__(self, current_key, parent=None):
        super().__init__(parent)
        self.setWindowTitle("QRZ Settings")

        self.key_input = QLineEdit(current_key)
        self.key_input.setEchoMode(QLineEdit.Password)
        self.key_input.setToolTip(
            "From your QRZ.com account's Logbook Data subscription -- "
            "optional. Leave blank to use the Log Book without QRZ sync."
        )

        form = QFormLayout()
        form.addRow(QLabel(
            "QRZ sync is optional -- the Log Book works fully offline/\n"
            "local-only without a key. Enter one to push/pull QSOs\n"
            "with your QRZ.com Logbook."
        ))
        form.addRow("API Key:", self.key_input)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(button_box)
        self.setLayout(layout)

    def result_api_key(self):
        return self.key_input.text().strip()


class LogBookWindow(QWidget):
    def __init__(self, dashboard, parent=None):
        super().__init__(parent)
        self._dashboard = dashboard
        self.setWindowTitle("Log Book")
        self.resize(900, 500)

        self._qsos = qso_log.load_qso_log()
        self._visible_columns = self._load_visible_columns()
        self._sync_worker = None
        self._upload_worker = None
        self._reupload_worker = None

        self.table = QTableWidget(0, 0)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)

        self.new_qso_button = QPushButton("New QSO...")
        self.new_qso_button.clicked.connect(self._on_new_qso_clicked)
        self.edit_button = QPushButton("Edit Selected")
        self.edit_button.clicked.connect(self._on_edit_clicked)
        self.columns_button = QPushButton("Manage Columns...")
        self.columns_button.clicked.connect(self._on_manage_columns_clicked)
        self.sync_button = QPushButton()
        self.sync_button.clicked.connect(self._on_sync_clicked)
        self.qrz_settings_button = QPushButton("QRZ Settings...")
        self.qrz_settings_button.clicked.connect(self._on_qrz_settings_clicked)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #aaa; font-size: 11px;")

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.new_qso_button)
        toolbar.addWidget(self.edit_button)
        toolbar.addWidget(self.columns_button)
        toolbar.addStretch()
        toolbar.addWidget(self.sync_button)
        toolbar.addWidget(self.qrz_settings_button)

        layout = QVBoxLayout()
        layout.addLayout(toolbar)
        layout.addWidget(self.table)
        layout.addWidget(self.status_label)
        self.setLayout(layout)

        self._update_sync_button_state()
        self._rebuild_table()

    # ---- persistence helpers ----

    def _load_visible_columns(self):
        stored = _settings().value(_VISIBLE_COLUMNS_SETTING, "")
        if stored:
            keys = [key for key in stored.split(",") if key in qso_log.QSO_FIELD_DEFINITIONS]
            if keys:
                return keys
        return [key for key, definition in qso_log.QSO_FIELD_DEFINITIONS.items() if definition["default_visible"]]

    def _save_visible_columns(self):
        _settings().setValue(_VISIBLE_COLUMNS_SETTING, ",".join(self._visible_columns))

    def _get_qrz_api_key(self):
        return _settings().value(_QRZ_API_KEY_SETTING, "") or ""

    # ---- table rendering ----

    def _rebuild_table(self):
        self.table.setSortingEnabled(False)
        self.table.setColumnCount(len(self._visible_columns))
        self.table.setHorizontalHeaderLabels(
            [qso_log.QSO_FIELD_DEFINITIONS[key]["label"] for key in self._visible_columns]
        )
        self.table.setRowCount(len(self._qsos))
        for row, qso in enumerate(self._qsos):
            for col, key in enumerate(self._visible_columns):
                text = ("Yes" if qso_log.is_synced(qso) else "No") if key == "_SYNCED" else qso.get(key, "")
                item = QTableWidgetItem(text)
                # Remembers this row's position in self._qsos at the time
                # of this rebuild -- the table view's own row order
                # changes when the user sorts, but self._qsos itself
                # never reorders, so this is a stable way to map a
                # selected VIEW row back to the right list entry.
                item.setData(Qt.UserRole, row)
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)

    def _selected_qso_index(self):
        selected = self.table.selectedItems()
        if not selected:
            return None
        return selected[0].data(Qt.UserRole)

    # ---- New QSO / Edit ----

    def _on_new_qso_clicked(self):
        dialog = NewQsoDialog(self._dashboard, parent=self)
        if dialog.exec() != QDialog.Accepted or dialog.result_fields is None:
            return
        self._qsos.append(dialog.result_fields)
        qso_log.save_qso_log(self._qsos)
        self._rebuild_table()

        api_key = self._get_qrz_api_key()
        if not api_key:
            return
        index = len(self._qsos) - 1
        self.status_label.setText("Logged locally; uploading to QRZ...")
        self._upload_worker = qso_log.QrzUploadWorker(api_key, self._qsos[index], self)
        self._upload_worker.finished_upload.connect(lambda updated, i=index: self._on_quick_upload_finished(i, updated))
        self._upload_worker.failed.connect(self._on_quick_upload_failed)
        self._upload_worker.start()

    def _on_quick_upload_finished(self, index, updated):
        if index < len(self._qsos):
            self._qsos[index] = updated
            qso_log.save_qso_log(self._qsos)
            self._rebuild_table()
        self.status_label.setText("New QSO uploaded to QRZ.")
        self._upload_worker = None

    def _on_quick_upload_failed(self, message):
        self.status_label.setText(f"New QSO logged locally; QRZ upload failed ({message}) -- will retry on next sync.")
        self._upload_worker = None

    def _on_edit_clicked(self):
        index = self._selected_qso_index()
        if index is None:
            QMessageBox.information(self, "Edit Selected", "Select a QSO in the table first.")
            return
        original = self._qsos[index]
        dialog = NewQsoDialog(self._dashboard, existing_qso=original, parent=self)
        if dialog.exec() != QDialog.Accepted or dialog.result_fields is None:
            return
        updated = dialog.result_fields
        self._qsos[index] = updated
        qso_log.save_qso_log(self._qsos)
        self._rebuild_table()

        old_logid = original.get(qso_log.LOGID_FIELD)
        api_key = self._get_qrz_api_key()
        if not (old_logid and api_key):
            return
        self.status_label.setText("Re-uploading corrected QSO to QRZ...")
        self._reupload_worker = qso_log.QrzReuploadWorker(api_key, old_logid, updated, self)
        self._reupload_worker.finished_reupload.connect(lambda result, i=index: self._on_reupload_finished(i, result))
        self._reupload_worker.failed.connect(lambda message, i=index: self._on_reupload_failed(i, message))
        self._reupload_worker.start()

    def _on_reupload_finished(self, index, updated):
        if index < len(self._qsos):
            self._qsos[index] = updated
            qso_log.save_qso_log(self._qsos)
            self._rebuild_table()
        self.status_label.setText("QSO updated and re-uploaded to QRZ.")
        self._reupload_worker = None

    def _on_reupload_failed(self, index, message):
        # See qso_log.reupload_qso_to_qrz's docstring -- a failure here
        # might mean the old QRZ record is already gone (DELETE
        # succeeded, INSERT didn't). Clearing the local sync tags
        # either way is the safe, self-healing choice: the next sync
        # pushes it fresh as a new record rather than silently leaving
        # a QSO missing from QRZ.
        if index < len(self._qsos):
            self._qsos[index].pop(qso_log.LOGID_FIELD, None)
            self._qsos[index].pop(qso_log.SYNCED_FIELD, None)
            qso_log.save_qso_log(self._qsos)
            self._rebuild_table()
        self.status_label.setText(f"QRZ re-upload failed ({message}) -- QSO saved locally, will re-sync next time.")
        self._reupload_worker = None

    # ---- Manage Columns / QRZ Settings ----

    def _on_manage_columns_clicked(self):
        dialog = ManageColumnsDialog(self._visible_columns, self)
        if dialog.exec() != QDialog.Accepted:
            return
        self._visible_columns = dialog.result_visible_keys()
        self._save_visible_columns()
        self._rebuild_table()

    def _on_qrz_settings_clicked(self):
        dialog = QrzSettingsDialog(self._get_qrz_api_key(), self)
        if dialog.exec() != QDialog.Accepted:
            return
        _settings().setValue(_QRZ_API_KEY_SETTING, dialog.result_api_key())
        self._update_sync_button_state()

    # ---- Sync with QRZ ----

    def _update_sync_button_state(self):
        if self._get_qrz_api_key():
            self.sync_button.setText("Sync with QRZ")
            self.sync_button.setEnabled(True)
        else:
            self.sync_button.setText("Sync with QRZ (no key configured)")
            self.sync_button.setEnabled(False)

    def _on_sync_clicked(self):
        api_key = self._get_qrz_api_key()
        if not api_key or self._sync_worker is not None:
            return
        cursor = int(_settings().value(_QRZ_SYNC_CURSOR_SETTING, 0) or 0)
        self.status_label.setText("Syncing with QRZ...")
        self.sync_button.setEnabled(False)
        self._sync_worker = qso_log.QrzSyncWorker(api_key, self._qsos, cursor, self)
        self._sync_worker.finished_sync.connect(self._on_sync_finished)
        self._sync_worker.failed.connect(self._on_sync_failed)
        self._sync_worker.start()

    def _on_sync_finished(self, updated_qsos, new_cursor, summary):
        self._qsos = updated_qsos
        qso_log.save_qso_log(self._qsos)
        _settings().setValue(_QRZ_SYNC_CURSOR_SETTING, new_cursor)
        self._rebuild_table()
        message = f"Synced: {summary['uploaded']} uploaded, {summary['pulled']} pulled down"
        if summary["upload_failures"]:
            message += f", {summary['upload_failures']} upload failure(s)"
        self.status_label.setText(message)
        self._sync_worker = None
        self._update_sync_button_state()

    def _on_sync_failed(self, message):
        self.status_label.setText(f"QRZ sync failed: {message}")
        self._sync_worker = None
        self._update_sync_button_state()

    def closeEvent(self, event):
        # Singleton -- hidden, not destroyed, so HamClockWindow can
        # always show()/raise_() the SAME instance again without
        # needing a "closed" signal to know when to build a new one.
        event.ignore()
        self.hide()
