"""Tests for workspace_persistence.py: saving and restoring the workspace
arrangement, using a real WorkspaceHost with a stand-in dashboard and radios."""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QWidget

import workspace_host
import workspace_state
from workspace_host import WorkspaceHost, present_window
from workspace_persistence import WorkspacePersistence


class Tab(QWidget):
    def __init__(self, key, title=None, closable_like_singleton=True):
        super().__init__()
        self.workspace_key = key
        self.setWindowTitle(title or key)
        self.closed = False
        self._singleton = closable_like_singleton

    def closeEvent(self, event):
        if self._singleton:
            event.ignore()
            self.hide()
        else:
            self.closed = True
            event.accept()


class FakeRadio(Tab):
    TOOLS = ("CW Tool", "APRS Tool", "SSTV Tool")

    def __init__(self, identity):
        super().__init__(f"radio:{workspace_state.identity_key(identity)}", f"{identity['radio_model']} - Non-Sat",
                         closable_like_singleton=False)
        self.identity = identity
        self.tools = {}

    def ensure_tool_window(self, name):
        if name not in self.TOOLS:
            return None
        if name not in self.tools:
            self.tools[name] = Tab(f"tool:{workspace_state.identity_key(self.identity)}:{name}", f"{name} - {self.identity['radio_model']}")
        return self.tools[name]


class FakeDashboard(Tab):
    """Just the surface WorkspacePersistence uses."""

    def __init__(self, known_radios):
        super().__init__("map", "Map", closable_like_singleton=False)
        self.known = known_radios                      # identity_key -> True if it has a saved connection profile
        self.radios = []
        self.logbook = None
        self.reconnect_attempts = []

    def workspace_radios(self):
        return list(self.radios)

    def reconnect_radio(self, identity):
        key = workspace_state.identity_key(identity)
        self.reconnect_attempts.append(key)
        if not self.known.get(key):
            return None
        return self.open_radio(identity)

    def open_radio(self, identity):
        radio = FakeRadio(identity)
        self.radios.append(radio)
        present_window(radio)
        return radio

    def open_log_book(self):
        if self.logbook is None:
            self.logbook = Tab("logbook", "Log Book")
        present_window(self.logbook)


NINE = {"radio_model": "IC-9700", "role": "full_duplex", "connection_type": "network", "host": "192.0.2.5", "port": 50011}
SEVEN = {"radio_model": "IC-7300", "role": "non_sat", "connection_type": "usb", "serial_port": "/dev/ttyUSB0"}
KEY9, KEY7 = workspace_state.identity_key(NINE), workspace_state.identity_key(SEVEN)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "workspace_state.json"


def _settle(app):
    for _ in range(6):
        app.processEvents()


def _launch(app, state_path, profile="Home", known=None, start=True):
    """A fresh 'app launch': new host, dashboard with its data pages, persistence."""
    host = WorkspaceHost()
    host.show()
    dashboard = FakeDashboard(known if known is not None else {KEY9: True, KEY7: True})
    pages = {title: Tab(f"page:{title}", title) for title in ("Band Conditions", "Contests", "Parks")}
    host.add_window(dashboard, closable=False, pane_index=0)
    host.set_dashboard(dashboard)
    for page in pages.values():
        host.register_window(page)
    persistence = WorkspacePersistence(host, dashboard, profile, state_path=state_path)
    problems = persistence.start() if start else None
    _settle(app)
    return host, dashboard, pages, persistence, problems


def _shutdown(host, persistence=None):
    host._closing = True
    workspace_host._HOST = None
    if persistence is not None:
        persistence._closed = True
    host.close()


def _arrange(app, host, dashboard, pages):
    """Builds an arrangement worth restoring: three workspaces, two radios,
    their tools, the log book, pages in unexpected places, tabs reordered."""
    host.add_workspace(0, "right")
    host.add_workspace(0, "below", "all")                                    # (H(0,1) over 2)
    dashboard.open_radio(NINE)
    radio9 = dashboard.radios[0]
    host.open_in(radio9, 1)
    host.open_in(pages["Parks"], 0)
    host.open_in(pages["Contests"], 2)
    dashboard.open_log_book()
    host.open_in(dashboard.logbook, 2)
    tool = radio9.ensure_tool_window("CW Tool")
    host.open_in(tool, 1)
    dashboard.open_radio(SEVEN)
    radio7 = dashboard.radios[1]
    host.open_in(radio7, 2)
    host.move_window(dashboard, 1)                                           # even the map moved
    host.panes[1].tabBar().moveTab(host.panes[1].indexOf(host._entries[tool].page), 0)   # reorder
    host.panes[2].setCurrentWidget(host._entries[pages["Contests"]].page)
    host._set_active_pane(host.panes[2])
    _settle(app)


def test_everything_comes_back_where_it_was_after_a_restart(app, state_path):
    host, dashboard, pages, persistence, _ = _launch(app, state_path)
    _arrange(app, host, dashboard, pages)
    before = persistence.capture()
    assert persistence.save_now()
    _shutdown(host, persistence)

    host2, dashboard2, pages2, persistence2, problems = _launch(app, state_path)
    try:
        assert problems == []
        after = persistence2.capture()
        assert after == before
        assert host2._tree == workspace_state.tree_from_json(before["layout"]) and host2.workspace_count == 3
        assert host2.pane_of(dashboard2) == 1                                  # the map where it was left
        assert host2.pane_of(pages2["Parks"]) == 0 and host2.pane_of(pages2["Contests"]) == 2
        assert host2.pane_of(pages2["Band Conditions"]) is None                # was never open: still closed
        assert [r.workspace_key for r in dashboard2.radios] == [f"radio:{KEY9}", f"radio:{KEY7}"]
        assert dashboard2.logbook is not None and host2.pane_of(dashboard2.logbook) == 2
        assert host2._active_pane.index == 2
    finally:
        _shutdown(host2, persistence2)


def test_a_first_launch_with_nothing_saved_opens_normally_and_saves_nothing_yet(app, state_path):
    host, dashboard, pages, persistence, problems = _launch(app, state_path)
    try:
        assert problems == [] and host.workspace_count == 1
        assert not state_path.exists()                                         # no save just for starting up
        _settle(app)
        assert not state_path.exists()
    finally:
        _shutdown(host, persistence)


def test_a_radio_without_a_saved_connection_profile_is_reported_and_the_rest_still_restores(app, state_path):
    host, dashboard, pages, persistence, _ = _launch(app, state_path)
    _arrange(app, host, dashboard, pages)
    persistence.save_now()
    _shutdown(host, persistence)

    host2, dashboard2, pages2, persistence2, problems = _launch(app, state_path, known={KEY9: True, KEY7: False})
    try:
        assert len(problems) == 1 and "IC-7300" in problems[0] and "/dev/ttyUSB0" in problems[0]
        assert [r.workspace_key for r in dashboard2.radios] == [f"radio:{KEY9}"]      # the other radio came back
        assert host2.pane_of(pages2["Parks"]) == 0 and host2.pane_of(dashboard2.logbook) == 2
        assert any(key.startswith(f"tool:{KEY9}") for key in host2.window_keys())      # its tool tab too
    finally:
        _shutdown(host2, persistence2)


def test_tool_tabs_of_a_radio_that_could_not_reconnect_are_dropped_quietly(app, state_path):
    host, dashboard, pages, persistence, _ = _launch(app, state_path)
    _arrange(app, host, dashboard, pages)
    persistence.save_now()
    _shutdown(host, persistence)
    host2, dashboard2, pages2, persistence2, problems = _launch(app, state_path, known={})
    try:
        assert len(problems) == 2 and dashboard2.radios == []
        assert not any(key.startswith("tool:") for key in host2.window_keys())
    finally:
        _shutdown(host2, persistence2)


def test_changes_are_saved_shortly_after_and_in_one_write(app, state_path, monkeypatch):
    host, dashboard, pages, persistence, _ = _launch(app, state_path)
    try:
        persistence._timer.setInterval(20)
        writes = []
        real = workspace_state.save_state
        monkeypatch.setattr(workspace_state, "save_state", lambda *a, **k: (writes.append(1), real(*a, **k))[1])
        host.add_workspace(0, "right")
        host.open_in(pages["Parks"], 1)
        host.open_in(pages["Contests"], 1)
        assert not state_path.exists()                                         # debounced, not immediate
        deadline = time.monotonic() + 3
        while not state_path.exists() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        _settle(app)
        assert len(writes) == 1                                                # three changes, one write
        saved = workspace_state.load_state("Home", state_path)
        assert saved["layout"] == ["h", [0, 1]]
        assert saved["workspaces"]["1"]["tabs"] == ["page:Parks", "page:Contests"]
    finally:
        _shutdown(host, persistence)


def test_closing_saves_the_arrangement_before_the_radios_are_torn_down(app, state_path):
    host, dashboard, pages, persistence, _ = _launch(app, state_path)
    _arrange(app, host, dashboard, pages)
    arrangement = persistence.capture()
    workspace_host._HOST = None
    host.close()                                                               # a real close: the host emits "closing" first
    saved = workspace_state.load_state("Home", state_path)
    assert saved is not None and [r["key"] for r in saved["radios"]] == [f"radio:{KEY9}", f"radio:{KEY7}"]
    assert saved["workspaces"] == {k: v for k, v in arrangement["workspaces"].items()}
    assert saved["workspaces"]["1"]["tabs"][0].startswith("tool:")


def test_nothing_is_saved_after_closing_has_begun(app, state_path):
    host, dashboard, pages, persistence, _ = _launch(app, state_path)
    host.add_workspace(0, "right")
    workspace_host._HOST = None
    host.close()
    first = state_path.read_text()
    assert persistence.save_now() is False                                     # the torn-down state must not overwrite it
    persistence._schedule_save()
    assert not persistence._timer.isActive()
    assert state_path.read_text() == first


def test_profiles_keep_separate_arrangements(app, state_path):
    host, dashboard, pages, persistence, _ = _launch(app, state_path, profile="Home")
    host.add_workspace(0, "right")
    host.open_in(pages["Parks"], 1)
    persistence.save_now()
    _shutdown(host, persistence)

    host2, dashboard2, pages2, persistence2, _ = _launch(app, state_path, profile="POTA")
    try:
        assert host2.workspace_count == 1 and host2.pane_of(pages2["Parks"]) is None        # POTA has its own, empty history
        host2.add_workspace(0, "below")
        persistence2.save_now()
    finally:
        _shutdown(host2, persistence2)

    host3, dashboard3, pages3, persistence3, _ = _launch(app, state_path, profile="Home")
    try:
        assert host3._tree == ("h", (0, 1)) and host3.pane_of(pages3["Parks"]) == 1            # Home untouched by POTA
    finally:
        _shutdown(host3, persistence3)


def test_a_corrupt_state_file_just_means_a_normal_start(app, state_path):
    state_path.write_text("{ definitely not json")
    host, dashboard, pages, persistence, problems = _launch(app, state_path)
    try:
        assert problems == [] and host.workspace_count == 1 and host.pane_of(dashboard) == 0
    finally:
        _shutdown(host, persistence)


def test_no_saving_while_restoring(app, state_path):
    host, dashboard, pages, persistence, _ = _launch(app, state_path)
    _arrange(app, host, dashboard, pages)
    persistence.save_now()
    _shutdown(host, persistence)
    host2, dashboard2, pages2, persistence2, _ = _launch(app, state_path, start=False)
    try:
        calls = []
        orig = persistence2.save_now
        persistence2.save_now = lambda: (calls.append(1), orig())[1]
        host2.layout_changed.connect(persistence2._schedule_save)              # as start() would, but BEFORE restoring ...
        persistence2.restore()
        assert not persistence2._timer.isActive()                              # ... the restore's own changes schedule no save
        assert calls == []
    finally:
        _shutdown(host2, persistence2)
