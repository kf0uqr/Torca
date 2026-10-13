"""The VOACAP propagation overlay: the input VOACAP is given, reading its answer, painting it in the
map's projection, running the real program (when installed), and the dashboard's button / band / settings."""

import datetime
import math
import os
import pathlib
import subprocess
import sys
import textwrap

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

import voacap

# A real install, or an unpacked copy of Debian's voacapl packages named by this variable (for machines where
# it is not installed system-wide): <root>/usr/bin/voacapl and <root>/usr/share/voacapl/itshfbc
ROOT = os.environ.get("TORCA_TEST_VOACAPL_ROOT")


def real_voacap():
    if ROOT:
        return pathlib.Path(ROOT) / "usr/bin/voacapl", pathlib.Path(ROOT) / "usr/share/voacapl/itshfbc"
    if voacap.is_available():
        return voacap.voacapl_path(), voacap.source_dir()
    return None


needs_voacap = pytest.mark.skipif(real_voacap() is None, reason="voacapl is not installed (sudo apt install voacapl)")


def test_the_input_deck_matches_voacaps_own_layout():
    deck = voacap.build_deck(37.7, -97.3, 14.1, 18, 10, 93.4, power_w=100, required_snr=13, grid_size=90, noise=145)
    lines = deck.splitlines()
    assert lines[0] == "Model    :VOACAP" and lines[8] == "Transmit :%10s%10s%20s %5s" % ("37.70N", "97.30W", "Home", "Short")
    assert "Area     :    -180.0     180.0     -90.0      90.0" in lines and "Gridsize :   90    1" in lines
    assert "Months   :  10.00   0.00   0.00   0.00   0.00   0.00   0.00   0.00   0.00" in lines
    assert "Ssns     :     93      0      0      0      0      0      0      0      0" in lines
    assert "Hours    :     18      0      0      0      0      0      0      0      0" in lines
    assert "Freqs    : 14.100  0.000  0.000  0.000  0.000  0.000  0.000  0.000  0.000" in lines
    assert "System   :  145     3.000   90   13     3.000     0.100" in lines                 # noise, takeoff angle, reliability, required SNR
    assert any(l.startswith("Tx Ants  :[default /torca_flat  ]") and l.endswith("0.1000") for l in lines)   # 100 W = 0.1 kW
    assert all(len(l) < 90 for l in lines)
    south_west = voacap.build_deck(-33.9, -70.5, 7.1, 3, 1, 5, 1500).splitlines()[8]
    assert "33.90S" in south_west and "70.50W" in south_west


def fake_vg(rows_by_lat, nx=5):
    """A result file in VOACAP's fixed columns, `ny` rows (south to north) by `nx` columns."""
    ny = len(rows_by_lat)
    out = ["VOACAPL Version 16.1207W", " Home 100W 18ut", f" {nx} {ny}  Latitude Longitude   MUF ..."]
    for y, (lat, rels) in enumerate(rows_by_lat, start=1):
        for x, rel in enumerate(rels, start=1):
            lon = -180.0 + (x - 1) * 360.0 / (nx - 1)
            line = "%3d%3d%10.4f%10.4f%6.2f" % (x, y, lat, 0.0 if abs(lat) > 89 else lon, 20.0)
            line = line.ljust(98) + "%6.3f" % rel
            out.append(line.ljust(140))
    return "\n".join(out)


def test_reading_a_result_file():
    text = fake_vg([(-90.0, [0.1] * 5), (0.0, [0.0, 0.2, 0.9, 0.2, 0.0]), (90.0, [0.5] * 5)])
    grid = voacap.parse_output(text)
    assert grid.reliability.shape == (3, 5) and list(grid.lats) == [-90.0, 0.0, 90.0]
    assert list(grid.lons) == [-180.0, -90.0, 0.0, 90.0, 180.0]                  # not the placeholder zeros of the pole rows
    assert grid.reliability[1, 2] == 0.9 and grid.muf[1, 2] == 20.0
    with pytest.raises(ValueError):
        voacap.parse_output("nothing useful")


def test_sampling_is_smooth_between_grid_points():
    grid = voacap.parse_output(fake_vg([(-90.0, [0.0] * 5), (0.0, [0.0, 0.0, 1.0, 0.0, 0.0]), (90.0, [0.0] * 5)]))
    assert grid.sample(0.0, 0.0) == pytest.approx(1.0)
    assert grid.sample(0.0, 45.0) == pytest.approx(0.5)                          # halfway to the next point
    assert grid.sample(45.0, 0.0) == pytest.approx(0.5)
    assert grid.sample(0.0, -179.0) == pytest.approx(0.0)


def test_colours_run_from_nothing_through_red_and_yellow_to_green():
    r = voacap.color_for(np.array([0.0, 0.02, 0.1, 0.5, 1.0]))
    assert list(r[:2, 3]) == [0, 0]                                              # no chance: transparent
    assert r[2, 0] > r[2, 1] and r[2, 3] > 0                                     # low: reddish
    assert r[3, 0] > 200 and r[3, 1] > 200                                       # middling: yellow
    assert r[4, 1] > r[4, 0] and r[4, 3] > r[2, 3]                               # high: green, and more solid


def test_the_picture_is_in_the_maps_projection():
    # bright band at the equator only
    rows = [(lat, [1.0 if abs(lat) < 5 else 0.0] * 5) for lat in np.linspace(-90, 90, 37)]
    grid = voacap.parse_output(fake_vg(rows))
    width, height = 90, 90
    data = np.frombuffer(voacap.render_rgba(grid, width, height), dtype=np.uint8).reshape(height, width, 4)
    lit = np.where(data[:, 0, 3] > 0)[0]
    centre_row = (lit.min() + lit.max()) / 2
    assert abs(centre_row - (height - 1) / 2) <= 1                               # the equator is the middle row of a Mercator picture
    mid_lat_row = int(round(((1 - math.log(math.tan(math.radians(60)) + 1 / math.cos(math.radians(60))) / math.pi) / 2) * height))
    assert data[mid_lat_row, 0, 3] == 0                                          # 60 degrees north is dark
    assert len(voacap.render_rgba(grid)) == 720 * 360 * 4


def test_the_cache_key_changes_with_what_matters():
    when = datetime.datetime(2026, 10, 13, 18, 20, tzinfo=datetime.timezone.utc)
    key, params = voacap.current_params("20m", "CW", 100, 3.0, "Residential", 37.7, -97.3, 93.4, now=when)
    assert params["freq_mhz"] == 14.1 and params["utc_hour"] == 18 and params["month"] == 10 and params["required_snr"] == 30
    assert voacap.current_params("20m", "CW", 100, 3.0, "Residential", 37.7, -97.3, 93.4, now=when.replace(minute=50))[0] == key   # same hour
    assert voacap.current_params("20m", "CW", 100, 3.0, "Residential", 37.7, -97.3, 93.4, now=when.replace(hour=19))[0] != key
    assert voacap.current_params("17m", "CW", 100, 3.0, "Residential", 37.7, -97.3, 93.4, now=when)[0] != key
    assert voacap.current_params("20m", "SSB voice", 100, 3.0, "Residential", 37.7, -97.3, 93.4, now=when)[0] != key


def test_without_voacapl_it_says_how_to_install_it(monkeypatch):
    monkeypatch.setattr(voacap.shutil, "which", lambda name: None)
    assert not voacap.is_available()
    with pytest.raises(RuntimeError) as caught:
        voacap.run_area(37.7, -97.3, 14.1, 18, 10, 93)
    assert "sudo apt install voacapl" in str(caught.value)


@needs_voacap
def test_a_real_run_gives_sensible_reliability(tmp_path, monkeypatch):
    binary, source = real_voacap()
    monkeypatch.setattr(voacap, "SOURCE_DIRS", (str(source),))
    grid = voacap.run_area(37.7, -97.3, 14.1, 18, 10, 93, power_w=100, gain_dbi=3.0, required_snr=13, grid_size=45,
                           work=tmp_path, binary=binary)
    assert grid.reliability.shape == (45, 45) and 0.0 <= grid.reliability.min() and grid.reliability.max() <= 1.0
    assert grid.sample(41.0, -88.0) > 0.5                                         # a thousand km away, 20 m, afternoon: a solid path
    weak = voacap.run_area(37.7, -97.3, 14.1, 18, 10, 93, power_w=100, gain_dbi=3.0, required_snr=48, grid_size=45,
                           work=tmp_path, binary=binary)
    assert weak.reliability.mean() < grid.reliability.mean()                      # SSB needs a stronger signal than FT8
    loud = voacap.run_area(37.7, -97.3, 14.1, 18, 10, 93, power_w=1000, gain_dbi=3.0, required_snr=48, grid_size=45,
                           work=tmp_path, binary=binary)
    assert loud.reliability.mean() > weak.reliability.mean()                      # ten times the power helps


# ---- the dashboard ------------------------------------------------------------------------------

SCRIPT = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, %r)
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication([])
    s = QSettings("IcomRadioApp", "RadioControl")
    s.setValue("operator_callsign", "KF0UQR"); s.setValue("operator_lat", 38.2); s.setValue("operator_lon", -97.5); s.sync()
    import numpy as np
    import ham_dashboard, voacap
    from workspace_host import WorkspaceHost
    host = WorkspaceHost(); dash = ham_dashboard.HamClockWindow(); host.add_dashboard(dash); host.show()

    runs, boxes = [], []
    def fake_run(**params):
        runs.append(params)
        lats, lons = np.linspace(-90, 90, 5), np.linspace(-180, 180, 5)
        return voacap.Grid(lats, lons, np.full((5, 5), 0.8), np.full((5, 5), 20.0))
    voacap.run_area = fake_run
    QMessageBox.information = staticmethod(lambda *a, **k: boxes.append(a[2]))
    QMessageBox.warning = staticmethod(lambda *a, **k: boxes.append(a[2]))
    def settle(cond, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents(); time.sleep(0.02)
            if cond(): return True
        return False

    # no VOACAP installed: it explains and stays off
    voacap.is_available = lambda: False
    dash.voacap_button.setChecked(True)
    assert not dash.voacap_button.isChecked() and "sudo apt install voacapl" in boxes[-1]
    assert dash.map_widget._voacap_image is None

    voacap.is_available = lambda: True
    dash._on_solar_data({"sunspot_number": 93})
    dash.voacap_band_combo.setCurrentIndex(dash.voacap_band_combo.findData("20m"))
    dash.voacap_button.setChecked(True)
    assert settle(lambda: dash.map_widget._voacap_image is not None), boxes
    assert len(runs) == 1 and runs[0]["freq_mhz"] == 14.1 and runs[0]["ssn"] == 93.0 and runs[0]["lat"] == 38.2
    assert "20m" in dash.map_widget._voacap_label and "UTC" in dash.map_widget._voacap_label and dash.voacap_button.text() == "VOACAP: ON"
    dash.map_widget.resize(900, 450); dash.map_widget.grab()             # the overlay and its key paint

    # another band: a new run; back again: from the cache, no new run
    dash.voacap_band_combo.setCurrentIndex(dash.voacap_band_combo.findData("40m"))
    assert settle(lambda: len(runs) == 2) and runs[1]["freq_mhz"] == 7.1
    assert settle(lambda: "40m" in dash.map_widget._voacap_label)
    dash.voacap_band_combo.setCurrentIndex(dash.voacap_band_combo.findData("20m"))
    assert settle(lambda: "20m" in dash.map_widget._voacap_label) and len(runs) == 2
    assert s.value("voacap_band") == "20m"

    # settings change the request
    dash._voacap_power_w, dash._voacap_mode = 500, "CW"
    dash._update_voacap()
    assert settle(lambda: len(runs) == 3) and runs[2]["power_w"] == 500 and runs[2]["required_snr"] == 30

    # off removes it
    dash.voacap_button.setChecked(False)
    assert dash.map_widget._voacap_image is None and not dash._voacap_hour_timer.isActive()

    # a failed run switches it off with the reason
    def broken(**params): raise RuntimeError("VOACAP failed: boom")
    voacap.run_area = broken
    dash._voacap_cache.clear()
    dash.voacap_button.setChecked(True)
    assert settle(lambda: not dash.voacap_button.isChecked()) and "boom" in boxes[-1]

    # the settings dialog round-trips
    d = ham_dashboard.VoacapSettingsDialog("CW", 250, 5.5, "Rural")
    assert d.result() == ("CW", 250, 5.5, "Rural")
    assert dash.voacap_button in dash.filters_sidebar.content_layout().parentWidget().findChildren(type(dash.voacap_button))
    print("OK"); sys.stdout.flush(); os._exit(0)
''' % os.path.dirname(os.path.abspath(__file__)))


def test_dashboard_overlay_button_band_cache_and_settings(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), QT_QPA_PLATFORM="offscreen")
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr
