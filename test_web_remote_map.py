"""
Tests for web_remote/routes_map.py -- the map-overlay toggle row
(Satellites/QSO Map/PSKReporter/POTA/APRS + band filter). Exercises
the route layer against a fake MapLayersRemoteState double (a real one
needs a real HamClockWindow with all its buttons/combo); confirms each
endpoint calls the right request_* method with the right argument, and
that auth gating matches every other REST route in this app.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi.testclient import TestClient

from web_remote.app import create_app


class FakeMapLayersRemoteState:
    def __init__(self):
        self.calls = []

    def request_satellites(self, on):
        self.calls.append(("satellites", on))

    def request_qsos(self, on):
        self.calls.append(("qsos", on))

    def request_pskreporter(self, on):
        self.calls.append(("pskreporter", on))

    def request_pota(self, on):
        self.calls.append(("pota", on))

    def request_aprs(self, on):
        self.calls.append(("aprs", on))

    def request_band_filter(self, band):
        self.calls.append(("band_filter", band))


class FakeDashboard:
    def __init__(self):
        self._connected_radios = []
        self._upcoming_passes = []
        self._pota_spots_cache = []
        self._pskreporter_spots_cache = []
        self.map_layers_remote_state = FakeMapLayersRemoteState()


def make_client(token=None):
    dashboard = FakeDashboard()
    app = create_app(dashboard, operator_token=token)
    return TestClient(app), dashboard


def test_toggle_satellites():
    client, dashboard = make_client()
    response = client.post("/api/map/satellites", json={"on": True})
    assert response.status_code == 200
    assert dashboard.map_layers_remote_state.calls == [("satellites", True)]


def test_toggle_qsos_off():
    client, dashboard = make_client()
    response = client.post("/api/map/qsos", json={"on": False})
    assert response.status_code == 200
    assert dashboard.map_layers_remote_state.calls == [("qsos", False)]


def test_toggle_pskreporter():
    client, dashboard = make_client()
    client.post("/api/map/pskreporter", json={"on": True})
    assert dashboard.map_layers_remote_state.calls == [("pskreporter", True)]


def test_toggle_pota():
    client, dashboard = make_client()
    client.post("/api/map/pota", json={"on": True})
    assert dashboard.map_layers_remote_state.calls == [("pota", True)]


def test_toggle_aprs():
    client, dashboard = make_client()
    client.post("/api/map/aprs", json={"on": True})
    assert dashboard.map_layers_remote_state.calls == [("aprs", True)]


def test_set_band_filter():
    client, dashboard = make_client()
    response = client.post("/api/map/band_filter", json={"band": "20m"})
    assert response.status_code == 200
    assert dashboard.map_layers_remote_state.calls == [("band_filter", "20m")]


def test_set_band_filter_all_bands_is_null():
    client, dashboard = make_client()
    client.post("/api/map/band_filter", json={"band": None})
    assert dashboard.map_layers_remote_state.calls == [("band_filter", None)]


def test_rejects_missing_token():
    client, _ = make_client(token="secret")
    response = client.post("/api/map/pota", json={"on": True})
    assert response.status_code == 401


def test_accepts_correct_bearer_token():
    client, dashboard = make_client(token="secret")
    response = client.post("/api/map/pota", json={"on": True}, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert dashboard.map_layers_remote_state.calls == [("pota", True)]
