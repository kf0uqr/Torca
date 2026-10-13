"""The dashboard side of reporting: stations heard appear on the PSKReporter
map as "you heard them", and are uploaded only when the operator has switched
reporting on."""

import os
import sys
import subprocess
import textwrap

SCRIPT = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, %r)
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    s = QSettings("IcomRadioApp", "RadioControl")
    s.setValue("operator_callsign", "KF0UQR"); s.setValue("operator_lat", 38.2); s.setValue("operator_lon", -97.5); s.sync()
    import ham_dashboard, pskreporter, pskreporter_report
    from workspace_host import WorkspaceHost
    host = WorkspaceHost(); dash = ham_dashboard.HamClockWindow()
    host.add_dashboard(dash); host.show()

    assert dash._pskreporter_uploader is None                      # off unless the operator turns it on
    markers = []
    dash.map_widget.set_pskreporter_markers = markers.append
    dash._pskreporter_direction = pskreporter.DIRECTION_HEARING
    dash.pskreporter_button.blockSignals(True); dash.pskreporter_button.setChecked(True); dash.pskreporter_button.blockSignals(False)

    now = int(time.time())
    dash._on_stations_heard([
        {"callsign": "W1AW", "locator": "FN31", "frequency_hz": 14075200, "snr": -12, "mode": "FT8", "epoch": now},
        {"callsign": "K9XYZ", "locator": "", "frequency_hz": 14075800, "snr": -3, "mode": "FT8", "epoch": now},
    ])
    for _ in range(40):
        app.processEvents(); time.sleep(0.05)
    assert len(markers) == 1 and len(markers[0]) == 1, markers      # only W1AW has a locator to plot
    assert "W1AW" in markers[0][0]["tooltip"] and "You heard them" in markers[0][0]["tooltip"]
    # K9XYZ's grid arrives later, in another message: it can be plotted now
    dash._on_stations_heard([{"callsign": "K9XYZ", "locator": "EN34", "frequency_hz": 14075800, "snr": -3, "mode": "FT8", "epoch": now}])
    for _ in range(40):
        app.processEvents(); time.sleep(0.05)
    assert len(markers[-1]) == 2

    # direction "heard you": our own decodes are not shown
    dash._pskreporter_direction = pskreporter.DIRECTION_HEARD_YOU
    assert dash._build_pskreporter_markers() == []

    # reporting: nothing is queued until it is switched on, then spots go to the uploader
    sent = []
    dash._pskreporter_report_enabled = True
    dash._sync_pskreporter_uploader()
    assert dash._pskreporter_uploader is not None and dash._pskreporter_uploader.callsign == "KF0UQR"
    assert dash._pskreporter_uploader.locator == "EM18ge"
    dash._pskreporter_uploader._send = sent.append
    dash._on_stations_heard([{"callsign": "N0CALL", "locator": "EM28", "frequency_hz": 7075000, "snr": 4, "mode": "FT8", "epoch": now}])
    dash._pskreporter_uploader.flush()
    assert len(sent) == 1 and b"N0CALL" in sent[0] and b"KF0UQR" in sent[0]
    dash._pskreporter_report_enabled = False
    dash._sync_pskreporter_uploader()
    assert dash._pskreporter_uploader is None
    print("OK"); sys.stdout.flush(); os._exit(0)
''' % os.path.dirname(os.path.abspath(__file__)))


def test_heard_stations_reach_the_map_and_the_uploader(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), QT_QPA_PLATFORM="offscreen")
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr


FILTER_SCRIPT = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, %r)
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    s = QSettings("IcomRadioApp", "RadioControl")
    s.setValue("operator_callsign", "KF0UQR"); s.setValue("operator_lat", 38.2); s.setValue("operator_lon", -97.5); s.sync()
    import ham_dashboard, pskreporter
    from workspace_host import WorkspaceHost
    host = WorkspaceHost(); dash = ham_dashboard.HamClockWindow()
    host.add_dashboard(dash); host.show()
    assert sorted(dash._pskreporter_modes) == ["FT4", "FT8", "OTHER", "WSPR"]          # everything by default
    dash._pskreporter_direction = "both"
    now = int(time.time())
    dash._on_stations_heard([
        {"callsign": "W1AW", "locator": "FN31", "frequency_hz": 14075200, "snr": -12, "mode": "FT8", "epoch": now},
        {"callsign": "K1ABC", "locator": "FN42", "frequency_hz": 14097100, "snr": -20, "mode": "WSPR", "epoch": now, "report": False},
        {"callsign": "N0XYZ", "locator": "EM28", "frequency_hz": 7040000, "snr": -5, "mode": "FT4", "epoch": now},
    ])
    dash._pskreporter_spots_cache = [{"callsign": "G3ABC", "locator": "IO91", "frequency_hz": 14070000, "band": "20m", "mode": "PSK31",
                                      "snr": "3", "flow_start_seconds": now, "direction": pskreporter.DIRECTION_HEARING, "raw": {}}]
    def shown():
        return sorted(m["tooltip"].splitlines()[0] for m in dash._build_pskreporter_markers())
    assert shown() == ["G3ABC", "K1ABC", "N0XYZ", "W1AW"]
    dash._pskreporter_modes = ["WSPR"]
    assert shown() == ["K1ABC"]
    dash._pskreporter_modes = ["FT8", "FT4"]
    assert shown() == ["N0XYZ", "W1AW"]
    dash._pskreporter_modes = ["OTHER"]                                                  # PSK31 and anything not named
    assert shown() == ["G3ABC"]
    dash._pskreporter_modes = []
    assert shown() == []

    # the dialog round-trips the choice
    dialog = ham_dashboard.PskReporterSettingsDialog("KF0UQR", 1800, "both", modes=["FT8", "WSPR"])
    assert [k for k, c in dialog.mode_checks.items() if c.isChecked()] == ["FT8", "WSPR"] and dialog.result_modes() == ["FT8", "WSPR"]
    print("OK"); sys.stdout.flush(); os._exit(0)
''' % os.path.dirname(os.path.abspath(__file__)))


def test_map_mode_filter(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), QT_QPA_PLATFORM="offscreen")
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-c", FILTER_SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr


OUTAGE_SCRIPT = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, %r)
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication([])
    s = QSettings("IcomRadioApp", "RadioControl")
    s.setValue("operator_callsign", "KF0UQR"); s.setValue("operator_lat", 38.2); s.setValue("operator_lon", -97.5); s.sync()
    import ham_dashboard, pskreporter
    from workspace_host import WorkspaceHost
    host = WorkspaceHost(); dash = ham_dashboard.HamClockWindow()
    host.add_dashboard(dash); host.show()
    markers = []
    dash.map_widget.set_pskreporter_markers = markers.append
    shown = []
    QMessageBox.show = lambda self: shown.append(self.text())          # the notice is non-blocking: record it
    dash._pskreporter_direction = pskreporter.DIRECTION_HEARING
    now = int(time.time())
    dash._on_stations_heard([{"callsign": "K1ABC", "locator": "FN42", "frequency_hz": 14097100, "snr": -20, "mode": "WSPR", "epoch": now, "report": False}])

    calls = []
    def server_down(callsign, direction, since):
        calls.append(1)
        raise pskreporter.PskReporterUnavailable("PSKReporter says it is being asked too often (too many requests).", rate_limited=True)
    pskreporter.fetch_pskreporter_spots = server_down
    dash.pskreporter_button.setChecked(True)
    for _ in range(60):
        app.processEvents(); time.sleep(0.05)
    assert dash.pskreporter_button.isChecked(), "the layer must stay on"
    assert dash.pskreporter_button.text() == "PSKReporter: ON (server unavailable)"
    assert "too many requests" in dash.pskreporter_button.toolTip() and "missing" in dash.pskreporter_button.toolTip()
    assert markers and [m["tooltip"].splitlines()[0] for m in markers[-1]] == ["K1ABC"]       # what we have heard ourselves is still plotted
    assert len(shown) == 1 and "too many requests" in shown[0] and "only the stations you have heard yourself" in shown[0]

    # asking again straight away is held off (the server asked to be left alone) -- no new request
    dash.pskreporter_button.setChecked(False); dash.pskreporter_button.setChecked(True)
    for _ in range(20):
        app.processEvents(); time.sleep(0.05)
    assert len(calls) == 1 and "not asking again" in dash.pskreporter_button.toolTip()

    # once the cool-down is over and the server is back, the notice clears and its spots join in
    dash._pskreporter_retry_after = 0
    pskreporter.fetch_pskreporter_spots = lambda c, d, s: [{"callsign": "G3ABC", "locator": "IO91", "frequency_hz": 14070000, "band": "20m",
        "mode": "PSK31", "snr": "3", "flow_start_seconds": now, "direction": d, "raw": {}}]
    dash.pskreporter_button.setChecked(False); dash.pskreporter_button.setChecked(True)
    for _ in range(60):
        app.processEvents(); time.sleep(0.05)
    assert dash.pskreporter_button.text() == "PSKReporter: ON" and sorted(m["tooltip"].splitlines()[0] for m in markers[-1]) == ["G3ABC", "K1ABC"]
    print("OK"); sys.stdout.flush(); os._exit(0)
''' % os.path.dirname(os.path.abspath(__file__)))


def test_server_outage_keeps_the_layer_on_with_a_notice(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), QT_QPA_PLATFORM="offscreen")
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-c", OUTAGE_SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr


PERSIST_SCRIPT = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, %r)
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    s = QSettings("IcomRadioApp", "RadioControl")
    s.setValue("operator_callsign", "KF0UQR"); s.setValue("operator_lat", 38.2); s.setValue("operator_lon", -97.5); s.sync()
    import ham_dashboard, heard_spots, pskreporter
    from workspace_host import WorkspaceHost
    now = int(time.time())
    host = WorkspaceHost(); dash = ham_dashboard.HamClockWindow(); host.add_dashboard(dash)
    dash._on_stations_heard([{"callsign": "W1AW", "locator": "FN31", "frequency_hz": 14075200, "snr": -12, "mode": "FT8", "epoch": now}])
    dash.close()                                                   # closing saves what was heard
    assert [s["callsign"] for s in heard_spots.load()] == ["W1AW"]

    host2 = WorkspaceHost(); dash2 = ham_dashboard.HamClockWindow(); host2.add_dashboard(dash2)     # "the next launch"
    dash2._pskreporter_direction = pskreporter.DIRECTION_HEARING
    assert [m["tooltip"].splitlines()[0] for m in dash2._build_pskreporter_markers()] == ["W1AW"]
    print("OK"); sys.stdout.flush(); os._exit(0)
''' % os.path.dirname(os.path.abspath(__file__)))


def test_heard_stations_survive_a_restart(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), QT_QPA_PLATFORM="offscreen")
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-c", PERSIST_SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr
