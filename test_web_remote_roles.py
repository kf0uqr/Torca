"""
Tests for the FCC Part 97 compliance features: role resolution
(web_remote/common.py), supervision gating + kill switch + auto-ID
(web_remote/bridge.py's RadioRemoteState), and the end-to-end websocket
gating in web_remote/app.py. Uses the REAL RadioRemoteState (not a
fake) for the app.py-level tests -- these are exactly the code paths
that have to be right for compliance, so exercising the real
can_transmit()/register_session()/kill_tx() through the actual command
dispatch matters more here than in most of this app's other tests.
"""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi.testclient import TestClient

from web_remote.app import create_app
from web_remote.audit import AuditLog
from web_remote.common import ROLE_GUEST, ROLE_OPERATOR, ROLE_VIEWER, make_role_checker


# ---- common.py: role resolution ----

def test_role_for_resolves_each_configured_token():
    role_for = make_role_checker(operator_token="op", guest_token="gu", viewer_token="vw")
    assert role_for("op") == ROLE_OPERATOR
    assert role_for("gu") == ROLE_GUEST
    assert role_for("vw") == ROLE_VIEWER
    assert role_for("nope") is None
    assert role_for(None) is None


def test_role_for_wide_open_when_nothing_configured():
    # Matches the old single-token make_token_check's "no token
    # configured means anything passes" convenience -- only relevant to
    # a dev/test setup with no tokens at all; ham_dashboard.py always
    # generates all three in real use.
    role_for = make_role_checker()
    assert role_for(None) == ROLE_OPERATOR
    assert role_for("anything") == ROLE_OPERATOR


def test_role_for_unreachable_when_own_token_unset():
    role_for = make_role_checker(operator_token="op")
    assert role_for(None) is None
    assert role_for("") is None


# ---- bridge.py: RadioRemoteState supervision/kill-switch/auto-ID ----

def _make_qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class _FakeWorker:
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
        self.ptt_calls = []
        self.cw_sent = []

    def __getattr__(self, name):
        return getattr(self._obj, name)

    def start_ptt(self):
        self.ptt_calls.append("start")

    def stop_ptt(self):
        self.ptt_calls.append("stop")

    def send_cw_text(self, text):
        self.cw_sent.append(text)


def _make_remote_state():
    _make_qapp()
    from PySide6.QtWidgets import QPushButton
    from web_remote.bridge import RadioRemoteState

    class FakeWindow:
        _current_freq_hz = None
        worker = _FakeWorker()
        ptt_button = QPushButton()

    window = FakeWindow()
    return RadioRemoteState(window), window


def test_can_transmit_operator_always_true_unless_locked():
    remote_state, _ = _make_remote_state()
    assert remote_state.can_transmit(ROLE_OPERATOR) is True
    remote_state.kill_tx()
    assert remote_state.can_transmit(ROLE_OPERATOR) is False


def test_can_transmit_viewer_never():
    remote_state, _ = _make_remote_state()
    assert remote_state.can_transmit(ROLE_VIEWER) is False


def test_can_transmit_guest_requires_operator_session():
    remote_state, _ = _make_remote_state()
    assert remote_state.can_transmit(ROLE_GUEST) is False
    remote_state.register_session(ROLE_OPERATOR)
    assert remote_state.can_transmit(ROLE_GUEST) is True
    remote_state.unregister_session(ROLE_OPERATOR)
    assert remote_state.can_transmit(ROLE_GUEST) is False


def test_can_transmit_guest_blocked_by_lock_even_when_supervised():
    remote_state, _ = _make_remote_state()
    remote_state.register_session(ROLE_OPERATOR)
    assert remote_state.can_transmit(ROLE_GUEST) is True
    remote_state.kill_tx()
    assert remote_state.can_transmit(ROLE_GUEST) is False


def test_kill_tx_stops_ptt_and_clear_tx_lock_restores():
    remote_state, window = _make_remote_state()
    remote_state.request_ptt(True)
    remote_state.kill_tx()
    assert remote_state.state["tx_locked"] is True
    assert remote_state.state["ptt"] is False
    assert window.worker.ptt_calls[-1] == "stop"
    assert remote_state.can_transmit(ROLE_OPERATOR) is False
    remote_state.clear_tx_lock()
    assert remote_state.can_transmit(ROLE_OPERATOR) is True


def test_auto_id_does_not_fire_before_interval_elapses():
    remote_state, window = _make_remote_state()
    remote_state.request_ptt(True)
    remote_state.request_ptt(False)
    assert window.worker.cw_sent == []  # far under AUTO_ID_INTERVAL_SECONDS


def test_auto_id_fires_after_interval_elapses():
    remote_state, window = _make_remote_state()
    remote_state.request_ptt(True)
    remote_state._last_id_monotonic = time.monotonic() - 600  # force "overdue"
    remote_state.request_ptt(False)
    assert len(window.worker.cw_sent) == 1


# ---- app.py: end-to-end websocket gating ----

class FakeWorkerStub:
    def __init__(self, is_dual_receiver=False):
        self.is_dual_receiver = is_dual_receiver


class RealRemoteWindow:
    """A FakeWindow that uses the REAL RadioRemoteState (bridge.py),
    not a double -- these tests are exercising the actual supervision
    gate through app.py's real command dispatch."""
    def __init__(self, remote_id, audit=None):
        from PySide6.QtWidgets import QPushButton
        from web_remote.bridge import RadioRemoteState

        self.remote_id = remote_id
        self._details = {"radio_model": "IC-7300"}
        self._current_freq_hz = 14074000
        self.worker = _FakeWorker()
        self.worker.is_dual_receiver = False
        self.ptt_button = QPushButton()
        self.remote_state = RadioRemoteState(self, audit=audit)


class FakeDashboard:
    def __init__(self, radios=None):
        self._connected_radios = radios or []
        self._upcoming_passes = []
        self._pota_spots_cache = []
        self._pskreporter_spots_cache = []


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_viewer_ptt_is_rejected_and_logged():
    _make_qapp()
    window = RealRemoteWindow(1)
    app = create_app(FakeDashboard(radios=[window]), operator_token="op", guest_token="gu", viewer_token="vw")
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/1?token=vw") as ws:
        ws.receive_json()  # initial snapshot
        ws.send_json({"cmd": "ptt", "on": True})
        error = ws.receive_json()
        assert "error" in error
    assert window.worker.ptt_calls == []


def test_guest_ptt_rejected_without_operator_then_allowed_once_supervised():
    _make_qapp()
    window = RealRemoteWindow(1)
    app = create_app(FakeDashboard(radios=[window]), operator_token="op", guest_token="gu", viewer_token="vw")
    client = TestClient(app)

    with client.websocket_connect("/ws/radio/1?token=gu") as guest_ws:
        guest_ws.receive_json()
        guest_ws.send_json({"cmd": "ptt", "on": True})
        error = guest_ws.receive_json()
        assert "error" in error
        assert window.worker.ptt_calls == []

        with client.websocket_connect("/ws/radio/1?token=op") as operator_ws:
            operator_ws.receive_json()
            assert _wait_for(lambda: window.remote_state.state.get("operator_present") is True)
            guest_ws.send_json({"cmd": "ptt", "on": True})
            assert _wait_for(lambda: "start" in window.worker.ptt_calls)


def test_operator_kill_tx_blocks_further_transmission_until_cleared():
    _make_qapp()
    window = RealRemoteWindow(1)
    app = create_app(FakeDashboard(radios=[window]), operator_token="op", guest_token="gu", viewer_token="vw")
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/1?token=op") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "kill_tx"})
        assert _wait_for(lambda: window.remote_state.state.get("tx_locked") is True)
        ws.send_json({"cmd": "ptt", "on": True})
        error = ws.receive_json()
        assert "error" in error
        assert window.worker.ptt_calls == ["stop"]  # kill_tx's own request_ptt(False), not the blocked one

        ws.send_json({"cmd": "clear_tx_lock"})
        assert _wait_for(lambda: window.remote_state.state.get("tx_locked") is False)
        ws.send_json({"cmd": "ptt", "on": True})
        assert _wait_for(lambda: "start" in window.worker.ptt_calls)


def test_audit_log_records_blocked_and_allowed_ptt():
    _make_qapp()
    audit = AuditLog(None)  # memory-only: no file path, see AuditLog.log's own guard

    window = RealRemoteWindow(1, audit=audit)
    app = create_app(FakeDashboard(radios=[window]), operator_token="op", viewer_token="vw", audit=audit)
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/1?token=vw") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "ptt", "on": True})
        ws.receive_json()
    with client.websocket_connect("/ws/radio/1?token=op") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "ptt", "on": True})

        def has_both():
            cmds = [(e["role"], e["allowed"]) for e in audit.recent(50) if e["cmd"] == "ptt"]
            return (ROLE_VIEWER, False) in cmds and (ROLE_OPERATOR, True) in cmds

        assert _wait_for(has_both)
