"""Rigctld / Virtual Cables buttons live in each RadioWindow (below SWR Protect)."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])


def test_buttons_sit_below_swr_protect_and_show_status(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    import main_window
    window = main_window.RadioWindow.__new__(main_window.RadioWindow)
    # status text only needs the two buttons
    from PySide6.QtWidgets import QPushButton
    window.rigctld_button = QPushButton("Rigctld...")
    window.virtual_cable_button = QPushButton("Virtual Cables...")
    main_window.RadioWindow.set_service_status(window, "port 4532", "RX+TX")
    assert window.rigctld_button.text() == "Rigctld: port 4532"
    assert window.virtual_cable_button.text() == "Virtual Cables: RX+TX"
    main_window.RadioWindow.set_service_status(window, "", "")
    assert (window.rigctld_button.text(), window.virtual_cable_button.text()) == ("Rigctld...", "Virtual Cables...")


def test_radio_window_source_places_buttons_after_swr_protect():
    src = open(os.path.join(os.path.dirname(__file__), "main_window.py")).read()
    a = src.index("tx_column.addWidget(self.swr_protection_button)")
    assert a < src.index("tx_column.addWidget(self.rigctld_button)") < src.index("tx_column.addWidget(self.virtual_cable_button)")


def test_log_book_drives_the_dashboards_wsjtx_autolog(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    import types
    from PySide6.QtWidgets import QPushButton
    import log_book_window
    monkeypatch.setattr(log_book_window.qso_log, "load_qso_log", lambda: [])
    source = QPushButton("WSJT-X Auto-Log: OFF")
    source.setCheckable(True)
    source.toggled.connect(lambda on: source.setText("WSJT-X Auto-Log: ON" if on else "WSJT-X Auto-Log: OFF"))
    ports = []
    dash = types.SimpleNamespace(wsjtx_autolog_button=source, _on_wsjtx_autolog_menu_requested=lambda p: ports.append(p))
    window = log_book_window.LogBookWindow(dash)
    window.autolog_button.click()
    assert source.isChecked() and window.autolog_button.text() == "WSJT-X Auto-Log: ON"
    source.setChecked(False)                         # changed elsewhere: the log book follows
    assert not window.autolog_button.isChecked()
    window.autolog_port_button.click()
    assert ports == [None]


def test_new_qso_is_a_tab_that_goes_away_when_logged(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    import types
    import log_book_window
    import workspace_host
    from workspace_host import WorkspaceHost
    monkeypatch.setattr(log_book_window.qso_log, "load_qso_log", lambda: [])
    monkeypatch.setattr(log_book_window.qso_log, "save_qso_log", lambda q: None)
    host = WorkspaceHost(); host.show()
    dash = types.SimpleNamespace(_connected_radios=[])
    book = log_book_window.LogBookWindow(dash)
    book._on_new_qso_clicked(place=lambda w: host.open_in(w, 0))
    dialog = book._open_dialogs[0]
    assert host.pane_of(dialog) == 0 and host.panes[0].tabText(0) == "New QSO"
    dialog.call_input.setText("W1AW")
    dialog._on_accept()
    for _ in range(5):
        _app.processEvents()
    assert host.panes[0].count() == 0 and dialog not in host.manager.entries and not book._open_dialogs
    assert [q.get("CALL") for q in book._qsos] == ["W1AW"]
    workspace_host._HOST = None
    host._closing = True; host.close()
