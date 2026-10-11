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
