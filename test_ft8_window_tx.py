"""Making contacts from the FT8 tool: choosing a station, the slot timing of a
transmission, the audio that goes to the radio (decoded back to prove it is a
valid FT8 signal), the whole QSO sequence, logging, and the safety stops."""

import time
import types
import uuid

import numpy as np
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

import ft8_decoder as ft8
import ft8_window
from ft8_window import Ft8ToolWindow
from test_ft8_decoder import built, Clock    # noqa: F401  (the real encoder/decoder fixture)

START = 9_000_000.0 - (9_000_000.0 % 30.0)      # an even slot boundary (and a whole minute)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch, app):
    QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope, str(tmp_path / "qsettings"))
    monkeypatch.setattr(ft8_window, "_SETTINGS_ORG", f"TorcaFt8TxTest{uuid.uuid4().hex}")
    settings = QSettings(ft8_window._SETTINGS_ORG, ft8_window._SETTINGS_APP)
    settings.setValue("operator_callsign", "KF0UQR")
    settings.setValue("operator_lat", 38.2)
    settings.setValue("operator_lon", -97.5)


class FakeWorker:
    def __init__(self):
        self.radio = types.SimpleNamespace(audio_sample_rate=8000)
        self.sent = []
        self.halted = 0

    def send_tx_audio_pcm(self, pcm):
        self.sent.append(pcm)

    def stop_tx_audio_send(self):
        self.halted += 1

    def start_ft8_decode(self, cb): pass
    def stop_ft8_decode(self): pass


class FakeRadio:
    def __init__(self):
        self.worker = FakeWorker()
        self._current_freq_hz = 14_074_000

    def tool_title(self, name):
        return name


@pytest.fixture
def tool(app, built):
    clock = Clock(START)
    radio = FakeRadio()
    window = Ft8ToolWindow(radio, clock=clock)
    window.logged = []
    window.qso_logged.connect(window.logged.append)
    yield window, radio.worker, clock
    window.closing_for_real()


def hear(window, text, slot_start, snr=-8.0, freq=1200.0):
    """As if the decoder had just reported this message from the slot."""
    window._on_decoded(slot_start, [ft8.Ft8Message(slot_start, "FT8", snr, 0.2, freq, text)], True)


def double_click(window, text):
    for row in range(window.table.rowCount()):
        if window.table.item(row, 4).text() == text:
            window._on_row_double_clicked(row, 4)
            return
    raise AssertionError(f"{text!r} not listed")


def decode_back(pcm48, mode="FT8"):
    """What a receiver would make of the audio we hand the radio."""
    pcm12 = ft8.resample(pcm48, 48000, 12000)
    lead = b"\0" * int(ft8.TX_START_OFFSET * 12000) * 2
    full = (lead + pcm12).ljust(int(ft8.SLOT_SECONDS[mode] * 12000) * 2, b"\0")
    return [m.text for m in ft8.decode_slot(ft8.CapturedSlot(0.0, full, 1.0), 12000, mode)]


def test_double_clicking_a_cq_chooses_the_station_and_arms_tx(tool):
    window, worker, clock = tool
    hear(window, "CQ W1AW FN31", START)                          # heard in an even slot
    double_click(window, "CQ W1AW FN31")
    assert window.dx_call_edit.text() == "W1AW"
    assert window.tx_message_edit.text() == "W1AW KF0UQR EM18"    # our grid from the profile location
    assert window.tx_parity_combo.currentData() == 1              # we answer in the other (odd) slot
    assert window.tx_enable_button.isChecked()


def test_transmission_goes_out_once_in_the_right_slot_and_is_valid_ft8(tool):
    window, worker, clock = tool
    hear(window, "CQ W1AW FN31", START)
    double_click(window, "CQ W1AW FN31")
    window._tx_tick(START + 2)                                    # still the even slot: wrong parity
    assert worker.sent == []
    window._tx_tick(START + 15.1)                                 # too early in the odd slot (audio starts at 0.5 s)
    assert worker.sent == []
    window._tx_tick(START + 15.45)
    assert len(worker.sent) == 1
    assert len(worker.sent[0]) == int(12.64 * 48000) * 2 or abs(len(worker.sent[0]) - 12.64 * 48000 * 2) < 200
    window._tx_tick(START + 15.4)                                 # not again in the same slot
    assert len(worker.sent) == 1
    assert decode_back(worker.sent[0]) == ["W1AW KF0UQR EM18"]
    quiet = np.abs(np.frombuffer(worker.sent[0], dtype="<i2")).max()
    assert quiet <= 32767 * 0.31                                  # the default 30 % level, not full scale


def test_too_late_into_the_slot_waits_for_the_next_one(tool):
    window, worker, clock = tool
    hear(window, "CQ W1AW FN31", START)
    double_click(window, "CQ W1AW FN31")
    window._tx_tick(START + 17.0)                                 # 2 s in: the start would be lost
    assert worker.sent == []
    window._tx_tick(START + 45.45)                                 # the next odd slot
    assert len(worker.sent) == 1


def test_a_whole_contact_is_sequenced_and_logged(tool):
    window, worker, clock = tool
    hear(window, "CQ W1AW FN31", START, snr=-7)
    double_click(window, "CQ W1AW FN31")
    window._tx_tick(START + 15.45)
    window._on_tx_finished("W1AW KF0UQR EM18")
    assert window.logged == []

    hear(window, "KF0UQR W1AW -12", START + 30, snr=-9)          # they report
    assert window.tx_message_edit.text() == "W1AW KF0UQR R-09"
    window._tx_tick(START + 45.45)
    assert decode_back(worker.sent[-1]) == ["W1AW KF0UQR R-09"]
    window._on_tx_finished("W1AW KF0UQR R-09")

    hear(window, "KF0UQR W1AW RR73", START + 60, snr=-9)
    assert window.tx_message_edit.text() == "W1AW KF0UQR 73"
    window._tx_tick(START + 75.45)
    window._on_tx_finished("W1AW KF0UQR 73")

    assert len(window.logged) == 1
    qso = window.logged[0]
    assert (qso["CALL"], qso["MODE"], qso["RST_SENT"], qso["RST_RCVD"], qso["GRIDSQUARE"]) == ("W1AW", "FT8", "-09", "-12", "FN31")
    assert qso["BAND"] == "20m" and qso["FREQ"] == "14.074000" and qso["STATION_CALLSIGN"] == "KF0UQR"
    assert qso["QSO_DATE"] == time.strftime("%Y%m%d", time.gmtime(START))
    assert not window.tx_enable_button.isChecked()               # nothing more to say: TX switches itself off
    assert window.tx_message_edit.text() == ""


def test_calling_cq_repeats_and_resumes_after_a_contact(tool):
    window, worker, clock = tool
    window.tx_parity_combo.setCurrentIndex(0)
    window._on_cq_clicked()
    assert window.tx_message_edit.text() == "CQ KF0UQR EM18"
    window._tx_tick(START + 0.45)
    assert decode_back(worker.sent[0]) == ["CQ KF0UQR EM18"]
    window._on_tx_finished("CQ KF0UQR EM18")
    hear(window, "KF0UQR N0XYZ EM28", START + 15, snr=-3)         # someone answers
    assert window.dx_call_edit.text() == "N0XYZ" and window.tx_message_edit.text() == "N0XYZ KF0UQR -03"
    window._tx_tick(START + 30.45); window._on_tx_finished("N0XYZ KF0UQR -03")
    hear(window, "KF0UQR N0XYZ R-10", START + 45, snr=-3)
    window._tx_tick(START + 60.45); window._on_tx_finished("N0XYZ KF0UQR RR73")
    assert [q["CALL"] for q in window.logged] == ["N0XYZ"]
    assert window.tx_message_edit.text() == "CQ KF0UQR EM18" and window.tx_enable_button.isChecked()   # back to CQ


def test_halt_stops_the_audio_and_the_timer(tool):
    window, worker, clock = tool
    window._on_cq_clicked()
    window._tx_tick(START + 0.45)
    assert window._sending
    window.halt_tx()
    assert worker.halted == 1 and not window._sending and not window.tx_enable_button.isChecked()
    assert not window._tx_timer.isActive()


def test_watchdog_switches_tx_off_when_nobody_answers(tool):
    window, worker, clock = tool
    clock.now = START
    window._on_cq_clicked()
    window._tx_tick(START + ft8_window.WATCHDOG_SECONDS - 1)
    assert window.tx_enable_button.isChecked()
    window._tx_tick(START + ft8_window.WATCHDOG_SECONDS + 1)
    assert not window.tx_enable_button.isChecked() and "nothing heard" in window.tx_status_label.text()


def test_giving_up_after_no_answer(tool):
    window, worker, clock = tool
    hear(window, "CQ W1AW FN31", START)
    double_click(window, "CQ W1AW FN31")
    for n in range(5):
        window._tx_tick(START + 15.45 + 30 * n)                   # each odd slot sends it again
        assert len(worker.sent) == n + 1
        window._on_tx_finished("W1AW KF0UQR EM18")
    assert not window.tx_enable_button.isChecked() and "gave up" in window.tx_status_label.text()


def test_unsendable_message_halts_with_a_reason(tool):
    window, worker, clock = tool
    window._on_cq_clicked()
    window.tx_message_edit.setText("THIS IS NOT A VALID FT8 MESSAGE AT ALL")
    window._tx_tick(START + 0.45)
    assert worker.sent == [] and not window.tx_enable_button.isChecked()
    assert "not a message" in window.tx_status_label.text()


def test_without_a_callsign_it_refuses_to_transmit(tool):
    window, worker, clock = tool
    QSettings(ft8_window._SETTINGS_ORG, ft8_window._SETTINGS_APP).setValue("operator_callsign", "")
    window._on_cq_clicked()
    assert not window.tx_enable_button.isChecked() and "callsign" in window.tx_status_label.text()


def test_closing_the_window_never_leaves_it_transmitting(tool):
    window, worker, clock = tool
    window._on_cq_clicked()
    window._tx_tick(START + 0.45)
    window.close()
    assert worker.halted == 1 and not window.tx_enable_button.isChecked()


def test_decoded_stations_are_published_with_the_radios_frequency(tool):
    window, worker, clock = tool
    got = []
    window.stations_heard.connect(got.append)
    hear(window, "CQ W1AW FN31", START, snr=-12.4, freq=1200.0)
    assert got and got[0][0] == {"callsign": "W1AW", "locator": "FN31", "frequency_hz": 14_075_200, "snr": -12,
                                 "mode": "FT8", "epoch": int(START)}


def test_nothing_is_published_without_a_known_radio_frequency(tool):
    window, worker, clock = tool
    window._radio_window._current_freq_hz = None
    got = []
    window.stations_heard.connect(got.append)
    hear(window, "CQ W1AW FN31", START)
    assert got == []
