"""
Test for ham_dashboard.py's pre-launch rigctld check
(_confirm_rigctld_before_launch), shared by both "Launch WSJT-X" and
"Launch JS8Call" -- warns if no rigctld server is running for any
connected radio (self._rigctld_servers empty) before actually
launching either app, since neither one can control a radio without
one. Covers all button choices: "Continue Without It" (proceeds),
"Cancel" (aborts), "Open Rigctld..." with and without a radio selected
in the Radios list (opens that dialog instead of launching, and nudges
the operator to select one first if none is).

Needs a real QApplication (offscreen platform is fine, no display
required): run with QT_QPA_PLATFORM=offscreen.

NOTE on a real gotcha found while writing this: assigning a
unittest.mock.MagicMock() as a CLASS attribute on a QWidget subclass
segfaults PySide6/Shiboken's metaclass introspection (confirmed
directly, isolated down to a 3-line repro: `class Foo(QWidget): bar =
MagicMock()` crashes the interpreter the moment Foo() is instantiated,
no exception raised at all). Every mock used below is either a
plain-object fake (not QWidget-derived) or assigned as an INSTANCE
attribute in __init__ instead.

Run directly: QT_QPA_PLATFORM=offscreen ./bin/python3 test_ham_dashboard_rigctld_warning.py
"""

import sys
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

_app = QApplication.instance() or QApplication(sys.argv)

import ham_dashboard


class FakeDashboard(QWidget):
    _confirm_rigctld_before_launch = ham_dashboard.HamClockWindow._confirm_rigctld_before_launch

    def __init__(self):
        super().__init__()
        self._rigctld_servers = {}
        self._selected_window = None
        self._on_rigctld_button_clicked = MagicMock()  # instance attribute -- see module docstring

    def _selected_radio_window(self):
        return self._selected_window


class ClickingQMessageBox(QMessageBox):
    """Drop-in QMessageBox that auto-"clicks" whichever button's text
    matches CLICK_LABEL the moment .exec() would normally block,
    instead of actually showing/waiting for a human. .information() is
    a plain recorder for the same reason."""
    CLICK_LABEL = None
    information_calls = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._fake_clicked = None

    def exec(self):
        for button in self.buttons():
            if button.text().replace("&", "") == ClickingQMessageBox.CLICK_LABEL:
                self._fake_clicked = button
                return
        raise AssertionError(
            f"no button with text {ClickingQMessageBox.CLICK_LABEL!r} "
            f"(have: {[b.text() for b in self.buttons()]})"
        )

    def clickedButton(self):
        return self._fake_clicked

    @staticmethod
    def information(*args, **kwargs):
        ClickingQMessageBox.information_calls.append((args, kwargs))


def _run_with_clicked_label(dash, label):
    ClickingQMessageBox.CLICK_LABEL = label
    ClickingQMessageBox.information_calls.clear()
    with patch("ham_dashboard.QMessageBox", ClickingQMessageBox):
        return dash._confirm_rigctld_before_launch("WSJT-X")


def test_server_already_running_proceeds_with_no_prompt():
    print("--- a rigctld server is already running -- proceeds silently ---")
    dash = FakeDashboard()
    dash._rigctld_servers = {"fake_window": object()}
    constructed = []
    with patch("ham_dashboard.QMessageBox", side_effect=constructed.append):
        result = dash._confirm_rigctld_before_launch("WSJT-X")
    assert result is True
    assert not constructed, "should not have prompted at all"
    print("  PASSED\n")


def test_continue_without_it_proceeds():
    print("--- Continue Without It -> proceeds ---")
    dash = FakeDashboard()
    result = _run_with_clicked_label(dash, "Continue Without It")
    assert result is True, result
    print("  PASSED\n")


def test_cancel_aborts():
    print("--- Cancel -> aborts ---")
    dash = FakeDashboard()
    result = _run_with_clicked_label(dash, "Cancel")
    assert result is False, result
    print("  PASSED\n")


def test_open_rigctld_with_radio_selected_opens_dialog_and_aborts_this_launch():
    print("--- Open Rigctld... (radio selected) -> opens dialog, aborts this launch ---")
    dash = FakeDashboard()
    dash._selected_window = "fake_window"
    result = _run_with_clicked_label(dash, "Open Rigctld...")
    assert result is False, result
    assert dash._on_rigctld_button_clicked.called
    print("  PASSED\n")


def test_open_rigctld_with_no_radio_selected_nudges_and_aborts():
    print("--- Open Rigctld... (no radio selected) -> nudges to select one, aborts ---")
    dash = FakeDashboard()
    dash._selected_window = None
    result = _run_with_clicked_label(dash, "Open Rigctld...")
    assert result is False, result
    assert dash._on_rigctld_button_clicked.called
    assert ClickingQMessageBox.information_calls, "should have nudged the user to select a radio"
    print("  PASSED\n")


def main():
    ok = True
    try:
        test_server_already_running_proceeds_with_no_prompt()
        test_continue_without_it_proceeds()
        test_cancel_aborts()
        test_open_rigctld_with_radio_selected_opens_dialog_and_aborts_this_launch()
        test_open_rigctld_with_no_radio_selected_nudges_and_aborts()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        ok = False
    if ok:
        print("ALL RIGCTLD-WARNING TESTS PASSED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
