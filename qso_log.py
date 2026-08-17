"""
Local QSO log -- the log book's source of truth (a plain ADIF file,
adif.py's read_adif_log/write_adif_log), with QRZ.com Logbook API sync
(qrz_logbook.py) as an optional, periodic, bidirectional reconciliation
on top -- never a requirement to use the log at all. A user with no QRZ
account, or QRZ briefly unreachable, still gets a fully working log.
"""

import pathlib
import uuid

from PySide6.QtCore import QThread, Signal

import adif
import qrz_logbook

QSO_LOG_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "qsolog.adi"

# App-specific ADIF fields (the "APP_<progid>_<name>" convention ADIF
# itself defines for exactly this kind of private metadata) recording
# QRZ sync state directly on each record -- keeps qsolog.adi a single,
# genuinely valid, portable ADIF file rather than a local-only format
# with sync bookkeeping bolted on separately. Deliberately NOT renamed
# to APP_TORCA_* for the app's TORCA rebrand -- these field names are
# already written into every user's real, persisted qsolog.adi. Renaming
# them would silently orphan every already-synced QSO's LOGID (making
# already_synced_to_qrz() return False for old rows) and could push
# duplicates to QRZ Logbook. The field name is a private wire format,
# invisible in the UI -- no reason to churn it just for the rebrand.
LOGID_FIELD = "APP_RADIONE_LOGID"
SYNCED_FIELD = "APP_RADIONE_SYNCED"
# A permanent local identity, independent of QRZ's LOGID (which a
# local-only, not-yet-synced QSO doesn't have yet) and independent of
# list position (which shifts on sort/reload/sync-merge). Needed once
# the Log Book window's New QSO/Edit dialogs became non-modal windows
# rather than blocking dialogs -- an async edit or sync can now
# legitimately complete while another window is still open, so any
# code that needs to find "that one QSO again" later has to do it by a
# stable ID, not a captured list index.
UUID_FIELD = "APP_RADIONE_UUID"


def ensure_uuid(qso: dict) -> dict:
    """Returns qso unchanged if it already has UUID_FIELD, otherwise a
    NEW dict with one assigned. Called on every local QSO the first
    time it's created, and on every record pulled from QRZ (which has
    no notion of this app's local UUID convention)."""
    if qso.get(UUID_FIELD):
        return qso
    updated = dict(qso)
    updated[UUID_FIELD] = uuid.uuid4().hex
    return updated

# Ordered ADIF field -> {"label", "default_visible"} -- drives both the
# Log Book table's columns (in this order, left to right) and the
# Manage Columns dialog. "_CONFIRMED"/"_SYNCED" are synthetic pseudo-
# fields (not written to the ADIF file itself, not real ADIF field
# names) -- log_book_window.py special-cases them rather than reading
# them as plain stored values. "_CONFIRMED" is listed first so it
# renders as the leftmost column.
QSO_FIELD_DEFINITIONS = {
    "_CONFIRMED": {"label": "Confirmed", "default_visible": True},
    "QSO_DATE": {"label": "Date", "default_visible": True},
    "TIME_ON": {"label": "Time On", "default_visible": True},
    "CALL": {"label": "Call", "default_visible": True},
    "BAND": {"label": "Band", "default_visible": True},
    "FREQ": {"label": "Freq (MHz)", "default_visible": True},
    "MODE": {"label": "Mode", "default_visible": True},
    "SUBMODE": {"label": "Submode", "default_visible": False},
    "RST_SENT": {"label": "RST Sent", "default_visible": True},
    "RST_RCVD": {"label": "RST Rcvd", "default_visible": True},
    "SAT_NAME": {"label": "Satellite", "default_visible": True},
    "PROP_MODE": {"label": "Prop Mode", "default_visible": False},
    "NAME": {"label": "Name", "default_visible": True},
    "GRIDSQUARE": {"label": "Grid Square", "default_visible": True},
    "QTH": {"label": "QTH", "default_visible": False},
    "COMMENT": {"label": "Comment", "default_visible": False},
    "COUNTRY": {"label": "Country", "default_visible": False},
    "TX_PWR": {"label": "TX Power", "default_visible": False},
    "TIME_OFF": {"label": "Time Off", "default_visible": False},
    "QSO_DATE_OFF": {"label": "Date Off", "default_visible": False},
    "STATION_CALLSIGN": {"label": "Station Callsign", "default_visible": False},
    "MY_GRIDSQUARE": {"label": "My Grid Square", "default_visible": False},
    "QSL_RCVD": {"label": "QSL Rcvd (paper)", "default_visible": False},
    "LOTW_QSL_RCVD": {"label": "LoTW Rcvd", "default_visible": False},
    "EQSL_QSL_RCVD": {"label": "eQSL Rcvd", "default_visible": False},
    "APP_QRZLOG_STATUS": {"label": "QRZ Status (raw)", "default_visible": False},
    "_SYNCED": {"label": "Synced", "default_visible": True},
}


def is_confirmed(qso: dict) -> bool:
    """A QSO counts as confirmed if any confirmation method says so --
    standard ADIF QSL_RCVD (paper), or either electronic confirmation
    service (LOTW_QSL_RCVD/EQSL_QSL_RCVD), each "Y" when confirmed
    (ADIF's own Y/N/R/I enumeration for these fields). Deliberately
    NOT based on QRZ's own APP_QRZLOG_STATUS -- its exact code meanings
    ("C", others seen in the wild) aren't documented anywhere findable,
    so it's kept as a raw, inspectable-but-unverified column instead of
    being trusted for this derived Yes/No."""
    return any(qso.get(field) == "Y" for field in ("QSL_RCVD", "LOTW_QSL_RCVD", "EQSL_QSL_RCVD"))


def load_qso_log():
    qsos = adif.read_adif_log(QSO_LOG_PATH)
    migrated = [ensure_uuid(qso) for qso in qsos]
    if migrated != qsos:  # dict equality -- catches whichever records actually got a fresh UUID
        save_qso_log(migrated)
    return migrated


def save_qso_log(qsos):
    adif.write_adif_log(QSO_LOG_PATH, qsos)


def is_synced(qso: dict) -> bool:
    return bool(qso.get(LOGID_FIELD))


def push_qso_to_qrz(api_key, qso: dict) -> dict:
    """Uploads a single QSO record to QRZ (qrz_insert), returning a NEW
    dict with LOGID_FIELD/SYNCED_FIELD set on success. Raises on
    failure (network, bad key, QRZ RESULT=FAIL) -- callers catch. The
    shared push primitive behind both sync_with_qrz's bulk push loop
    and QrzUploadWorker's single-record quick upload."""
    adif_record = adif.format_adif_record(
        {key: value for key, value in qso.items() if not key.startswith("APP_")}
    )
    result = qrz_logbook.qrz_insert(api_key, adif_record)
    updated = dict(qso)
    if result.get("logid"):
        updated[LOGID_FIELD] = str(result["logid"])
        updated[SYNCED_FIELD] = "Y"
    return updated


# Matches QRZ's own pagination guidance (recommended MAX:250,AFTERLOGID:n
# for large fetches) -- see the QRZ Logbook API docs.
_QRZ_FETCH_PAGE_SIZE = 250


def sync_with_qrz(api_key, qsos, cursor):
    """Bidirectional reconciliation against QRZ's Logbook API -- no Qt
    dependency (pure network I/O + list manipulation), so it's testable
    without a running event loop; QrzSyncWorker below is the thin Qt
    wrapper that runs this off the GUI thread.

    Push: every record with no LOGID_FIELD yet gets uploaded
    (qrz_insert); on success it's tagged with the returned LOGID and
    SYNCED_FIELD. A push failure for one record (bad data, transient
    network error) doesn't stop the rest -- it just stays untagged and
    is retried on the next sync.

    Pull: qrz_fetch("AFTERLOGID:<cursor>,MAX:250"), paginated -- any
    returned record whose LOGID isn't already present locally is
    appended. This is how a QSO logged on QRZ's own web UI, or another
    app on the same account, ends up here too.

    Returns (updated_qsos, new_cursor, summary). updated_qsos is a NEW
    list (the caller decides if/when to persist it via save_qso_log());
    summary is a plain {"uploaded", "upload_failures", "pulled"} dict
    for a user-facing status message."""
    qsos = [dict(qso) for qso in qsos]  # work on a copy

    uploaded = 0
    upload_failures = 0
    for index, qso in enumerate(qsos):
        if is_synced(qso):
            continue
        try:
            updated = push_qso_to_qrz(api_key, qso)
        except Exception as exc:
            upload_failures += 1
            print(f"[ERROR] QSO Log: QRZ upload failed for {qso.get('CALL', '?')} ({exc}); will retry next sync.")
            continue
        if updated.get(LOGID_FIELD):
            qsos[index] = updated
            uploaded += 1

    existing_logids = {qso[LOGID_FIELD] for qso in qsos if qso.get(LOGID_FIELD)}
    pulled = 0
    new_cursor = cursor
    try:
        page_cursor = cursor
        if page_cursor == 0:
            # Confirmed directly against QRZ's own docs: "When the
            # option ALL is given, only the options TYPE and STATUS may
            # also be specified" -- MAX (or anything else) CANNOT be
            # combined with ALL. An earlier attempt sent
            # "ALL,MAX:250" for a from-scratch sync, which QRZ evidently
            # rejects/ignores outright (confirmed live: RESULT=OK,
            # COUNT=0, no error) -- that combination is invalid per
            # their own documented constraint, not a fluke. ALL alone
            # has no MAX-based pagination available, so a from-scratch
            # sync fetches the entire logbook in one request/response;
            # every later, already-synced sync uses AFTERLOGID (which
            # CAN combine with MAX) and paginates normally.
            fetched = qrz_logbook.qrz_fetch(api_key, option="ALL")
            print(f"[QSO Log] QRZ FETCH (option='ALL') returned {len(fetched)} record(s).")
            batch_pulled, page_cursor = _apply_pulled_records(fetched, qsos, existing_logids, page_cursor)
            pulled += batch_pulled
        else:
            while True:
                option = f"AFTERLOGID:{page_cursor},MAX:{_QRZ_FETCH_PAGE_SIZE}"
                fetched = qrz_logbook.qrz_fetch(api_key, option=option)
                print(f"[QSO Log] QRZ FETCH (option={option!r}) returned {len(fetched)} record(s).")
                if not fetched:
                    break
                batch_pulled, page_cursor = _apply_pulled_records(fetched, qsos, existing_logids, page_cursor)
                pulled += batch_pulled
                if len(fetched) < _QRZ_FETCH_PAGE_SIZE:
                    break
        new_cursor = page_cursor
    except Exception as exc:
        print(f"[ERROR] QSO Log: QRZ fetch failed ({exc}); local log unaffected, will retry next sync.")

    summary = {"uploaded": uploaded, "upload_failures": upload_failures, "pulled": pulled}
    return qsos, new_cursor, summary


def _apply_pulled_records(fetched, qsos, existing_logids, page_cursor):
    """Tags each freshly-fetched record with LOGID_FIELD/SYNCED_FIELD
    (when QRZ's own per-record LOGID field is recognized), assigns a
    local UUID, skips anything already present locally, and appends
    the rest to qsos in place. Returns (pulled_count,
    updated_page_cursor) -- shared between sync_with_qrz's ALL (single
    request) and AFTERLOGID (paginated) branches."""
    pulled = 0
    for record in fetched:
        # QRZ's own docs don't spell out the exact per-record field
        # name their FETCH ADIF output uses for a record's LOGID --
        # APP_QRZLOG_LOGID is the commonly-used convention, with a bare
        # LOGID fallback. NOT yet confirmed against a live response --
        # worth double-checking the first time this runs against a real
        # account, and adjusting here if QRZ actually uses a different
        # field name.
        logid = record.get("APP_QRZLOG_LOGID") or record.get("LOGID")
        if logid:
            record[LOGID_FIELD] = str(logid)
            record[SYNCED_FIELD] = "Y"
            if str(logid).isdigit():
                page_cursor = max(page_cursor, int(logid))
        else:
            print(f"[QSO Log] Pulled record has no recognized LOGID field -- raw keys: {sorted(record.keys())}")
        if logid and record[LOGID_FIELD] in existing_logids:
            continue
        qsos.append(ensure_uuid(record))  # QRZ has no notion of this app's local UUID convention
        if logid:
            existing_logids.add(record[LOGID_FIELD])
        pulled += 1
    return pulled, page_cursor


class QrzSyncWorker(QThread):
    """Runs sync_with_qrz() off the GUI thread -- one-shot, matching
    world_map.py's WorldMapImageFetcher shape (a small dedicated
    QThread per operation, not the persistent-connection RadioWorker
    pattern -- QRZ sync has no ongoing connection to maintain)."""

    # Named finished_sync, not finished -- QThread already has its own
    # built-in finished signal (emitted when run() returns); reusing
    # that name would shadow it instead of adding a new one.
    finished_sync = Signal(list, int, dict)  # (updated_qsos, new_cursor, summary)
    failed = Signal(str)

    def __init__(self, api_key, qsos, cursor, parent=None):
        super().__init__(parent)
        self._api_key = api_key
        self._qsos = qsos
        self._cursor = cursor

    def run(self):
        try:
            updated_qsos, new_cursor, summary = sync_with_qrz(self._api_key, self._qsos, self._cursor)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished_sync.emit(updated_qsos, new_cursor, summary)


class QrzUploadWorker(QThread):
    """One-shot upload of a single freshly-logged QSO -- used by
    NewQsoDialog for a quick, best-effort background push right after
    logging, separate from QrzSyncWorker's full push+pull
    reconciliation (which would also pull the user's entire QRZ
    history on a first-ever sync -- not wanted here, just "upload this
    one record")."""

    finished_upload = Signal(dict)  # the updated qso dict (LOGID/SYNCED tagged on success)
    failed = Signal(str)

    def __init__(self, api_key, qso, parent=None):
        super().__init__(parent)
        self._api_key = api_key
        self._qso = qso

    def run(self):
        try:
            updated = push_qso_to_qrz(self._api_key, self._qso)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished_upload.emit(updated)


def reupload_qso_to_qrz(api_key, old_logid, qso: dict) -> dict:
    """DELETE + INSERT -- the only unambiguous way to correct a
    previously-synced QSO, since QRZ's API has no UPDATE action (see
    qrz_logbook.py's module docstring). Raises on failure at either
    step; callers catch. If DELETE succeeds but the following INSERT
    fails, the old record is genuinely gone from QRZ already -- the
    caller should treat the QSO as unsynced again (clear its LOGID/
    SYNCED fields) rather than assume nothing happened, so the next
    sync re-uploads it as a fresh record instead of silently leaving
    it missing from QRZ."""
    qrz_logbook.qrz_delete(api_key, [int(old_logid)])
    fresh = {key: value for key, value in qso.items() if not key.startswith("APP_")}
    return push_qso_to_qrz(api_key, fresh)


class QrzReuploadWorker(QThread):
    """Runs reupload_qso_to_qrz() off the GUI thread -- used by
    LogBookWindow's Edit Selected flow when the edited record already
    has a LOGID and a QRZ key is configured."""

    finished_reupload = Signal(dict)  # updated qso (new LOGID tagged on success)
    failed = Signal(str)

    def __init__(self, api_key, old_logid, qso, parent=None):
        super().__init__(parent)
        self._api_key = api_key
        self._old_logid = old_logid
        self._qso = qso

    def run(self):
        try:
            updated = reupload_qso_to_qrz(self._api_key, self._old_logid, self._qso)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished_reupload.emit(updated)


class QrzDeleteWorker(QThread):
    """Deletes one or more QSOs from QRZ by LOGID, in a single request
    (qrz_delete already accepts a list) -- used by LogBookWindow's
    Delete Selected for whichever selected QSOs were previously synced
    (have a LOGID) when a QRZ key is configured. Local removal only
    happens after this succeeds -- see log_book_window.py's
    _on_delete_finished -- so a QRZ delete failure never leaves a QSO
    silently missing locally while still sitting on QRZ."""

    finished_delete = Signal()
    failed = Signal(str)

    def __init__(self, api_key, logids, parent=None):
        super().__init__(parent)
        self._api_key = api_key
        self._logids = logids

    def run(self):
        try:
            qrz_logbook.qrz_delete(self._api_key, self._logids)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished_delete.emit()
