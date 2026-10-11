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


def test_dashboard_tab_has_no_close_button_but_can_move_to_another_window(host, app):
    dash = _dashboard(host)
    assert host.panes[0].tabBar().tabButton(0, workspace_host.QTabBar.RightSide) is None
    menu = host._tab_menu(host.panes[0], 0)
    assert "Close" not in [a.text() for a in menu.actions()]                       # no Close ...
    assert "Move to Window" in [a.text() for a in menu.actions()]                  # ... but it can still go to another window

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
    fake.origins = []                                                  # menu contexts the dashboard was told to remember
    fake._remember_connect_origin = lambda ctx: (fake.origins.append(ctx), calls.append("origin"))
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
    # connecting from a workspace's menu remembers THAT workspace first (so the radio opens there),
    # for the direct entry and for the Radios dialog's own Connect button
    assert calls == ["origin", "connect", "origin", "radios", "profile", "autolog_port"]
    assert fake.origins == [ctx, ctx]


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


# ---- the layout tree (pure functions) ----

from workspace_host import H, V, tree_add, tree_for_count, tree_leaves, tree_normalize, tree_remove


def test_tree_add_this_splits_just_that_workspace():
    assert tree_add(0, 0, "right", "this", 1) == (H, (0, 1))
    assert tree_add(0, 0, "below", "this", 1) == (V, (0, 1))
    side_by_side = (H, (0, 1))
    assert tree_add(side_by_side, 0, "below", "this", 2) == (H, ((V, (0, 2)), 1))     # WS1 over WS3, WS2 full height
    assert tree_add(side_by_side, 1, "below", "this", 2) == (H, (0, (V, (1, 2))))


def test_tree_add_all_goes_beside_or_below_everything():
    side_by_side = (H, (0, 1))
    assert tree_add(side_by_side, 0, "below", "all", 2) == (V, ((H, (0, 1)), 2))      # WS3 across the bottom of both
    assert tree_add((V, (0, 1)), 0, "right", "all", 2) == (H, ((V, (0, 1)), 2))        # WS3 beside both stacked ones


def test_same_orientation_neighbours_share_equally():
    assert tree_add((H, (0, 1)), 0, "right", "this", 2) == (H, (0, 2, 1))             # inserted right after WS1: 3 equal columns
    assert tree_add((H, (0, 1)), 0, "right", "all", 2) == (H, (0, 1, 2))              # appended: 3 equal columns
    assert tree_add((V, (0, 1)), 1, "below", "this", 2) == (V, (0, 1, 2))             # 3 equal rows


def test_tree_remove_closes_the_gap_and_collapses():
    assert tree_remove((H, (0, 1)), 1) == 0
    assert tree_remove((H, ((V, (0, 2)), 1)), 2) == (H, (0, 1))
    assert tree_remove((H, ((V, (0, 2)), 1)), 1) == (V, (0, 2))                       # the lone remaining split becomes the layout
    assert tree_remove((V, ((H, (0, 1)), (H, (2, 3)))), 3) == (V, ((H, (0, 1)), 2))
    assert tree_remove((V, ((H, (0, 1)), 2)), 2) == (H, (0, 1))


def test_tree_normalize_flattens_and_collapses():
    assert tree_normalize((H, ((H, (0, 1)), 2))) == (H, (0, 1, 2))
    assert tree_normalize((H, (0,))) == 0
    assert tree_normalize((H, ((V, (0, 1)), 2))) == (H, ((V, (0, 1)), 2))


def test_leaves_and_standard_arrangements():
    assert tree_leaves((V, ((H, (0, 1)), (H, (2, 3))))) == [0, 1, 2, 3]
    assert [tree_leaves(tree_for_count(n)) for n in (1, 2, 3, 4)] == [[0], [0, 1], [0, 1, 2], [0, 1, 2, 3]]


# ---- growing the workspaces from each one's menu ----

def _menu_for(host, index):
    pane = host.panes[index]
    pane._fill_workspace_menu()
    return [a.text() for a in pane._badge.menu().actions() if not a.isSeparator()], pane._badge.menu()


def _trigger(menu, text):
    next(a for a in menu.actions() if a.text() == text).trigger()


def test_the_first_workspace_offers_right_and_below(host):
    texts, menu = _menu_for(host, 0)
    assert texts == ["Add Workspace Right", "Add Workspace Below"]
    _trigger(menu, "Add Workspace Right")
    assert host._tree == (H, (0, 1)) and host.workspace_count == 2


def test_with_two_side_by_side_the_third_can_only_go_below_this_one_or_both(host):
    host.add_workspace(0, "right")                                            # [1 | 2]
    texts, menu = _menu_for(host, 0)
    assert texts == ["Add Workspace Below This", "Add Workspace Below Both"]  # no 3rd column: max 2 wide
    texts_ws2, _ = _menu_for(host, 1)
    assert texts_ws2 == ["Add Workspace Below This", "Add Workspace Below Both", "Close Workspace"]
    _trigger(menu, "Add Workspace Below Both")                                # WS3 across the bottom
    assert host._tree == (V, ((H, (0, 1)), 2))


def test_each_third_workspace_option_gives_its_layout(host):
    expected = {
        "Add Workspace Below This": (H, ((V, (0, 2)), 1)),
        "Add Workspace Below Both": (V, ((H, (0, 1)), 2)),
    }
    for label, tree in expected.items():
        host.set_workspace_count(1)
        host.add_workspace(0, "right")
        _texts, menu = _menu_for(host, 0)
        _trigger(menu, label)
        assert host._tree == tree, label
        assert host.workspace_count == 3
    # stacked first, then the third can only go beside
    host.set_workspace_count(1)
    host.add_workspace(0, "below")
    texts, menu = _menu_for(host, 0)
    assert texts == ["Add Workspace Right of This", "Add Workspace Right of Both"]
    _trigger(menu, "Add Workspace Right of This")
    assert host._tree == (V, ((H, (0, 2)), 1))                                # WS1 and WS3 side by side over WS2
    host.set_workspace_count(1)
    host.add_workspace(0, "below")
    _texts, menu = _menu_for(host, 0)
    _trigger(menu, "Add Workspace Right of Both")
    assert host._tree == (H, ((V, (0, 1)), 2))                                # WS3 beside the whole stack


def test_a_third_column_or_row_is_refused_everywhere(host):
    host.add_workspace(0, "right")                                            # two columns
    for scope in ("this", "all"):
        for index in (0, 1):
            assert host.add_workspace(index, "right", scope) is None          # would be a third column
            assert not host.can_add_workspace(index, "right", scope)
    assert host.workspace_count == 2 and host._tree == (H, (0, 1))
    host.set_workspace_count(1)
    host.add_workspace(0, "below")                                            # two rows
    for scope in ("this", "all"):
        assert host.add_workspace(0, "below", scope) is None                  # would be a third row
    assert host.workspace_count == 2


def test_the_fourth_workspace_completes_the_2x2_and_is_offered_only_where_it_fits(host):
    host.add_workspace(0, "right")
    host.add_workspace(0, "below", "all")                                     # (H(0,1) over 2)
    texts0, _ = _menu_for(host, 0)
    texts2, menu2 = _menu_for(host, 2)
    assert texts0 == ["Add more from the other workspaces' menus"]            # nothing fits next to / under the top-left one
    assert texts2 == ["Add Workspace Right", "Close Workspace"]               # the bottom one can take the 4th beside it
    _trigger(menu2, "Add Workspace Right")
    assert host._tree == (V, ((H, (0, 1)), (H, (2, 3)))) and host.workspace_count == 4


def test_every_layout_reachable_from_the_menus_stays_within_2x2_and_never_gets_stuck(host):
    """Exhaustive: from one workspace, apply every offered option, over and over,
    and check every layout that can ever result."""
    from workspace_host import MAX_WORKSPACES
    from workspace_state import fits_grid, tree_extent

    seen = set()
    frontier = [0]
    while frontier:
        tree = frontier.pop()
        if tree in seen:
            continue
        seen.add(tree)
        columns, rows = tree_extent(tree)
        assert columns <= 2 and rows <= 2 and len(tree_leaves(tree)) <= MAX_WORKSPACES, tree
        host._tree = tree
        any_option = False
        for index in tree_leaves(tree):
            for direction, scope in host._add_options(index):
                any_option = True
                frontier.append(tree_add(tree, index, direction, scope, host._unused_index()))
        if len(tree_leaves(tree)) < MAX_WORKSPACES:
            assert any_option, f"stuck below four workspaces: {tree}"          # always some way to grow until it is full
    assert len(seen) >= 8                                                     # and it really explored the space
    host._tree = 0


def test_a_full_layout_cannot_grow_and_says_so(host):
    host.set_workspace_count(4)
    for index in range(4):
        texts, _ = _menu_for(host, index)
        assert not any(t.startswith("Add Workspace") for t in texts)
    texts, _ = _menu_for(host, 0)
    assert texts == ["All 4 workspaces are in use"]
    assert host.add_workspace(0, "right") is None


def test_only_workspaces_two_to_four_can_be_closed(host):
    host.set_workspace_count(4)
    texts, _ = _menu_for(host, 0)
    assert "Close Workspace" not in texts
    assert host.close_workspace(0) is False and host.workspace_count == 4
    for index in (1, 2, 3):
        texts, _ = _menu_for(host, index)
        assert "Close Workspace" in texts


def test_new_workspace_takes_the_lowest_free_number(host):
    assert host.add_workspace(0, "below") == 1
    assert host.add_workspace(0, "right", "all") == 2
    host.close_workspace(1)
    assert host.add_workspace(0, "below", "all") == 1                         # number 2 is reused


def test_new_workspace_shares_the_space_equally_as_laid_out(host, app):
    host.resize(1500, 900)
    host.show()
    host.add_workspace(0, "right")                                            # two equal columns
    _settle(app)
    assert abs(host.panes[0].width() - host.panes[1].width()) <= 6

    host.add_workspace(0, "below", "all")                                     # a row of two over one: halves vertically
    _settle(app)
    top = host.panes[0].mapTo(host, host.panes[0].rect().topLeft()).y()
    bottom = host.panes[2].mapTo(host, host.panes[2].rect().topLeft()).y()
    heights = (host.panes[0].height(), host.panes[2].height())
    assert bottom > top and abs(heights[0] - heights[1]) <= 8, heights
    assert host.panes[2].width() >= host._layout_root.width() - 4             # spans both columns above it
    assert abs(host.panes[0].width() - host.panes[1].width()) <= 6            # the two above split equally

    host.add_workspace(2, "right")                                            # the 2x2: four equal quarters
    _settle(app)
    sizes = [(host.panes[i].width(), host.panes[i].height()) for i in range(4)]
    assert max(w for w, _ in sizes) - min(w for w, _ in sizes) <= 8, sizes
    assert max(h for _, h in sizes) - min(h for _, h in sizes) <= 8, sizes


def test_below_this_leaves_the_other_workspace_full_height(host, app):
    host.resize(1500, 900)
    host.show()
    host.add_workspace(0, "right")
    host.add_workspace(0, "below", "this")                                    # WS1 over WS3, WS2 beside both
    _settle(app)
    assert host.panes[1].height() >= host._layout_root.height() - 4          # WS2 keeps the full height
    assert abs(host.panes[0].height() - host.panes[2].height()) <= 8         # WS1 and WS3 split equally
    assert abs(host.panes[0].width() - host.panes[1].width()) <= 6            # and the columns are halves


def test_closing_a_workspace_moves_its_tabs_into_the_others_and_keeps_everything_running(host, app):
    dash = _dashboard(host)
    host.set_workspace_count(3)
    radios = [RealClose(f"R{i}") for i in range(2)]
    for r in radios:
        host.present(r)                                                       # workspaces 2 and 3
    assert sorted(host.pane_of(r) for r in radios) == [1, 2]
    host.close_workspace(1)
    _settle(app)
    assert host.workspace_count == 2 and 1 not in tree_leaves(host._tree)
    assert all(host.pane_of(r) in (0, 2) for r in radios)
    assert sum(p.count() for p in host.panes) == 3                            # nothing lost
    assert not any(r.closed for r in radios)
    assert not host.panes[1].isVisibleTo(host)


def test_closing_a_workspace_restores_the_remaining_layout(host):
    host.add_workspace(0, "right")
    host.add_workspace(0, "below", "all")                                     # (H(0,1) over 2)
    host.close_workspace(2)
    assert host._tree == (H, (0, 1))
    host.close_workspace(1)
    assert host._tree == 0


def test_closing_the_active_workspace_makes_workspace_one_active(host):
    host.set_workspace_count(2)
    host._set_active_pane(host.panes[1])
    host.close_workspace(1)
    assert host._active_pane is host.panes[0]


def test_empty_workspace_still_shows_a_clickable_workspace_label(host, app):
    host.resize(1200, 800)
    host.show()
    host.add_workspace(0, "right")
    _settle(app)
    badge = host.panes[1]._badge
    assert host.panes[1].count() == 0
    assert badge.isVisible() and badge.height() > 0 and badge.text() == "Workspace 2"


def test_changing_the_layout_never_destroys_panes_or_hosted_windows(host, app):
    from shiboken6 import isValid
    dash = _dashboard(host)
    tool = HideOnClose("Tool")
    host.present(tool)
    steps = [("add", 0, "right", "this"), ("add", 0, "below", "all"), ("add", 1, "right", "this"), ("close", 2),
             ("close", 3), ("close", 1), ("add", 0, "below", "this"), ("close", 1)]
    for step in steps:
        if step[0] == "add":
            host.add_workspace(step[1], step[2], step[3])
        else:
            host.close_workspace(step[1])
        _settle(app)
        _flush_deletes(app)
        assert all(isValid(p) for p in host.panes) and isValid(dash) and isValid(tool), step
        assert sum(p.count() for p in host.panes) == 2


# ---- widgets the dashboard hands to the host's top bar (Kill All Remote TX) ----

def test_top_bar_widget_goes_to_the_far_right_of_the_windows_menu_row(host, app):
    from PySide6.QtWidgets import QPushButton

    class DashboardWithTopBarButton(RealClose):
        def __init__(self):
            super().__init__("Ham Dashboard")
            self.kill = QPushButton("Kill All Remote TX")

        def take_top_bar_widgets(self):
            return [self.kill]

    host.resize(1400, 700)
    host.show()
    dash = DashboardWithTopBarButton()
    host.add_dashboard(dash)
    _settle(app)
    kill, floating = dash.kill, host._windows_button
    assert kill.isVisible() and kill.window() is host
    assert abs(kill.mapTo(host, kill.rect().center()).y() - floating.mapTo(host, floating.rect().center()).y()) <= 4   # same row
    assert kill.mapTo(host, kill.rect().topRight()).x() >= host.width() - 12                                           # far right
    assert kill.mapTo(host, kill.rect().topLeft()).x() > floating.mapTo(host, floating.rect().topRight()).x()   # to the right of it
    # reachable from any tab: still visible with another tab selected and in a second workspace
    other = HideOnClose("Other")
    host.open_in(other, 0)
    host.add_workspace(0, "right")
    host.open_in(other, 1)
    _settle(app)
    assert kill.isVisible()
    assert kill.mapTo(host, kill.rect().topRight()).x() >= host.width() - 12


def test_dashboard_without_top_bar_widgets_still_works(host):
    dash = RealClose("Ham Dashboard")
    host.add_dashboard(dash)                       # no take_top_bar_widgets: nothing handed over, nothing breaks
    assert host.pane_of(dash) == 0


# ---- saved arrangement: capture / restore ----

def _keyed(cls, key, title=None):
    widget = cls(title or key)
    widget.workspace_key = key
    return widget


def test_capture_records_layout_tabs_order_current_and_active_by_key(host, app):
    host.add_workspace(0, "right")
    a, b, c = _keyed(RealClose, "map"), _keyed(HideOnClose, "page:Contests"), _keyed(HideOnClose, "logbook")
    host.add_window(a, closable=False, pane_index=0)
    host.open_in(b, 0)
    host.open_in(c, 1)
    host.panes[0].setCurrentWidget(host._entries[a].page)
    host._set_active_pane(host.panes[1])
    unkeyed = HideOnClose("no key")
    host.open_in(unkeyed, 1)                                   # windows without a key are simply not recorded

    state = host.capture_state()
    assert state["layout"] == ["h", [0, 1]]
    assert state["workspaces"]["0"] == {"tabs": ["map", "page:Contests"], "current": "map"}
    assert state["workspaces"]["1"]["tabs"] == ["logbook"]
    assert state["active"] == 1


def test_restore_puts_existing_and_later_created_windows_in_their_saved_workspaces(host, app):
    saved = {
        "layout": ["h", [0, 1]],
        "workspaces": {
            "0": {"tabs": ["page:Parks", "map"], "current": "page:Parks"},
            "1": {"tabs": ["logbook", "radio:r", "tool:r:CW"], "current": "radio:r"},
        },
        "active": 1,
    }
    dash = _keyed(RealClose, "map")
    host.add_window(dash, closable=False, pane_index=0)
    parks, contests = _keyed(HideOnClose, "page:Parks"), _keyed(HideOnClose, "page:Contests")
    host.register_window(parks)
    host.register_window(contests)

    assert host.restore_state(saved)
    assert host.workspace_count == 2
    assert host.place_by_hint(dash) and host.place_by_hint(parks)       # already registered: placed now
    assert not host.place_by_hint(contests)                             # was not open when saved: stays closed
    assert host.pane_of(contests) is None and host.pane_of(parks) == 0

    # windows created later (a radio reconnecting, a tool, the Log Book) dock where they were
    radio, tool, logbook = _keyed(RealClose, "radio:r"), _keyed(HideOnClose, "tool:r:CW"), _keyed(HideOnClose, "logbook")
    for widget in (radio, tool, logbook):
        host.present(widget)
        assert host.pane_of(widget) == 1, widget.workspace_key
    host.finish_restore(saved)
    assert host.capture_state()["workspaces"] == {
        "0": {"tabs": ["page:Parks", "map"], "current": "page:Parks"},
        "1": {"tabs": ["logbook", "radio:r", "tool:r:CW"], "current": "radio:r"},
    }
    assert host._active_pane is host.panes[1]


def test_restore_applies_the_saved_split_layout(host, app):
    saved = {"layout": ["v", [["h", [0, 2]], 1]], "workspaces": {"0": {"tabs": [], "current": None}}, "active": 0}
    assert host.restore_state(saved)
    assert host._tree == (V, ((H, (0, 2)), 1)) and host.workspace_count == 3


def test_unknown_or_invalid_saved_data_is_ignored_safely(host, app):
    assert host.restore_state({"layout": ["h", [1, 2]], "workspaces": {}}) is False
    assert host.workspace_count == 1
    stray = _keyed(HideOnClose, "tool:never:CW")
    host.restore_state({"layout": ["h", [0, 1]], "workspaces": {"3": {"tabs": ["tool:never:CW"], "current": None}}})
    host.present(stray)                                                   # hint points at a workspace that doesn't exist
    assert host.pane_of(stray) in (0, 1)
    host.finish_restore({"layout": ["h", [0, 1]], "workspaces": {"9": {"tabs": ["x"], "current": "x"}}, "active": 7})


def test_a_hint_is_used_once_so_a_reopened_window_follows_the_normal_rule(host, app):
    host.restore_state({"layout": ["h", [0, 1]], "workspaces": {"1": {"tabs": ["logbook"], "current": "logbook"}}, "active": 0})
    logbook = _keyed(HideOnClose, "logbook")
    host.present(logbook)
    assert host.pane_of(logbook) == 1
    host.panes[1].tabCloseRequested.emit(0)                               # closed by the user ...
    _settle(app)
    host.add_window(_keyed(RealClose, "map"), closable=False, pane_index=1)   # ... and workspace 2 is no longer empty
    host.present(logbook)
    assert host.pane_of(logbook) == host._active_pane.index               # normal rule, not the stale hint


def test_layout_changed_fires_for_structural_changes_and_closing_fires_first(host, app):
    seen = []
    host.layout_changed.connect(lambda: seen.append("changed"))
    host.closing.connect(lambda: seen.append("closing"))
    host.add_workspace(0, "right")
    assert "changed" in seen
    seen.clear()
    widget = _keyed(HideOnClose, "logbook")
    host.present(widget)
    assert "changed" in seen
    seen.clear()
    host.move_window(widget, 1)
    assert "changed" in seen
    seen.clear()
    dash = _keyed(RealClose, "map")
    host.add_window(dash, closable=False, pane_index=0)
    host.set_dashboard(dash)
    seen.clear()
    host.close()
    assert seen[0] == "closing" and dash.closed


def test_add_dashboard_gives_the_dashboard_and_its_pages_keys(host):
    class Dash(RealClose):
        def take_data_pages(self):
            return [("Band Conditions", QWidget()), ("Contests", QWidget())]

    dash = Dash("Map")
    host.add_dashboard(dash)
    assert dash.workspace_key == "map"
    assert set(host.window_keys()) == {"map", "page:Band Conditions", "page:Contests"}


# ---- several windows ----

from workspace_host import WindowManager


def _new_window(host):
    return host.manager.new_window()


def test_new_window_is_a_second_host_numbered_and_titled(host, app):
    assert host.manager.main_host is host and host.windowTitle() == "TORCA"
    second = _new_window(host)
    third = _new_window(host)
    assert host.manager.hosts == [host, second, third]
    assert [h.windowTitle() for h in host.manager.hosts] == ["TORCA", "TORCA - Window 2", "TORCA - Window 3"]
    assert second.workspace_count == 1 and second.isVisible()
    second.close()
    assert host.manager.hosts == [host, third] and third.windowTitle() == "TORCA - Window 2"      # renumbered


def test_a_tab_can_move_to_another_window_and_back_without_being_closed(host, app):
    _dashboard(host)
    radio = _keyed(RealClose, "radio:r")
    host.present(radio)
    second = _new_window(host)
    host.manager.move_widget(radio, second)
    _settle(app)
    assert host.manager.host_of(radio) is second and second.pane_of(radio) == 0
    assert host.pane_of(radio) is None                                       # no longer in the main window
    assert not radio.closed and radio.isVisible()
    assert second.panes[0].tabText(0) == "radio:r"
    host.manager.move_widget(radio, host)
    _settle(app)
    assert host.manager.host_of(radio) is host and second.pane_of(radio) is None and not radio.closed


def test_every_window_operation_keeps_every_tab(host, app):
    dash = _keyed(RealClose, "map")
    host.add_window(dash, closable=False, pane_index=0)
    host.set_dashboard(dash)
    tabs = [_keyed(HideOnClose, f"page:{i}") for i in range(4)]
    for tab in tabs:
        host.present(tab)
    second = _new_window(host)
    for tab in tabs[:2]:
        host.manager.move_widget(tab, second)
    third = _new_window(host)
    host.manager.move_widget(tabs[0], third)
    host.manager.move_widget(dash, second)

    def docked():
        return sorted(k for h in host.manager.hosts for k in
                      (getattr(e.widget, "workspace_key", None) for e in host.manager.entries.values() if e.host is h))
    assert docked() == sorted(["map"] + [t.workspace_key for t in tabs])
    third.close()
    second.close()                                                             # closing windows must not lose or close a thing
    _settle(app)
    assert docked() == sorted(["map"] + [t.workspace_key for t in tabs])
    assert all(host.manager.host_of(w) is host for w in [dash, *tabs])


def test_closing_a_secondary_window_hands_its_tabs_to_the_main_window_in_order(host, app):
    _dashboard(host)
    second = _new_window(host)
    radio, tool = _keyed(RealClose, "radio:r"), _keyed(HideOnClose, "tool:r:CW")
    second.add_window(radio, pane_index=0)
    second.open_in(tool, 0)
    second.close()
    _settle(app)
    assert host.manager.hosts == [host]
    assert host.manager.host_of(radio) is host and host.manager.host_of(tool) is host
    assert not radio.closed                                                    # moved, not closed
    pane = host.panes[host.pane_of(radio)]
    texts = [pane.tabText(i) for i in range(pane.count())]
    assert texts.index("radio:r") < texts.index("tool:r:CW")


def test_closing_the_main_window_closes_every_window_and_runs_the_shutdown_once(host, app):
    dash = _dashboard(host)
    second = _new_window(host)
    third = _new_window(host)
    radio = _keyed(RealClose, "radio:r")
    second.add_window(radio, pane_index=0)
    seen = []
    host.closing.connect(lambda: seen.append("closing"))
    workspace_host._HOST = None
    host.close()
    _settle(app)
    from shiboken6 import isValid
    assert seen == ["closing"] and dash.closed
    assert all(not isValid(w) or not w.isVisible() for w in (second, third))      # closed (and deleted)
    assert host.manager.hosts == [host] and host.manager.shutting_down


def test_new_tabs_go_to_an_empty_workspace_in_any_window_before_stacking(host, app):
    _dashboard(host)                                                           # main's only workspace is now occupied
    second = _new_window(host)                                                 # ... but the new window has an empty one
    first_tab, next_tab = _keyed(HideOnClose, "page:a"), _keyed(HideOnClose, "page:b")
    host.present(first_tab)
    assert host.manager.host_of(first_tab) is second and second.pane_of(first_tab) == 0
    host.present(next_tab)                                                     # nothing empty anywhere now: stack in the active window
    assert host.manager.host_of(next_tab) is host


def test_presenting_a_tab_open_in_another_window_selects_it_there(host, app):
    _dashboard(host)
    second = _new_window(host)
    tab = _keyed(HideOnClose, "page:a")
    second.add_window(tab, pane_index=0)
    other = _keyed(HideOnClose, "page:b")
    second.open_in(other, 0)
    second.panes[0].setCurrentWidget(second._entries[other].page)
    host.present(tab)                                                          # asked from the MAIN window
    assert host.manager.host_of(tab) is second and second.panes[0].currentWidget() is second._entries[tab].page


def test_plus_menus_are_shared_and_open_into_the_window_they_were_clicked_in(host, app):
    class Dash(RealClose):
        def take_data_pages(self):
            self.pages = [("Parks", QWidget())]
            return self.pages

        def populate_workspace_menu(self, menu, ctx):
            for title, page in self.pages:
                menu.addAction(ctx.label(title, page)).triggered.connect(lambda _c=False, p=page: ctx.open(p))

    dash = Dash("Map")
    host.add_dashboard(dash)
    second = _new_window(host)
    texts, menu = _menu_texts(second, 0)
    assert texts == ["Parks"]                                                   # the second window offers the same entries
    next(a for a in menu.actions() if a.text() == "Parks").trigger()
    parks = dash.pages[0][1]
    assert host.manager.host_of(parks) is second
    texts_main, menu_main = _menu_texts(host, 0)
    assert texts_main == ["Parks  (Window 2, Workspace 1)"]                     # and says where it already is
    next(a for a in menu_main.actions() if a.text().startswith("Parks")).trigger()
    assert host.manager.host_of(parks) is host and second.pane_of(parks) is None   # picking it from here moves it here


def test_tab_context_menu_offers_move_to_window_with_new_window_and_each_other_window(host, app):
    _dashboard(host)
    tab = _keyed(HideOnClose, "page:a")
    host.present(tab)
    menu = host._tab_menu(host.panes[0], host.panes[0].indexOf(host._entries[tab].page))
    move = next(a for a in menu.actions() if a.text() == "Move to Window").menu()
    assert [a.text() for a in move.actions()] == ["New Window"]
    second = _new_window(host)
    menu = host._tab_menu(host.panes[0], host.panes[0].indexOf(host._entries[tab].page))
    move = next(a for a in menu.actions() if a.text() == "Move to Window").menu()
    assert [a.text() for a in move.actions()] == ["New Window", "Window 2"]
    next(a for a in move.actions() if a.text() == "Window 2").trigger()
    assert host.manager.host_of(tab) is second
    # and from the second window the main one is offered as "Window 1"
    menu2 = second._tab_menu(second.panes[0], second.panes[0].indexOf(second._entries[tab].page))
    move2 = next(a for a in menu2.actions() if a.text() == "Move to Window").menu()
    assert [a.text() for a in move2.actions()] == ["New Window", "Window 1"]
    next(a for a in move2.actions() if a.text() == "New Window").trigger()
    assert len(host.manager.hosts) == 3 and host.manager.host_of(tab) is host.manager.hosts[2]


def test_windows_menu_lists_windows_and_can_open_and_close_them(host, app):
    texts = lambda h: [a.text() for a in (h._rebuild_windows_menu() or h._windows_button.menu().actions()) if not a.isSeparator()]
    assert texts(host) == ["New Window", "Window 1  (main)  ✓ this one"]
    _trigger(host._windows_button.menu(), "New Window")
    assert len(host.manager.hosts) == 2
    second = host.manager.hosts[1]
    assert texts(second) == ["New Window", "Window 1  (main)", "Window 2  ✓ this one",
                             "Close This Window (its tabs move to Window 1)"]
    _trigger(second._windows_button.menu(), "Close This Window (its tabs move to Window 1)")
    _settle(app)
    assert host.manager.hosts == [host]
    assert "Close This Window (its tabs move to Window 1)" not in texts(host)         # the main window can't be "closed" this way


def test_each_new_window_gets_its_own_kill_button_that_runs_the_same_handler(host, app):
    from PySide6.QtWidgets import QPushButton

    class Dash(RealClose):
        def __init__(self):
            super().__init__("Map")
            self.kills = 0
            self.kill = QPushButton("Kill All Remote TX")

        def take_top_bar_widgets(self):
            return [self.kill]

        def make_top_bar_widgets(self):
            button = QPushButton("Kill All Remote TX")
            button.clicked.connect(lambda: setattr(self, "kills", self.kills + 1))
            return [button]

    dash = Dash()
    host.add_dashboard(dash)
    second = _new_window(host)
    third = _new_window(host)
    buttons = [second.findChildren(QPushButton), third.findChildren(QPushButton)]
    for found in buttons:
        kill = next(b for b in found if b.text() == "Kill All Remote TX")
        kill.click()
    assert dash.kills == 2                                                      # both reach the handler
    assert dash.kill.window() is host                                           # the original stays in the main window
    assert buttons[0][0] is not buttons[1][0]


def test_secondary_windows_are_deleted_when_closed_and_do_not_leak(host, app):
    from shiboken6 import isValid
    second = _new_window(host)
    second.close()
    _settle(app)
    _flush_deletes(app)
    assert not isValid(second)
    assert host.manager.hosts == [host]
