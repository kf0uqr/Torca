"""RadioWindow's vertical layout: height beyond what the controls need
goes to the scope, waterfall and meters; the controls keep their size.

Builds a real RadioWindow offscreen with RadioWorker.start stubbed (no
radio, no connection thread)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import radio_worker
import main_window
from satellite_session import SatelliteSession
from workspace_host import WorkspaceHost
import workspace_host

DETAILS = {"radio_model": "IC-9700", "connection_type": "network", "host": "192.0.2.1",
           "role": "non_sat", "username": "u", "password": "p", "port": 50001}


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def radio_window(app, monkeypatch):
    monkeypatch.setattr(radio_worker.RadioWorker, "start", lambda self, *a, **k: None)
    window = main_window.RadioWindow(DETAILS, SatelliteSession())
    yield window
    window.close()


def _settle(app):
    for _ in range(8):
        app.processEvents()


def _heights(window):
    return {
        "spectrum": window.spectrum_widget.height(),
        "waterfall": window.waterfall_widget.height(),
        "meter1": window.meters_row.itemAt(0).widget().height(),
        "meter2": window.meters_row_2.itemAt(0).widget().height(),
        # controls: must NOT grow
        "band_plan": window.band_plan_widget.height(),
        "tuning_knob": window.tuning_knob.height(),
        "ptt": window.ptt_button.height(),
        "status": window.status_label.height(),
    }


GROWS = ("spectrum", "waterfall", "meter1", "meter2")
FIXED = ("band_plan", "tuning_knob", "ptt", "status")


def test_extra_height_goes_to_scope_waterfall_and_meters_not_to_controls(app, radio_window):
    radio_window.show()
    minimum_height = radio_window.minimumSizeHint().height()
    radio_window.resize(1100, minimum_height)
    _settle(app)
    small = _heights(radio_window)
    radio_window.resize(1100, minimum_height + 500)
    _settle(app)
    big = _heights(radio_window)
    for name in GROWS:
        assert big[name] > small[name] + 10, (name, small[name], big[name])
    for name in FIXED:
        assert big[name] == small[name], (name, small[name], big[name])


def test_scope_and_waterfall_get_the_larger_share_than_meters(app, radio_window):
    radio_window.show()
    radio_window.resize(1100, 1400)
    _settle(app)
    h = _heights(radio_window)
    assert h["waterfall"] > h["spectrum"] > h["meter1"]
    assert abs(h["meter1"] - h["meter2"]) <= 1   # integer rounding of two equal shares


def test_displays_keep_their_small_minimum_sizes(app, radio_window):
    radio_window.show()
    radio_window.resize(1100, radio_window.minimumSizeHint().height())
    _settle(app)
    h = _heights(radio_window)
    assert h["spectrum"] >= main_window._MIN_SPECTRUM_HEIGHT
    assert h["waterfall"] >= main_window._MIN_WATERFALL_HEIGHT
    assert h["meter1"] >= main_window._MIN_METER_HEIGHT


def test_radio_window_is_short_enough_for_a_maximized_1080p_workspace(app, radio_window):
    # A workspace's scroll area never shrinks its content below the content's
    # minimum size, so the minimum height decides whether a scroll bar appears.
    # A maximized 1080p window leaves ~940px for the radio tab.
    assert radio_window.minimumSizeHint().height() <= 910


def test_it_also_grows_when_docked_in_a_workspace(app, radio_window):
    host = WorkspaceHost()
    try:
        host.resize(1300, 900)
        host.show()
        host.present(radio_window)
        _settle(app)
        before = _heights(radio_window)
        host.resize(1300, 1500)
        _settle(app)
        after = _heights(radio_window)
        assert after["spectrum"] > before["spectrum"] and after["waterfall"] > before["waterfall"]
        assert all(after[n] == before[n] for n in FIXED)
    finally:
        host._closing = True
        workspace_host._HOST = None
        host.close()


def _dock_in_second_workspace(app, radio_window, size):
    """The real-world case: dashboard-like tab in workspace 1, the radio in
    workspace 2, in a window maximized to `size`."""
    from PySide6.QtWidgets import QWidget
    host = WorkspaceHost()
    host.add_workspace(0, "right")
    stand_in = QWidget()
    stand_in.setWindowTitle("Ham Dashboard")
    host.add_window(stand_in, closable=False, pane_index=0)
    host.resize(*size)
    host.show()
    host.present(radio_window)
    _settle(app)
    return host, host._entries[radio_window].page


def _close(host):
    host._closing = True
    workspace_host._HOST = None
    host.close()


@pytest.mark.parametrize("size", [(1920, 1010), (1920, 960), (2560, 1400)])
def test_no_scroll_bars_when_maximized_at_a_decent_resolution(app, radio_window, size):
    host, page = _dock_in_second_workspace(app, radio_window, size)
    try:
        assert host.pane_of(radio_window) == 1
        assert not page.verticalScrollBar().isVisible(), "vertical scroll bar appeared"
        assert not page.horizontalScrollBar().isVisible(), "horizontal scroll bar appeared"
        # and the bottom-most controls really are on screen
        bottom = radio_window.status_label.mapTo(page.viewport(), radio_window.status_label.rect().bottomLeft()).y()
        assert bottom <= page.viewport().height()
    finally:
        _close(host)


def test_knobs_sit_under_the_buttons_and_left_of_the_passband(app, radio_window):
    radio_window.show()
    radio_window.resize(1100, 1000)
    _settle(app)

    def top_left(widget):
        return widget.mapTo(radio_window, widget.rect().topLeft())

    mode_row_bottom = max(widget.mapTo(radio_window, widget.rect().bottomLeft()).y()
                          for widget in radio_window.findChildren(main_window.QPushButton)
                          if widget.text() in ("Digital Mode", "NR", "NB", "Split") and widget.parent() is radio_window)
    rit, vfo, passband = (top_left(radio_window.rit_knob), top_left(radio_window.tuning_knob),
                          top_left(radio_window.filter_shape_widget))
    assert rit.x() < vfo.x() < passband.x()                   # RIT knob, VFO knob, then the passband
    assert vfo.y() >= mode_row_bottom - 4                     # knobs are BELOW the mode/AGC buttons ...
    assert passband.y() >= mode_row_bottom - 4                # ... and so is the passband
    assert radio_window.filter_shape_widget.height() <= main_window._MAX_PASSBAND_HEIGHT


def test_mode_controls_span_the_full_width_above_the_knobs(app, radio_window):
    radio_window.show()
    radio_window.resize(1100, 1000)
    _settle(app)
    last_button = max((w for w in radio_window.findChildren(main_window.QPushButton)
                       if w.isVisible() and w.parent() is radio_window and w.text() == "Split"),
                      key=lambda w: w.x(), default=None)
    assert last_button is not None
    # the Split button (right end of the button row) reaches the right-hand side of the window,
    # i.e. the row is no longer confined to the left of the knobs
    assert last_button.geometry().right() > radio_window.width() * 0.8
