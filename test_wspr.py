"""WSPR: parsing, slot timing, a REAL round trip through wsprd (skipped where WSJT-X is not
installed), and the tool window."""

import os
import shutil
import time
import types
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

import ft8_decoder as ft8
import wspr
import wspr_window
from wspr_window import WsprToolWindow

needs_wsjtx = pytest.mark.skipif(not (shutil.which("wsprd") and shutil.which("wsprsim")), reason="WSJT-X's wsprd/wsprsim are not installed")
SLOT = 1_800_000_000.0 - (1_800_000_000.0 % 120)      # an even-minute slot boundary


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, app):
    QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope, str(tmp_path / "qsettings"))
    monkeypatch.setattr(wspr_window, "_SETTINGS_ORG", f"TorcaWsprTest{uuid.uuid4().hex}")
    monkeypatch.setattr(wspr, "DATA_DIR", tmp_path / "wspr")


# ---- pure parts ---------------------------------------------------------------------------------

def test_parse_message_forms():
    assert wspr.parse_message("K1ABC FN42 37") == ("K1ABC", "FN42", 37)
    assert wspr.parse_message("PJ4/K1ABC 37") == ("PJ4/K1ABC", "", 37)
    assert wspr.parse_message("<PJ4/K1ABC> FK52UD 33") == ("PJ4/K1ABC", "FK52UD", 33)
    assert wspr.parse_message("") is None


def test_parse_output_reads_wsprd_lines_and_skips_the_rest():
    out = ("0802  -23  0.3  14.097103  0  K1ABC FN42 37\n"
           " 0802 -9 -1.2 14.097210 -2  <...> FK52UD 33\n"
           "<DecodeFinished>\n")
    spots = wspr.parse_output(out, SLOT)
    assert [(s.callsign, s.grid, s.power_dbm, s.snr, s.drift) for s in spots] == [("K1ABC", "FN42", 37, -23, 0)]
    assert spots[0].frequency_mhz == 14.097103 and abs(spots[0].power_watts - 5.0) < 0.05
    assert spots[0].utc == time.strftime("%H:%M", time.gmtime(SLOT))


def test_bands_distance_and_slot_timing():
    assert wspr.nearest_band(14_095_600) == ("20m", 14.0956) and wspr.nearest_band(14_074_000) is None
    assert 6000 < wspr.distance_km("EM18", "JN58") < 9500 and wspr.distance_km("EM18", "") is None
    assert abs(wspr.distance_km("EM18", "EM18")) < 100
    capture = ft8.Ft8SlotCapture("WSPR", 8000, clock=lambda: SLOT + 0.0)
    assert capture.slot_seconds == 120.0 and ft8.EARLY_DECODE_SECONDS["WSPR"] > 111.6      # the signal ends at 111.6 s


# ---- the real decoder ----------------------------------------------------------------------------

def noisy(pcm, sigma=2000, seed=1):
    a = np.frombuffer(pcm, dtype="<i2").astype(float) + np.random.default_rng(seed).normal(0, sigma, len(pcm) // 2)
    return a.clip(-32768, 32767).astype("<i2").tobytes()


@needs_wsjtx
def test_a_real_wspr_signal_is_decoded(tmp_path):
    pcm = noisy(wspr.synthesize(wspr.channel_symbols("K1ABC FN42 37"), audio_hz=1500.0, amplitude=0.3))
    spots = wspr.decode_slot(ft8.CapturedSlot(SLOT, pcm, 1.0), 12000, 14.0956)
    assert [(s.callsign, s.grid, s.power_dbm) for s in spots] == [("K1ABC", "FN42", 37)]
    assert abs(spots[0].frequency_mhz - 14.0971) < 0.0003 and spots[0].slot_start == SLOT


@needs_wsjtx
def test_several_signals_at_once_and_a_resampled_capture():
    mix = None
    for call, hz in (("K1ABC FN42 37", 1450.0), ("W9XYZ EN52 30", 1550.0)):
        sig = np.frombuffer(wspr.synthesize(wspr.channel_symbols(call), audio_hz=hz, amplitude=0.15), dtype="<i2").astype(float)
        mix = sig if mix is None else mix + sig
    pcm12 = noisy(mix.astype("<i2").tobytes())
    pcm8 = ft8.resample(pcm12, 12000, 8000)                       # as a radio at 8 kHz would deliver it
    spots = wspr.decode_slot(ft8.CapturedSlot(SLOT, pcm8, 1.0), 8000, 7.0386)
    assert sorted(s.callsign for s in spots) == ["K1ABC", "W9XYZ"]


@needs_wsjtx
def test_an_empty_or_partial_slot_decodes_to_nothing():
    silent = bytes(2 * 12000 * 120)
    assert wspr.decode_slot(ft8.CapturedSlot(SLOT, silent, 1.0), 12000, 14.0956) == []
    assert wspr.decode_slot(ft8.CapturedSlot(SLOT, silent, 0.3), 12000, 14.0956) == []     # too little of it was captured


# ---- the window ----------------------------------------------------------------------------------

class FakeWorker:
    def __init__(self):
        self.radio = types.SimpleNamespace(audio_sample_rate=8000)
        self.taps, self.tuned, self.fail = [], [], None

    def add_audio_tap(self, cb):
        if self.fail:
            raise RuntimeError(self.fail)
        self.taps.append(cb)

    def remove_audio_tap(self, cb):
        self.taps.remove(cb)

    def set_frequency(self, hz, check_conflict=True):
        self.tuned.append(("freq", hz))

    def set_control_value(self, key, value):
        self.tuned.append((key, value))


class FakeRadio:
    workspace_key = "radio:IC-7300@test"      # a radio window's identity, as the receive-radio box uses it

    def other_radios(self):
        return []

    def display_name(self):
        return "IC-7300"

    def __init__(self, freq=14_095_600):
        self.worker = FakeWorker()
        self._current_freq_hz = freq

    def tool_title(self, name):
        return f"{name} - IC-7300"


class Clock:
    def __init__(self, now): self.now = now
    def __call__(self): return self.now


def make_window(freq=14_095_600):
    radio = FakeRadio(freq)
    return WsprToolWindow(radio, clock=Clock(SLOT)), radio.worker


def wait(app, condition, seconds=30.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_window_basics_and_tuning(app):
    window, worker = make_window()
    assert window.windowTitle() == "WSPR Tool - IC-7300"
    assert [window.band_combo.itemData(i) for i in range(3)] == ["160m", "80m", "60m"]
    window.band_combo.setCurrentIndex(window.band_combo.findData("40m"))
    window._on_tune_clicked()
    assert worker.tuned == [("freq", 7_038_600), ("mode", "USB")]
    window.closing_for_real()


def test_without_wsprd_it_says_what_to_install(app, monkeypatch):
    monkeypatch.setattr(wspr, "is_available", lambda: False)
    window, _ = make_window()
    assert not window.decode_toggle_button.isEnabled() and "sudo apt install wsjtx" in window.status_label.text()
    window.closing_for_real()


def test_start_stop_attach_and_detach_the_audio_and_report_a_missing_radio(app):
    window, worker = make_window()
    window.decode_toggle_button.setChecked(True)
    assert worker.taps == [window._on_audio_frame] and window._capture.mode == "WSPR"
    window.decode_toggle_button.setChecked(False)
    assert worker.taps == [] and window._capture is None
    worker.fail = "not connected yet"
    window.decode_toggle_button.setChecked(True)
    assert not window.decode_toggle_button.isChecked() and "not connected" in window.status_label.text()
    window.closing_for_real()


@needs_wsjtx
def test_audio_flows_through_the_window_into_the_table_and_onto_the_map(app):
    QSettings(wspr_window._SETTINGS_ORG, wspr_window._SETTINGS_APP).setValue("operator_lat", 38.2)
    QSettings(wspr_window._SETTINGS_ORG, wspr_window._SETTINGS_APP).setValue("operator_lon", -97.5)
    window, worker = make_window()
    heard = []
    window.stations_heard.connect(heard.append)
    window.decode_toggle_button.setChecked(True)
    pcm12 = noisy(wspr.synthesize(wspr.channel_symbols("K1ABC FN42 37"), audio_hz=1500.0, amplitude=0.3))
    pcm8 = ft8.resample(pcm12, 12000, 8000)
    clock = window._clock
    step = 8000 * 2                                   # one second of audio at a time
    for offset in range(0, 115 * step, step):         # 115 s of the slot: past the early-decode point
        clock.now = SLOT + (offset + step) / 2 / 8000
        window._on_audio_frame(pcm8[offset:offset + step])
    assert wait(app, lambda: window.table.rowCount() == 1)
    cells = [window.table.item(0, c).text() for c in range(len(wspr_window._COLUMNS))]
    assert cells[1] == "+" + str(int(cells[1])) or cells[1].startswith(("+", "-"))
    assert cells[5:8] == ["K1ABC", "FN42", "37 dBm (5 W)"] and cells[8].isdigit() and 1000 < int(cells[8]) < 3000
    assert heard and heard[0][0]["callsign"] == "K1ABC" and heard[0][0]["mode"] == "WSPR" and heard[0][0]["report"] is False
    assert abs(heard[0][0]["frequency_hz"] - 14_097_100) < 300
    window.decode_toggle_button.setChecked(False)
    window.closing_for_real()


def test_a_stalled_audio_stream_is_called_out(app):
    window, worker = make_window()
    window.decode_toggle_button.setChecked(True)
    window._check_audio_flow(now=time.monotonic() + 30)
    assert "No audio received" in window.audio_warning_label.text() and not window.audio_warning_label.isHidden()
    window._last_audio_monotonic = time.monotonic()
    window._check_audio_flow()
    assert window.audio_warning_label.isHidden()
    window.decode_toggle_button.setChecked(False)
    window.closing_for_real()


def test_closing_stops_decoding_but_keeps_the_spots(app):
    window, worker = make_window()
    window.decode_toggle_button.setChecked(True)
    window._on_decoded(SLOT, [wspr.WsprSpot(SLOT, -20, 0.1, 14.0971, 0, "K1ABC", "FN42", 37, "raw")], True)
    window.show(); window.close()
    assert worker.taps == [] and window.table.rowCount() == 1 and window.isHidden()
    window.closing_for_real()


def test_dashboard_keeps_wspr_spots_off_the_pskreporter_upload():
    import inspect, ham_dashboard
    source = inspect.getsource(ham_dashboard.HamClockWindow._on_stations_heard)
    assert 'station.get("report", True)' in source


# ---- WSPRnet upload -------------------------------------------------------------------------------

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import wsprnet

SPOT = wspr.WsprSpot(SLOT + 120, -23, -0.34, 14.097103, 1, "K1ABC", "FN42", 37, "raw")


class _Server:
    """A stand-in WSPRnet that records what is posted and answers like the real one."""
    def __init__(self, reply=b"1 spot(s) added"):
        outer = self
        outer.posts, outer.reply = [], reply

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                outer.posts.append((self.path, self.headers["Content-Type"], {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}))
                self.send_response(200); self.end_headers(); self.wfile.write(outer.reply)
            def log_message(self, *a): pass
        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/post/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def test_form_fields_match_wsjtxs_wsprnet_interface():
    form = wsprnet.build_form(SPOT, "kf0uqr", "EM18ge", 14.0956)
    assert form == {
        "function": "wspr", "rcall": "KF0UQR", "rgrid": "EM18ge", "rqrg": "14.095600",
        "date": time.strftime("%y%m%d", time.gmtime(SLOT + 120)), "time": time.strftime("%H%M", time.gmtime(SLOT + 120)),
        "sig": "-23", "dt": "-0.3", "drift": "1", "tqrg": "14.097103", "tcall": "K1ABC", "tgrid": "FN42",
        "dbm": "37", "version": wsprnet.VERSION, "mode": "2"}


def test_upload_posts_each_spot_and_counts_the_accepted_ones():
    server = _Server()
    try:
        other = wspr.WsprSpot(SLOT + 120, -9, 0.1, 14.0972, 0, "W9XYZ", "EN52", 30, "raw")
        hashed = wspr.WsprSpot(SLOT + 120, -9, 0.1, 14.0972, 0, "PJ4/K1ABC", "", None, "raw")     # no grid / power: WSPRnet cannot take it
        accepted, problems = wsprnet.upload([SPOT, other, hashed], "KF0UQR", "EM18ge", 14.0956, url=server.url)
    finally:
        server.close()
    assert (accepted, problems) == (2, [])
    assert [p[0] for p in server.posts] == ["/post/", "/post/"] and server.posts[0][1] == "application/x-www-form-urlencoded"
    assert [p[2]["tcall"] for p in server.posts] == ["K1ABC", "W9XYZ"] and server.posts[0][2]["rcall"] == "KF0UQR"


def test_a_refusal_and_a_dead_network_are_reported():
    server = _Server(reply=b"error")
    try:
        accepted, problems = wsprnet.upload([SPOT], "KF0UQR", "EM18ge", 14.0956, url=server.url)
    finally:
        server.close()
    assert accepted == 0 and "not accepted" in problems[0]
    accepted, problems = wsprnet.upload([SPOT, SPOT], "KF0UQR", "EM18ge", 14.0956, url="http://127.0.0.1:1/post/", timeout=2)
    assert accepted == 0 and len(problems) == 1                                              # gave up after the first failure


def test_window_uploads_only_after_the_operator_agrees(app, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    settings = QSettings(wspr_window._SETTINGS_ORG, wspr_window._SETTINGS_APP)
    settings.setValue("operator_callsign", "KF0UQR"); settings.setValue("operator_lat", 38.2); settings.setValue("operator_lon", -97.5)
    sent = []
    monkeypatch.setattr(wsprnet, "upload", lambda spots, call, grid, dial, **kw: (sent.append((call, grid, dial, [s.callsign for s in spots])) or (len(spots), [])))
    window, worker = make_window()
    window._dial_mhz = 14.0956
    window._on_decoded(SLOT, [SPOT], True)
    assert sent == []                                                                         # off by default: nothing leaves the machine

    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.No)
    window.upload_check.click()                                                               # asked, and said no
    assert not window.upload_check.isChecked()
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Yes)
    window.upload_check.click()
    assert window.upload_check.isChecked() and settings.value("wspr_upload", False, type=bool)
    window._on_decoded(SLOT, [SPOT], True)
    assert wait(app, lambda: sent) and sent == [("KF0UQR", "EM18ge", 14.0956, ["K1ABC"])]
    assert wait(app, lambda: "uploaded to WSPRnet" in window._upload_status)
    window.closing_for_real()


def test_window_wont_offer_upload_without_a_callsign(app):
    window, _ = make_window()
    QSettings(wspr_window._SETTINGS_ORG, wspr_window._SETTINGS_APP).setValue("operator_callsign", "")
    window.upload_check.click()
    assert not window.upload_check.isChecked() and "callsign" in window.status_label.text()
    window.closing_for_real()


# ---- the beacon -----------------------------------------------------------------------------------

def test_beacon_message_rules():
    assert wspr.beacon_message("kf0uqr", "em18ge", 30) == "KF0UQR EM18 30"
    assert wspr.beacon_message("W1AW", "FN31", 37) == "W1AW FN31 37"
    for call in ("", "PJ4/K1ABC", "K1ABC/P", "TOOLONGCALL1"):
        with pytest.raises(ValueError):
            wspr.beacon_message(call, "FN31", 30)
    with pytest.raises(ValueError):
        wspr.beacon_message("K1ABC", "", 30)                         # no location
    with pytest.raises(ValueError):
        wspr.beacon_message("K1ABC", "FN31", 31)                     # not a WSPR power
    assert wspr.power_label(30) == "30 dBm  (1 W)" and wspr.power_label(10) == "10 dBm  (10 mW)" and wspr.power_label(37).startswith("37 dBm  (5")


@needs_wsjtx
def test_the_beacons_audio_is_a_valid_wspr_signal():
    message, pcm = wspr.beacon_audio("KF0UQR", "EM18ge", 37, 1500.0, 0.3, 48000)
    assert message == "KF0UQR EM18 37" and len(pcm) / 2 / 48000 == pytest.approx(110.592, abs=0.01)
    assert np.abs(np.frombuffer(pcm, dtype="<i2")).max() == pytest.approx(0.3 * 32767, rel=0.02)
    # a receiver hears exactly what was sent: one second into the slot, 12 kHz
    p12 = ft8.resample(pcm, 48000, 12000)
    slot = (bytes(12000 * 2) + p12).ljust(int(120 * 12000 * 2), b"\x00")
    spots = wspr.decode_slot(ft8.CapturedSlot(SLOT, slot, 1.0), 12000, 14.0956)
    assert [(s.callsign, s.grid, s.power_dbm) for s in spots] == [("KF0UQR", "EM18", 37)]
    assert abs(spots[0].frequency_mhz - 14.0971) < 0.0003             # dial + the 1500 Hz tone


class BeaconRadio(FakeRadio):
    def __init__(self, freq=14_095_600):
        super().__init__(freq)
        self.ptt_button = types.SimpleNamespace(isChecked=lambda: False)
        self.worker.sent, self.worker.halts = [], 0
        self.worker.send_tx_audio_pcm = lambda pcm: self.worker.sent.append(pcm)
        self.worker.stop_tx_audio_send = lambda: setattr(self.worker, "halts", self.worker.halts + 1)


def beacon_window(monkeypatch, freq=14_095_600, answer="yes"):
    from PySide6.QtWidgets import QMessageBox
    settings = QSettings(wspr_window._SETTINGS_ORG, wspr_window._SETTINGS_APP)
    settings.setValue("operator_callsign", "KF0UQR"); settings.setValue("operator_lat", 38.2); settings.setValue("operator_lon", -97.5)
    radio = BeaconRadio(freq)
    window = WsprToolWindow(radio, clock=Clock(SLOT))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Yes if answer == "yes" else QMessageBox.No)
    # no real encoder needed for the scheduling tests
    monkeypatch.setattr(wspr, "can_encode", lambda: True)
    monkeypatch.setattr(wspr, "beacon_audio", lambda call, grid, dbm, hz, level, rate: (f"{call} {grid[:4]} {dbm}", bytes(rate * 2 * 111)))
    return window, radio


def test_the_beacon_is_off_until_enabled_and_asks_first(app, monkeypatch):
    window, radio = beacon_window(monkeypatch, answer="no")
    assert not window.beacon_button.isChecked()
    window.beacon_button.click()                                       # asked, and said no
    assert not window.beacon_button.isChecked() and not window._beacon_timer.isActive() and radio.worker.sent == []
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Yes)
    window.beacon_button.click()
    assert window.beacon_button.isChecked() and window._beacon_timer.isActive()
    window.halt_tx()
    window.beacon_button.click()                                       # it remembers it was asked this session
    assert window.beacon_button.isChecked()
    window.closing_for_real()


def test_it_refuses_without_a_usable_callsign_or_location(app, monkeypatch):
    window, radio = beacon_window(monkeypatch)
    QSettings(wspr_window._SETTINGS_ORG, wspr_window._SETTINGS_APP).setValue("operator_callsign", "PJ4/K1ABC")
    window.beacon_button.click()
    assert not window.beacon_button.isChecked() and "can't be sent" in window.beacon_status.text()
    QSettings(wspr_window._SETTINGS_ORG, wspr_window._SETTINGS_APP).setValue("operator_callsign", "")
    window.beacon_button.click()
    assert not window.beacon_button.isChecked()
    window.closing_for_real()


def test_it_transmits_a_second_into_chosen_slots_only(app, monkeypatch):
    window, radio = beacon_window(monkeypatch)
    window.tx_percent_spin.setValue(25)
    draws = iter([0.10, 0.60, 0.20, 0.99])                              # transmit, skip, transmit, skip
    window._rng = lambda: next(draws)
    window.beacon_button.click()
    window._beacon_tick(SLOT + 0.2)                                     # slot 1 decided (10 < 25: yes) but the second has not started
    assert radio.worker.sent == []
    window._beacon_tick(SLOT + 0.95)
    assert len(radio.worker.sent) == 1 and window._sending and "TRANSMITTING: KF0UQR EM18 30" in window.beacon_status.text()
    window._beacon_tick(SLOT + 1.5)                                     # not twice in the same slot
    assert len(radio.worker.sent) == 1
    window._on_beacon_finished()
    window._beacon_tick(SLOT + 120 + 1.0)                               # next slot: the draw says skip
    assert len(radio.worker.sent) == 1 and "listening" in window.beacon_status.text()
    window._beacon_tick(SLOT + 240 + 1.0)                               # the one after: transmit
    assert len(radio.worker.sent) == 2
    window._on_beacon_finished()
    window._beacon_tick(SLOT + 360 + 6.0)                               # too late into a slot to start (and the draw says skip anyway)
    assert len(radio.worker.sent) == 2
    assert {SLOT, SLOT + 240} <= window._tx_slots                       # the slots we sent in are remembered
    window.closing_for_real()


def test_zero_percent_never_transmits_and_full_always_does(app, monkeypatch):
    window, radio = beacon_window(monkeypatch)
    window.tx_percent_spin.setValue(0)
    window.beacon_button.click()
    for n in range(20):
        window._beacon_tick(SLOT + 120 * n + 1.0)
    assert radio.worker.sent == []
    window.halt_tx()
    window.tx_percent_spin.setValue(100)
    window._rng = lambda: 0.999
    window.beacon_button.click()
    window._beacon_tick(SLOT + 1.0)
    assert len(radio.worker.sent) == 1
    window.closing_for_real()


def test_it_will_not_transmit_off_a_wspr_frequency_or_over_a_manual_transmission(app, monkeypatch):
    window, radio = beacon_window(monkeypatch, freq=14_074_000)         # the FT8 frequency
    window.tx_percent_spin.setValue(100); window._rng = lambda: 0.0
    window.beacon_button.click()
    window._beacon_tick(SLOT + 1.0)
    assert radio.worker.sent == [] and "not on a WSPR frequency" in window.beacon_status.text()
    radio._current_freq_hz = 14_095_600                                 # tuned: the next slot goes
    window._beacon_tick(SLOT + 120 + 1.0)
    assert len(radio.worker.sent) == 1
    window._on_beacon_finished()
    radio.ptt_button = types.SimpleNamespace(isChecked=lambda: True)    # the operator is keying the radio
    window._beacon_tick(SLOT + 240 + 1.0)
    assert len(radio.worker.sent) == 1 and "already transmitting" in window.beacon_status.text()
    window.closing_for_real()


def test_halt_and_closing_stop_the_transmission_and_the_beacon(app, monkeypatch):
    window, radio = beacon_window(monkeypatch)
    window.tx_percent_spin.setValue(100); window._rng = lambda: 0.0
    window.beacon_button.click()
    window._beacon_tick(SLOT + 1.0)
    assert window._sending
    window.halt_tx()
    assert radio.worker.halts == 1 and not window._sending and not window.beacon_button.isChecked() and not window._beacon_timer.isActive()
    window.beacon_button.click()
    window._beacon_tick(SLOT + 120 + 1.0)
    assert window._sending
    window.close()                                                      # closing the tab: never leave it keyed
    assert radio.worker.halts == 2 and not window.beacon_button.isChecked()
    window.closing_for_real()


def test_slots_we_transmitted_in_are_not_decoded(app, monkeypatch):
    window, radio = beacon_window(monkeypatch)
    window._tx_slots.add(SLOT)
    submitted = []
    window._executor = types.SimpleNamespace(submit=lambda *a: submitted.append(a), shutdown=lambda **k: None)
    window._submit(ft8.CapturedSlot(SLOT, bytes(10), 1.0), 8000)
    window._submit(ft8.CapturedSlot(SLOT + 120, bytes(10), 1.0), 8000)
    assert len(submitted) == 1 and submitted[0][1].slot_start == SLOT + 120
    window.closing_for_real()


def test_beacon_settings_are_remembered(app, monkeypatch):
    window, radio = beacon_window(monkeypatch)
    window.tx_percent_spin.setValue(35); window.tx_freq_spin.setValue(1450); window.level_spin.setValue(22)
    window.power_combo.setCurrentIndex(window.power_combo.findData(37))
    window.beacon_button.click()
    window.halt_tx()
    again = WsprToolWindow(radio, clock=Clock(SLOT))
    assert (again.tx_percent_spin.value(), again.tx_freq_spin.value(), again.level_spin.value(), again.power_combo.currentData()) == (35, 1450, 22, 37)
    assert not again.beacon_button.isChecked()                           # but the beacon itself always starts off
    window.closing_for_real(); again.closing_for_real()


def test_a_failed_transmission_is_reported_not_silent(app, monkeypatch):
    from PySide6.QtCore import QObject, Signal

    class Emitter(QObject):
        error = Signal(str)

    emitter = Emitter()
    window, radio = beacon_window(monkeypatch)
    window.closing_for_real()
    radio.worker.error = emitter.error
    window, radio = beacon_window(monkeypatch)
    radio.worker.error = emitter.error
    window2 = type(window)(radio)                # built with the worker's error signal present
    window2.tx_percent_spin.setValue(100); window2._rng = lambda: 0.0
    window2.beacon_button.click()
    window2._beacon_tick(SLOT + 1.0)
    assert window2._sending
    emitter.error.emit("Send TX audio: PTT keying failed (boom) -- aborting send.")
    assert not window2._sending and "Transmission failed" in window2.beacon_status.text() and "boom" in window2.beacon_status.text()
    window2.closing_for_real(); window.closing_for_real()
