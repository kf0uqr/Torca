"""Operator profiles: required on the very first launch, then loaded
automatically, and switched from within the app."""

import json
import os
import types
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QInputDialog, QMessageBox

import operator_profile
from operator_profile import (OperatorProfileDialog, active_profile_name, apply_profile, ensure_active_profile,
                              load_operator_profiles, save_operator_profiles)

HOME = {"callsign": "KF0UQR", "name": "Pat", "address": "1 Main St", "lat": 38.2, "lon": -97.5, "elevation_m": 450.0}
POTA = {"callsign": "KF0UQR/P", "name": "Pat", "address": "", "lat": 40.0, "lon": -105.0, "elevation_m": 2000.0}


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, app):
    """Profiles file and QSettings both redirected to a temp area."""
    monkeypatch.setattr(operator_profile, "OPERATOR_PROFILES_PATH", tmp_path / "operator_profiles.json")
    QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope, str(tmp_path / "qsettings"))
    monkeypatch.setattr(operator_profile, "_SETTINGS_ORG", f"TorcaProfileTest{uuid.uuid4().hex}")
    yield


def _settings():
    return QSettings(operator_profile._SETTINGS_ORG, operator_profile._SETTINGS_APP)


def _forbid_dialog(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the profile dialog must not be shown")
    monkeypatch.setattr(OperatorProfileDialog, "run", staticmethod(boom))


# ---- startup ----

def test_with_a_saved_profile_launch_loads_the_last_used_one_without_asking(monkeypatch):
    save_operator_profiles({"Home": HOME, "POTA": POTA})
    _settings().setValue("last_operator_profile", "POTA")
    _forbid_dialog(monkeypatch)
    assert ensure_active_profile() == "POTA"
    settings = _settings()
    assert settings.value("operator_callsign") == "KF0UQR/P" and float(settings.value("operator_lat")) == 40.0
    assert float(settings.value("operator_elevation_m")) == 2000.0 and active_profile_name() == "POTA"


def test_profile_values_are_reapplied_each_launch_even_if_settings_drifted(monkeypatch):
    save_operator_profiles({"Home": HOME})
    _settings().setValue("last_operator_profile", "Home")
    _settings().setValue("operator_callsign", "SOMEONE")
    _forbid_dialog(monkeypatch)
    ensure_active_profile()
    assert _settings().value("operator_callsign") == "KF0UQR"


def test_missing_or_deleted_last_profile_falls_back_to_the_first_without_asking(monkeypatch):
    save_operator_profiles({"Zulu": POTA, "Alpha": HOME})
    _settings().setValue("last_operator_profile", "Deleted")
    _forbid_dialog(monkeypatch)
    assert ensure_active_profile() == "Alpha" and active_profile_name() == "Alpha"
    _settings().remove("last_operator_profile")
    assert ensure_active_profile() == "Alpha"


def test_first_launch_shows_the_required_dialog_and_returns_the_new_profile(monkeypatch):
    seen = {}

    def fake_run(parent=None, required=False):
        seen["required"] = required
        save_operator_profiles({"Home": HOME})
        _settings().setValue("last_operator_profile", "Home")
        return True

    monkeypatch.setattr(OperatorProfileDialog, "run", staticmethod(fake_run))
    assert ensure_active_profile() == "Home" and seen == {"required": True}


def test_declining_the_required_dialog_means_quit(monkeypatch):
    monkeypatch.setattr(OperatorProfileDialog, "run", staticmethod(lambda parent=None, required=False: False))
    assert ensure_active_profile() is None


def test_once_a_profile_exists_the_dialog_never_returns_on_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(OperatorProfileDialog, "run", staticmethod(lambda parent=None, required=False: calls.append(1) or True))
    save_operator_profiles({"Home": HOME})
    for _ in range(3):
        ensure_active_profile()
    assert calls == []


def test_apply_profile_rejects_unknown_names():
    assert apply_profile("Nope") is False and active_profile_name() is None
    save_operator_profiles({"Home": HOME})
    assert apply_profile("Home") is True and active_profile_name() == "Home"


# ---- the real dialog in required mode ----

def test_required_dialog_has_no_cancel_only_quit(app):
    dialog = OperatorProfileDialog(required=True)
    buttons = dialog.findChild(QDialogButtonBox)
    labels = [b.text().replace("&", "") for b in buttons.buttons()]
    assert "Cancel" not in labels and "Quit TORCA" in labels and "OK" in labels
    optional_dialog = OperatorProfileDialog(required=False)          # keep a reference: a temporary would be deleted
    optional = optional_dialog.findChild(QDialogButtonBox)
    assert "Cancel" in [b.text().replace("&", "") for b in optional.buttons()]


def test_required_dialog_asks_for_a_name_and_saves_the_profile_on_ok(app, monkeypatch):
    dialog = OperatorProfileDialog(required=True)
    dialog.callsign_input.setText("kf0uqr")
    dialog.lat_input.setValue(38.2)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Home", True)))
    dialog._on_accept()
    assert dialog.result() == QDialog.Accepted
    assert "Home" in load_operator_profiles() and load_operator_profiles()["Home"]["callsign"] == "KF0UQR"
    assert active_profile_name() == "Home"
    assert _settings().value("operator_callsign") == "KF0UQR"


def test_required_dialog_will_not_close_without_a_named_profile(app, monkeypatch):
    dialog = OperatorProfileDialog(required=True)
    told = []
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))     # name prompt cancelled
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: told.append(a[2] if len(a) > 2 else "")))
    dialog._on_accept()
    assert dialog.result() != QDialog.Accepted and told and "name" in told[0].lower()
    assert load_operator_profiles() == {} and active_profile_name() is None


def test_optional_dialog_still_accepts_without_a_profile_name(app):
    dialog = OperatorProfileDialog(required=False)
    dialog.callsign_input.setText("W1AW")
    dialog._on_accept()
    assert dialog.result() == QDialog.Accepted and load_operator_profiles() == {}


# ---- switching profiles from within the app ----

def _fake_dashboard(monkeypatch, answer):
    import ham_dashboard
    events = []
    cls = ham_dashboard.HamClockWindow
    fake = types.SimpleNamespace()
    fake._restart_application = lambda: events.append("restart")
    fake._apply_operator_settings = lambda: events.append("reread")
    fake._offer_profile_switch = cls._offer_profile_switch.__get__(fake)
    monkeypatch.setattr(ham_dashboard.QMessageBox, "question", staticmethod(lambda *a, **k: answer))
    monkeypatch.setattr(ham_dashboard, "apply_profile", lambda name: events.append(f"apply:{name}"))
    return cls, fake, events, ham_dashboard


def test_switching_profile_restarts_into_the_new_profiles_workspace(monkeypatch):
    import ham_dashboard
    cls, fake, events, mod = _fake_dashboard(monkeypatch, QMessageBox.Yes)
    fake._offer_profile_switch("Home", "POTA")
    assert events == ["restart"]


def test_declining_the_switch_puts_the_old_profile_back(monkeypatch):
    cls, fake, events, mod = _fake_dashboard(monkeypatch, QMessageBox.No)
    fake._offer_profile_switch("Home", "POTA")
    assert events == ["apply:Home", "reread"]


def _profile_button_harness(monkeypatch, run_result, names):
    import ham_dashboard
    cls = ham_dashboard.HamClockWindow
    events = []
    fake = types.SimpleNamespace(_apply_operator_settings=lambda: events.append("reread"),
                                 _offer_profile_switch=lambda b, a: events.append(f"offer:{b}->{a}"))
    queue = list(names)
    monkeypatch.setattr(ham_dashboard, "active_profile_name", lambda: queue.pop(0))
    monkeypatch.setattr(ham_dashboard.OperatorProfileDialog, "run", staticmethod(lambda parent=None, required=False: run_result))
    cls._on_profile_button_clicked(fake)
    return events


def test_picking_a_different_profile_offers_the_switch(monkeypatch):
    assert _profile_button_harness(monkeypatch, True, ["Home", "POTA"]) == ["reread", "offer:Home->POTA"]


def test_creating_a_new_profile_offers_the_switch_too(monkeypatch):
    assert _profile_button_harness(monkeypatch, True, ["Home", "Field Day"]) == ["reread", "offer:Home->Field Day"]


def test_editing_the_same_profile_just_applies_the_changes(monkeypatch):
    assert _profile_button_harness(monkeypatch, True, ["Home", "Home"]) == ["reread"]


def test_cancelling_the_profile_dialog_does_nothing_more(monkeypatch):
    assert _profile_button_harness(monkeypatch, False, ["Home", "Home"]) == ["reread"]
