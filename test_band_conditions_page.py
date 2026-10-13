"""The Band Conditions tab: VHF conditions parsed from the N0NBH feed and shown, and the NASA SDO
pictures of the Sun fetched, shown, aged, and flagged when stale."""

import io
import os
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

import band_conditions_page as bcp
import solar_data

HAMQSL = b"""<?xml version="1.0" encoding="UTF-8" ?>
<solar><solardata>
<updated> 13 Oct 2026 0815 GMT</updated><solarflux>123</solarflux><aindex> 7</aindex><kindex> 3</kindex><xray>B9.3</xray>
<aurora> 1</aurora><latdegree>67.5</latdegree><solarwind>537.0</solarwind><magneticfield>  1.1</magneticfield>
<calculatedconditions>
<band name="80m-40m" time="day">Poor</band><band name="30m-20m" time="day">Good</band>
<band name="80m-40m" time="night">Fair</band><band name="30m-20m" time="night">Good</band>
</calculatedconditions>
<calculatedvhfconditions>
<phenomenon name="vhf-aurora" location="northern_hemi">Band Closed</phenomenon>
<phenomenon name="E-Skip" location="europe">Fair</phenomenon>
<phenomenon name="E-Skip" location="north_america">Good</phenomenon>
<phenomenon name="E-Skip" location="europe_6m">Band Closed</phenomenon>
<phenomenon name="E-Skip" location="europe_4m">Poor</phenomenon>
</calculatedvhfconditions></solardata></solar>"""


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def jpeg(color="#aa5500", size=64):
    image = QImage(size, size, QImage.Format_RGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "JPG")
    return bytes(data)


def hf_table():
    table = QTableWidget(4, 2)
    for r in range(4):
        for c in range(2):
            table.setItem(r, c, QTableWidgetItem("--"))
    return table


def test_vhf_conditions_are_read_from_the_feed(monkeypatch):
    class Page:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return HAMQSL
    monkeypatch.setattr(solar_data.urllib.request, "urlopen", lambda url, timeout=0: Page())
    conditions, error = solar_data.fetch_band_conditions()
    assert error is None
    assert conditions[("80m-40m", "day")] == "Poor"                                     # HF still parsed as before
    assert conditions[("vhf-aurora", "northern_hemi")] == "Band Closed" and conditions[("E-Skip", "north_america")] == "Good"
    assert conditions[("E-Skip", "europe_4m")] == "Poor"
    assert conditions[("extra", "solarwind")] == "537.0" and conditions[("extra", "latdegree")] == "67.5"


def test_vhf_table_and_space_weather_line(app):
    page = bcp.BandConditionsPage(hf_table())
    page.update_vhf({("vhf-aurora", "northern_hemi"): "Band Closed", ("E-Skip", "north_america"): "Good", ("E-Skip", "europe"): "Fair",
                     ("extra", "aurora"): "1", ("extra", "latdegree"): "67.5", ("extra", "solarwind"): "537.0",
                     ("extra", "magneticfield"): "1.1", ("extra", "kindex"): "3", ("extra", "xray"): "B9.3"})
    cells = {page.vhf_table.verticalHeaderItem(r).text(): page.vhf_table.item(r, 0) for r in range(page.vhf_table.rowCount())}
    assert cells["2 m E-skip, North America"].text() == "Good" and cells["2 m E-skip, North America"].background().color() == QColor(40, 130, 60)
    assert cells["2 m E-skip, Europe"].text() == "Fair" and cells["6 m E-skip, Europe"].text() == "--"      # not in this data
    text = page.space_weather_label.text()
    assert "Aurora index 1 (reaching 67.5° latitude)" in text and "solar wind 537.0 km/s" in text and "Bz 1.1 nT" in text and "X-ray B9.3" in text


def test_age_text():
    assert [bcp.age_text(s) for s in (5, 600, 7200, 90_000, 21 * 86400)] == ["just now", "10 min ago", "2 h ago", "25 h ago", "21 days ago"]
    assert bcp.parse_last_modified(formatdate(1_700_000_000, usegmt=True)) == 1_700_000_000 and bcp.parse_last_modified("garbage") is None


class _Sun:
    """A stand-in for Helioviewer AND NASA's own static pictures. `age_seconds`: how old the newest image
    is; `helioviewer=False` makes Helioviewer fail so the NASA fallback is used; `missing`: view codes
    (or Helioviewer source ids) whose pictures cannot be had from either."""
    def __init__(self, age_seconds=600, missing=(), helioviewer=True):
        outer = self
        outer.requests, outer.age, outer.missing, outer.helioviewer = [], age_seconds, set(missing), helioviewer

        class Handler(BaseHTTPRequestHandler):
            def _send(self, body, ctype, extra=None):
                self.send_response(200)
                self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers(); self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(self.path)
                path = self.path
                if path.startswith("/hv/"):
                    if not outer.helioviewer:
                        self.send_response(503); self.end_headers(); return
                    source = path.split("sourceId=")[1].split("&")[0] if "sourceId=" in path else path.split("layers=[")[1].split(",")[0]
                    if any(f"={m}" in path or f"[{m}," in path for m in outer.missing):
                        self.send_response(404); self.end_headers(); return
                    if "/getClosestImage/" in path:
                        when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - outer.age))
                        self._send(('{"id":"1","date":"%s","name":"AIA"}' % when).encode(), "application/json")
                    else:
                        assert "date=" in path and "width=512" in path
                        self._send(jpeg(), "image/png")
                    return
                if any(code in path for code in outer.missing):
                    self.send_response(404); self.end_headers(); return
                self._send(jpeg(), "image/jpeg", {"Last-Modified": formatdate(time.time() - outer.age, usegmt=True)})
            def log_message(self, *a): pass
        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.url, self.hv = base + "/latest_512_{code}.jpg", base + "/hv"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def point_at(monkeypatch, sun):
    monkeypatch.setattr(bcp, "SDO_URL", sun.url)
    monkeypatch.setattr(bcp, "HELIOVIEWER_URL", sun.hv)


def wait(app, condition, seconds=15.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def loaded(page, n=len(bcp.SDO_VIEWS)):
    return len(page._pixmaps) + len(page._errors) >= n and page._fetcher.isFinished()


def test_one_picture_is_shown_fetched_and_dated(app, monkeypatch):
    sun = _Sun(age_seconds=600)
    point_at(monkeypatch, sun)
    page = bcp.BandConditionsPage(hf_table())
    try:
        page.resize(900, 900); page.show()                               # showing it fetches
        assert wait(app, lambda: loaded(page))
        app.processEvents()
        assert page.title_label.text() == "193 Å  corona   (1 of 9)" and not page.picture.pixmap().isNull()
        assert "Taken 10 min ago, from Helioviewer." in page.detail_label.text() and "⚠" not in page.detail_label.text()
        closest = [r.split("sourceId=")[1] for r in sun.requests if "getClosestImage" in r]
        assert closest[0] == "11" and sorted(closest) == sorted(str(i) for i in bcp.HELIOVIEWER_SOURCES.values())   # the one on screen first, then all the rest
        assert sum("takeScreenshot" in r for r in sun.requests) == 9
        assert not any(r.startswith("/latest_") for r in sun.requests)                        # NASA's static files are only a fallback
        before = len(sun.requests)
        page.refresh_sun()                                                # fresh enough: no new requests
        assert len(sun.requests) == before
        page.refresh_button.click()                                       # the button always fetches
        assert wait(app, lambda: len(sun.requests) == before + 18)
    finally:
        page.hide(); page._fetcher.wait(5000); sun.close()


def test_the_arrows_step_through_every_picture_and_wrap_round(app, monkeypatch):
    sun = _Sun()
    point_at(monkeypatch, sun)
    page = bcp.BandConditionsPage(hf_table())
    try:
        page.resize(900, 900); page.show()
        assert wait(app, lambda: loaded(page))
        seen = [page.title_label.text()]
        for _ in range(len(bcp.SDO_VIEWS)):
            page.next_button.click()
            seen.append(page.title_label.text())
            assert not page.picture.pixmap().isNull()                      # already fetched: no waiting between pictures
        assert seen[0] == seen[-1] and len(set(seen)) == 9 and seen[1].endswith("(2 of 9)")
        page.prev_button.click()
        assert page.title_label.text().endswith("(9 of 9)")                # backwards from the first wraps to the last
        assert bcp.SDO_VIEWS[page._index][1] in page.title_label.text()
        assert bcp.SDO_VIEWS[page._index][2] in page.detail_label.text()    # what the picture shows is explained
    finally:
        page.hide(); page._fetcher.wait(5000); sun.close()


def test_an_old_picture_is_flagged_not_passed_off_as_current(app, monkeypatch):
    sun = _Sun(age_seconds=21 * 86400)
    point_at(monkeypatch, sun)
    page = bcp.BandConditionsPage(hf_table())
    try:
        page.show()
        assert wait(app, lambda: loaded(page))
        app.processEvents()
        assert "21 days ago" in page.detail_label.text() and "⚠" in page.detail_label.text() and "may not show the Sun as it is now" in page.detail_label.text()
    finally:
        page.hide(); page._fetcher.wait(5000); sun.close()


def test_the_hmi_pictures_may_be_hours_old_before_they_are_flagged(app, monkeypatch):
    sun = _Sun(age_seconds=5 * 3600)                                       # AIA 5 h old is stale; HMI 5 h old is normal
    point_at(monkeypatch, sun)
    page = bcp.BandConditionsPage(hf_table())
    try:
        page.show()
        assert wait(app, lambda: loaded(page))
        app.processEvents()
        assert "⚠" in page.detail_label.text()                             # the corona picture
        page.step(1); page.step(1)                                         # visible light
        assert page.title_label.text().startswith("Visible light") and "5 h ago" in page.detail_label.text() and "⚠" not in page.detail_label.text()
    finally:
        page.hide(); page._fetcher.wait(5000); sun.close()


def test_when_helioviewer_is_down_nasas_static_pictures_are_used(app, monkeypatch):
    sun = _Sun(age_seconds=1200, helioviewer=False)
    point_at(monkeypatch, sun)
    page = bcp.BandConditionsPage(hf_table())
    try:
        page.show()
        assert wait(app, lambda: loaded(page))
        app.processEvents()
        assert set(page._sources.values()) == {"NASA"} and "Taken 20 min ago, from NASA." in page.detail_label.text()
    finally:
        page.hide(); page._fetcher.wait(5000); sun.close()


def test_a_missing_picture_is_reported_and_the_others_still_show(app, monkeypatch):
    sun = _Sun(missing=("HMIB", "19"))
    point_at(monkeypatch, sun)
    page = bcp.BandConditionsPage(hf_table())
    try:
        page.show()
        assert wait(app, lambda: loaded(page))
        app.processEvents()
        assert len(page._pixmaps) == 8 and "HMIB" in page._errors
        page.step(1); page.step(1); page.step(1)                           # the magnetogram
        assert page.title_label.text().startswith("Magnetogram") and page.picture.text() == "unavailable"
        assert "Couldn't fetch it" in page.detail_label.text()
        page.step(1)
        assert not page.picture.pixmap().isNull()                          # the next one is fine
    finally:
        page.hide(); page._fetcher.wait(5000); sun.close()


def test_the_dashboards_band_conditions_tab_is_this_page(app, tmp_path):
    import subprocess, sys, textwrap
    script = textwrap.dedent('''
        import os, sys
        sys.path.insert(0, %r)
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        s = QSettings("IcomRadioApp", "RadioControl")
        s.setValue("operator_callsign", "KF0UQR"); s.setValue("operator_lat", 38.2); s.setValue("operator_lon", -97.5); s.sync()
        import band_conditions_page, ham_dashboard
        band_conditions_page.SdoFetcher.run = lambda self: None            # no network
        from workspace_host import WorkspaceHost
        host = WorkspaceHost(); dash = ham_dashboard.HamClockWindow(); host.add_dashboard(dash); host.show()
        page = dict(dash._data_pages)["Band Conditions"]
        assert isinstance(page, band_conditions_page.BandConditionsPage) and page.hf_table is dash.band_conditions_table
        dash._on_solar_data({"band_conditions": {("80m-40m", "day"): "Good", ("E-Skip", "europe"): "Fair"}})
        assert dash.band_conditions_table.item(0, 0).text() == "Good" and page.vhf_table.item(2, 0).text() == "Fair"
        host.open_in(page, 0)
        for _ in range(10): app.processEvents()
        assert page.isVisible()
        print("OK"); sys.stdout.flush(); os._exit(0)
    ''' % os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"), XDG_CACHE_HOME=str(tmp_path / "cache"), QT_QPA_PLATFORM="offscreen")
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr
