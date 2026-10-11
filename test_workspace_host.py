"""Tests for workspace_host.py -- the single main window with 1-4
workspaces. Uses stand-in windows that mimic the two close conventions
of the real ones: HideOnClose (the tool windows: closeEvent ignores the
close and hide()s itself so the instance can be reopened) and
RealClose (a RadioWindow: closeEvent accepts)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QWidget

import workspace_host
from workspace_host import WorkspaceHost, present_window


class HideOnClose(QWidget):
    def __init__(self, title="Tool"):
        super().__init__()
        self.setWindowTitle(title)

    def closeEvent(self, event):
        event.ignore()
        self.hide()


class RealClose(QWidget):
    def __init__(self, title="Radio"):
        super().__init__()
        self.setWindowTitle(title)
        self.closed = False

    def closeEvent(self, event):
        self.closed = True
        event.accept()


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def host(app, tmp_path, monkeypatch):
    # Never touch the real user's QSettings.
    QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope, str(tmp_path))
    monkeypatch.setattr(workspace_host, "_SETTINGS_ORG", "TorcaTestOrg")
    h = WorkspaceHost()
    h.show()
    yield h
    workspace_host._HOST = None
    h._closing = True
    h.close()


def _settle(app):
    app.processEvents()
    app.processEvents()


def _dashboard(host):
    dash = RealClose("Ham Dashboard")
    host.add_window(dash, closable=False, pane_index=0)
    host.set_dashboard(dash)
    return dash


def test_default_is_two_workspaces_and_dashboard_in_first(host):
    dash = _dashboard(host)
    assert host.workspace_count == 2
    assert host.pane_of(dash) == 0
    assert host.panes[0].count() == 1 and host.panes[1].count() == 0


def test_new_window_takes_next_empty_workspace_else_tab_in_current(host):
    dash = _dashboard(host)
    radio1, radio2, radio3 = RealClose("R1"), RealClose("R2"), RealClose("R3")
    host.present(radio1)
    assert host.pane_of(radio1) == 1                      # next free workspace
    host.present(radio2)                                  # none free: current workspace gets a tab
    assert host.pane_of(radio2) == host.pane_of(radio1)
    host.move_window(radio2, 0)
    host.present(radio3)
    assert host.pane_of(radio3) == 0                      # last-used workspace


def test_single_workspace_holds_everything_as_tabs(host):
    dash = _dashboard(host)
    host.set_workspace_count(1)
    radio = RealClose()
    host.present(radio)
    assert host.pane_of(radio) == 0 and host.panes[0].count() == 2


def test_move_window_between_workspaces(host):
    dash = _dashboard(host)
    radio = RealClose()
    host.present(radio)
    host.move_window(radio, 0)
    assert host.pane_of(radio) == 0 and host.panes[0].count() == 2 and host.panes[1].count() == 0
    assert host.panes[0].currentWidget() is host._entries[radio].page


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_all_layouts_show_exactly_that_many_workspaces(host, app, count):
    host.set_workspace_count(count)
    _settle(app)
    assert [p.isVisibleTo(host) for p in host.panes] == [i < count for i in range(4)]


def test_reducing_workspaces_moves_tabs_instead_of_closing_them(host, app):
    host.set_workspace_count(4)
    dash = _dashboard(host)
    radios = [RealClose(f"R{i}") for i in range(3)]
    for r in radios:
        host.present(r)
    assert sorted(host.pane_of(r) for r in radios) == [1, 2, 3]
    host.set_workspace_count(2)
    _settle(app)
    assert all(host.pane_of(r) in (0, 1) for r in radios)
    assert sum(p.count() for p in host.panes) == 4        # nothing lost
    assert not any(r.closed for r in radios)


def test_closing_a_singleton_tool_removes_tab_but_keeps_widget_reusable(host, app):
    _dashboard(host)
    tool = HideOnClose("CW Tool")
    host.present(tool)
    pane = host.panes[host.pane_of(tool)]
    host._on_tab_close_requested(pane, pane.indexOf(host._entries[tool].page))
    _settle(app)
    assert host.pane_of(tool) is None and pane.count() == 0
    host.present(tool)                                     # same instance comes back
    assert host.pane_of(tool) is not None and tool.isVisible()


def test_closing_a_radio_window_runs_its_closeEvent_and_removes_tab(host, app):
    _dashboard(host)
    radio = RealClose()
    host.present(radio)
    pane = host.panes[host.pane_of(radio)]
    host._on_tab_close_requested(pane, pane.indexOf(host._entries[radio].page))
    _settle(app)
    assert radio.closed and host.pane_of(radio) is None


def test_switching_tabs_is_not_mistaken_for_closing(host, app):
    _dashboard(host)
    host.set_workspace_count(1)
    a, b = HideOnClose("A"), HideOnClose("B")
    host.present(a)
    host.present(b)
    host.panes[0].setCurrentWidget(host._entries[a].page)
    _settle(app)
    host.panes[0].setCurrentWidget(host._entries[b].page)
    _settle(app)
    assert host.pane_of(a) == 0 and host.pane_of(b) == 0 and host.panes[0].count() == 3


def test_dashboard_tab_cannot_be_closed_or_popped_out(host, app):
    dash = _dashboard(host)
    assert host.panes[0].tabBar().tabButton(0, workspace_host.QTabBar.RightSide) is None
    host.pop_out(dash)
    assert host.pane_of(dash) == 0 and not host._entries[dash].floating


def test_pop_out_and_dock_back(host, app):
    _dashboard(host)
    tool = HideOnClose("APRS Tool")
    host.present(tool)
    host.pop_out(tool)
    _settle(app)
    assert host.pane_of(tool) is None and host._entries[tool].floating and tool.isWindow() and tool.isVisible()
    host.dock_floating(tool)
    _settle(app)
    assert host.pane_of(tool) is not None and not tool.isWindow() and tool.isVisible()


def test_presenting_a_floating_window_just_raises_it(host, app):
    _dashboard(host)
    tool = HideOnClose()
    host.present(tool)
    host.pop_out(tool)
    host.present(tool)
    assert host._entries[tool].floating and host.pane_of(tool) is None


def test_tab_title_follows_window_title(host):
    _dashboard(host)
    radio = RealClose("TORCA -- IC-9700")
    host.present(radio)
    radio.setWindowTitle("TORCA -- IC-7300")
    pane = host.panes[host.pane_of(radio)]
    assert pane.tabText(pane.indexOf(host._entries[radio].page)) == "TORCA -- IC-7300"


def test_workspace_count_is_remembered(host, app):
    host.set_workspace_count(3)
    assert int(QSettings("TorcaTestOrg", workspace_host._SETTINGS_APP).value("workspace_count")) == 3


def test_closing_host_runs_dashboard_shutdown(host, app):
    dash = _dashboard(host)
    host.close()
    assert dash.closed


def test_present_window_without_host_falls_back_to_plain_show(app):
    workspace_host._HOST = None
    w = HideOnClose()
    present_window(w)
    assert w.isVisible()
    w.hide()
