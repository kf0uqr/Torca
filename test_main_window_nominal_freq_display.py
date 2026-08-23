"""
Test for main_window.py's nominal_freq_display (the left-side, pre-
Doppler-correction overlay) visibility logic -- per explicit
instruction, it should only be visible while satellite tracking is
actually running, not just whenever the radio has a satellite-tracking
role. Exercises the actual bound method (_update_nominal_freq_display_
visibility) against a lightweight fake rather than constructing a real
RadioWindow (which needs a full radio_worker thread, satellite
session, connection details, etc. -- much heavier than this pure
visibility-toggle logic needs).

Needs a real QApplication (offscreen platform is fine): run with
QT_QPA_PLATFORM=offscreen.

Run directly: QT_QPA_PLATFORM=offscreen ./bin/python3 test_main_window_nominal_freq_display.py
"""

import sys

from PySide6.QtWidgets import QApplication, QLabel

_app = QApplication.instance() or QApplication(sys.argv)

import main_window


class FakeRadioWindow:
    _update_nominal_freq_display_visibility = main_window.RadioWindow._update_nominal_freq_display_visibility

    def __init__(self, role):
        self._role = role
        self.nominal_freq_display = QLabel()
        self.nominal_freq_display.setVisible(False)  # matches the real constructor's starting state


def test_hidden_when_tracking_starts_for_non_sat_role():
    print("--- non_sat role: stays hidden even if tracking starts ---")
    window = FakeRadioWindow("non_sat")
    window._update_nominal_freq_display_visibility(True)
    assert not window.nominal_freq_display.isVisible()
    print("  PASSED\n")


def test_shown_and_hidden_for_each_satellite_role():
    print("--- full_duplex/downlink/uplink: shown while tracking, hidden once stopped ---")
    for role in ("full_duplex", "downlink", "uplink"):
        window = FakeRadioWindow(role)
        assert not window.nominal_freq_display.isVisible(), f"{role}: should start hidden"

        window._update_nominal_freq_display_visibility(True)
        assert window.nominal_freq_display.isVisible(), f"{role}: should be visible while tracking"

        window._update_nominal_freq_display_visibility(False)
        assert not window.nominal_freq_display.isVisible(), f"{role}: should be hidden once tracking stops"
    print("  PASSED\n")


def main():
    ok = True
    try:
        test_hidden_when_tracking_starts_for_non_sat_role()
        test_shown_and_hidden_for_each_satellite_role()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        ok = False
    if ok:
        print("ALL NOMINAL-FREQ-DISPLAY TESTS PASSED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
