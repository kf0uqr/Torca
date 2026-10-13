"""Tools take their audio from "this radio" or another connected radio, and a Satellite tool is what gives a
radio a satellite role (no role when connecting). Real RadioWindows offscreen with the connection stubbed."""

import os
import subprocess
import sys
import textwrap
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

import main_window
import radio_worker
import rx_radio
from satellite_session import SatelliteSession

DETAILS = {"radio_model": "IC-9700", "connection_type": "network", "host": "192.0.2.1",
           "username": "u", "password": "p", "port": 50001}


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch, app):
    QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope, str(tmp_path / "qsettings"))
    monkeypatch.setattr(rx_radio, "_SETTINGS_ORG", f"TorcaRxTest{uuid.uuid4().hex}")
    monkeypatch.setattr(radio_worker.RadioWorker, "start", lambda self, *a, **k: None)


@pytest.fixture
def radios(app):
    session = SatelliteSession()
    nine = main_window.RadioWindow(dict(DETAILS), session)
    seven = main_window.RadioWindow(dict(DETAILS, radio_model="IC-7300", connection_type="serial", serial_port="/dev/ttyUSB0"), session)
    yield nine, seven, session
    nine.close()
    seven.close()


def test_a_radio_has_no_satellite_role_when_it_connects(radios):
    nine, seven, session = radios
    assert nine.satellite_role == seven.satellite_role == "non_sat" and not session._radios
    assert nine.ptt_button.isVisibleTo(nine) and nine.tune_button.isVisibleTo(nine)      # a plain radio: nothing hidden


def test_by_default_a_tool_listens_to_its_own_radio_and_can_be_pointed_at_another(radios):
    nine, seven, session = radios
    selector = rx_radio.ReceiveRadioSelector(nine, "FT8 Tool")
    assert selector.radio() is nine and selector.worker() is nine.worker and not selector.is_other()
    assert [selector.combo.itemData(i) for i in range(selector.combo.count())] == [None, seven]
    assert "This radio (IC-9700)" in selector.combo.itemText(0) and "IC-7300" in selector.combo.itemText(1)
    changes = []
    selector.changed.connect(lambda: changes.append(1))
    selector.select(seven)
    assert selector.radio() is seven and selector.worker() is seven.worker and selector.is_other() and changes == [1]
    selector.set_locked(True)
    assert not selector.combo.isEnabled()
    selector.set_locked(False)
    selector.combo.setCurrentIndex(0)
    selector._on_activated(0)
    assert selector.radio() is nine


def test_the_choice_is_remembered_per_tool(radios):
    nine, seven, session = radios
    first = rx_radio.ReceiveRadioSelector(nine, "FT8 Tool")
    first.select(seven)
    assert rx_radio.ReceiveRadioSelector(nine, "FT8 Tool").radio() is seven               # same tool, same radio: restored
    assert rx_radio.ReceiveRadioSelector(nine, "WSPR Tool").radio() is nine               # another tool: its own choice
    assert rx_radio.ReceiveRadioSelector(seven, "FT8 Tool").radio() is seven


def test_closing_the_chosen_radio_puts_the_tool_back_on_its_own(radios):
    nine, seven, session = radios
    selector = rx_radio.ReceiveRadioSelector(nine, "CW Tool")
    selector.select(seven)
    lost = []
    selector.lost.connect(lambda: lost.append(1))
    seven.close()
    assert selector.radio() is nine and lost == [1]


def test_tools_decode_from_the_chosen_radio(app, radios, monkeypatch):
    nine, seven, session = radios
    started = []
    monkeypatch.setattr(seven.worker, "add_audio_tap", lambda cb: started.append(("seven", cb)), raising=False)
    monkeypatch.setattr(nine.worker, "add_audio_tap", lambda cb: started.append(("nine", cb)), raising=False)
    monkeypatch.setattr(seven.worker, "remove_audio_tap", lambda cb: started.append(("seven-off", cb)), raising=False)
    monkeypatch.setattr(seven, "_current_freq_hz", 14_095_600, raising=False)
    import wspr
    monkeypatch.setattr(wspr, "is_available", lambda: True)
    tool = nine.ensure_wspr_tool_window()
    tool.rx_selector.select(seven)
    tool.decode_toggle_button.setChecked(True)
    assert started and started[0][0] == "seven"                      # the audio comes from the other radio...
    assert not tool.rx_selector.combo.isEnabled()                    # ...and cannot be changed while decoding
    assert tool._radio_dial_mhz() == pytest.approx(14.0956)          # ...and the frequency is ITS frequency
    tool.decode_toggle_button.setChecked(False)
    assert started[-1][0] == "seven-off" and tool.rx_selector.combo.isEnabled()
    seven.close()                                                    # closing it while decoding stops decoding
    tool.decode_toggle_button.setChecked(True)
    assert tool.rx_selector.radio() is nine
    tool.decode_toggle_button.setChecked(False)
    tool.closing_for_real()


# ---- the Satellite tool, with the real dashboard ----------------------------------------------------

SCRIPT = textwrap.dedent('''
    import os, sys
    sys.path.insert(0, %r)
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    s = QSettings("IcomRadioApp", "RadioControl")
    s.setValue("operator_callsign", "KF0UQR"); s.setValue("operator_lat", 38.2); s.setValue("operator_lon", -97.5); s.sync()
    import radio_worker
    radio_worker.RadioWorker.start = lambda self, *a, **k: None
    import ham_dashboard
    from workspace_host import WorkspaceHost
    host = WorkspaceHost(); dash = ham_dashboard.HamClockWindow(); host.add_dashboard(dash); host.show()
    pages = dict(dash._data_pages)
    assert "Satellites" not in pages, "no Satellites tab any more"

    nine = dash._open_radio({"radio_model": "IC-9700", "connection_type": "network", "host": "192.0.2.1", "username": "u", "password": "p", "port": 50001})
    seven = dash._open_radio({"radio_model": "IC-7300", "connection_type": "serial", "serial_port": "/dev/ttyUSB0", "baud_rate": 115200})
    assert nine.satellite_role == seven.satellite_role == "non_sat"
    assert not dash.satellite_session._radios and nine.windowTitle() == "IC-9700"
    page = dash.satellite_passes_page
    assert page.parent() is None

    # started on the 9700 alone: it does the whole pass
    nine.satellite_tool_button.setEnabled(True)
    tool = nine.ensure_satellite_tool_window()
    assert tool is not None and tool.windowTitle() == "Satellite Tool - IC-9700" and tool.workspace_key.endswith(":Satellite Tool")
    assert nine.satellite_role == "full_duplex" and seven.satellite_role == "non_sat"
    assert page.parent() is not None and tool.page_holder.indexOf(page) >= 0          # the pass controls are inside the tool
    assert nine.windowTitle() == "IC-9700 - Satellite Full Duplex"
    assert dash.satellite_session._radios == {nine: "full_duplex"}

    # add the 7300 as the receive radio: a pair
    tool._refresh_receive_choices()
    tool.receive_combo.setCurrentIndex(tool.receive_combo.findData(seven)); tool._on_receive_chosen(0)
    assert (nine.satellite_role, seven.satellite_role) == ("uplink", "downlink")
    assert dash.satellite_session._radios == {nine: "uplink", seven: "downlink"}
    assert not seven.ptt_button.isVisibleTo(seven) and not seven.tune_button.isVisibleTo(seven)    # a downlink radio never transmits
    assert "7300" in seven.windowTitle() and "Downlink" in seven.windowTitle()

    # the choice survives reopening the tool
    tool.closeEvent(__import__("PySide6.QtGui", fromlist=["QCloseEvent"]).QCloseEvent())
    assert nine.satellite_role == seven.satellite_role == "non_sat" and not dash.satellite_session._radios
    assert seven.ptt_button.isVisibleTo(seven) and seven.tune_button.isVisibleTo(seven)   # it may transmit again
    assert page.parent() is None and nine.windowTitle() == "IC-9700"
    tool = nine.ensure_satellite_tool_window()
    assert (nine.satellite_role, seven.satellite_role) == ("uplink", "downlink") and tool.receive_combo.currentData() is seven

    # started on the OTHER radio: everything moves there, and the first tool says so
    tool2 = seven.ensure_satellite_tool_window()
    assert seven.satellite_role == "full_duplex" and nine.satellite_role == "non_sat"      # the 7300 has no receive radio chosen
    assert tool.page_holder.indexOf(page) == -1 and "IC-7300" in tool.moved_label.text()
    assert tool2.page_holder.indexOf(page) >= 0 and dash._satellite_tool is tool2

    # the receive radio closing puts the transmit radio back on its own
    nine.close()
    for _ in range(5): app.processEvents()
    assert dash.satellite_session._radios == {seven: "full_duplex"}
    seven.close()
    assert not dash.satellite_session._radios and dash._satellite_tool is None
    print("OK"); sys.stdout.flush(); os._exit(0)
''' % os.path.dirname(os.path.abspath(__file__)))


def test_satellite_tool_gives_radios_their_roles(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), QT_QPA_PLATFORM="offscreen")
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr
