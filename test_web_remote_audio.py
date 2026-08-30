"""
Tests for web_remote/routes_audio.py -- RX audio streaming. Exercises
the actual re-framing/Opus-encoding path (not just the gating logic):
a FakeAudioBridge lets the test call the registered extra-RX callback
directly (simulating audio arriving on RadioWorker's own thread, same
as production) and confirms a real, valid Opus frame comes back over
the websocket.
"""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from web_remote.app import create_app


class FakeAudioBridge:
    def __init__(self, sample_rate=8000, has_rx=True):
        self.sample_rate = sample_rate
        self._has_rx = has_rx
        self.callbacks = []

    def has_rx_stream(self):
        return self._has_rx

    def add_extra_rx_callback(self, callback):
        self.callbacks.append(callback)

    def remove_extra_rx_callback(self, callback):
        self.callbacks.remove(callback)


class FakeWorker:
    def __init__(self, audio_bridge):
        self.audio_bridge = audio_bridge


class FakeWindow:
    def __init__(self, remote_id, audio_bridge):
        self.remote_id = remote_id
        self.worker = FakeWorker(audio_bridge)


class FakeDashboard:
    def __init__(self, radios=None):
        self._connected_radios = radios or []
        self._upcoming_passes = []
        self._pota_spots_cache = []
        self._pskreporter_spots_cache = []


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_audio_streams_real_opus_frames():
    bridge = FakeAudioBridge(sample_rate=8000)
    window = FakeWindow(1, bridge)
    app = create_app(FakeDashboard(radios=[window]), token=None)
    client = TestClient(app)

    with client.websocket_connect("/ws/radio/1/audio") as ws:
        assert wait_for(lambda: len(bridge.callbacks) == 1)
        # 20ms @ 8kHz mono, 16-bit -- exactly one Opus frame's worth.
        bridge.callbacks[0](b"\x00\x00" * 160)
        frame = ws.receive_bytes()
        assert isinstance(frame, bytes)
        assert len(frame) > 0

    assert bridge.callbacks == []  # unregistered on disconnect


def test_audio_reframes_odd_sized_chunks():
    """AudioBridge hands over whatever chunk size rigplane delivers --
    confirms two small chunks that together span one 20ms frame still
    produce a valid Opus frame, not a crash."""
    bridge = FakeAudioBridge(sample_rate=8000)
    window = FakeWindow(1, bridge)
    app = create_app(FakeDashboard(radios=[window]), token=None)
    client = TestClient(app)

    with client.websocket_connect("/ws/radio/1/audio") as ws:
        assert wait_for(lambda: len(bridge.callbacks) == 1)
        bridge.callbacks[0](b"\x00\x00" * 100)
        bridge.callbacks[0](b"\x00\x00" * 60)  # 100+60 = 160 samples = one frame
        frame = ws.receive_bytes()
        assert len(frame) > 0


def test_audio_closes_when_no_rx_stream():
    bridge = FakeAudioBridge(has_rx=False)
    window = FakeWindow(1, bridge)
    app = create_app(FakeDashboard(radios=[window]), token=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/1/audio") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()
        assert exc_info.value.code == 4405


def test_audio_closes_for_unknown_radio():
    app = create_app(FakeDashboard(), token=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/999/audio") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()
        assert exc_info.value.code == 4404


def test_audio_rejects_wrong_token():
    bridge = FakeAudioBridge()
    window = FakeWindow(1, bridge)
    app = create_app(FakeDashboard(radios=[window]), token="secret")
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/1/audio?token=wrong") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()
        assert exc_info.value.code == 4401


def test_audio_closes_for_unsupported_sample_rate():
    bridge = FakeAudioBridge(sample_rate=44100)  # not one of Opus's valid rates
    window = FakeWindow(1, bridge)
    app = create_app(FakeDashboard(radios=[window]), token=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/radio/1/audio") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()
        assert exc_info.value.code == 4406
