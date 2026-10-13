"""Remote Access is a local web app: no tunnel fields, and the listen address is a plain choice."""

import os
import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent('''
    import os, sys, time, socket, urllib.request
    sys.path.insert(0, %r)
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton
    app = QApplication([])
    s = QSettings("IcomRadioApp", "RadioControl")
    s.setValue("operator_callsign", "KF0UQR"); s.setValue("operator_lat", 38.2); s.setValue("operator_lon", -97.5); s.sync()
    import ham_dashboard
    from ham_dashboard import RemoteAccessDialog
    from workspace_host import WorkspaceHost
    assert not os.path.exists(os.path.join(%r, "cloudflare_tunnel.py"))
    host = WorkspaceHost(); dash = ham_dashboard.HamClockWindow(); host.add_dashboard(dash)

    started = []
    dialog = RemoteAccessDialog(False, 8765, "local", "op", "gu", "vi",
                                on_start=lambda port, listen: started.append((port, listen)) or True,
                                on_stop=lambda: started.append("stopped"), on_kill_tx=lambda: None, on_clear_tx_lock=lambda: None)
    labels = [w.text() for w in dialog.findChildren(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel)]
    assert not any("loudflare" in t or "unnel name" in t or "ostname" in t for t in labels), labels
    assert not any("Cloudflare" in b.text() for b in dialog.findChildren(QPushButton))
    assert [dialog.listen_combo.itemData(i) for i in range(2)] == ["local", "network"]
    dialog.listen_combo.setCurrentIndex(1)
    dialog.toggle_button.setChecked(True)
    assert started == [(8765, "network")] and not dialog.listen_combo.isEnabled()
    assert dialog.url_label.text().startswith("http://") and dialog.url_label.text().endswith(":8765") and "127.0.0.1" not in dialog.url_label.text()
    dialog.toggle_button.setChecked(False)
    assert started[-1] == "stopped" and dialog.listen_combo.isEnabled() and dialog.url_label.text() == ""
    assert RemoteAccessDialog.url_for("local", 9000) == "http://127.0.0.1:9000"

    # the dashboard starts the real server on the chosen address
    seen = {}
    real = ham_dashboard.RemoteWebServer
    class Spy(real):
        def __init__(self, *a, **k):
            seen.update(host=k.get("host"), port=k.get("port")); super().__init__(*a, **k)
        def start(self): pass
        def stop(self): pass
    ham_dashboard.RemoteWebServer = Spy
    assert dash._on_remote_access_start(8800, "local") and seen == {"host": "127.0.0.1", "port": 8800}
    dash._on_remote_access_stop()
    assert dash._on_remote_access_start(8801, "network") and seen == {"host": "0.0.0.0", "port": 8801}
    assert s.value("remote_access_listen") == "network"
    dash._on_remote_access_stop()
    print("OK"); sys.stdout.flush(); os._exit(0)
''' % (os.path.dirname(os.path.abspath(__file__)), os.path.dirname(os.path.abspath(__file__))))


def test_remote_access_dialog_has_no_tunnel_and_chooses_the_listen_address(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), QT_QPA_PLATFORM="offscreen")
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr


def test_server_listens_where_it_is_told(unused_port=None):
    import socket
    import web_remote.server as server
    assert server.RemoteWebServer.__init__.__defaults__[0] == "127.0.0.1"          # local only unless asked
    assert server.DEFAULT_PORT == 8765
