"""Tests for workspace_host.py -- the single main window with 1-4
workspaces. Uses stand-in windows that mimic the two close conventions
of the real ones: HideOnClose (the tool windows: closeEvent ignores the
close and hide()s itself so the instance can be reopened) and
RealClose (a RadioWindow: closeEvent accepts)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
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
def host(app):
    h = WorkspaceHost()
    h.show()
    yield h
    workspace_host._HOST = None
    h._closing = True
    h.close()


def _settle(app):
    app.processEvents()
    app.processEvents()


def _flush_deletes(app):
    """Actually run pending deleteLater() calls (a plain processEvents
    does not)."""
    from PySide6.QtCore import QEvent
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    app.processEvents()


def _dashboard(host):
    dash = RealClose("Ham Dashboard")
    host.add_window(dash, closable=False, pane_index=0)
    host.set_dashboard(dash)
    return dash


def test_starts_with_a_single_workspace_holding_the_dashboard(host):
    dash = _dashboard(host)
    assert host.workspace_count == 1
    assert [p.index for p in host.visible_panes()] == [0]
    assert host.pane_of(dash) == 0 and host.panes[0].count() == 1


def test_new_window_takes_next_empty_workspace_else_tab_in_current(host):
    host.set_workspace_count(2)
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
    host.set_workspace_count(2)
    _dashboard(host)
    tool = HideOnClose("CW Tool")
    host.present(tool)
    pane = host.panes[host.pane_of(tool)]
    assert pane.index == 1
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


def test_changing_layouts_never_destroys_panes_or_hosted_windows(host, app):
    """Regression: going 1 -> 2 workspaces deleted pane 0 (it was the
    previous layout's 'body'), taking the dashboard and its running
    QThreads with it -- "QThread: Destroyed while thread is still running"."""
    from shiboken6 import isValid

    dash = _dashboard(host)
    tool = HideOnClose("Tool")
    host.present(tool)
    for count in (1, 2, 3, 4, 3, 2, 1, 2, 1, 4, 2):
        host.set_workspace_count(count)
        _settle(app)
        _flush_deletes(app)
        assert all(isValid(p) for p in host.panes), f"a pane was destroyed switching to {count}"
        assert isValid(dash) and isValid(tool), f"a hosted window was destroyed switching to {count}"
        assert sum(p.count() for p in host.panes) == 2


def test_hosted_qthread_survives_layout_changes(host, app):
    """The actual crash: a QThread parented to a hosted widget must still
    be alive and running after switching workspace counts."""
    from PySide6.QtCore import QThread

    class Busy(QThread):
        def run(self):
            self.exec()

    dash = _dashboard(host)
    thread = Busy(dash)
    thread.start()
    try:
        for count in (1, 2, 1, 2):
            host.set_workspace_count(count)
            _settle(app)
            _flush_deletes(app)
        assert thread.isRunning()
    finally:
        thread.quit()
        thread.wait(5000)


class FakeDashboard(RealClose):
    """Stand-in for HamClockWindow's workspace hooks."""

    def __init__(self):
        super().__init__("Ham Dashboard")
        self.pages = []
        self.log_book = None

    def take_data_pages(self):
        self.pages = [(t, QWidget()) for t in ("Band Conditions", "Contests", "DX Spots", "Parks", "Satellites")]
        return self.pages

    def populate_workspace_menu(self, menu, ctx):
        for title, page in self.pages:
            menu.addAction(ctx.label(title, page)).triggered.connect(lambda _c=False, p=page: ctx.open(p))
        menu.addAction(ctx.label("Log Book", self.log_book)).triggered.connect(lambda _c=False: ctx.open(self._log()))

    def _log(self):
        if self.log_book is None:
            self.log_book = HideOnClose("Log Book")
        return self.log_book


def _menu_texts(host, pane_index):
    from PySide6.QtWidgets import QMenu
    menu = QMenu()
    host.panes[pane_index]._fill_menu(menu)   # exactly what opening the "+" menu does
    return [a.text() for a in menu.actions() if not a.isSeparator()], menu


def test_add_dashboard_opens_only_the_dashboard_and_offers_pages_in_plus_menu(host, app):
    dash = FakeDashboard()
    host.add_dashboard(dash)
    assert sum(p.count() for p in host.panes) == 1 and host.pane_of(dash) == 0   # nothing auto-opened
    texts, _ = _menu_texts(host, 1)
    assert texts == ["Band Conditions", "Contests", "DX Spots", "Parks", "Satellites", "Log Book"]


def test_plus_menu_opens_tab_in_that_workspace_and_marks_where_open_ones_are(host, app):
    dash = FakeDashboard()
    host.add_dashboard(dash)
    _t, menu = _menu_texts(host, 1)
    next(a for a in menu.actions() if a.text() == "Contests").trigger()
    contests = dash.pages[1][1]
    assert host.pane_of(contests) == 1 and host.panes[1].currentWidget() is host._entries[contests].page

    texts, menu0 = _menu_texts(host, 0)                       # from workspace 1: it's open in workspace 2
    assert "Contests  (Workspace 2)" in texts
    next(a for a in menu0.actions() if a.text().startswith("Contests")).trigger()
    assert host.pane_of(contests) == 0                         # picking it moves it here
    texts, _ = _menu_texts(host, 0)
    assert "Contests" in texts                                 # open HERE: plain label


def test_data_page_tabs_can_be_closed_and_reopened_from_plus(host, app):
    dash = FakeDashboard()
    host.add_dashboard(dash)
    page = dash.pages[3][1]
    _t, menu = _menu_texts(host, 0)
    next(a for a in menu.actions() if a.text() == "Parks").trigger()
    pane = host.panes[0]
    host._on_tab_close_requested(pane, pane.indexOf(host._entries[page].page))
    _settle(app)
    assert host.pane_of(page) is None and pane.count() == 1
    _t, menu = _menu_texts(host, 0)
    next(a for a in menu.actions() if a.text() == "Parks").trigger()
    assert host.pane_of(page) == 0 and page.isVisible()


def test_lazily_built_tab_is_only_created_when_picked(host, app):
    dash = FakeDashboard()
    host.add_dashboard(dash)
    assert dash.log_book is None
    _t, menu = _menu_texts(host, 1)                            # listing the menu must not build it
    assert dash.log_book is None
    next(a for a in menu.actions() if a.text() == "Log Book").trigger()
    assert dash.log_book is not None and host.pane_of(dash.log_book) == 1


def test_empty_workspace_shows_add_button_and_hides_it_once_it_has_a_tab(host, app):
    dash = FakeDashboard()
    host.add_dashboard(dash)
    host.show()
    _settle(app)
    assert host.panes[1].count() == 0 and not host.panes[1]._empty_button.isHidden()
    assert host.panes[0].count() == 1 and host.panes[0]._empty_button.isHidden()
    host.open_in(dash.pages[0][1], 1)
    assert host.panes[1]._empty_button.isHidden()
    host.move_window(dash.pages[0][1], 0)
    assert not host.panes[1]._empty_button.isHidden()          # back to empty -> button returns


def test_add_dashboard_works_for_a_dashboard_without_hooks(host):
    dash = RealClose("Ham Dashboard")
    host.add_dashboard(dash)
    assert host.pane_of(dash) == 0 and host.panes[0].count() == 1
    texts, _ = _menu_texts(host, 0)
    assert texts == ["(nothing to add)"]


def test_closing_host_still_runs_dashboard_shutdown_with_pages_registered(host):
    dash = FakeDashboard()
    host.add_dashboard(dash)
    host.close()
    assert dash.closed


def _fake_radio(app, connected):
    import types
    from PySide6.QtWidgets import QPushButton
    built = []
    fake = types.SimpleNamespace(
        cw_window=None, aprs_window=None, sstv_window=None, rtty_window=None,
        psk31_window=None, recording_window=None, memories_window=None,
    )
    for name in ("cw_tool", "aprs_tool", "sstv_tool", "rtty_tool", "psk31_tool", "recording_tool", "memories"):
        button = QPushButton()
        button.setEnabled(connected)  # the real buttons are enabled by RadioWindow._on_connected
        setattr(fake, f"{name}_button", button)
    for name in ("cw_tool", "aprs_tool", "sstv_tool", "rtty_tool", "psk31_tool", "recording_tool"):
        setattr(fake, f"ensure_{name}_window", lambda n=name: built.append(n) or n)
    fake.ensure_memories_window = lambda: built.append("memories") or "memories"
    return fake, built


def test_radio_window_tool_menu_lists_every_tool_and_builds_lazily(app):
    import types
    from PySide6.QtWidgets import QMenu
    from main_window import RadioWindow

    fake, built = _fake_radio(app, connected=True)
    opened = []
    ctx = types.SimpleNamespace(open=opened.append, label=lambda title, widget=None: title)
    menu = QMenu()
    RadioWindow.populate_tool_menu(fake, menu, ctx)
    assert [a.text() for a in menu.actions()] == [
        "CW Tool", "APRS Tool", "SSTV Tool", "RTTY Tool", "PSK31 Tool", "Recording Tool", "Memories"]
    assert all(a.isEnabled() for a in menu.actions())
    assert built == []                                   # nothing constructed just by listing
    menu.actions()[1].trigger()
    assert built == ["aprs_tool"] and opened == ["aprs_tool"]


def test_radio_tool_menu_entries_are_disabled_until_the_radio_has_connected(app):
    import types
    from PySide6.QtWidgets import QMenu
    from main_window import RadioWindow

    fake, _built = _fake_radio(app, connected=False)
    ctx = types.SimpleNamespace(open=lambda w: None, label=lambda title, widget=None: title)
    menu = QMenu()
    RadioWindow.populate_tool_menu(fake, menu, ctx)
    assert menu.actions() and not any(a.isEnabled() for a in menu.actions())


def _fake_dashboard_for_actions(app):
    import types
    from PySide6.QtWidgets import QPushButton
    import ham_dashboard

    calls = []
    fake = types.SimpleNamespace(_data_pages=[], log_book_window=None, _connected_radios=[])
    for name in ("radios", "new_qso", "profile", "wsjtx", "js8call", "remote_access", "update"):
        button = QPushButton({"update": "Check for Updates..."}.get(name, name))
        button.setToolTip(f"{name} tip")
        button.clicked.connect(lambda _c=False, n=name: calls.append(n))
        setattr(fake, f"{name}_button", button)
    fake.wsjtx_autolog_button = QPushButton("WSJT-X Auto-Log: OFF")
    fake.wsjtx_autolog_button.setCheckable(True)
    fake.wsjtx_autolog_button.toggled.connect(lambda on: calls.append(f"autolog={on}"))
    fake._on_connect_radio_clicked = lambda: calls.append("connect")
    fake._on_wsjtx_autolog_menu_requested = lambda _pos: calls.append("autolog_port")
    fake._ensure_log_book_window = lambda: None
    fake._radio_list_base_label = lambda d: "radio"
    cls = ham_dashboard.HamClockWindow
    fake._add_action_for_button = cls._add_action_for_button
    return fake, calls, cls


def test_dashboard_actions_section_replaces_the_button_row(app):
    import types
    from PySide6.QtWidgets import QMenu
    fake, calls, cls = _fake_dashboard_for_actions(app)
    ctx = types.SimpleNamespace(open=lambda w: None, label=lambda title, widget=None: title)
    menu = QMenu()
    cls.populate_workspace_menu(fake, menu, ctx)
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts == [
        "Log Book", "Connect New Radio...", "radios", "new_qso", "profile", "wsjtx", "js8call",
        "WSJT-X Auto-Log", "WSJT-X Auto-Log UDP Port...", "remote_access", "Check for Updates..."]
    assert any(a.isSeparator() and a.text() == "Actions" for a in menu.actions())  # a Qt "section" is a labeled separator
    for a in menu.actions():
        if a.text() in ("Connect New Radio...", "radios", "profile", "WSJT-X Auto-Log UDP Port..."):
            a.trigger()
    assert calls == ["connect", "radios", "profile", "autolog_port"]


def test_actions_mirror_button_state_and_toggle_autolog(app):
    import types
    from PySide6.QtWidgets import QMenu
    fake, calls, cls = _fake_dashboard_for_actions(app)
    ctx = types.SimpleNamespace(open=lambda w: None, label=lambda title, widget=None: title)

    # mid-update: the button's progress text and disabled state show in the menu
    fake.update_button.setText("Updating: Pulling the latest changes...")
    fake.update_button.setEnabled(False)
    menu = QMenu()
    cls.populate_workspace_menu(fake, menu, ctx)
    update = next(a for a in menu.actions() if a.text().startswith("Updating"))
    assert not update.isEnabled() and update.toolTip() == "update tip"

    # auto-log is a checkable entry bound to the toggle button
    autolog = next(a for a in menu.actions() if a.text() == "WSJT-X Auto-Log")
    assert autolog.isCheckable() and not autolog.isChecked()
    autolog.trigger()
    assert fake.wsjtx_autolog_button.isChecked() and calls == ["autolog=True"]
    menu2 = QMenu()
    cls.populate_workspace_menu(fake, menu2, ctx)
    assert next(a for a in menu2.actions() if a.text() == "WSJT-X Auto-Log").isChecked()


def test_retired_buttons_stay_alive_but_hidden(app):
    from PySide6.QtWidgets import QPushButton, QWidget
    from workspace_host import retire_button
    parent = QWidget()
    parent.show()
    button = QPushButton("x", parent)
    button.show()
    retire_button(parent, button)
    assert button.isHidden() and button.parent() is parent
    clicks = []
    button.clicked.connect(lambda: clicks.append(1))
    button.click()                                   # still works as a state/signal holder
    assert clicks == [1]


# ---- "+" button placement ----

def _plus_x_and_last_tab_right(host, app, pane_index=0):
    _settle(app)
    pane = host.panes[pane_index]
    bar = pane.tabBar()
    return bar.add_button.x(), bar.tabRect(bar.count() - 1).right(), bar


def test_plus_button_sits_directly_after_the_last_tab_and_follows_it(host, app):
    host.resize(1400, 700)
    host.show()
    _dashboard(host)
    x, last_right, bar = _plus_x_and_last_tab_right(host, app)
    assert last_right < x <= last_right + 8, (x, last_right)            # hugging the last tab
    assert x < bar.width() // 2                                          # nowhere near the workspace label / right edge

    for i in range(3):
        host.open_in(HideOnClose(f"Another tab {i}"), 0)
    x2, last_right2, bar = _plus_x_and_last_tab_right(host, app)
    assert x2 > x and last_right2 < x2 <= last_right2 + 8                # moved right with the new tabs

    pane = host.panes[0]
    host._on_tab_close_requested(pane, pane.count() - 1)
    _settle(app)
    x3, last_right3, bar = _plus_x_and_last_tab_right(host, app)
    assert x3 < x2 and last_right3 < x3 <= last_right3 + 8               # and back left when one closes


def test_plus_moves_to_the_corner_when_tabs_overflow_and_never_overlaps_a_tab(host, app):
    host.set_workspace_count(4)                                          # narrow workspaces
    host.resize(900, 700)
    host.show()
    _dashboard(host)
    pane = host.panes[0]
    bar = pane.tabBar()
    _settle(app)
    assert bar.add_button.isVisible() and not pane._corner_add_button.isVisible()   # plenty of room

    for i in range(12):
        host.open_in(HideOnClose(f"A fairly long tab title number {i}"), 0)
    _settle(app)
    assert not bar.add_button.isVisible()                                # would sit on top of the last tab
    assert pane._corner_add_button.isVisible()                           # so the corner one is offered instead

    for _ in range(12):                                                  # close every extra tab: only the dashboard fits
        host._on_tab_close_requested(pane, pane.count() - 1)
        _settle(app)
    assert pane.count() == 1
    assert bar.add_button.isVisible() and not pane._corner_add_button.isVisible()


def test_both_plus_buttons_open_the_same_menu(host, app):
    dash = FakeDashboard()
    host.add_dashboard(dash)
    pane = host.panes[0]
    for button in (pane.tabBar().add_button, pane._corner_add_button):
        pane._fill_menu(button.menu())
        assert [a.text() for a in button.menu().actions() if not a.isSeparator()][:2] == ["Band Conditions", "Contests"]


# ---- growing the workspace grid: add right / below, close ----

def _menu_for(host, index):
    pane = host.panes[index]
    pane._fill_workspace_menu()
    return [a.text() for a in pane._badge.menu().actions() if not a.isSeparator()], pane._badge.menu()


def _trigger(menu, text):
    next(a for a in menu.actions() if a.text() == text).trigger()


def test_workspace_one_offers_right_and_below_and_each_choice_places_the_new_one(host):
    texts, menu = _menu_for(host, 0)
    assert texts == ["Add Workspace Right", "Add Workspace Below"]
    _trigger(menu, "Add Workspace Right")
    assert host._slots == {0: (0, 0), 1: (0, 1)}
    texts, menu = _menu_for(host, 0)
    assert texts == ["Add Workspace Below"]                                  # right is taken now
    _trigger(menu, "Add Workspace Below")
    assert host._slots == {0: (0, 0), 1: (0, 1), 2: (1, 0)}
    texts, _ = _menu_for(host, 0)
    assert texts == ["Add more from the other workspaces' menus"]            # workspace 1 is boxed in, but 3 < 4...
    host.add_workspace(1, "below")
    texts, _ = _menu_for(host, 0)
    assert texts == ["All 4 workspaces are in use"]                          # ...until the grid is actually full


def test_menu_options_follow_the_square_grid_rules(host):
    # top-left only
    assert host.can_add_workspace(0, "right") and host.can_add_workspace(0, "below")
    host.add_workspace(0, "right")                                           # workspace 2, top-right
    assert not host.can_add_workspace(1, "right")                            # nothing further right of a square
    assert host.can_add_workspace(1, "below")                                # top-right can add below it
    texts, _ = _menu_for(host, 1)
    assert texts == ["Add Workspace Below", "Close Workspace"]
    host.add_workspace(0, "below")                                           # workspace 3, bottom-left
    assert host.can_add_workspace(2, "right") and not host.can_add_workspace(2, "below")
    texts, _ = _menu_for(host, 2)
    assert texts == ["Add Workspace Right", "Close Workspace"]
    assert host.add_workspace(2, "right") == 3                               # workspace 4, bottom-right
    assert host.workspace_count == 4
    texts, _ = _menu_for(host, 3)
    assert texts == ["Close Workspace"]                                      # the corner can only be closed
    assert host.add_workspace(1, "below") is None and host.add_workspace(0, "right") is None   # full / taken


def test_only_workspaces_two_to_four_can_be_closed(host):
    host.set_workspace_count(4)
    texts, _ = _menu_for(host, 0)
    assert "Close Workspace" not in texts
    assert host.close_workspace(0) is False and host.workspace_count == 4
    for index in (1, 2, 3):
        texts, _ = _menu_for(host, index)
        assert "Close Workspace" in texts


def test_new_workspace_takes_the_lowest_free_number(host):
    assert host.add_workspace(0, "below") == 1                               # first one added is "Workspace 2"
    assert host._slots[1] == (1, 0)
    assert host.add_workspace(0, "right") == 2
    host.close_workspace(1)
    assert host.add_workspace(2, "below") == 1                               # number 2 is reused, now top... right-below
    assert host._slots[1] == (1, 1)


def test_closing_a_workspace_moves_its_tabs_into_the_others_and_keeps_everything_running(host, app):
    dash = _dashboard(host)
    host.set_workspace_count(3)
    radios = [RealClose(f"R{i}") for i in range(2)]
    for r in radios:
        host.present(r)                                                       # workspaces 2 and 3
    assert sorted(host.pane_of(r) for r in radios) == [1, 2]
    host.close_workspace(1)
    _settle(app)
    assert host.workspace_count == 2 and 1 not in host._slots
    assert all(host.pane_of(r) in (0, 2) for r in radios)
    assert sum(p.count() for p in host.panes) == 3                            # nothing lost
    assert not any(r.closed for r in radios)
    assert not host.panes[1].isVisibleTo(host)


def test_closing_the_active_workspace_makes_workspace_one_active(host):
    host.set_workspace_count(2)
    host._set_active_pane(host.panes[1])
    host.close_workspace(1)
    assert host._active_pane is host.panes[0]


def test_layout_follows_the_slots_and_a_lone_row_spans_the_full_width(host, app):
    host.resize(1200, 800)
    host.show()
    host.add_workspace(0, "below")                                            # slots: (0,0) and (1,0)
    host.add_workspace(1, "right")                                            # ... plus (1,1): top row has ONE workspace
    _settle(app)
    top, bottom_left, bottom_right = host.panes[0], host.panes[1], host.panes[2]
    assert top.width() >= host._layout_root.width() - 4                       # lone top row spans the full width
    assert top.y() < bottom_left.mapTo(host, bottom_left.rect().topLeft()).y()
    assert abs(bottom_left.width() - bottom_right.width()) <= 8               # bottom row split evenly
    assert bottom_left.mapTo(host, bottom_left.rect().topLeft()).x() < bottom_right.mapTo(host, bottom_right.rect().topLeft()).x()


def test_empty_workspace_still_shows_a_clickable_workspace_label(host, app):
    host.resize(1200, 800)
    host.show()
    host.add_workspace(0, "right")
    _settle(app)
    badge = host.panes[1]._badge
    assert host.panes[1].count() == 0
    assert badge.isVisible() and badge.height() > 0 and badge.text() == "Workspace 2"


def test_changing_the_grid_never_destroys_panes_or_hosted_windows(host, app):
    from shiboken6 import isValid
    dash = _dashboard(host)
    tool = HideOnClose("Tool")
    host.present(tool)
    steps = [("add", 0, "right"), ("add", 0, "below"), ("add", 1, "below"), ("close", 2), ("close", 3),
             ("close", 1), ("add", 0, "below"), ("close", 1)]
    for step in steps:
        host.add_workspace(step[1], step[2]) if step[0] == "add" else host.close_workspace(step[1])
        _settle(app)
        _flush_deletes(app)
        assert all(isValid(p) for p in host.panes) and isValid(dash) and isValid(tool), step
        assert sum(p.count() for p in host.panes) == 2
