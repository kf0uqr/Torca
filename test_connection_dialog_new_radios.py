"""
Test for connection_dialog.py's protocol-aware CI-V Address field
handling, added alongside IC-7610/X6200/FTX-1 support -- FTX-1 speaks
Yaesu CAT, not CI-V, so it has no CI-V address at all
(RADIO_PROFILES["FTX-1"]["protocol"] == "yaesu_cat"). Confirms the
address field is hidden/disabled and NOT required to accept the dialog
for a non-CI-V radio, while every CI-V radio (old and new) keeps
requiring it exactly as before.

Needs a real QApplication (offscreen platform is fine, no display
required): run with QT_QPA_PLATFORM=offscreen.

Run directly: QT_QPA_PLATFORM=offscreen ./bin/python3 test_connection_dialog_new_radios.py
"""

import sys

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(sys.argv)

import connection_dialog as connection_dialog_module
from connection_dialog import ConnectionDialog
from constants import RADIO_PROFILES

# _on_accept's invalid-address path calls QMessageBox.warning(...), a
# real modal exec() -- fine interactively, but blocks forever in a
# headless test with nothing to dismiss it. Monkeypatched to a no-op
# for the whole module: every test here only cares whether accept()
# itself fired, not the warning dialog's own appearance/text.
connection_dialog_module.QMessageBox.warning = staticmethod(lambda *a, **k: None)


def test_civ_radios_show_and_require_addr_field():
    print("--- CI-V radios (old and new) show + require the addr field ---")
    dlg = ConnectionDialog()
    for model, profile in RADIO_PROFILES.items():
        if profile.get("protocol", "civ") != "civ":
            continue
        dlg.radio_combo.setCurrentText(model)
        assert dlg.connection_combo.currentData() != "remote"
        assert dlg.addr_input.isEnabled(), f"{model}: addr field should be enabled"

        dlg.serial_port_input.setText("/dev/ttyUSB0")
        dlg.host_input.setText("127.0.0.1")
        dlg.addr_input.setText("")  # invalid -- must block accept
        blocked = []
        dlg.accept = lambda: blocked.append(True)
        dlg._on_accept()
        assert not blocked, f"{model}: accept should have been blocked by an empty addr field"
    print("  PASSED\n")


def test_ftx1_hides_and_does_not_require_addr_field():
    print("--- FTX-1 (Yaesu CAT) hides the addr field and doesn't require it ---")
    dlg = ConnectionDialog()
    dlg.radio_combo.setCurrentText("FTX-1")
    assert dlg.connection_combo.currentData() == "usb", "FTX-1 has no LAN backend -- must default to serial"
    assert not dlg.addr_input.isEnabled(), "FTX-1: addr field should be disabled (no CI-V address)"

    dlg.serial_port_input.setText("/dev/ttyUSB0")
    dlg.addr_input.setText("")  # deliberately empty/invalid -- must NOT block accept for FTX-1
    accepted = []
    dlg.accept = lambda: accepted.append(True)
    dlg._on_accept()
    assert accepted, "FTX-1: accept should NOT be blocked by an empty addr field"
    print("  PASSED\n")


def test_switching_between_two_usb_default_radios_still_refreshes_addr_field():
    """Regression check for a real bug this fix could easily reintroduce:
    switching from one "usb"-default radio to another (e.g. X6200 ->
    FTX-1) doesn't change the connection-type combo's index at all, so
    naively relying on currentIndexChanged alone to refresh the addr
    field's visibility would silently leave it in the PREVIOUS radio's
    state."""
    print("--- switching between two usb-default radios still refreshes the addr field ---")
    dlg = ConnectionDialog()
    dlg.radio_combo.setCurrentText("X6200")
    assert dlg.connection_combo.currentData() == "usb"
    assert dlg.addr_input.isEnabled(), "X6200: addr field should be enabled"

    dlg.radio_combo.setCurrentText("FTX-1")
    assert dlg.connection_combo.currentData() == "usb", "connection type shouldn't have changed"
    assert not dlg.addr_input.isEnabled(), "FTX-1: addr field should now be disabled"
    print("  PASSED\n")


def main():
    ok = True
    try:
        test_civ_radios_show_and_require_addr_field()
        test_ftx1_hides_and_does_not_require_addr_field()
        test_switching_between_two_usb_default_radios_still_refreshes_addr_field()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        ok = False
    if ok:
        print("ALL CONNECTION-DIALOG NEW-RADIO TESTS PASSED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
