"""A radio connected from a workspace's "+" menu opens in THAT workspace --
the right window and the right workspace -- not wherever the general placement
rule would have put it. Real dashboard, in a subprocess with HOME pointed at a
temp dir; the connection itself is stubbed (no hardware), everything else --
the menus, _open_radio, the docking -- is the real thing."""

import os
import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent('''
    import os, sys
    sys.path.insert(0, %r)
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QDialog, QMenu
    app = QApplication([])
    s = QSettings("IcomRadioApp", "RadioControl")
    s.setValue("operator_callsign", "KF0UQR"); s.setValue("operator_lat", 38.2); s.setValue("operator_lon", -97.5); s.sync()

    import radio_worker
    radio_worker.RadioWorker.start = lambda self, *a, **k: None          # no connection thread: no hardware
    import ham_dashboard
    from workspace_host import WorkspaceHost

    NEXT = {"n": 0}
    PARENTS = []                                                          # the window each dialog was parented to
    class FakeConnectionDialog:
        """Stands in for the real dialog: 'user fills it in and presses Connect'."""
        def __init__(self, parent=None, active_roles=None):
            PARENTS.append(parent.window() if parent is not None else None)
        def exec(self):
            NEXT["n"] += 1
            self.details = {"radio_model": "IC-9700", "connection_type": "network", "host": f"192.0.2.{NEXT['n']}",
                            "port": 50001, "username": "u", "password": "p", "role": "non_sat"}
            return QDialog.Accepted
    ham_dashboard.ConnectionDialog = FakeConnectionDialog

    host = WorkspaceHost(); dash = ham_dashboard.HamClockWindow()
    host.add_dashboard(dash); host.resize(1400, 800); host.show()
    def settle():
        for _ in range(10):
            app.processEvents()
    settle()

    def trigger(window, workspace, text):
        """Click an entry of that window's workspace's "+" menu, like the user would."""
        menu = QMenu(); window.panes[workspace]._fill_menu(menu)
        action = next(a for a in menu.actions() if a.text() == text)
        action.trigger(); settle()

    def where(radio):
        h = host.manager.host_of(radio)
        return host.manager.hosts.index(h) + 1, h.pane_of(radio) + 1                 # (window number, workspace number)

    # Window 1 has workspaces 1 and 2 (the map in 1); window 2 has workspaces 1 and 2, both empty.
    host.add_workspace(0, "right")
    second = host.manager.new_window()
    second.add_workspace(0, "right")
    settle()

    # 1. from window 2, workspace 2
    trigger(second, 1, "Connect New Radio...")
    assert where(dash.workspace_radios()[-1]) == (2, 2), where(dash.workspace_radios()[-1])
    assert PARENTS[-1] is second, "the connection dialog must open over the window it was launched from"

    # 2. from window 2, workspace 1 (not the first EMPTY one, and not the active one)
    trigger(second, 0, "Connect New Radio...")
    assert where(dash.workspace_radios()[-1]) == (2, 1), where(dash.workspace_radios()[-1])

    # 3. from window 1, workspace 1 (where the map is -- not the empty workspace 2 next to it)
    trigger(host, 0, "Connect New Radio...")
    assert where(dash.workspace_radios()[-1]) == (1, 1), where(dash.workspace_radios()[-1])
    assert PARENTS[-1] is host                                            # ... and from window 1 it opens over window 1

    # 5. a different menu later: the origin follows the most recent one
    trigger(host, 1, "Connect New Radio...")
    assert where(dash.workspace_radios()[-1]) == (1, 2), where(dash.workspace_radios()[-1])

    # 5b. the other dialogs the Actions open appear over the window whose menu was used
    from PySide6.QtWidgets import QInputDialog
    seen = {}
    ham_dashboard.OperatorProfileDialog.run = staticmethod(lambda parent=None, required=False: seen.__setitem__("profile", parent) or False)
    QInputDialog.getInt = staticmethod(lambda parent, *a, **k: (seen.__setitem__("autolog", parent), (0, False))[1])
    for window, label in ((second, "window 2"), (host, "window 1")):
        trigger(window, 0, "Profile...")
        assert seen["profile"].window() is window, (label, seen["profile"])
        trigger(window, 0, "WSJT-X Auto-Log UDP Port...")
        assert seen["autolog"].window() is window, (label, seen["autolog"])
    trigger(second, 1, "Connect New Radio...")                            # leave the origin on window 2 for the next step
    host.add_workspace(0, "right") if host.workspace_count < 2 else None

    # 6. the window it was started from is gone: falls back to normal placement, no error
    second.close(); settle()
    count = len(dash.workspace_radios())
    dash._on_connect_radio_clicked(); settle()
    assert len(dash.workspace_radios()) == count + 1
    assert host.manager.host_of(dash.workspace_radios()[-1]) is host

    # 7. nothing remembered at all (e.g. started some other way): the normal rule
    dash._menu_origin = None
    dash._on_connect_radio_clicked(); settle()
    assert host.manager.host_of(dash.workspace_radios()[-1]) is host
    print("OK")
    sys.stdout.flush()
    os._exit(0)
''' % os.path.dirname(os.path.abspath(__file__)))


def test_connecting_a_radio_adds_it_to_the_workspace_it_was_started_from(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), QT_QPA_PLATFORM="offscreen")
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr
