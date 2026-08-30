"""
Tests for web_remote/routes_satellite.py -- satellite picker + start/
stop/transponder/offset control. Patches satellite_tracking.
load_satellite_data (a real file read) with fixed test data, and uses
a fake SatelliteRemoteState double (not a real one -- that needs a
real SatelliteSession/QTimer) to verify the route layer calls the
right request_* method with the right arguments.
"""

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi.testclient import TestClient

from web_remote.app import create_app

FAKE_SATELLITES = [
    {
        "name": "AO-91",
        "line1": "1 43017U ...",
        "line2": "2 43017 ...",
        "transponders": [
            {"description": "FM Voice", "uplink_mhz": 435.25, "downlink_mhz": 145.96, "mode": "FM"},
        ],
    },
    {
        "name": "SO-50",
        "line1": "1 27607U ...",
        "line2": "2 27607 ...",
        "transponders": [],
    },
]


class FakeSatelliteRemoteState:
    def __init__(self):
        self.state = {"satellite_name": None, "tracking": False}
        self.calls = []

    def request_start(self, satellite):
        self.calls.append(("start", satellite))

    def request_stop(self):
        self.calls.append(("stop",))

    def request_transponder(self, index):
        self.calls.append(("transponder", index))

    def request_offset(self, delta_hz):
        self.calls.append(("offset", delta_hz))


class FakeDashboard:
    def __init__(self):
        self._connected_radios = []
        self._upcoming_passes = []
        self._pota_spots_cache = []
        self._pskreporter_spots_cache = []
        self.satellite_remote_state = FakeSatelliteRemoteState()


def make_client(token=None):
    dashboard = FakeDashboard()
    app = create_app(dashboard, token=token)
    return TestClient(app), dashboard


@patch("web_remote.routes_satellite.satellite_tracking.load_satellite_data", return_value=FAKE_SATELLITES)
def test_list_satellites(mock_load):
    client, _ = make_client()
    response = client.get("/api/satellites")
    assert response.status_code == 200
    body = response.json()
    assert body[0]["name"] == "AO-91"
    assert body[0]["transponders"][0]["mode"] == "FM"
    assert "line1" not in body[0]  # raw TLE not exposed to the browser


@patch("web_remote.routes_satellite.satellite_tracking.load_satellite_data", return_value=FAKE_SATELLITES)
def test_start_satellite_by_name(mock_load):
    client, dashboard = make_client()
    response = client.post("/api/satellite/start", json={"name": "AO-91"})
    assert response.status_code == 200
    calls = dashboard.satellite_remote_state.calls
    assert calls[0][0] == "start"
    assert calls[0][1]["name"] == "AO-91"


@patch("web_remote.routes_satellite.satellite_tracking.load_satellite_data", return_value=FAKE_SATELLITES)
def test_start_unknown_satellite_404s(mock_load):
    client, _ = make_client()
    response = client.post("/api/satellite/start", json={"name": "NOT-A-SAT"})
    assert response.status_code == 404


@patch("web_remote.routes_satellite.satellite_tracking.ground_track_points",
       return_value=[(1.0, 2.0), (3.0, 4.0)])
@patch("web_remote.routes_satellite.satellite_tracking.load_satellite_data", return_value=FAKE_SATELLITES)
def test_ground_track(mock_load, mock_track):
    client, _ = make_client()
    response = client.get("/api/satellites/AO-91/ground_track")
    assert response.status_code == 200
    body = response.json()
    assert body["points"] == [{"lat": 1.0, "lon": 2.0}, {"lat": 3.0, "lon": 4.0}]
    assert body["current"] == {"lat": 1.0, "lon": 2.0}


@patch("web_remote.routes_satellite.satellite_tracking.load_satellite_data", return_value=FAKE_SATELLITES)
def test_ground_track_unknown_satellite_404s(mock_load):
    client, _ = make_client()
    response = client.get("/api/satellites/NOT-A-SAT/ground_track")
    assert response.status_code == 404


def test_stop_satellite():
    client, dashboard = make_client()
    response = client.post("/api/satellite/stop")
    assert response.status_code == 200
    assert dashboard.satellite_remote_state.calls == [("stop",)]


@patch("web_remote.routes_satellite.satellite_tracking.load_satellite_data", return_value=FAKE_SATELLITES)
def test_set_transponder_by_index(mock_load):
    client, dashboard = make_client()
    dashboard.satellite_remote_state.state["satellite_name"] = "AO-91"
    response = client.post("/api/satellite/transponder", json={"index": 0})
    assert response.status_code == 200
    assert dashboard.satellite_remote_state.calls[0] == ("transponder", 0)


def test_set_transponder_with_no_satellite_selected_errors():
    client, dashboard = make_client()
    response = client.post("/api/satellite/transponder", json={"index": 0})
    assert response.status_code == 400


def test_adjust_offset():
    client, dashboard = make_client()
    response = client.post("/api/satellite/offset", json={"delta_hz": 500})
    assert response.status_code == 200
    assert dashboard.satellite_remote_state.calls == [("offset", 500)]


def test_rejects_missing_token():
    client, _ = make_client(token="secret")
    response = client.post("/api/satellite/stop")
    assert response.status_code == 401


def test_accepts_correct_bearer_token():
    client, dashboard = make_client(token="secret")
    response = client.post("/api/satellite/stop", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert dashboard.satellite_remote_state.calls == [("stop",)]
