"""Tests for collapsible_sidebar.py and its use as the dashboard's Filters
sidebar."""

import os
import subprocess
import sys
import textwrap

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from collapsible_sidebar import CollapsibleSidebar


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _sidebar(expanded=True):
    sidebar = CollapsibleSidebar("Filters", expanded=expanded)
    buttons = [QPushButton(f"Button {i}") for i in range(3)]
    for button in buttons:
        sidebar.content_layout().addWidget(button)
    return sidebar, buttons


def test_expanded_shows_controls_and_title(app):
    sidebar, buttons = _sidebar()
    sidebar.show()
    assert sidebar.is_expanded() and all(b.isVisible() for b in buttons)
    assert sidebar.header.text() == "Filters"


def test_collapsing_hides_controls_but_keeps_them_alive_and_stateful(app):
    sidebar, buttons = _sidebar()
    sidebar.show()
    buttons[0].setCheckable(True)
    buttons[0].setChecked(True)
    expanded_width = sidebar.width()
    sidebar.header.click()
    assert not sidebar.is_expanded()
    assert not any(b.isVisible() for b in buttons)
    assert buttons[0].isChecked()                         # hidden, not destroyed or reset
    assert sidebar.header.text() == ""                    # just the arrow
    app.processEvents()
    assert sidebar.sizeHint().width() < expanded_width    # a thin strip


def test_toggle_signal_and_reopening(app):
    sidebar, buttons = _sidebar()
    sidebar.show()
    seen = []
    sidebar.toggled.connect(seen.append)
    sidebar.header.click()
    sidebar.header.click()
    assert seen == [False, True] and sidebar.is_expanded() and all(b.isVisible() for b in buttons)


def test_can_start_collapsed_without_emitting(app):
    seen = []
    sidebar, buttons = _sidebar(expanded=False)
    sidebar.toggled.connect(seen.append)
    sidebar.show()
    assert not sidebar.is_expanded() and not any(b.isVisible() for b in buttons) and seen == []


def test_set_expanded_is_idempotent(app):
    sidebar, _ = _sidebar()
    seen = []
    sidebar.toggled.connect(seen.append)
    sidebar.set_expanded(True)
    assert seen == []


def test_dashboard_filters_live_in_the_sidebar_and_the_choice_is_remembered(tmp_path):
    """Runs the real dashboard in a subprocess with HOME/XDG pointed at a temp
    dir, so nothing touches the user's real settings or cache."""
    script = textwrap.dedent('''
        import os, sys
        sys.path.insert(0, %r)
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        s = QSettings("IcomRadioApp", "RadioControl")
        s.setValue("operator_callsign", "KF0UQR"); s.setValue("operator_lat", 40.0); s.setValue("operator_lon", -105.0); s.sync()
        from ham_dashboard import HamClockWindow

        def inside(widget, ancestor):
            while widget is not None:
                if widget is ancestor:
                    return True
                widget = widget.parentWidget()
            return False

        def settle():
            for _ in range(8):
                app.processEvents()

        dash = HamClockWindow(); dash.resize(1200, 800); dash.show(); settle()
        controls = [dash.satellite_button, dash.qso_map_button, dash.pskreporter_button,
                    dash.pota_button, dash.aprs_button, dash.qso_band_filter_combo]
        assert all(inside(c, dash.filters_sidebar) for c in controls), "filters not in the sidebar"
        assert dash.filters_sidebar.is_expanded() and all(c.isVisible() for c in controls)   # default: shown

        dash.filters_sidebar.header.click(); settle()
        assert not any(c.isVisible() for c in controls)
        dash.aprs_button.setChecked(True)                      # still wired up while hidden
        assert dash.aprs_button.text() == "APRS: ON"
        assert QSettings("IcomRadioApp", "RadioControl").value("filters_sidebar_expanded", True, type=bool) is False

        # a fresh dashboard starts the way it was left: collapsed
        dash2 = HamClockWindow(); dash2.show(); settle()
        assert not dash2.filters_sidebar.is_expanded()
        print("OK")
        sys.stdout.flush()
        os._exit(0)   # skip the dashboard's shutdown, which waits on network workers (tens of seconds offline)
    ''' % os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"), XDG_CACHE_HOME=str(tmp_path / "cache"),
               QT_QPA_PLATFORM="offscreen")
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr


def test_real_dashboard_hands_its_kill_tx_button_to_the_hosts_top_bar(tmp_path):
    script = textwrap.dedent('''
        import os, sys
        sys.path.insert(0, %r)
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        s = QSettings("IcomRadioApp", "RadioControl")
        s.setValue("operator_callsign", "KF0UQR"); s.setValue("operator_lat", 40.0); s.setValue("operator_lon", -105.0); s.sync()
        from workspace_host import WorkspaceHost
        from ham_dashboard import HamClockWindow
        calls = []
        HamClockWindow._on_kill_all_remote_tx = lambda self, *a: calls.append("killed")   # before construction: the button connects to it
        host = WorkspaceHost(); dash = HamClockWindow()
        host.resize(1500, 800); host.show()
        host.add_dashboard(dash)
        for _ in range(10):
            app.processEvents()
        assert dash.windowTitle() == "Map" and host.panes[0].tabText(0) == "Map", host.panes[0].tabText(0)
        kill = dash.kill_remote_tx_button
        floating = host._floating_button
        assert kill.window() is host and kill.isVisible(), "button not in the host"
        assert abs(kill.mapTo(host, kill.rect().center()).y() - floating.mapTo(host, floating.rect().center()).y()) <= 4, "not on the same row"
        assert kill.mapTo(host, kill.rect().topRight()).x() >= host.width() - 12, "not at the far right"
        kill.click()                                  # still hooked up to its handler after the move
        assert calls == ["killed"], calls
        print("OK")
        sys.stdout.flush()
        os._exit(0)
    ''' % os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"), XDG_CACHE_HOME=str(tmp_path / "cache"),
               QT_QPA_PLATFORM="offscreen")
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr
