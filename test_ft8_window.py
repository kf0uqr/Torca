"""Tests for ft8_window.py: the FT8/FT4 tool window, including a REAL decode
through the whole window (generated FT8 audio -> slot capture -> decoder thread
-> table)."""

import os
import threading
import time
import types
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

import ft8_decoder as ft8
import ft8_window
from ft8_window import Ft8ToolWindow
from test_ft8_decoder import built, _generate, _resample, _mix, Clock    # noqa: F401  (the real-decoder fixture and helpers)

RATE = 8000


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch, app):
    QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope, str(tmp_path / "qsettings"))
    monkeypatch.setattr(ft8_window, "_SETTINGS_ORG", f"TorcaFt8Test{uuid.uuid4().hex}")


class FakeWorker:
    def __init__(self):
        self.radio = types.SimpleNamespace(audio_sample_rate=RATE)
        self.callback = None
        self.started = 0
        self.stopped = 0
        self.fail = None

    def start_ft8_decode(self, callback):
        if self.fail:
            raise RuntimeError(self.fail)
        self.callback = callback
        self.started += 1

    def stop_ft8_decode(self):
        self.callback = None
        self.stopped += 1


class FakeRadioWindow:
    def __init__(self):
        self.worker = FakeWorker()

    def tool_title(self, name):
        return f"{name} - IC-7300"


def _window(clock=None):
    radio = FakeRadioWindow()
    window = Ft8ToolWindow(radio, clock=clock or time.time)
    return window, radio.worker


def _wait(app, condition, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def _texts(window):
    return [window.table.item(r, 4).text() for r in range(window.table.rowCount())]


def _close(window):
    window.closing_for_real()


# ---- basics ---------------------------------------------------------------------

def test_title_and_initial_state(app, built):
    window, worker = _window()
    assert window.windowTitle() == "FT8 Tool - IC-7300"
    assert not window.decode_toggle_button.isChecked() and window.table.rowCount() == 0
    assert [window.mode_combo.itemText(i) for i in range(window.mode_combo.count())] == ["FT8", "FT4"]
    assert not window.build_button.isVisibleTo(window)            # the decoder is there
    assert window.status_label.text() == "Not decoding."
    _close(window)


def test_start_and_stop_attach_and_detach_the_radio_audio(app, built):
    window, worker = _window()
    window.show()
    window.decode_toggle_button.setChecked(True)
    assert worker.started == 1 and worker.callback is not None
    assert window.decode_toggle_button.text() == "Stop Decoding" and not window.mode_combo.isEnabled()
    assert "Decoding FT8" in window.status_label.text() and "next slot in" in window.status_label.text()
    window.decode_toggle_button.setChecked(False)
    assert worker.stopped == 1 and worker.callback is None
    assert window.mode_combo.isEnabled() and window.status_label.text() == "Not decoding."
    _close(window)


def test_the_chosen_mode_is_used_and_cannot_change_while_decoding(app, built):
    window, worker = _window()
    window.mode_combo.setCurrentText("FT4")
    window.decode_toggle_button.setChecked(True)
    assert window._capture.mode == "FT4" and window._capture.sample_rate == RATE
    assert "Decoding FT4" in window.status_label.text()
    assert not window.mode_combo.isEnabled()
    window.decode_toggle_button.setChecked(False)
    _close(window)


def test_not_connected_is_reported_and_leaves_the_button_off(app, built):
    window, worker = _window()
    worker.fail = "start_ft8_decode: not connected yet."
    window.decode_toggle_button.setChecked(True)
    assert not window.decode_toggle_button.isChecked()
    assert "not connected" in window.status_label.text()
    _close(window)


def test_closing_stops_decoding_and_hides_but_keeps_the_decodes(app, built):
    window, worker = _window()
    window.show()
    window.decode_toggle_button.setChecked(True)
    window._append_row(ft8.Ft8Message(1_800_000_000.0, "FT8", -5, 0.1, 1500, "CQ KF0UQR EM18"), "")
    window.close()                                               # the user closing the tab: hide, don't destroy
    assert worker.stopped == 1 and not window.decode_toggle_button.isChecked() and not window.isVisible()
    assert window.table.rowCount() == 1
    _close(window)


# ---- the decoder is missing -----------------------------------------------------------

def test_without_a_decoder_it_says_so_and_offers_to_build(app, monkeypatch):
    monkeypatch.setattr(ft8, "is_available", lambda: False)
    monkeypatch.setattr(ft8, "can_build", lambda: True)
    window, worker = _window()
    window.show()
    assert window.build_button.isVisible() and "Build Decoder" in window.status_label.text()
    window.decode_toggle_button.setChecked(True)                  # starting is refused ...
    assert not window.decode_toggle_button.isChecked() and worker.started == 0
    _close(window)


def test_without_a_decoder_or_a_compiler_it_explains_what_to_install(app, monkeypatch):
    monkeypatch.setattr(ft8, "is_available", lambda: False)
    monkeypatch.setattr(ft8, "can_build", lambda: False)
    window, _ = _window()
    assert "build-essential" in window.status_label.text()
    _close(window)


def test_build_button_builds_then_enables_decoding(app, monkeypatch):
    state = {"built": False}
    monkeypatch.setattr(ft8, "is_available", lambda: state["built"])
    monkeypatch.setattr(ft8, "can_build", lambda: True)

    def fake_build():
        state["built"] = True
        return True, "FT8 decoder built."
    monkeypatch.setattr(ft8, "build_decoder", fake_build)
    window, worker = _window()
    window.show()
    window.build_button.click()
    assert _wait(app, lambda: not window.build_button.isVisible())
    assert "Click Start Decoding" in window.status_label.text()
    window.decode_toggle_button.setChecked(True)
    assert worker.started == 1
    window.decode_toggle_button.setChecked(False)
    _close(window)


def test_a_failed_build_shows_why(app, monkeypatch):
    monkeypatch.setattr(ft8, "is_available", lambda: False)
    monkeypatch.setattr(ft8, "can_build", lambda: True)
    monkeypatch.setattr(ft8, "build_decoder", lambda: (False, "Building the FT8 decoder failed:\nboom"))
    window, _ = _window()
    window.show()
    window.build_button.click()
    assert _wait(app, lambda: "boom" in window.status_label.text())
    assert window.build_button.isVisible() and window.build_button.isEnabled()      # can try again
    _close(window)


# ---- real decoding through the whole window ------------------------------------------------

def _feed_a_slot(app, window, worker, audio, start, rate=RATE):
    """Delivers `audio` (one whole transmission) in real-time-sized chunks from `start`,
    through the callback the radio would call."""
    clock = window._clock
    step = int(0.25 * rate) * 2
    t = start
    for offset in range(0, len(audio), step):
        chunk = audio[offset: offset + step]
        t += len(chunk) / 2 / rate
        clock.now = t
        worker.callback(chunk)
    clock.now = t + 0.1


def test_real_ft8_audio_is_decoded_into_the_table_with_highlights(app, built, tmp_path):
    cq = _generate(built, "CQ DX W1AW FN31", 1400, tmp_path)
    to_me = _generate(built, "KF0UQR W9XYZ -07", 2300, tmp_path)
    other = _generate(built, "K1ABC W2DEF RR73", 800, tmp_path)
    audio = _resample(_mix(cq, to_me, other), 12000, RATE)

    QSettings(ft8_window._SETTINGS_ORG, ft8_window._SETTINGS_APP).setValue("operator_callsign", "KF0UQR")
    start = 9_000_000.0 - (9_000_000.0 % 15.0)
    clock = Clock(start)
    window, worker = _window(clock=clock)
    window.show()
    window.decode_toggle_button.setChecked(True)
    _feed_a_slot(app, window, worker, audio, start)
    assert _wait(app, lambda: window.table.rowCount() >= 3), _texts(window)
    assert sorted(_texts(window)) == sorted(["CQ DX W1AW FN31", "KF0UQR W9XYZ -07", "K1ABC W2DEF RR73"])

    rows = {window.table.item(r, 4).text(): r for r in range(window.table.rowCount())}
    assert window.table.item(rows["CQ DX W1AW FN31"], 0).text() == time.strftime("%H:%M:%S", time.gmtime(start))
    freq = float(window.table.item(rows["CQ DX W1AW FN31"], 3).text())
    assert abs(freq - 1400) <= 10
    assert window.table.item(rows["CQ DX W1AW FN31"], 4).foreground().color() == ft8_window._COLOR_CQ        # a CQ: green
    assert window.table.item(rows["KF0UQR W9XYZ -07"], 4).foreground().color() == ft8_window._COLOR_MINE     # to me: amber
    assert window.table.item(rows["K1ABC W2DEF RR73"], 4).foreground().color() != ft8_window._COLOR_MINE      # someone else's QSO: plain
    assert "3 decodes" in (window._last_slot_summary or "")
    window.decode_toggle_button.setChecked(False)
    _close(window)


def test_the_partial_slot_at_the_start_adds_nothing(app, built, tmp_path):
    audio = _resample(_generate(built, "CQ KF0UQR EM18", 1500, tmp_path), 12000, RATE)
    start = 9_100_000.0 - (9_100_000.0 % 15.0)
    clock = Clock(start + 7.0)                                     # joins half way through a slot
    window, worker = _window(clock=clock)
    window.decode_toggle_button.setChecked(True)
    _feed_a_slot(app, window, worker, audio[int(7.0 * RATE) * 2:], start + 7.0)
    for _ in range(40):
        app.processEvents()
        time.sleep(0.01)
    assert window.table.rowCount() == 0 and window._slots_decoded == 0
    window.decode_toggle_button.setChecked(False)
    _close(window)


def test_a_slow_decoder_drops_slots_instead_of_falling_behind(app, built, monkeypatch):
    gate = threading.Event()
    ran = []

    def slow_decode(slot, sample_rate, mode):
        ran.append(slot.slot_start)
        gate.wait(10)
        return []
    monkeypatch.setattr(ft8, "decode_slot", slow_decode)
    window, worker = _window()
    slot = lambda n: ft8.CapturedSlot(slot_start=1000.0 + 15 * n, pcm=b"", fraction=1.0)
    for n in range(6):
        window._submit_decode(slot(n), RATE, "FT8")
    assert window._pending == 2                                   # only two were accepted; the rest were dropped
    gate.set()
    assert _wait(app, lambda: window._pending == 0)
    assert len(ran) == 2
    window._submit_decode(slot(9), RATE, "FT8")                   # and it recovers once it has caught up
    assert _wait(app, lambda: len(ran) == 3)
    _close(window)


def test_a_decoder_crash_never_takes_the_tool_down(app, built, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("decoder blew up")
    monkeypatch.setattr(ft8, "decode_slot", boom)
    window, _ = _window()
    window._submit_decode(ft8.CapturedSlot(slot_start=1000.0, pcm=b"", fraction=1.0), RATE, "FT8")
    assert _wait(app, lambda: window._slots_decoded == 1 and window._pending == 0)
    assert window.table.rowCount() == 0
    _close(window)


# ---- table and watchdog --------------------------------------------------------------------------

def test_rows_are_capped_and_clear_empties_the_table(app, built, monkeypatch):
    monkeypatch.setattr(ft8_window, "_MAX_ROWS", 5)
    window, _ = _window()
    for i in range(9):
        window._append_row(ft8.Ft8Message(1_800_000_000.0 + i, "FT8", -5, 0.1, 1000 + i, f"CQ TEST{i}"), "")
    assert window.table.rowCount() == 5 and _texts(window)[0] == "CQ TEST4" and _texts(window)[-1] == "CQ TEST8"
    window._on_clear_clicked()
    assert window.table.rowCount() == 0
    _close(window)


def test_audio_watchdog_warns_when_the_stream_stalls_and_clears_when_it_returns(app, built):
    window, worker = _window()
    window.show()
    window.decode_toggle_button.setChecked(True)
    window._last_audio_monotonic = 100.0
    window._check_audio_flow(now=102.0)
    assert not window.audio_warning_label.isVisible()
    window._check_audio_flow(now=100.0 + ft8_window._AUDIO_STALL_SECONDS + 3)
    assert window.audio_warning_label.isVisible() and "No audio received" in window.audio_warning_label.text()
    window._on_decode_audio_frame(b"\x00\x00")                    # audio is back (stamps the time itself)
    window._check_audio_flow()
    assert not window.audio_warning_label.isVisible()
    window.decode_toggle_button.setChecked(False)
    _close(window)


def test_the_callsign_in_settings_decides_what_is_highlighted(app, built):
    window, _ = _window()
    message = ft8.Ft8Message(1_800_000_000.0, "FT8", -5, 0.1, 1500, "W1AW KF0UQR -12")
    window._append_row(message, "kf0uqr")
    window._append_row(message, "")
    window._append_row(message, "N0CALL")
    colors = [window.table.item(r, 4).foreground().color() for r in range(3)]
    assert colors[0] == ft8_window._COLOR_MINE
    assert colors[1] != ft8_window._COLOR_MINE and colors[2] != ft8_window._COLOR_MINE
    _close(window)
