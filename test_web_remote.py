"""
Tests for web_remote/ (Remote Access) -- app.py's pure snapshot-shaping
functions and token-gated websocket auth, plus bridge.py's
RadioRemoteState cross-thread cache. Doesn't spin up a real
RemoteWebServer/QThread/uvicorn -- FastAPI's TestClient exercises the
ASGI app directly (same convention as any other FastAPI app test), and
RadioRemoteState is exercised against a fake window/worker rather than
a real RadioWorker (needs a real QApplication for its Qt signals, same
"offscreen QApplication" pattern already used by
test_radio_worker_meters.py's test_meter_widget_reflects_set_definitions).
"""

import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from web_remote.app import (
    create_app,
    dashboard_snapshot,
    find_radio_window,
    radio_snapshot,
    map_markers,
    satellite_positions,
)


class FakeRemoteState:
    def __init__(self, state):
        self.state = state


class FakeWorkerStub:
    def __init__(self, is_dual_receiver=False):
        self.is_dual_receiver = is_dual_receiver


class FakeWindow:
    def __init__(self, remote_id, model, freq_hz=None, mode=None, is_dual_receiver=False):
        self.remote_id = remote_id
        self._details = {"radio_model": model}
        self.remote_state = FakeRemoteState({
            "freq_hz": freq_hz, "mode": mode, "ptt": False,
            "meters": {}, "meter_defs": {}, "scope": None,
        })
        self.worker = FakeWorkerStub(is_dual_receiver)


class FakeMapLayersRemoteState:
    def __init__(self, **enabled):
        self.state = {
            "pota": False, "pskreporter": False, "qsos": False, "aprs": False,
            "satellites": False, "band_filter": None, "band_options": [],
            **enabled,
        }


class FakeDashboard:
    def __init__(self, radios=None, passes=None, pota=None, pskreporter=None):
        self._connected_radios = radios or []
        self._upcoming_passes = passes or []
        self._pota_spots_cache = pota or []
        self._pskreporter_spots_cache = pskreporter or []
        self._aprs_stations = {}
        self.map_layers_remote_state = FakeMapLayersRemoteState()


def test_dashboard_snapshot_shape_with_no_radios():
    snapshot = dashboard_snapshot(FakeDashboard())
    assert snapshot["radios"] == []
    assert snapshot["passes"] == []
    assert snapshot["spots"] == {"pota": 0, "pskreporter": 0}
    assert isinstance(snapshot["recent_qsos"], list)


def test_dashboard_snapshot_includes_connected_radio():
    window = FakeWindow(1, "IC-7300", freq_hz=14074000, mode="USB")
    snapshot = dashboard_snapshot(FakeDashboard(radios=[window]))
    assert snapshot["radios"] == [{"id": 1, "label": "IC-7300", "freq_hz": 14074000, "mode": "USB"}]


def test_dashboard_snapshot_operator_location_none_by_default():
    snapshot = dashboard_snapshot(FakeDashboard())
    assert snapshot["operator_location"] is None


def test_dashboard_snapshot_operator_location_present():
    dashboard = FakeDashboard()
    dashboard._observer_lat = 38.5
    dashboard._observer_lon = -97.3
    snapshot = dashboard_snapshot(dashboard)
    assert snapshot["operator_location"] == {"lat": 38.5, "lon": -97.3}


def test_map_markers_pota_from_cache():
    dashboard = FakeDashboard(pota=[{"lat": 1.0, "lon": 2.0, "activator": "N0CALL"}])
    dashboard.map_layers_remote_state = FakeMapLayersRemoteState(pota=True)
    markers = map_markers(dashboard)
    assert markers["pota"] == [{"lat": 1.0, "lon": 2.0, "label": "N0CALL"}]


def test_map_markers_pskreporter_uses_grid_square():
    dashboard = FakeDashboard(pskreporter=[{"locator": "EM12ab", "callsign": "W0TEST"}])
    dashboard.map_layers_remote_state = FakeMapLayersRemoteState(pskreporter=True)
    markers = map_markers(dashboard)
    assert len(markers["pskreporter"]) == 1
    assert markers["pskreporter"][0]["label"] == "W0TEST"
    assert isinstance(markers["pskreporter"][0]["lat"], float)


def test_map_markers_skips_unparseable_locator():
    dashboard = FakeDashboard(pskreporter=[{"locator": "??", "callsign": "BAD"}])
    dashboard.map_layers_remote_state = FakeMapLayersRemoteState(pskreporter=True)
    markers = map_markers(dashboard)
    assert markers["pskreporter"] == []


def test_map_markers_hidden_when_layer_disabled():
    """The actual bug this fixes: the desktop's _pota_spots_cache/
    _pskreporter_spots_cache keep the last fetch's data even after
    the corresponding toggle button is switched OFF (only the desktop
    map widget's markers get cleared, not the cache) -- so without
    gating on map_layers_remote_state, the web map kept showing
    spots the desktop wasn't showing at all."""
    dashboard = FakeDashboard(
        pota=[{"lat": 1.0, "lon": 2.0, "activator": "N0CALL"}],
        pskreporter=[{"locator": "EM12ab", "callsign": "W0TEST"}],
    )
    # FakeDashboard's default map_layers_remote_state is all-disabled.
    markers = map_markers(dashboard)
    assert markers["pota"] == []
    assert markers["pskreporter"] == []


def test_map_markers_default_hidden_when_dashboard_has_no_toggle_state():
    # Guards against a dashboard object that predates this attribute
    # (or a test double that doesn't set it) silently showing markers.
    dashboard = FakeDashboard(pota=[{"lat": 1.0, "lon": 2.0, "activator": "N0CALL"}])
    del dashboard.map_layers_remote_state
    markers = map_markers(dashboard)
    assert markers["pota"] == []


def test_map_markers_band_filter_applies_to_pota_and_qsos():
    dashboard = FakeDashboard(pota=[
        {"lat": 1.0, "lon": 2.0, "activator": "N0CALL", "band": "20m"},
        {"lat": 3.0, "lon": 4.0, "activator": "W0OTHER", "band": "40m"},
    ])
    dashboard.map_layers_remote_state = FakeMapLayersRemoteState(pota=True, band_filter="20m")
    markers = map_markers(dashboard)
    assert markers["pota"] == [{"lat": 1.0, "lon": 2.0, "label": "N0CALL"}]


def test_map_markers_aprs_from_stations():
    dashboard = FakeDashboard()
    dashboard._aprs_stations = {"N0CALL-9": {"lat": 5.0, "lon": 6.0, "tooltip": "N0CALL-9 details"}}
    dashboard.map_layers_remote_state = FakeMapLayersRemoteState(aprs=True)
    markers = map_markers(dashboard)
    assert markers["aprs"] == [{"lat": 5.0, "lon": 6.0, "label": "N0CALL-9 details"}]


def test_map_markers_aprs_hidden_when_disabled():
    dashboard = FakeDashboard()
    dashboard._aprs_stations = {"N0CALL-9": {"lat": 5.0, "lon": 6.0, "tooltip": "x"}}
    markers = map_markers(dashboard)
    assert markers["aprs"] == []


def test_satellite_positions_hidden_when_toggle_off():
    dashboard = FakeDashboard()
    dashboard._satellite_positions_cache = [
        {"name": "AO-7", "lat": 1.0, "lon": 2.0, "active": True, "footprint": [(1.0, 2.0), (3.0, 4.0)]},
    ]
    positions = satellite_positions(dashboard, dashboard.map_layers_remote_state)
    assert positions == []


def test_satellite_positions_shown_when_toggle_on():
    dashboard = FakeDashboard()
    dashboard._satellite_positions_cache = [
        {
            "name": "AO-7", "lat": 1.0, "lon": 2.0, "active": True,
            "footprint": [(1.0, 2.0), (3.0, 4.0)],
            "path": [(5.0, 6.0), (7.0, 8.0)],
        },
        {"name": "SO-50", "lat": 9.0, "lon": 10.0, "active": False, "footprint": []},
    ]
    dashboard.map_layers_remote_state = FakeMapLayersRemoteState(satellites=True)
    positions = satellite_positions(dashboard, dashboard.map_layers_remote_state)
    assert positions[0] == {
        "name": "AO-7", "lat": 1.0, "lon": 2.0, "active": True,
        "footprint": [{"lat": 1.0, "lon": 2.0}, {"lat": 3.0, "lon": 4.0}],
        "path": [{"lat": 5.0, "lon": 6.0}, {"lat": 7.0, "lon": 8.0}],
    }
    assert positions[1]["name"] == "SO-50"
    assert positions[1]["path"] is None


def test_satellite_positions_empty_with_no_map_layers_state():
    dashboard = FakeDashboard()
    dashboard._satellite_positions_cache = [{"name": "AO-7", "lat": 1.0, "lon": 2.0}]
    positions = satellite_positions(dashboard, None)
    assert positions == []


def test_dashboard_snapshot_counts_spots():
    dashboard = FakeDashboard(pota=[{"a": 1}, {"a": 2}], pskreporter=[{"a": 1}])
    snapshot = dashboard_snapshot(dashboard)
    assert snapshot["spots"] == {"pota": 2, "pskreporter": 1}


def test_find_radio_window_matches_by_remote_id():
    window1 = FakeWindow(1, "IC-7300")
    window2 = FakeWindow(2, "IC-9700")
    dashboard = FakeDashboard(radios=[window1, window2])
    assert find_radio_window(dashboard, 2) is window2
    assert find_radio_window(dashboard, 99) is None


def test_radio_snapshot_includes_label_and_id():
    window = FakeWindow(5, "IC-705", freq_hz=7074000, mode="DATA")
    snapshot = radio_snapshot(window)
    assert snapshot["id"] == 5
    assert snapshot["label"] == "IC-705"
    assert snapshot["freq_hz"] == 7074000
    assert snapshot["mode"] == "DATA"


def test_radio_snapshot_reads_is_dual_receiver_live_from_worker():
    # Read fresh from window.worker on every snapshot, NOT cached at
    # RadioRemoteState construction time -- RadioWorker only finalizes
    # is_dual_receiver once the connection actually completes, which
    # can race a one-time read/cache taken right after the worker
    # thread is merely *started* (see radio_snapshot's own comment).
    window = FakeWindow(9, "IC-9700", is_dual_receiver=False)
    assert radio_snapshot(window)["is_dual_receiver"] is False

    window.worker.is_dual_receiver = True
    assert radio_snapshot(window)["is_dual_receiver"] is True


def test_radio_snapshot_includes_bands_for_known_radio_model():
    window = FakeWindow(3, "IC-9700")
    bands = radio_snapshot(window)["bands"]
    assert {"label": "2m", "low_hz": 144_000_000, "high_hz": 148_000_000} in bands
    assert {"label": "70cm", "low_hz": 430_000_000, "high_hz": 450_000_000} in bands


def test_radio_snapshot_bands_empty_for_unknown_radio_model():
    window = FakeWindow(4, "Some Unknown Radio")
    assert radio_snapshot(window)["bands"] == []


def test_ws_dashboard_rejects_missing_token():
    # Must accept() before close(code=...) -- a close BEFORE accept
    # sends an HTTP-level rejection per the ASGI/WebSocket spec, not a
    # real close frame, so a real browser's onclose never actually
    # sees the custom code (confirmed live: it showed up as an opaque
    # 1006, not 4401) -- exactly why a real user's dashboard silently
    # retried forever with an empty token instead of showing the
    # token-entry banner. Asserting the actual code here is what would
    # have caught that before it shipped.
    app = create_app(FakeDashboard(), operator_token="secret")
    client = TestClient(app)
    with client.websocket_connect("/ws/dashboard") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 4401


def test_ws_dashboard_rejects_wrong_token():
    app = create_app(FakeDashboard(), operator_token="secret")
    client = TestClient(app)
    with client.websocket_connect("/ws/dashboard?token=nope") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 4401


def test_ws_dashboard_accepts_correct_token_and_streams_snapshot():
    app = create_app(FakeDashboard(), operator_token="secret")
    client = TestClient(app)
    with client.websocket_connect("/ws/dashboard?token=secret") as ws:
        message = ws.receive_json()
        assert message["radios"] == []


def test_ws_dashboard_allows_any_token_when_none_configured():
    app = create_app(FakeDashboard(), operator_token=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/dashboard") as ws:
        message = ws.receive_json()
        assert "radios" in message


def test_ws_radio_closes_for_unknown_id():
    app = create_app(FakeDashboard(), operator_token=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/999") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 4404


def test_dashboard_page_and_radio_page_serve_html():
    app = create_app(FakeDashboard(), operator_token=None)
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/radio/1").status_code == 200


def test_static_assets_are_never_cached():
    """Guards against the exact bug this was added to fix: a browser
    silently serving a stale cached dashboard.js after the app updates
    on disk, making new HTML markup (new buttons, a new dropdown) look
    completely non-functional even after a plain page reload."""
    app = create_app(FakeDashboard(), operator_token=None)
    client = TestClient(app)
    response = client.get("/static/dashboard.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_html_page_routes_are_never_cached():
    """The narrower version of this guard (only /static/* assets) let
    a real bug through: the page routes themselves (dashboard/radio/
    cw/aprs) had NO Cache-Control at all, confirmed live via a plain
    curl against a real running instance -- a browser could still
    cache a stale PAGE even with its assets already protected, which
    is exactly what made a just-added feature (number-stepper arrows)
    invisible on a real user's machine after updating."""
    window = FakeWindow(1, "IC-7300")
    app = create_app(FakeDashboard(radios=[window]), operator_token=None)
    client = TestClient(app)
    for path in ["/", "/radio/1", "/radio/1/tool/cw", "/radio/1/tool/aprs"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-store", path


class FakeWorker:
    """Real QObject with the subset of RadioWorker's signals
    RadioRemoteState connects to -- exercises the actual Qt signal/slot
    wiring, not a mock of it."""
    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class _Worker(QObject):
            frequency_updated = Signal(int)
            control_updated = Signal(str, object)
            meter_updated = Signal(str, int)
            meters_ready = Signal(dict)
            scope_frame_received = Signal(object)
            active_receiver_changed = Signal(int)
            level_updated = Signal(str, float)

        self._obj = _Worker()

    def __getattr__(self, name):
        return getattr(self._obj, name)


def _make_qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_radio_remote_state_caches_frequency_and_control_updates():
    _make_qapp()
    from PySide6.QtWidgets import QPushButton
    from web_remote.bridge import RadioRemoteState

    class FakeWindow:
        _current_freq_hz = None
        worker = FakeWorker()
        ptt_button = QPushButton()

    window = FakeWindow()
    remote_state = RadioRemoteState(window)

    window.worker.frequency_updated.emit(14074000)
    window.worker.control_updated.emit("mode", "USB")
    window.worker.control_updated.emit("vfo", "B")
    window.worker.meter_updated.emit("s_meter", 42)
    window.worker.active_receiver_changed.emit(1)

    assert remote_state.state["freq_hz"] == 14074000
    assert remote_state.state["mode"] == "USB"
    assert remote_state.state["vfo"] == "B"
    assert remote_state.state["meters"]["s_meter"] == 42
    assert remote_state.state["active_receiver"] == 1


def test_radio_remote_state_request_active_receiver_calls_worker():
    _make_qapp()
    from PySide6.QtWidgets import QPushButton
    from web_remote.bridge import RadioRemoteState

    calls = []

    class FakeWorkerWithReceiver(FakeWorker):
        def select_receiver(self, receiver):
            calls.append(("select", receiver))

        def set_scope_receiver(self, receiver):
            calls.append(("scope", receiver))

    class FakeWindow:
        _current_freq_hz = None
        worker = FakeWorkerWithReceiver()
        ptt_button = QPushButton()

    window = FakeWindow()
    remote_state = RadioRemoteState(window)

    remote_state.request_active_receiver(1)
    assert calls == [("select", 1), ("scope", 1)]


def test_radio_remote_state_caches_level_updates():
    _make_qapp()
    from PySide6.QtWidgets import QPushButton
    from web_remote.bridge import RadioRemoteState

    class FakeWindow:
        _current_freq_hz = None
        worker = FakeWorker()
        ptt_button = QPushButton()

    window = FakeWindow()
    remote_state = RadioRemoteState(window)

    window.worker.level_updated.emit("squelch", 0.25)
    window.worker.level_updated.emit("tx_level", 1.0)

    assert remote_state.state["levels"]["squelch"] == 0.25
    assert remote_state.state["levels"]["tx_level"] == 1.0


def test_radio_remote_state_request_level_calls_worker():
    _make_qapp()
    from PySide6.QtWidgets import QPushButton
    from web_remote.bridge import RadioRemoteState

    calls = []

    class FakeWorkerWithLevel(FakeWorker):
        def set_level_value(self, key, value):
            calls.append((key, value))

    class FakeWindow:
        _current_freq_hz = None
        worker = FakeWorkerWithLevel()
        ptt_button = QPushButton()

    window = FakeWindow()
    remote_state = RadioRemoteState(window)

    remote_state.request_level("rf_level", 0.5)
    assert calls == [("rf_level", 0.5)]


def test_radio_remote_state_request_select_band_calls_worker():
    _make_qapp()
    from PySide6.QtWidgets import QPushButton
    from web_remote.bridge import RadioRemoteState

    calls = []

    class FakeWorkerWithBand(FakeWorker):
        def select_band(self, band_label, low_edge_hz, receiver):
            calls.append((band_label, low_edge_hz, receiver))

    class FakeWindow:
        _current_freq_hz = None
        worker = FakeWorkerWithBand()
        ptt_button = QPushButton()

    window = FakeWindow()
    remote_state = RadioRemoteState(window)

    remote_state.request_select_band("20m", 14000000, 1)
    assert calls == [("20m", 14000000, 1)]


def test_radio_remote_state_request_tx_audio_stream_calls_worker():
    _make_qapp()
    from PySide6.QtWidgets import QPushButton
    from web_remote.bridge import RadioRemoteState

    calls = []

    class FakeWorkerWithTxStream(FakeWorker):
        def start_tx_audio_stream(self):
            calls.append(("start",))

        def push_tx_audio_pcm(self, pcm_bytes):
            calls.append(("push", pcm_bytes))

        def stop_tx_audio_stream(self):
            calls.append(("stop",))

    class FakeWindow:
        _current_freq_hz = None
        worker = FakeWorkerWithTxStream()
        ptt_button = QPushButton()

    window = FakeWindow()
    remote_state = RadioRemoteState(window)

    remote_state.request_start_tx_audio_stream()
    remote_state.request_push_tx_audio(b"\x00\x01")
    remote_state.request_stop_tx_audio_stream()
    assert calls == [("start",), ("push", b"\x00\x01"), ("stop",)]


def test_radio_remote_state_request_ptt_updates_widget_and_calls_worker():
    _make_qapp()
    from PySide6.QtWidgets import QPushButton
    from web_remote.bridge import RadioRemoteState

    calls = []

    class FakeWorkerWithPtt(FakeWorker):
        def start_ptt(self):
            calls.append("start")

        def stop_ptt(self):
            calls.append("stop")

    class FakeWindow:
        _current_freq_hz = None
        worker = FakeWorkerWithPtt()
        ptt_button = QPushButton()
        ptt_button.setCheckable(True)

    window = FakeWindow()
    remote_state = RadioRemoteState(window)

    remote_state.request_ptt(True)
    assert calls == ["start"]
    assert remote_state.state["ptt"] is True
    assert window.ptt_button.isChecked() is True

    remote_state.request_ptt(False)
    assert calls == ["start", "stop"]
    assert window.ptt_button.isChecked() is False


def test_aprs_remote_state_strips_non_json_safe_info_raw_from_cached_packets():
    # Regression test: AprsRemoteState._on_packet used to cache the
    # full decoded packet dict, including info_raw -- the raw
    # UNDECODED bytes of the info field (aprs.py's AprsDecoder.feed).
    # bytes aren't JSON-serializable, so routes_tools.py's ws_aprs
    # polling loop (websocket.send_json) crashed on every snapshot
    # after the first packet ever arrived -- the actual cause of a
    # real "opens then immediately disconnects/retries" report.
    _make_qapp()
    import json
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QPushButton, QComboBox
    from web_remote.bridge import AprsRemoteState

    class _W(QObject):
        packet_decoded = Signal(dict)

    class FakeAprsWindow:
        def __init__(self):
            self._obj = _W()
            self.decode_toggle_button = QPushButton()
            self.decode_toggle_button.setCheckable(True)
            self.backend_combo = QComboBox()
            self.backend_combo.addItem("Direwolf")

        def __getattr__(self, name):
            return getattr(self._obj, name)

    window = FakeAprsWindow()
    remote_state = AprsRemoteState(window)

    packet = {
        "source": "N0CALL", "destination": "APRS", "destination_raw": "APRS  ",
        "digipeaters": [], "info": {"type": "position", "lat": 38.0, "lon": -97.0},
        "info_raw": b"!3800.00N/09700.00W-test",
    }
    window.packet_decoded.emit(packet)

    cached = remote_state.state["packets"][0]
    assert "info_raw" not in cached
    assert cached["source"] == "N0CALL"
    json.dumps(remote_state.state)  # must not raise TypeError

    # The desktop table's own copy (a DIFFERENT dict, per aprs_window.py's
    # QTableWidgetItem.setData(Qt.UserRole, packet)) must be untouched --
    # confirms _on_packet copies rather than mutating the shared object.
    assert packet["info_raw"] == b"!3800.00N/09700.00W-test"
