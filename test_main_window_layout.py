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
           "username": "u", "password": "p", "port": 50001}


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


def test_controls_under_the_buttons_run_rit_then_pbt_then_tx_buttons_then_vfo_knob(app, radio_window):
    radio_window.show()
    radio_window.resize(1100, 1000)
    _settle(app)

    def top_left(widget):
        return widget.mapTo(radio_window, widget.rect().topLeft())

    mode_row_bottom = max(widget.mapTo(radio_window, widget.rect().bottomLeft()).y()
                          for widget in radio_window.findChildren(main_window.QPushButton)
                          if widget.text() in ("Digital Mode", "NR", "NB", "Split") and widget.parent() is radio_window)
    rit = top_left(radio_window.rit_knob)
    pbt = top_left(radio_window.filter_shape_widget)
    ptt = top_left(radio_window.ptt_button)
    tune = top_left(radio_window.tune_button)
    swr = top_left(radio_window.swr_protection_button)
    vfo = top_left(radio_window.tuning_knob)
    step = top_left(radio_window.step_combo)

    # left to right: RIT knob | PBT group | PTT/Tune/SWR buttons | VFO knob + tune step
    assert rit.x() < pbt.x() < ptt.x() < vfo.x()
    assert abs(ptt.x() - tune.x()) <= 2 and abs(ptt.x() - swr.x()) <= 2        # the three buttons share a column
    assert ptt.y() < tune.y() < swr.y()                                        # in this order, top to bottom
    assert abs((vfo.x() + radio_window.tuning_knob.width() // 2)
               - (step.x() + radio_window.step_combo.width() // 2)) <= 3       # tune step is centred under the knob
    assert vfo.y() < step.y()
    # everything is below the mode/AGC buttons, and the passband stays small
    for point in (rit, pbt, ptt, vfo):
        assert point.y() >= mode_row_bottom - 4
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


# ---- VFO knob / PTT column widths follow the mode-button row ----

def _rightmost_mode_button_width(window):
    buttons = [b for b in window._layout_widgets(window._mode_buttons_row) if b.isVisible()]
    return max(buttons, key=lambda b: b.x()).width()


@pytest.mark.parametrize("width", [1000, 1100, 1300])
def test_vfo_knob_and_tx_buttons_are_as_wide_as_the_mode_buttons_above(app, radio_window, width):
    radio_window.show()
    radio_window.resize(width, 1000)
    _settle(app)
    reference = _rightmost_mode_button_width(radio_window)
    # the knob matches the button above it, within its own size limits (the buttons themselves are not capped)
    expected_knob = min(max(reference, main_window._VFO_KNOB_MIN_DIAMETER), main_window._VFO_KNOB_MAX_DIAMETER)
    assert radio_window.tuning_knob.width() == radio_window.tuning_knob.height() == expected_knob
    if width <= 1100:
        assert expected_knob == reference            # in the normal range, an exact match
    for button in (radio_window.ptt_button, radio_window.tune_button, radio_window.swr_protection_button):
        assert button.width() == reference, button.text()


def test_knob_follows_the_window_when_it_is_resized(app, radio_window):
    radio_window.show()
    radio_window.resize(1000, 1000)
    _settle(app)
    narrow = radio_window.tuning_knob.width()
    radio_window.resize(1400, 1000)
    _settle(app)
    wider = radio_window.tuning_knob.width()
    assert wider > narrow
    assert wider == min(_rightmost_mode_button_width(radio_window), main_window._VFO_KNOB_MAX_DIAMETER)


def test_knob_diameter_is_capped_on_a_very_wide_window(app, radio_window):
    radio_window.show()
    radio_window.resize(2600, 1000)
    _settle(app)
    assert radio_window.tuning_knob.width() == main_window._VFO_KNOB_MAX_DIAMETER
    assert radio_window.ptt_button.width() == _rightmost_mode_button_width(radio_window)   # the buttons keep matching


def test_wider_controls_still_leave_no_scroll_bars_at_1080p(app, radio_window):
    host, page = _dock_in_second_workspace(app, radio_window, (1920, 1010))
    try:
        assert not page.verticalScrollBar().isVisible() and not page.horizontalScrollBar().isVisible()
        assert radio_window.minimumSizeHint().height() <= 860
    finally:
        _close(host)


# ---- column alignment under the mode buttons ----

def _span(window, widget):
    left = widget.mapTo(window, widget.rect().topLeft()).x()
    return left, left + widget.width()


def _mode_buttons(window):
    return sorted((b for b in window._layout_widgets(window._mode_buttons_row) if b.isVisible()), key=lambda b: b.x())


@pytest.mark.parametrize("width", [1000, 1100, 1400])
def test_control_groups_line_up_under_the_mode_buttons(app, radio_window, width):
    radio_window.show()
    radio_window.resize(width, 1000)
    _settle(app)
    digital, nr, nb, vfo_a, split = _mode_buttons(radio_window)[:5]
    rit, pbt, tx, vfo = (_span(radio_window, c) for c in radio_window._tuning_columns)
    tolerance = 2
    assert abs(rit[0] - _span(radio_window, digital)[0]) <= tolerance and abs(rit[1] - _span(radio_window, digital)[1]) <= tolerance
    assert abs(pbt[0] - _span(radio_window, nr)[0]) <= tolerance and abs(pbt[1] - _span(radio_window, nb)[1]) <= tolerance   # PBT = NR + NB
    assert abs(tx[0] - _span(radio_window, vfo_a)[0]) <= tolerance and abs(tx[1] - _span(radio_window, vfo_a)[1]) <= tolerance
    assert abs(vfo[0] - _span(radio_window, split)[0]) <= tolerance and abs(vfo[1] - _span(radio_window, split)[1]) <= tolerance


def test_ptt_buttons_fill_their_column_and_the_passband_fills_its_span(app, radio_window):
    radio_window.show()
    radio_window.resize(1100, 1000)
    _settle(app)
    _digital, nr, nb, vfo_a, split = _mode_buttons(radio_window)[:5]
    for button in (radio_window.ptt_button, radio_window.tune_button, radio_window.swr_protection_button):
        assert abs(_span(radio_window, button)[0] - _span(radio_window, vfo_a)[0]) <= 2, button.text()
        assert abs(button.width() - vfo_a.width()) <= 2, button.text()
    passband = _span(radio_window, radio_window.filter_shape_widget)
    assert abs(passband[0] - _span(radio_window, nr)[0]) <= 2 and abs(passband[1] - _span(radio_window, nb)[1]) <= 2
    knob = _span(radio_window, radio_window.tuning_knob)
    assert abs(knob[0] - _span(radio_window, split)[0]) <= 2


def test_window_can_still_be_made_narrower_after_being_wide(app, radio_window):
    # widths are applied as stretch proportions, not fixed sizes, so the layout
    # must not pin the window at the widest size it has been
    radio_window.show()
    radio_window.resize(1600, 1000)
    _settle(app)
    wide_min = radio_window.minimumSizeHint().width()
    radio_window.resize(radio_window.minimumSizeHint().width(), 1000)
    _settle(app)
    assert radio_window.width() <= wide_min + 2
    assert radio_window.minimumSizeHint().width() <= 1000



# ---- band buttons wrap instead of forcing a horizontal scroll bar ----

def _window_for(model, monkeypatch):
    monkeypatch.setattr(radio_worker.RadioWorker, "start", lambda self, *a, **k: None)
    return main_window.RadioWindow(dict(DETAILS, radio_model=model), SatelliteSession())


@pytest.mark.parametrize("model, band_count", [("IC-9700", 3), ("IC-7300", 11), ("IC-705", 13)])
def test_every_radio_fits_a_half_screen_workspace_without_a_horizontal_scroll_bar(app, monkeypatch, model, band_count):
    window = _window_for(model, monkeypatch)
    try:
        assert len(window.band_buttons) == band_count
        host, page = _dock_in_second_workspace(app, window, (1920, 1010))
        try:
            assert not page.horizontalScrollBar().isVisible(), f"{model}: horizontal scroll bar"
            assert not page.verticalScrollBar().isVisible(), f"{model}: vertical scroll bar"
            assert window.minimumSizeHint().width() <= 940, window.minimumSizeHint().width()
        finally:
            _close(host)
    finally:
        window.close()


def test_ic705_band_buttons_wrap_in_balanced_rows_when_narrow_and_use_one_row_when_wide(app, monkeypatch):
    window = _window_for("IC-705", monkeypatch)
    try:
        window.show()
        window.resize(940, 1000)
        _settle(app)
        assert window.band_buttons_row.grid_for_width(window.band_buttons_row.geometry().width()) == (7, 2)
        assert all(b.isVisible() for b in window.band_buttons)
        window.resize(1700, 1000)
        _settle(app)
        assert window.band_buttons_row.grid_for_width(window.band_buttons_row.geometry().width()) == (13, 1)
    finally:
        window.close()


def test_band_buttons_still_select_their_band_after_wrapping(app, monkeypatch):
    window = _window_for("IC-705", monkeypatch)
    try:
        picked = []
        window._on_band_selected = lambda label, freq: picked.append(label)
        for button in window.band_buttons:
            button.setEnabled(True)
        window.band_buttons[5].click()
        assert picked == [window.band_buttons[5].text()]
    finally:
        window.close()


# ---- tab titles ----

def _titled_window(monkeypatch, model="IC-9700", role="non_sat", host="192.0.2.1"):
    monkeypatch.setattr(radio_worker.RadioWorker, "start", lambda self, *a, **k: None)
    window = main_window.RadioWindow(dict(DETAILS, radio_model=model, host=host), SatelliteSession())
    if role != "non_sat":
        window.set_satellite_role(role)         # only a Satellite tool does this in the app
    return window


def test_radio_window_title_is_the_model_and_gains_a_role_only_while_in_a_satellite_role(app, monkeypatch):
    window = _titled_window(monkeypatch)
    try:
        assert window.windowTitle() == "IC-9700"                       # no role when a radio connects
        window.set_satellite_role("uplink")
        assert window.windowTitle() == "IC-9700 - Satellite Uplink"
        window.set_satellite_role("non_sat")
        assert window.windowTitle() == "IC-9700"
    finally:
        window.close()
    for role, label in (("full_duplex", "Satellite Full Duplex"), ("downlink", "Satellite Downlink"), ("uplink", "Satellite Uplink")):
        window = _titled_window(monkeypatch, role=role)
        try:
            assert window.windowTitle() == f"IC-9700 - {label}"
        finally:
            window.close()


def test_tool_windows_are_titled_tool_name_dash_model(app, monkeypatch):
    window = _titled_window(monkeypatch, model="IC-7300")
    try:
        built = {
            "CW Tool": window.ensure_cw_tool_window(),
            "APRS Tool": window.ensure_aprs_tool_window(),
            "SSTV Tool": window.ensure_sstv_tool_window(),
            "RTTY Tool": window.ensure_rtty_tool_window(),
            "PSK31 Tool": window.ensure_psk31_tool_window(),
            "FT8 Tool": window.ensure_ft8_tool_window(),
            "WSPR Tool": window.ensure_wspr_tool_window(),
            "Recording Tool": window.ensure_recording_tool_window(),
            "Memories": window.ensure_memories_window(),
        }
        for name, tool in built.items():
            assert tool.windowTitle() == f"{name} - IC-7300", name       # model, not the IP address
            assert "192.0.2.1" not in tool.windowTitle()
    finally:
        window.close()


def test_two_radios_of_the_same_model_are_told_apart_and_it_reverts_when_one_closes(app, monkeypatch):
    first = _titled_window(monkeypatch, host="192.0.2.1")
    first_cw = first.ensure_cw_tool_window()                              # built BEFORE the second radio exists
    assert first.windowTitle() == "IC-9700" and first_cw.windowTitle() == "CW Tool - IC-9700"
    second = _titled_window(monkeypatch, host="192.0.2.2")
    try:
        assert first.windowTitle() == "IC-9700 (192.0.2.1 (LAN))"          # existing windows re-titled too
        assert first_cw.windowTitle() == "CW Tool - IC-9700 (192.0.2.1 (LAN))"
        assert second.windowTitle() == "IC-9700 (192.0.2.2 (LAN))"
        assert second.ensure_aprs_tool_window().windowTitle() == "APRS Tool - IC-9700 (192.0.2.2 (LAN))"
        second.close()
        assert first.windowTitle() == "IC-9700"                            # back to the short form
        assert first_cw.windowTitle() == "CW Tool - IC-9700"
    finally:
        first.close()


def test_different_models_never_get_a_connection_suffix(app, monkeypatch):
    nine = _titled_window(monkeypatch, model="IC-9700")
    seven = _titled_window(monkeypatch, model="IC-7300", host="192.0.2.9")
    try:
        assert nine.windowTitle() == "IC-9700" and seven.windowTitle() == "IC-7300"
    finally:
        nine.close()
        seven.close()


def test_tab_text_follows_the_new_titles_in_the_workspace(app, monkeypatch):
    window = _titled_window(monkeypatch)
    host = WorkspaceHost()
    try:
        host.present(window)
        pane = host.panes[host.pane_of(window)]
        assert pane.tabText(pane.indexOf(host._entries[window].page)) == "IC-9700"
        tool = window.ensure_cw_tool_window()
        host.present(tool)
        assert "CW Tool - IC-9700" in [pane.tabText(i) for i in range(pane.count())]
    finally:
        _close(host)
        window.close()


def test_the_longest_normal_title_is_not_truncated_in_its_tab(app, monkeypatch):
    window = _titled_window(monkeypatch, role="full_duplex")
    host = WorkspaceHost()
    try:
        host.present(window)
        pane = host.panes[host.pane_of(window)]
        text = pane.tabText(pane.indexOf(host._entries[window].page))
        assert text == "IC-9700 - Satellite Full Duplex" and "…" not in text
    finally:
        _close(host)
        window.close()


def test_ft8_tool_is_a_normal_tool_tab_that_a_saved_workspace_can_restore(app, monkeypatch):
    window = _titled_window(monkeypatch)
    try:
        tool = window.ensure_tool_window("FT8 Tool")                       # how a restored workspace asks for it
        assert tool is window.ensure_ft8_tool_window() and tool is window.ft8_window
        assert tool.windowTitle() == "FT8 Tool - IC-9700"
        assert tool.workspace_key == window._tool_key("FT8 Tool") and tool.workspace_key.endswith(":FT8 Tool")
        assert window.ft8_tool_button.isHidden() and not window.ft8_tool_button.isEnabled()      # enabled on connect, like the others
        from PySide6.QtWidgets import QMenu
        import types
        menu = QMenu()
        ctx = types.SimpleNamespace(open=lambda w: None, is_open=lambda w: False, label=lambda title, widget=None: title)
        window.populate_tool_menu(menu, ctx)
        assert "FT8 Tool" in [a.text() for a in menu.actions()]
    finally:
        window.close()
