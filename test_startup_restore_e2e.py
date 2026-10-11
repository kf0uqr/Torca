"""End to end, with the REAL dashboard: one process builds an arrangement and
saves it; a second, fresh process (same settings folder) must bring it back.
Run in subprocesses with HOME pointed at a temp dir so nothing touches the
user's real settings or saved workspace. (Radios are not connected here --
that needs hardware; the orchestration for them is covered in
test_workspace_persistence.py with stand-ins.)"""

import os
import subprocess
import sys
import textwrap

PRELUDE = textwrap.dedent('''
    import os, sys
    sys.path.insert(0, %r)
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from operator_profile import ensure_active_profile, save_operator_profiles
    PROFILE = {"callsign": "KF0UQR", "name": "Pat", "address": "", "lat": 38.2, "lon": -97.5, "elevation_m": 450.0}
    if os.environ.get("PHASE") == "first":
        save_operator_profiles({"Home": PROFILE})          # as if created on the first launch
    from workspace_host import WorkspaceHost
    from workspace_persistence import WorkspacePersistence
    from ham_dashboard import HamClockWindow
    profile = ensure_active_profile()                      # must NOT show a dialog now that a profile exists
    assert profile == "Home", profile
    host = WorkspaceHost(); dash = HamClockWindow()
    host.add_dashboard(dash); host.resize(1500, 800); host.show()
    persistence = WorkspacePersistence(host, dash, profile)
    problems = persistence.start()
    def settle():
        for _ in range(10):
            app.processEvents()
    settle()
    pages = {title: page for title, page in dash._data_pages}
''' % os.path.dirname(os.path.abspath(__file__)))

FIRST = PRELUDE + textwrap.dedent('''
    assert problems == [] and host.workspace_count == 1                       # nothing saved yet: a normal start
    host.add_workspace(0, "right")
    host.open_in(pages["Parks"], 1)
    host.open_in(pages["Band Conditions"], 0)
    host.open_in(pages["Contests"], 1)
    dash.open_log_book()
    host.open_in(dash.log_book_window, 1)
    host.move_window(dash, 0)
    host.panes[1].setCurrentWidget(host._entries[pages["Parks"]].page)
    settle()
    assert persistence.save_now()
    print("OK")
    sys.stdout.flush()
    os._exit(0)
''')

SECOND = PRELUDE + textwrap.dedent('''
    assert problems == [], problems
    assert host.workspace_count == 2, host.workspace_count
    assert host.pane_of(dash) == 0
    assert host.pane_of(pages["Band Conditions"]) == 0
    assert host.pane_of(pages["Parks"]) == 1 and host.pane_of(pages["Contests"]) == 1
    assert host.pane_of(pages["DX Spots"]) is None and host.pane_of(pages["Satellites"]) is None   # never opened
    assert dash.log_book_window is not None and host.pane_of(dash.log_book_window) == 1
    saved_tabs = [host.panes[1].tabText(i) for i in range(host.panes[1].count())]
    assert saved_tabs == ["Parks", "Contests", "Log Book"] or sorted(saved_tabs) == ["Contests", "Log Book", "Parks"], saved_tabs
    assert host.panes[1].tabText(host.panes[1].currentIndex()) == "Parks"                          # the selected tab too
    print("OK")
    sys.stdout.flush()
    os._exit(0)
''')


def _run(script, home, phase):
    env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / "cfg"), XDG_CACHE_HOME=str(home / "cache"),
               QT_QPA_PLATFORM="offscreen", PHASE=phase)
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=180)


def test_arrangement_survives_a_real_restart_of_the_app(tmp_path):
    first = _run(FIRST, tmp_path, "first")
    assert first.returncode == 0 and "OK" in first.stdout, first.stdout + first.stderr
    saved = tmp_path / ".icom_radio_app_cache" / "workspace_state.json"
    assert saved.exists(), "nothing was saved"
    second = _run(SECOND, tmp_path, "second")
    assert second.returncode == 0 and "OK" in second.stdout, second.stdout + second.stderr
