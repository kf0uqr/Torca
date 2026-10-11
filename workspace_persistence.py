"""Remembers the workspace arrangement per operator profile and brings it
back on the next launch.

What is restored (see workspace_state.py for the format):
- every WINDOW (the main one and any extra ones), each on the monitor it was on,
- each window's workspace layout (how many workspaces, how they are split),
- which tabs were open in which workspace of which window, their order and the
  selected tab,
- the radios that were connected: each is reconnected from its entry in the
  saved connection profiles (matched by model + where it connects), and its
  tool tabs (CW, APRS, ...) reopen in place,
- the Log Book and the reference-data pages, wherever they were.

Not restored: what was happening inside a tab (a running decode, a half-typed
message, scroll positions), floating windows, or splitter handle positions
(workspaces start with equal shares).

Saving happens shortly after any change (debounced), and once more, first
thing, when the window closes -- so even a crash loses at most the last
second or so. Nothing is saved until the restore has finished, so a launch
can never overwrite the saved arrangement with the default one.

The dashboard is touched only through four methods (workspace_radios,
reconnect_radio, open_log_book, and the radios' own tool-window builders),
which keeps this testable without the real dashboard.
"""

from PySide6.QtCore import QObject, QTimer

import workspace_state
from workspace_host import present_window

SAVE_DELAY_MS = 1500


class WorkspacePersistence(QObject):
    def __init__(self, host, dashboard, profile_name, state_path=None, parent=None):
        super().__init__(parent)
        self._host = host                # the main window
        self._manager = host.manager     # all windows
        self._dashboard = dashboard
        self.profile_name = profile_name
        self._state_path = state_path
        self._restoring = False
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY_MS)
        self._timer.timeout.connect(self.save_now)

    # ---- lifecycle ----

    def start(self):
        """Restores the saved arrangement (if there is one), THEN starts
        saving changes. Returns a list of human-readable problems (radios
        that could not be reconnected) for the caller to show."""
        problems = self.restore()
        self._manager.layout_changed.connect(self._schedule_save)   # a change in ANY window
        self._host.closing.connect(self._on_closing)                # the main window closing = the app closing
        return problems

    def _schedule_save(self):
        if not self._restoring and not self._closed:
            self._timer.start()

    def _on_closing(self):
        # Before anything is torn down: closing the radios would remove their tabs.
        self.save_now()
        self._closed = True
        self._timer.stop()

    # ---- saving ----

    def capture(self):
        state = self._manager.capture_state()
        state["radios"] = [
            {"key": window.workspace_key, "identity": window.identity}
            for window in self._dashboard.workspace_radios()
        ]
        return state

    def save_now(self):
        if self._closed or self._restoring:
            return False
        return workspace_state.save_state(self.profile_name, self.capture(), self._state_path)

    # ---- restoring ----

    def restore(self):
        state = workspace_state.load_state(self.profile_name, self._state_path)
        if state is None:
            return []
        problems = []
        self._restoring = True
        try:
            if not self._manager.restore_state(state):
                return []
            wanted = {key for window in [state, *state.get("windows", [])]
                      for workspace in window["workspaces"].values() for key in workspace["tabs"]}

            # Windows that already exist (the map, the reference-data pages),
            # wherever the saved arrangement had them -- possibly another window.
            for widget in list(self._manager.window_keys().values()):
                self._manager.place_by_hint(widget)

            # Radios, in the order they were saved; each docks where it was.
            radios = {}
            for radio in state["radios"]:
                window = self._dashboard.reconnect_radio(radio["identity"])
                if window is None:
                    problems.append(self._describe_unrestored(radio["identity"]))
                else:
                    radios[workspace_state.identity_key(radio["identity"])] = window

            # Their tool tabs.
            for key in sorted(wanted):
                if not key.startswith("tool:"):
                    continue
                identity_part, _, tool_name = key[len("tool:"):].rpartition(":")
                radio_window = radios.get(identity_part)
                tool = radio_window.ensure_tool_window(tool_name) if radio_window is not None else None
                if tool is not None:
                    present_window(tool)

            if "logbook" in wanted:
                self._dashboard.open_log_book()

            self._manager.finish_restore(state)
        finally:
            self._restoring = False
        return problems

    @staticmethod
    def _describe_unrestored(identity):
        where = (identity.get("host") or identity.get("serial_port") or identity.get("remote_host") or "?")
        return (f"{identity.get('radio_model', 'Radio')} ({where}) was open last time, but it isn't saved "
                "as a connection profile, so it can't be reconnected automatically. Connect it from "
                "the + menu (Actions -> Connect New Radio) and use Save Profile to remember it.")
