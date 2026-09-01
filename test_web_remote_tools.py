"""
Tests for web_remote/routes_tools.py -- the CW/APRS websocket endpoints.
Exercises the ASGI app directly via FastAPI's TestClient against fake
CwRemoteState/AprsRemoteState doubles (not real bridge.py instances --
those need a real CwToolWindow/AprsToolWindow, which need a real
RadioWindow; this only tests the route layer's own logic: token/404
gating, snapshot shaping, and command dispatch to the right
remote_state method).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from web_remote.app import create_app


class FakeCwRemoteState:
    def __init__(self):
        self.state = {"text": "CQ CQ", "decoding": False, "wpm": 20, "pitch": 600}
        self.calls = []

    def macros(self):
        return [{"label": "CQ", "text": "CQ CQ DE TEST TEST K"}]

    def request_decoding(self, on):
        self.calls.append(("decoding", on))

    def request_wpm(self, wpm):
        self.calls.append(("wpm", wpm))

    def request_pitch(self, hz):
        self.calls.append(("pitch", hz))

    def request_send(self, text):
        self.calls.append(("send", text))


class FakeAprsRemoteState:
    def __init__(self):
        self.state = {"packets": [{"source": "N0CALL", "destination": "APRS", "info": {"type": "status", "text": "hi"}}],
                       "decoding": False, "backend_index": 0}
        self.calls = []

    def request_decoding(self, on):
        self.calls.append(("decoding", on))

    def request_backend(self, index):
        self.calls.append(("backend", index))

    def request_send_position(self, comment):
        self.calls.append(("send_position", comment))


class FakeRadioRemoteState:
    """Stand-in for bridge.py's RadioRemoteState -- routes_tools.py
    checks window.remote_state.can_transmit(role) before honoring
    send_text/send_position (the CW/APRS tools share the radio's own
    supervision state, they don't have one of their own)."""
    def __init__(self, can_transmit=True, operator_present=False, tx_locked=False):
        self.state = {"operator_present": operator_present, "tx_locked": tx_locked}
        self._can_transmit = can_transmit

    def can_transmit(self, role):
        return self._can_transmit


class FakeWindow:
    def __init__(self, remote_id, can_transmit=True):
        self.remote_id = remote_id
        self.cw_remote_state = FakeCwRemoteState()
        self.aprs_remote_state = FakeAprsRemoteState()
        self.remote_state = FakeRadioRemoteState(can_transmit=can_transmit)


class FakeDashboard:
    def __init__(self, radios=None):
        self._connected_radios = radios or []
        self._upcoming_passes = []
        self._pota_spots_cache = []
        self._pskreporter_spots_cache = []


def test_ws_cw_streams_snapshot_with_macros():
    window = FakeWindow(1)
    app = create_app(FakeDashboard(radios=[window]), operator_token=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/1/tool/cw") as ws:
        snapshot = ws.receive_json()
        assert snapshot["text"] == "CQ CQ"
        assert snapshot["decoding"] is False
        assert snapshot["macros"] == [{"label": "CQ", "text": "CQ CQ DE TEST TEST K"}]


def test_ws_cw_dispatches_commands():
    window = FakeWindow(1)
    app = create_app(FakeDashboard(radios=[window]), operator_token=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/1/tool/cw") as ws:
        ws.receive_json()  # initial snapshot
        ws.send_json({"cmd": "start_decode"})
        ws.send_json({"cmd": "set_wpm", "wpm": 25})
        ws.send_json({"cmd": "set_pitch", "hz": 700})
        ws.send_json({"cmd": "send_text", "text": "TEST"})

        def wait_for_calls():
            import time
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if len(window.cw_remote_state.calls) >= 4:
                    return
                ws.receive_json()
            raise AssertionError("commands were never processed")

        wait_for_calls()
    assert ("decoding", True) in window.cw_remote_state.calls
    assert ("wpm", 25) in window.cw_remote_state.calls
    assert ("pitch", 700) in window.cw_remote_state.calls
    assert ("send", "TEST") in window.cw_remote_state.calls


def test_ws_cw_closes_for_unknown_radio():
    app = create_app(FakeDashboard(), operator_token=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/999/tool/cw") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 4404


def test_ws_cw_rejects_wrong_token():
    window = FakeWindow(1)
    app = create_app(FakeDashboard(radios=[window]), operator_token="secret")
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/1/tool/cw?token=wrong") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 4401


def test_ws_aprs_streams_packets():
    window = FakeWindow(1)
    app = create_app(FakeDashboard(radios=[window]), operator_token=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/1/tool/aprs") as ws:
        snapshot = ws.receive_json()
        assert snapshot["packets"][0]["source"] == "N0CALL"
        assert snapshot["backend_index"] == 0


def test_ws_aprs_dispatches_commands():
    window = FakeWindow(1)
    app = create_app(FakeDashboard(radios=[window]), operator_token=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/1/tool/aprs") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "start_decode"})
        ws.send_json({"cmd": "set_backend", "index": 1})
        ws.send_json({"cmd": "send_position", "comment": "testing"})

        def wait_for_calls():
            import time
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if len(window.aprs_remote_state.calls) >= 3:
                    return
                ws.receive_json()
            raise AssertionError("commands were never processed")

        wait_for_calls()
    assert ("decoding", True) in window.aprs_remote_state.calls
    assert ("backend", 1) in window.aprs_remote_state.calls
    assert ("send_position", "testing") in window.aprs_remote_state.calls


def test_ws_aprs_closes_for_unknown_radio():
    app = create_app(FakeDashboard(), operator_token=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/999/tool/aprs") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 4404


def test_ws_aprs_survives_non_json_serializable_state():
    # Regression test for the route-layer defense: if something
    # non-JSON-serializable ever slips into remote_state.state again
    # (the real bug was bytes in a cached packet's info_raw field,
    # bridge.py -- fixed at the source, but this covers the class of
    # bug, not just that one instance), the connection should skip
    # that tick and keep polling, not die and send the browser into
    # an endless "disconnected -- retrying" loop.
    window = FakeWindow(1)
    window.aprs_remote_state.state["packets"] = [{"info_raw": b"not json safe"}]
    app = create_app(FakeDashboard(radios=[window]), operator_token=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/1/tool/aprs") as ws:
        window.aprs_remote_state.state["packets"] = [{"source": "N0CALL"}]  # fix it mid-flight
        deadline_snapshot = None
        import time
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                snapshot = ws.receive_json()
            except Exception:
                continue
            if snapshot["packets"] and snapshot["packets"][0].get("source") == "N0CALL":
                deadline_snapshot = snapshot
                break
        assert deadline_snapshot is not None, "connection died instead of skipping the bad tick"


def test_cw_and_aprs_tool_pages_serve_html():
    window = FakeWindow(1)
    app = create_app(FakeDashboard(radios=[window]), operator_token=None)
    client = TestClient(app)
    assert client.get("/radio/1/tool/cw").status_code == 200
    assert client.get("/radio/1/tool/aprs").status_code == 200
