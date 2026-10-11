"""Persistent store for decoded APRS packets (SQLite).

Qt-free, same reasoning as sstv_images.py/recording.py -- the owning
window (aprs_window.py) is the only part that needs Qt. Lives alongside
those under ~/.icom_radio_app_cache/ (aprs.db).

Why a real database rather than another flat JSON index like the SSTV/
recording ones: APRS decode is a continuous firehose (every packet on
the channel, for days at a time), so the store needs cheap appends, no
whole-file rewrite per packet, and indexed columns the decode window
and map can later sort/filter on in SQL (by time, station, type,
bounding box) without loading everything into RAM.

Each row keeps the packet's identifying fields as plain columns (for
querying) plus the full parsed `info` dict as JSON and the original
`info_raw` bytes as a BLOB, so a packet read back out of the database
is the same dict aprs_window.py/AprsDecoder originally produced --
"Copy Packet for Debugging" keeps working on history, not just on
packets heard this session.

Threading: one shared connection per AprsDatabase, guarded by a lock,
opened with check_same_thread=False. In practice every call comes from
the GUI thread (aprs_window._on_packets_ready), but several
AprsToolWindows (one per connected radio) share the one database, and
the lock makes that safe regardless.
"""

import json
import pathlib
import sqlite3
import threading
import time

DB_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "aprs.db"

# Rows older than this are dropped by prune() (called once at startup by
# aprs_window.py). A busy channel is on the order of tens of thousands
# of packets/day, so this keeps the file bounded for an unattended
# multi-day run rather than growing forever.
DEFAULT_RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at REAL    NOT NULL,           -- unix epoch seconds
    radio       TEXT,                       -- which radio window decoded it
    source      TEXT    NOT NULL,           -- AX.25 source (may be an IGate)
    station     TEXT    NOT NULL,           -- originating station (third-party unwrapped)
    destination TEXT,
    digipeaters TEXT,                       -- comma-separated
    type        TEXT,                       -- info["type"], or 'unknown'
    lat         REAL,
    lon         REAL,
    details     TEXT,                       -- human-readable summary (for text search)
    info_json   TEXT,                       -- full parsed info dict, or NULL
    info_raw    BLOB                        -- original info-field bytes, or NULL
);
CREATE INDEX IF NOT EXISTS idx_packets_received_at ON packets(received_at);
CREATE INDEX IF NOT EXISTS idx_packets_station     ON packets(station, received_at);
CREATE INDEX IF NOT EXISTS idx_packets_type        ON packets(type, received_at);
"""


def _json_default(value):
    # parse_aprs_info is meant to return plain JSON-able values, but a
    # stray bytes/other object must never cost us the whole packet.
    if isinstance(value, (bytes, bytearray)):
        return value.decode("latin-1")
    return repr(value)


class AprsDatabase:
    def __init__(self, path=None):
        self.path = pathlib.Path(path) if path is not None else DB_PATH
        self._lock = threading.Lock()
        self._conn = None

    def _connection(self):
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # WAL + NORMAL: appends don't fsync per packet, and a crash
            # or power loss (the reason this exists) can't corrupt the
            # database -- worst case is losing the last few packets.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---- Write ----

    def add_packet(self, packet, received_at=None, radio=None, details=None):
        """Stores one decoded packet dict (AprsDecoder/DirewolfBackend
        shape). Returns the new row id."""
        info = packet.get("info")
        # Same "real originating station" rule as ham_dashboard.py's
        # _on_aprs_packet_decoded: an unwrapped third-party packet's
        # AX.25 source is the relaying IGate, not the station itself.
        station = (info or {}).get("third_party_source") or packet["source"]
        lat = (info or {}).get("lat")
        lon = (info or {}).get("lon")
        info_raw = packet.get("info_raw")
        row = (
            time.time() if received_at is None else received_at,
            radio,
            packet["source"],
            station,
            packet.get("destination"),
            ",".join(packet.get("digipeaters") or []),
            info["type"] if info else "unknown",
            lat if isinstance(lat, (int, float)) else None,
            lon if isinstance(lon, (int, float)) else None,
            details,
            json.dumps(info, default=_json_default) if info is not None else None,
            bytes(info_raw) if info_raw else None,
        )
        with self._lock:
            conn = self._connection()
            cursor = conn.execute(
                "INSERT INTO packets (received_at, radio, source, station, destination,"
                " digipeaters, type, lat, lon, details, info_json, info_raw)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            conn.commit()
            return cursor.lastrowid

    def prune(self, retention_days=DEFAULT_RETENTION_DAYS):
        """Deletes rows older than retention_days. Returns rows removed."""
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            conn = self._connection()
            cursor = conn.execute("DELETE FROM packets WHERE received_at < ?", (cutoff,))
            conn.commit()
            return cursor.rowcount

    def clear(self):
        with self._lock:
            conn = self._connection()
            conn.execute("DELETE FROM packets")
            conn.commit()

    # ---- Read ----

    @staticmethod
    def _row_to_packet(row):
        info_json = row["info_json"]
        return {
            "id": row["id"],
            "received_at": row["received_at"],
            "radio": row["radio"],
            "source": row["source"],
            "destination": row["destination"] or "",
            "digipeaters": [d for d in (row["digipeaters"] or "").split(",") if d],
            "info": json.loads(info_json) if info_json else None,
            "info_raw": bytes(row["info_raw"]) if row["info_raw"] is not None else None,
        }

    def recent_packets(self, limit=500):
        """The newest `limit` packets, returned oldest-first (the order
        the decode table appends in)."""
        with self._lock:
            rows = self._connection().execute(
                "SELECT * FROM packets ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_packet(r) for r in reversed(rows)]

    @staticmethod
    def _like_escape(text):
        return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @classmethod
    def _add_callsign_clause(cls, where, params, callsign, columns):
        """Appends the callsign-match condition (see latest_positions'
        docstring for the rules) over any of `columns`."""
        call = (callsign or "").strip().upper()
        if not call:
            return
        parts = []
        for column in columns:
            if "-" in call:
                parts.append(f"UPPER({column}) = ?")
                params.append(call)
            else:
                parts.append(f"(UPPER({column}) = ? OR UPPER({column}) LIKE ? ESCAPE '\\')")
                params.extend([call, cls._like_escape(call) + "-%"])
        where.append("(" + " OR ".join(parts) + ")")

    def _filter_clause(self, since, until, callsign, ptype, text):
        where, params = ["1=1"], []
        if since is not None:
            where.append("received_at >= ?")
            params.append(since)
        if until is not None:
            where.append("received_at <= ?")
            params.append(until)
        # Either end of an IGate-relayed packet counts for the decode window.
        self._add_callsign_clause(where, params, callsign, ("source", "station"))
        if ptype:
            where.append("type = ?")
            params.append(ptype)
        needle = (text or "").strip()
        if needle:
            like = f"%{self._like_escape(needle)}%"
            where.append(
                "(details LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\'"
                " OR station LIKE ? ESCAPE '\\' OR destination LIKE ? ESCAPE '\\')"
            )
            params.extend([like] * 4)
        return " AND ".join(where), params

    # Sortable columns -> (SQL column, ...) -- a whitelist, never
    # interpolating caller text into the ORDER BY.
    SORT_COLUMNS = {
        "time": "id", "source": "source", "destination": "destination",
        "type": "type", "details": "details",
    }

    def query_packets(self, since=None, until=None, callsign=None, ptype=None, text=None,
                      sort="time", descending=False, limit=1000):
        """Filtered, sorted packets for the decode window. Returns
        (packets, total_matches): `packets` holds at most `limit` rows in
        the requested display order; `total_matches` is how many rows
        matched in all, so the caller can say "showing N of M".

        The sort is applied to the WHOLE match set before the limit
        (sorting by source shows the alphabetically-first stations, not
        the alphabetically-first of the newest 1000). The one special
        case is the default time-ascending order, which would otherwise
        show the OLDEST matches: it takes the newest `limit` instead and
        displays them oldest-first, matching the live view."""
        column = self.SORT_COLUMNS.get(sort, "id")
        clause, params = self._filter_clause(since, until, callsign, ptype, text)
        newest_window = column == "id" and not descending
        order = "DESC" if (descending or newest_window) else "ASC"
        with self._lock:
            conn = self._connection()
            total = conn.execute(f"SELECT COUNT(*) FROM packets WHERE {clause}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM packets WHERE {clause} ORDER BY {column} {order}, id {order} LIMIT ?",
                [*params, limit],
            ).fetchall()
        packets = [self._row_to_packet(r) for r in rows]
        if newest_window:
            packets.reverse()
        return packets, total

    def packet_types(self):
        """Distinct packet types stored so far, for the filter's type picker."""
        with self._lock:
            rows = self._connection().execute(
                "SELECT DISTINCT type FROM packets WHERE type IS NOT NULL ORDER BY type"
            ).fetchall()
        return [r[0] for r in rows]

    def latest_positions(self, since=None, until=None, callsign=None):
        """Most recent position report per originating station -- what
        the map overlay needs to repopulate itself after a restart, and
        (with the optional filters) what its right-click filter dialog
        asks for. Returns full packet dicts (same shape as
        recent_packets), one per station.

        since/until: unix epoch bounds on received_at (inclusive); the
        "latest" is picked among packets INSIDE the window, so a
        station last heard after `until` still shows its in-window
        position. callsign: case-insensitive; with an SSID ("N0CALL-9")
        it matches exactly that station, without one ("N0CALL") it
        matches every SSID of that callsign. Only type 'position'
        packets count -- the same ones aprs_window.py forwards to the
        map live."""
        where = ["type = 'position'", "lat IS NOT NULL", "lon IS NOT NULL"]
        params = []
        if since is not None:
            where.append("received_at >= ?")
            params.append(since)
        if until is not None:
            where.append("received_at <= ?")
            params.append(until)
        self._add_callsign_clause(where, params, callsign, ("station",))
        clause = " AND ".join(where)
        with self._lock:
            rows = self._connection().execute(
                f"SELECT * FROM packets WHERE id IN (SELECT MAX(id) FROM packets WHERE {clause} GROUP BY station)",
                params,
            ).fetchall()
        return [self._row_to_packet(r) for r in rows]

    def count(self):
        with self._lock:
            return self._connection().execute("SELECT COUNT(*) FROM packets").fetchone()[0]


_shared = None
_shared_lock = threading.Lock()


def shared_database():
    """The process-wide database every AprsToolWindow writes to."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = AprsDatabase()
            try:
                _shared.prune()
            except sqlite3.Error:
                pass  # retention is housekeeping; never block decode on it
        return _shared
