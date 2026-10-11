"""Tests for aprs_db.py (persistent decoded-packet store) and its
aprs_window.py integration -- the window is exercised offscreen with a
stub radio window, no real radio/audio involved."""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import aprs_db
from aprs import encode_position_info, parse_aprs_info
from aprs_db import AprsDatabase


def _position_packet(source="N0CALL-9", lat=40.0, lon=-105.0, **info_extra):
    """A packet shaped exactly like AprsDecoder.feed()'s output -- info
    comes from the real parse_aprs_info, not a hand-built dict."""
    info_raw = encode_position_info(lat, lon, "/", ">", "test") + b"\xb0"
    info = parse_aprs_info(info_raw, "APRS")
    info.update(info_extra)
    return {
        "source": source, "destination": "APRS", "digipeaters": ["WIDE1-1", "WIDE2-1"],
        "info": info, "info_raw": info_raw,
    }


def test_round_trip_preserves_packet(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    packet = _position_packet()
    db.add_packet(packet, received_at=1000.0, radio="IC-7300", details="40.0 -105.0")
    (loaded,) = db.recent_packets()
    assert loaded["source"] == "N0CALL-9"
    assert loaded["digipeaters"] == ["WIDE1-1", "WIDE2-1"]
    assert loaded["info"] == packet["info"]
    assert loaded["info_raw"] == packet["info_raw"]  # non-ASCII bytes survive intact
    assert loaded["received_at"] == 1000.0


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "aprs.db"
    db = AprsDatabase(path)
    db.add_packet(_position_packet())
    db.close()
    assert AprsDatabase(path).count() == 1


def test_packet_without_info_is_stored_as_unknown(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    db.add_packet({"source": "K0XYZ", "destination": "APRS", "digipeaters": [], "info": None, "info_raw": None})
    (loaded,) = db.recent_packets()
    assert loaded["info"] is None and loaded["info_raw"] is None
    assert loaded["digipeaters"] == []


def test_recent_packets_limit_returns_newest_oldest_first(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    for i in range(10):
        db.add_packet(_position_packet(source=f"K0A-{i}"))
    assert [p["source"] for p in db.recent_packets(3)] == ["K0A-7", "K0A-8", "K0A-9"]


def test_latest_positions_keys_on_third_party_origin_and_keeps_newest(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    db.add_packet(_position_packet(source="IGATE", lat=1.0, lon=1.0, third_party_source="REAL-1"))
    db.add_packet(_position_packet(source="IGATE", lat=2.0, lon=2.0, third_party_source="REAL-2"))
    db.add_packet(_position_packet(source="IGATE", lat=3.0, lon=3.0, third_party_source="REAL-1"))
    db.add_packet({"source": "NOPOS", "destination": "APRS", "digipeaters": [],
                   "info": {"type": "status", "raw": "x"}, "info_raw": b"x"})
    positions = {p["info"]["third_party_source"] if "third_party_source" in p["info"] else p["source"]:
                 (p["info"]["lat"], p["info"]["lon"]) for p in db.latest_positions()}
    assert positions == {"REAL-1": (3.0, 3.0), "REAL-2": (2.0, 2.0)}


def test_prune_removes_only_old_rows(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    db.add_packet(_position_packet(source="OLD"), received_at=time.time() - 40 * 86400)
    db.add_packet(_position_packet(source="NEW"))
    assert db.prune(retention_days=30) == 1
    assert [p["source"] for p in db.recent_packets()] == ["NEW"]


def test_non_json_safe_info_values_do_not_lose_the_packet(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    db.add_packet(_position_packet(blob=b"\x00\xff", odd=object()))
    assert db.count() == 1


# ---- aprs_window.py integration ----

def _make_window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import aprs_window

    db = AprsDatabase(tmp_path / "aprs.db")
    monkeypatch.setattr(aprs_window, "shared_database", lambda: db)

    class StubRadioWindow:
        _connection_label = "test-radio"

    return aprs_window, aprs_window.AprsToolWindow(StubRadioWindow()), db


def test_window_saves_decodes_and_caps_table(tmp_path, monkeypatch):
    aprs_window, window, db = _make_window(tmp_path, monkeypatch)
    monkeypatch.setattr(aprs_window, "_MAX_TABLE_ROWS", 5)
    emitted = []
    window.packet_decoded.connect(emitted.append)

    window._on_packets_ready([_position_packet(source=f"K0A-{i}") for i in range(8)])

    assert db.count() == 8                    # everything persisted...
    assert window.table.rowCount() == 5       # ...but the view is bounded
    assert window.table.item(0, 1).text() == "K0A-3"  # oldest rows dropped first
    assert len(emitted) == 8                  # map signal unaffected by the cap


def test_window_reloads_history_on_open_and_clear_keeps_it(tmp_path, monkeypatch):
    aprs_window, window, db = _make_window(tmp_path, monkeypatch)
    window._on_packets_ready([_position_packet(source="K0A-1"), _position_packet(source="K0A-2")])
    window._on_clear_clicked()
    assert window.table.rowCount() == 0 and db.count() == 2

    class StubRadioWindow:
        _connection_label = "test-radio"

    reopened = aprs_window.AprsToolWindow(StubRadioWindow())
    assert [reopened.table.item(r, 1).text() for r in range(reopened.table.rowCount())] == ["K0A-1", "K0A-2"]
    # reloaded rows still carry the full packet (incl. raw bytes) for "Copy Packet for Debugging"
    assert reopened.table.item(0, 0).data(aprs_window.Qt.UserRole)["info_raw"] is not None


# ---- latest_positions filters ----

def _seed(db):
    db.add_packet(_position_packet(source="N0CALL-9", lat=1.0, lon=1.0), received_at=1000.0)
    db.add_packet(_position_packet(source="N0CALL-9", lat=2.0, lon=2.0), received_at=2000.0)
    db.add_packet(_position_packet(source="N0CALL-1", lat=3.0, lon=3.0), received_at=1500.0)
    db.add_packet(_position_packet(source="N0CALLX", lat=4.0, lon=4.0), received_at=1500.0)
    db.add_packet(_position_packet(source="W1AW", lat=5.0, lon=5.0), received_at=3000.0)


def _latest(db, **kw):
    return {p["source"]: p["info"]["lat"] for p in db.latest_positions(**kw)}


def test_latest_positions_unfiltered_returns_newest_per_station(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    _seed(db)
    assert _latest(db) == {"N0CALL-9": 2.0, "N0CALL-1": 3.0, "N0CALLX": 4.0, "W1AW": 5.0}


def test_latest_positions_time_window_picks_newest_inside_window(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    _seed(db)
    # N0CALL-9's newest packet (t=2000) is outside; its t=1000 one is not
    assert _latest(db, until=1200.0) == {"N0CALL-9": 1.0}
    assert _latest(db, since=1400.0, until=2500.0) == {"N0CALL-9": 2.0, "N0CALL-1": 3.0, "N0CALLX": 4.0}


def test_latest_positions_callsign_filter(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    _seed(db)
    assert _latest(db, callsign="n0call-9") == {"N0CALL-9": 2.0}          # exact SSID, case-insensitive
    assert set(_latest(db, callsign="N0CALL")) == {"N0CALL-9", "N0CALL-1"}  # any SSID, but not N0CALLX
    assert _latest(db, callsign="W1AW") == {"W1AW": 5.0}
    assert _latest(db, callsign="100%_") == {}                             # LIKE wildcards are literal


def test_filter_dialog_round_trip():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from aprs_map_filter_dialog import AprsMapFilterDialog, filter_is_active

    assert not filter_is_active({"since": None, "until": None, "callsign": ""})
    dialog = AprsMapFilterDialog({"since": 1_700_000_000.0, "until": None, "callsign": "N0CALL"})
    flt = dialog.filter()
    assert abs(flt["since"] - 1_700_000_000.0) < 60  # picker has minute resolution
    assert flt["until"] is None and flt["callsign"] == "N0CALL"
    assert filter_is_active(flt)
    dialog.callsign_edit.setText(" w1aw ")
    assert dialog.filter()["callsign"] == "W1AW"


# ---- ham_dashboard.py APRS overlay logic (methods called on a stand-in `self`) ----

def _fake_dashboard(db, monkeypatch):
    import types
    import ham_dashboard
    from aprs_map_filter_dialog import EMPTY_FILTER

    monkeypatch.setattr(ham_dashboard, "shared_database", lambda: db)
    cls = ham_dashboard.HamClockWindow
    fake = types.SimpleNamespace(_aprs_stations={}, _aprs_filter=dict(EMPTY_FILTER))
    for name in ("_reload_aprs_stations_from_db", "_add_aprs_station", "_aprs_passes_filter"):
        setattr(fake, name, getattr(cls, name).__get__(fake))
    fake._aprs_tooltip_text = cls._aprs_tooltip_text
    return fake


def test_dashboard_seeds_map_from_database_with_heard_time(tmp_path, monkeypatch):
    db = AprsDatabase(tmp_path / "aprs.db")
    db.add_packet(_position_packet(source="IGATE", lat=1.0, lon=2.0, third_party_source="REAL-1"), received_at=1000.0)
    fake = _fake_dashboard(db, monkeypatch)
    fake._reload_aprs_stations_from_db()
    assert set(fake._aprs_stations) == {"REAL-1"}   # keyed by originating station, not the IGate
    assert "Heard: " in fake._aprs_stations["REAL-1"]["tooltip"]


def test_dashboard_live_packets_respect_filter(tmp_path, monkeypatch):
    db = AprsDatabase(tmp_path / "aprs.db")
    fake = _fake_dashboard(db, monkeypatch)
    fake._aprs_filter = {"since": None, "until": None, "callsign": "N0CALL"}
    assert fake._aprs_passes_filter(_position_packet(source="N0CALL-7"))
    assert not fake._aprs_passes_filter(_position_packet(source="N0CALLX"))
    assert not fake._aprs_passes_filter(_position_packet(source="W1AW"))
    fake._aprs_filter = {"since": None, "until": time.time() - 3600, "callsign": ""}
    assert not fake._aprs_passes_filter(_position_packet())   # window ended an hour ago


# ---- query_packets (decode-window filter/sort) ----

def _seed_query(db):
    db.add_packet(_position_packet(source="W1AW", lat=1.0, lon=1.0), received_at=1000.0, details="alpha beacon")
    db.add_packet(_position_packet(source="N0CALL-9", lat=2.0, lon=2.0), received_at=2000.0, details="bravo")
    db.add_packet(_position_packet(source="N0CALL-1", lat=3.0, lon=3.0), received_at=3000.0, details="100%_literal")
    db.add_packet({"source": "K0MSG", "destination": "APRS", "digipeaters": [],
                   "info": {"type": "message", "raw": "x"}, "info_raw": b"x"}, received_at=4000.0, details="hello")


def _sources(result):
    return [p["source"] for p in result[0]]


def test_query_default_is_time_ascending_and_limit_keeps_newest(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    _seed_query(db)
    assert _sources(db.query_packets()) == ["W1AW", "N0CALL-9", "N0CALL-1", "K0MSG"]
    packets, total = db.query_packets(limit=2)
    assert [p["source"] for p in packets] == ["N0CALL-1", "K0MSG"] and total == 4  # newest 2, oldest-first


def test_query_sorting_applies_before_limit(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    _seed_query(db)
    assert _sources(db.query_packets(sort="source")) == ["K0MSG", "N0CALL-1", "N0CALL-9", "W1AW"]
    assert _sources(db.query_packets(sort="source", descending=True, limit=2)) == ["W1AW", "N0CALL-9"]
    assert _sources(db.query_packets(sort="time", descending=True))[0] == "K0MSG"
    assert _sources(db.query_packets(sort="bogus; DROP TABLE packets")) == ["W1AW", "N0CALL-9", "N0CALL-1", "K0MSG"]


def test_query_filters(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    _seed_query(db)
    assert set(_sources(db.query_packets(callsign="n0call"))) == {"N0CALL-9", "N0CALL-1"}
    assert _sources(db.query_packets(callsign="N0CALL-9")) == ["N0CALL-9"]
    assert _sources(db.query_packets(ptype="message")) == ["K0MSG"]
    assert _sources(db.query_packets(text="ALPHA")) == ["W1AW"]            # case-insensitive
    assert _sources(db.query_packets(text="100%_")) == ["N0CALL-1"]         # wildcards literal
    assert _sources(db.query_packets(since=1500.0, until=3500.0)) == ["N0CALL-9", "N0CALL-1"]
    assert db.query_packets(callsign="N0CALL", ptype="message") == ([], 0)  # filters combine with AND
    assert db.packet_types() == ["message", "position"]


def test_query_callsign_matches_igate_relayed_originating_station(tmp_path):
    db = AprsDatabase(tmp_path / "aprs.db")
    db.add_packet(_position_packet(source="IGATE", third_party_source="REAL-1"))
    assert _sources(db.query_packets(callsign="REAL-1")) == ["IGATE"]
    assert _sources(db.query_packets(callsign="IGATE")) == ["IGATE"]


# ---- decode window in query mode ----

def test_window_filter_sort_and_live_requery(tmp_path, monkeypatch):
    aprs_window, window, db = _make_window(tmp_path, monkeypatch)
    window._on_packets_ready([_position_packet(source=s) for s in ("W1AW", "N0CALL-9", "N0CALL-1")])
    names = lambda: [window.table.item(r, 1).text() for r in range(window.table.rowCount())]
    assert names() == ["W1AW", "N0CALL-9", "N0CALL-1"] and not window._query_mode()

    window.callsign_filter.setText("N0CALL")
    window._refresh_view()                       # (normally fired by the debounce timer)
    assert window._query_mode() and names() == ["N0CALL-9", "N0CALL-1"]

    # live packet while filtered: not appended directly, a requery is scheduled instead
    window._on_packets_ready([_position_packet(source="N0CALL-5")])
    assert names() == ["N0CALL-9", "N0CALL-1"] and window._requery_timer.isActive()
    window._refresh_view()
    assert names() == ["N0CALL-9", "N0CALL-1", "N0CALL-5"]

    # header click cycles asc -> desc -> default
    window._on_header_clicked(1)
    assert names() == ["N0CALL-1", "N0CALL-5", "N0CALL-9"]
    window._on_header_clicked(1)
    assert names() == ["N0CALL-9", "N0CALL-5", "N0CALL-1"]
    window._on_header_clicked(1)
    assert window._sort is None and names() == ["N0CALL-9", "N0CALL-1", "N0CALL-5"]

    window._on_reset_filters_clicked()
    assert not window._query_mode() and len(names()) == 4


# ---- no-audio watchdog ----

def test_audio_watchdog_warns_when_stream_stalls_and_clears_on_resume(tmp_path, monkeypatch):
    aprs_window, window, db = _make_window(tmp_path, monkeypatch)
    now = 1000.0
    window._last_audio_monotonic = now

    window._check_audio_flow(now=now + 2)                       # within the grace/normal gap
    assert window.audio_warning_label.isHidden()
    window._check_audio_flow(now=now + aprs_window._AUDIO_STALL_SECONDS + 3)
    assert not window.audio_warning_label.isHidden()
    assert "No audio received" in window.audio_warning_label.text() and "8 s" in window.audio_warning_label.text()

    window._last_audio_monotonic = now + 20                     # audio came back
    window._check_audio_flow(now=now + 21)
    assert window.audio_warning_label.isHidden()


def test_audio_watchdog_idle_when_not_decoding(tmp_path, monkeypatch):
    aprs_window, window, db = _make_window(tmp_path, monkeypatch)
    window._check_audio_flow(now=10_000.0)                      # never started: no timestamp, no warning
    assert window.audio_warning_label.isHidden() and not window._audio_watchdog.isActive()


def test_both_audio_callbacks_stamp_the_watchdog(tmp_path, monkeypatch):
    aprs_window, window, db = _make_window(tmp_path, monkeypatch)
    window._on_decode_audio_frame(b"\x00\x00")                  # no decoder yet -- still counts as audio arriving
    first = window._last_audio_monotonic
    assert first is not None
    window._last_audio_monotonic = None
    window._on_direwolf_audio_frame(b"\x00\x00")
    assert window._last_audio_monotonic is not None
