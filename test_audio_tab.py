"""The audio hub (every radio's one RX stream feeds all consumers, the monitor is
optional and adjustable) and the Audio tab that controls it."""

import asyncio
import os
import queue
import types
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

import audio
import audio_tab
from audio import AudioBridge
from constants import AUDIO_DEVICE_SYSTEM_DEFAULT


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, app):
    QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope, str(tmp_path / "qsettings"))
    monkeypatch.setattr(audio_tab, "_SETTINGS_ORG", f"TorcaAudioTest{uuid.uuid4().hex}")


class FakeRadio:
    audio_sample_rate = 8000
    audio_codec = None

    def __init__(self):
        self.rx_callback = None

    async def start_rx(self, callback):
        self.rx_callback = callback

    async def stop_rx(self):
        self.rx_callback = None


def make_bridge(output_device=None):
    return AudioBridge(FakeRadio(), None, None, output_device, status_callback=lambda m: None)


def packet(samples):
    return types.SimpleNamespace(data=np.array(samples, dtype="<i2").tobytes())


def test_a_bridge_with_no_devices_still_claims_the_radio_and_feeds_every_consumer():
    bridge = make_bridge(output_device=None)
    asyncio.run(bridge.start())
    assert bridge.has_rx_stream() and bridge.radio.rx_callback is not None
    got_a, got_b = [], []
    bridge.add_extra_rx_callback(got_a.append)
    bridge.add_extra_rx_callback(got_b.append)
    bridge.radio.rx_callback(packet([1, 2, 3, 4]))
    assert len(got_a) == len(got_b) == 1                       # two decoders share the one stream
    assert bridge._rx_queue.empty()                            # and nothing is queued for a monitor that is not there


def test_a_failing_consumer_does_not_starve_the_others():
    bridge = make_bridge()
    asyncio.run(bridge.start())
    seen = []
    bridge.add_extra_rx_callback(lambda data: 1 / 0)
    bridge.add_extra_rx_callback(seen.append)
    bridge.radio.rx_callback(packet([5, 6]))
    assert len(seen) == 1


def test_level_meter_reports_the_peak_and_resets_on_read():
    bridge = make_bridge()
    asyncio.run(bridge.start())
    bridge.radio.rx_callback(packet([0, 16384, -8000]))
    assert abs(bridge.read_level() - 0.5) < 0.01
    assert bridge.read_level() == 0.0
    status = bridge.rx_status()
    assert status["receiving"] and status["chunks"] == 1 and status["consumers"] == 0 and status["monitor_supported"]


def test_monitor_gain_and_mute_change_playback_but_not_what_decoders_get():
    bridge = make_bridge()
    pcm = np.array([1000, -2000, 3000], dtype="<i2").tobytes()
    assert bridge._apply_monitor_gain(pcm) == pcm
    bridge.set_monitor(volume=0.5)
    assert np.frombuffer(bridge._apply_monitor_gain(pcm), dtype="<i2").tolist() == [500, -1000, 1500]
    bridge.set_monitor(muted=True)
    assert bridge._apply_monitor_gain(pcm) == b"\x00" * len(pcm)
    asyncio.run(bridge.start())
    got = []
    bridge.add_extra_rx_callback(got.append)
    bridge.radio.rx_callback(packet([1000, -2000, 3000]))
    assert got == [pcm]                                        # the decoder still gets the untouched audio


def test_output_device_can_be_switched_and_silenced_at_runtime(monkeypatch):
    opened = []

    class FakeStream:
        def __init__(self, **kwargs):
            opened.append(kwargs["device"])
            self.closed = False
        def start(self): pass
        def stop(self): pass
        def close(self): self.closed = True

    monkeypatch.setattr(audio, "SOUNDDEVICE_AVAILABLE", True)
    monkeypatch.setattr(audio, "sd", types.SimpleNamespace(RawOutputStream=FakeStream), raising=False)
    bridge = make_bridge(output_device=AUDIO_DEVICE_SYSTEM_DEFAULT)
    asyncio.run(bridge.start())
    first = bridge._out_stream
    assert opened == [None] and bridge.rx_status()["monitor_open"]
    assert bridge.set_output_device(7) and opened == [None, 7] and first.closed
    assert bridge.set_output_device(None) and bridge._out_stream is None and not bridge.rx_status()["monitor_open"]
    bridge.radio.rx_callback(packet([1, 2]))                   # silent monitor: still receives, queues nothing
    assert bridge._rx_queue.empty()


# ---- the tab ---------------------------------------------------------------------------

class FakeWorker:
    def __init__(self, bridge):
        self.audio_bridge = bridge
        self.outputs = []

    def audio_hub_status(self):
        return self.audio_bridge.rx_status() if self.audio_bridge else None

    def audio_level(self):
        return self.audio_bridge.read_level() if self.audio_bridge else 0.0

    def set_monitor(self, volume=None, muted=None):
        self.audio_bridge.set_monitor(volume=volume, muted=muted)

    def set_monitor_output(self, device):
        self.outputs.append(device)
        return True


class FakeWindow:
    def __init__(self, title, host, bridge=None):
        self._details = {"radio_model": title, "connection_type": "network", "host": host, "port": 1}
        self._title = title
        self.worker = FakeWorker(bridge)

    def windowTitle(self):
        return self._title


def test_tab_lists_each_radio_with_its_level_and_what_feeds_on_it(app, monkeypatch):
    monkeypatch.setattr(audio_tab, "output_devices", lambda: [("Speakers", 3), ("USB Audio", 5)])
    bridge = make_bridge(); asyncio.run(bridge.start())
    bridge.add_extra_rx_callback(lambda d: None)
    bridge.radio.rx_callback(packet([0, 20000]))
    dash = types.SimpleNamespace(_connected_radios=[FakeWindow("IC-9700", "10.0.0.1", bridge), FakeWindow("IC-7300", "tty", None)])
    tab = audio_tab.AudioTab(dash)
    tab.refresh()
    rows = list(tab._rows.values())
    assert len(rows) == 2 and not tab.empty_label.isVisibleTo(tab)
    assert rows[0].meter.value() > 500 and "feeding 1 tool" in rows[0].status.text()
    assert rows[1].status.text() == "No audio yet" and not rows[1].output.isEnabled()
    assert [rows[0].output.itemText(i) for i in range(rows[0].output.count())] == [
        "System default", "Silent (decoders only)", "Speakers", "USB Audio"]
    dash._connected_radios.pop()                                  # a radio goes away: its row goes too
    tab.refresh()
    assert len(tab._rows) == 1


def test_choices_reach_the_radio_and_are_remembered(app, monkeypatch):
    monkeypatch.setattr(audio_tab, "output_devices", lambda: [("Speakers", 3), ("USB Audio", 5)])
    bridge = make_bridge(); asyncio.run(bridge.start())
    window = FakeWindow("IC-9700", "10.0.0.1", bridge)
    tab = audio_tab.AudioTab(types.SimpleNamespace(_connected_radios=[window]))
    tab.refresh()
    row = tab._rows[window]
    row.output.setCurrentIndex(row.output.findData("USB Audio")); row._on_output_chosen(0)
    row.output.setCurrentIndex(row.output.findData(audio_tab._SILENT)); row._on_output_chosen(0)
    row.volume.setValue(40)
    row.mute.setChecked(True)
    assert window.worker.outputs == [5, None]
    assert abs(bridge._monitor_gain - 0.4) < 1e-6 and bridge._monitor_muted

    # the same radio, reconnected later: the choices come back
    bridge2 = make_bridge(); asyncio.run(bridge2.start())
    window2 = FakeWindow("IC-9700", "10.0.0.1", bridge2)
    tab2 = audio_tab.AudioTab(types.SimpleNamespace(_connected_radios=[window2]))
    tab2.refresh()
    assert window2.worker.outputs == [None]
    assert abs(bridge2._monitor_gain - 0.4) < 1e-6 and bridge2._monitor_muted
    assert tab2._rows[window2].output.currentData() == audio_tab._SILENT


def test_silence_is_called_out(app, monkeypatch):
    monkeypatch.setattr(audio_tab, "output_devices", lambda: [])
    bridge = make_bridge(); asyncio.run(bridge.start())
    bridge._last_audio_monotonic = __import__("time").monotonic() - 10
    window = FakeWindow("IC-7300", "tty", bridge)
    tab = audio_tab.AudioTab(types.SimpleNamespace(_connected_radios=[window]))
    tab.refresh()
    assert "no audio for 10 s" in tab._rows[window].status.text()
