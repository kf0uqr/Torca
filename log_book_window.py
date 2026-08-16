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
import qrz_logbook
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

        self.test_button = QPushButton("Test Connection")
        self.test_button.setToolTip(
            "Calls QRZ's STATUS action with the key currently typed above "
            "-- confirms the key works and shows how many QSOs QRZ has on "
            "file, independent of a full sync."
        )
        self.test_button.clicked.connect(self._on_test_clicked)
        self.test_result_label = QLabel("")
        self.test_result_label.setWordWrap(True)

        form = QFormLayout()
        form.addRow(QLabel(
            "QRZ sync is optional -- the Log Book works fully offline/\n"
            "local-only without a key. Enter one to push/pull QSOs\n"
            "with your QRZ.com Logbook."
        ))
        form.addRow("API Key:", self.key_input)
        form.addRow("", self.test_button)
        form.addRow("", self.test_result_label)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(button_box)
        self.setLayout(layout)

    def _on_test_clicked(self):
        key = self.key_input.text().strip()
        if not key:
            self.test_result_label.setText("Enter a key first.")
            return
        self.test_result_label.setText("Testing...")
        self.test_button.setEnabled(False)
        # A single, explicit, user-initiated STATUS call -- brief enough
        # to block for, same as ConnectionDialog's "Get GPS Coordinates".
        try:
            status = qrz_logbook.qrz_status(key)
        except Exception as exc:
            self.test_result_label.setText(f"Failed: {exc}")
        else:
            self.test_result_label.setText(f"OK -- QRZ says: {status}")
        self.test_button.setEnabled(True)

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
        self._delete_worker = None
        self._open_dialogs = []  # keeps non-modal New QSO/Edit windows alive -- see _track_dialog

        self.table = QTableWidget(0, 0)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        # Extended, not Single -- lets Delete Selected clear out several
        # QSOs at once (e.g. a batch of test entries) via ctrl/shift-
        # click, same as any standard list/table multi-select.
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)

        # New QSO's own button lives on the Ham Dashboard now, next to
        # "Log Book..." -- but the actual logic stays here (this window
        # owns self._qsos/persistence/table refresh); the dashboard's
        # button just calls this window's _on_new_qso_clicked()
        # directly, lazily constructing this window first if needed
        # (see ham_dashboard.py's _on_new_qso_clicked).
        self.edit_button = QPushButton("Edit Selected")
        self.edit_button.clicked.connect(self._on_edit_clicked)
        self.delete_button = QPushButton("Delete Selected")
        self.delete_button.clicked.connect(self._on_delete_clicked)
        self.columns_button = QPushButton("Manage Columns...")
        self.columns_button.clicked.connect(self._on_manage_columns_clicked)
        self.sync_button = QPushButton()
        self.sync_button.clicked.connect(self._on_sync_clicked)
        self.qrz_settings_button = QPushButton("QRZ Settings...")
        self.qrz_settings_button.clicked.connect(self._on_qrz_settings_clicked)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #aaa; font-size: 11px;")

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.edit_button)
        toolbar.addWidget(self.delete_button)
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

    @staticmethod
    def _cell_text(qso, key):
        if key == "_SYNCED":
            return "Yes" if qso_log.is_synced(qso) else "No"
        if key == "_CONFIRMED":
            return "Yes" if qso_log.is_confirmed(qso) else "No"
        return qso.get(key, "")

    def _rebuild_table(self):
        # Newest QSOs first by default -- QSO_DATE (YYYYMMDD) and
        # TIME_ON (HHMM/HHMMSS) both sort correctly as plain fixed-
        # width numeric-ish strings, so combining them is enough for a
        # real chronological order without parsing real datetimes.
        # Reorders self._qsos itself (not just the view) -- fine since
        # every lookup elsewhere in this class finds a QSO by UUID, not
        # by list position, and this is the only place that assigns
        # Qt.UserRole from the current position, right below.
        self._qsos.sort(key=lambda qso: (qso.get("QSO_DATE", ""), qso.get("TIME_ON", "")), reverse=True)

        self.table.setSortingEnabled(False)
        self.table.setColumnCount(len(self._visible_columns))
        self.table.setHorizontalHeaderLabels(
            [qso_log.QSO_FIELD_DEFINITIONS[key]["label"] for key in self._visible_columns]
        )
        self.table.setRowCount(len(self._qsos))
        for row, qso in enumerate(self._qsos):
            for col, key in enumerate(self._visible_columns):
                item = QTableWidgetItem(self._cell_text(qso, key))
                # Remembers this row's position in self._qsos at the time
                # of this rebuild -- the table VIEW's own row order
                # changes when the user clicks a column header to sort,
                # but self._qsos's position (as just set above) is
                # exactly what row was inserted here, so this is a
                # stable way to map a selected view row back to the
                # right list entry.
                item.setData(Qt.UserRole, row)
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)

    def _selected_qso_indexes(self):
        """Returns the self._qsos index (at the time of the last
        _rebuild_table) for every currently selected row, in no
        particular order -- possibly empty. ExtendedSelection mode
        means this can be more than one row (Delete Selected supports
        bulk removal; Edit Selected requires exactly one, checked at
        its own call site)."""
        rows = {item.row() for item in self.table.selectedItems()}
        indexes = []
        for row in rows:
            item = self.table.item(row, 0)
            if item is not None:
                indexes.append(item.data(Qt.UserRole))
        return indexes

    # ---- New QSO / Edit ----
    #
    # Both open a NON-MODAL NewQsoDialog (.show(), not .exec()) --
    # confirmed live that .exec()'s default application-modal blocking
    # meant the operator couldn't touch a RadioWindow (e.g. retune)
    # while New QSO was open, defeating the point of its live radio
    # autofill. That makes every follow-up step here genuinely
    # asynchronous relative to the rest of the app (a background sync
    # can legitimately finish, reordering/replacing self._qsos, while
    # one of these windows is still open) -- so every callback below
    # re-finds its QSO by UUID_FIELD at the moment it actually runs,
    # never by a list index captured earlier.

    def _find_qso_index_by_uuid(self, qso_uuid):
        for index, qso in enumerate(self._qsos):
            if qso.get(qso_log.UUID_FIELD) == qso_uuid:
                return index
        return None

    def _track_dialog(self, dialog):
        # Keeps a Python-side reference alive for as long as a non-modal
        # dialog might be open -- Qt's own parent-child ownership (we
        # already pass parent=self) keeps the underlying widget alive,
        # but a locally-scoped Python variable going out of scope right
        # after .show() returns is still worth avoiding explicitly.
        self._open_dialogs.append(dialog)
        dialog.finished.connect(lambda _result, d=dialog: self._open_dialogs.remove(d) if d in self._open_dialogs else None)

    def _on_new_qso_clicked(self):
        dialog = NewQsoDialog(self._dashboard, parent=self)
        dialog.submitted.connect(self._on_new_qso_submitted)
        self._track_dialog(dialog)
        dialog.show()

    def _on_new_qso_submitted(self, fields):
        fields = qso_log.ensure_uuid(fields)
        self._qsos.append(fields)
        qso_log.save_qso_log(self._qsos)
        self._rebuild_table()

        api_key = self._get_qrz_api_key()
        if not api_key:
            return
        qso_uuid = fields[qso_log.UUID_FIELD]
        self.status_label.setText("Logged locally; uploading to QRZ...")
        self._upload_worker = qso_log.QrzUploadWorker(api_key, fields, self)
        self._upload_worker.finished_upload.connect(lambda updated, u=qso_uuid: self._on_quick_upload_finished(u, updated))
        self._upload_worker.failed.connect(self._on_quick_upload_failed)
        self._upload_worker.start()

    def _on_quick_upload_finished(self, qso_uuid, updated):
        index = self._find_qso_index_by_uuid(qso_uuid)
        if index is not None:
            self._qsos[index] = updated
            qso_log.save_qso_log(self._qsos)
            self._rebuild_table()
        self.status_label.setText("New QSO uploaded to QRZ.")
        self._upload_worker = None

    def _on_quick_upload_failed(self, message):
        self.status_label.setText(f"New QSO logged locally; QRZ upload failed ({message}) -- will retry on next sync.")
        self._upload_worker = None

    def _on_edit_clicked(self):
        indexes = self._selected_qso_indexes()
        if len(indexes) != 1:
            QMessageBox.information(self, "Edit Selected", "Select exactly one QSO to edit.")
            return
        index = indexes[0]
        original = qso_log.ensure_uuid(self._qsos[index])
        self._qsos[index] = original  # persists a freshly-assigned UUID, if this record didn't have one yet
        qso_uuid = original[qso_log.UUID_FIELD]

        dialog = NewQsoDialog(self._dashboard, existing_qso=original, parent=self)
        dialog.submitted.connect(lambda fields, u=qso_uuid, orig=original: self._on_edit_submitted(u, orig, fields))
        self._track_dialog(dialog)
        dialog.show()

    def _on_edit_submitted(self, qso_uuid, original, updated):
        index = self._find_qso_index_by_uuid(qso_uuid)
        if index is None:
            self.status_label.setText("Couldn't save that edit -- the QSO no longer exists locally.")
            return
        self._qsos[index] = updated
        qso_log.save_qso_log(self._qsos)
        self._rebuild_table()

        old_logid = original.get(qso_log.LOGID_FIELD)
        api_key = self._get_qrz_api_key()
        if not (old_logid and api_key):
            return
        self.status_label.setText("Re-uploading corrected QSO to QRZ...")
        self._reupload_worker = qso_log.QrzReuploadWorker(api_key, old_logid, updated, self)
        self._reupload_worker.finished_reupload.connect(lambda result, u=qso_uuid: self._on_reupload_finished(u, result))
        self._reupload_worker.failed.connect(lambda message, u=qso_uuid: self._on_reupload_failed(u, message))
        self._reupload_worker.start()

    def _on_reupload_finished(self, qso_uuid, updated):
        index = self._find_qso_index_by_uuid(qso_uuid)
        if index is not None:
            self._qsos[index] = updated
            qso_log.save_qso_log(self._qsos)
            self._rebuild_table()
        self.status_label.setText("QSO updated and re-uploaded to QRZ.")
        self._reupload_worker = None

    def _on_reupload_failed(self, qso_uuid, message):
        # See qso_log.reupload_qso_to_qrz's docstring -- a failure here
        # might mean the old QRZ record is already gone (DELETE
        # succeeded, INSERT didn't). Clearing the local sync tags
        # either way is the safe, self-healing choice: the next sync
        # pushes it fresh as a new record rather than silently leaving
        # a QSO missing from QRZ.
        index = self._find_qso_index_by_uuid(qso_uuid)
        if index is not None:
            self._qsos[index].pop(qso_log.LOGID_FIELD, None)
            self._qsos[index].pop(qso_log.SYNCED_FIELD, None)
            qso_log.save_qso_log(self._qsos)
            self._rebuild_table()
        self.status_label.setText(f"QRZ re-upload failed ({message}) -- QSO saved locally, will re-sync next time.")
        self._reupload_worker = None

    # ---- Delete ----

    def _on_delete_clicked(self):
        indexes = self._selected_qso_indexes()
        if not indexes:
            QMessageBox.information(self, "Delete Selected", "Select one or more QSOs in the table first.")
            return

        targets = []
        for index in indexes:
            qso = qso_log.ensure_uuid(self._qsos[index])
            self._qsos[index] = qso
            targets.append(qso)

        preview = ", ".join(f"{qso.get('CALL', '?')} ({qso.get('QSO_DATE', '?')})" for qso in targets[:5])
        if len(targets) > 5:
            preview += f", and {len(targets) - 5} more"
        any_synced = any(qso_log.is_synced(qso) for qso in targets)
        confirm = QMessageBox.question(
            self, "Delete QSO" + ("s" if len(targets) > 1 else ""),
            f"Delete {len(targets)} QSO(s): {preview}?\n\n"
            "This cannot be undone" + (" on QRZ.com either." if any_synced else "."),
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        api_key = self._get_qrz_api_key()
        synced_targets = [qso for qso in targets if qso.get(qso_log.LOGID_FIELD) and api_key]
        synced_uuids = {qso[qso_log.UUID_FIELD] for qso in synced_targets}
        unsynced_targets = [qso for qso in targets if qso[qso_log.UUID_FIELD] not in synced_uuids]

        # Local-only (or no key configured) QSOs have nothing to
        # reconcile with QRZ -- remove them right away.
        for qso in unsynced_targets:
            self._remove_qso_by_uuid(qso[qso_log.UUID_FIELD])
        if unsynced_targets and not synced_targets:
            self.status_label.setText(f"Deleted {len(unsynced_targets)} QSO(s) locally.")

        if not synced_targets:
            return

        # Synced ones are only removed locally once QRZ confirms the
        # delete -- see QrzDeleteWorker's docstring -- so a failure
        # here never leaves a QSO silently missing locally while still
        # sitting on QRZ.
        logids = [qso[qso_log.LOGID_FIELD] for qso in synced_targets]
        uuids_to_remove = [qso[qso_log.UUID_FIELD] for qso in synced_targets]
        self.status_label.setText(f"Deleting {len(logids)} QSO(s) from QRZ...")
        self.delete_button.setEnabled(False)
        self._delete_worker = qso_log.QrzDeleteWorker(api_key, logids, self)
        self._delete_worker.finished_delete.connect(lambda u=uuids_to_remove: self._on_delete_finished(u))
        self._delete_worker.failed.connect(self._on_delete_failed)
        self._delete_worker.start()

    def _remove_qso_by_uuid(self, qso_uuid):
        index = self._find_qso_index_by_uuid(qso_uuid)
        if index is not None:
            del self._qsos[index]
            qso_log.save_qso_log(self._qsos)
            self._rebuild_table()

    def _on_delete_finished(self, uuids_to_remove):
        for qso_uuid in uuids_to_remove:
            index = self._find_qso_index_by_uuid(qso_uuid)
            if index is not None:
                del self._qsos[index]
        qso_log.save_qso_log(self._qsos)
        self._rebuild_table()
        self.status_label.setText(f"Deleted {len(uuids_to_remove)} QSO(s) from QRZ and locally.")
        self.delete_button.setEnabled(True)
        self._delete_worker = None

    def _on_delete_failed(self, message):
        self.status_label.setText(f"QRZ delete failed ({message}) -- those QSOs were NOT removed, try again.")
        self.delete_button.setEnabled(True)
        self._delete_worker = None

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
        # Merges by UUID into the CURRENT self._qsos rather than
        # replacing it outright -- the sync worker ran against a
        # snapshot taken when it started, so a wholesale replace here
        # would silently discard any QSO logged locally (New QSO/Edit,
        # now both non-modal and therefore genuinely concurrent with a
        # sync) while it was still in flight. Anything left in by_uuid
        # after matching existing records is a QRZ-only pull, appended
        # as new.
        by_uuid = {qso[qso_log.UUID_FIELD]: qso for qso in updated_qsos if qso.get(qso_log.UUID_FIELD)}
        for index, qso in enumerate(self._qsos):
            qso_uuid = qso.get(qso_log.UUID_FIELD)
            if qso_uuid in by_uuid:
                self._qsos[index] = by_uuid.pop(qso_uuid)
        self._qsos.extend(by_uuid.values())

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
