"""Tests for updater.py's restart-after-update helper (no network/git
involved -- check_for_update/perform_update shell out to real git/pip
and aren't exercised here)."""

import sys
import time

import updater


def _wait_for(path, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def test_relaunch_command_uses_same_interpreter_args_and_app_main(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--foo"])
    cmd = updater.relaunch_command()
    assert cmd[0] == sys.executable
    assert cmd[1] == str(updater.APP_DIR / "main.py")
    assert cmd[2:] == ["--foo"]


def test_relaunch_waits_for_this_process_to_exit_then_starts_new_one(tmp_path, monkeypatch):
    marker = tmp_path / "relaunched"
    monkeypatch.setattr(
        updater, "relaunch_command",
        lambda: [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ok')"],
    )
    updater.schedule_relaunch_after_exit()
    helper = updater._relaunch_helper

    # "This process" is still alive (the helper's stdin pipe is still
    # open): the new instance must NOT have started yet.
    time.sleep(1.0)
    assert not marker.exists()
    assert helper.poll() is None

    # Simulate this process exiting: the pipe's only writer goes away.
    helper.stdin.close()
    assert _wait_for(marker), "helper never started the new instance after the old one exited"
    helper.wait(timeout=10)


# ---- ham_dashboard.py's post-update prompt ----

def _prompt_harness(monkeypatch, choose_restart, schedule_error=None):
    import os
    import types
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import ham_dashboard

    class FakeBox:
        Question, AcceptRole, RejectRole = "q", "accept", "reject"
        last = None

        def __init__(self, parent=None):
            self.buttons = {}
            FakeBox.last = self

        def setIcon(self, *_): pass
        def setWindowTitle(self, *_): pass
        def setText(self, text): self.text = text
        def setInformativeText(self, text): self.info = text
        def setDefaultButton(self, *_): pass
        def exec(self): pass

        def addButton(self, label, role):
            self.buttons[label] = object()
            return self.buttons[label]

        def clickedButton(self):
            return self.buttons["Restart Now" if choose_restart else "Later"]

    events = []
    FakeBox.warning = staticmethod(lambda *args, **kwargs: events.append("warned"))
    monkeypatch.setattr(ham_dashboard, "QMessageBox", FakeBox)
    monkeypatch.setattr(ham_dashboard.QApplication, "quit", staticmethod(lambda: events.append("quit")))

    def fake_schedule():
        if schedule_error:
            raise schedule_error
        events.append("scheduled")
    monkeypatch.setattr(ham_dashboard.updater, "schedule_relaunch_after_exit", fake_schedule)

    cls = ham_dashboard.HamClockWindow
    fake = types.SimpleNamespace(
        update_button=types.SimpleNamespace(setEnabled=lambda *_: None, setText=lambda *_: None),
        close=lambda: events.append("close"),
    )
    fake._restart_application = cls._restart_application.__get__(fake)
    return cls._on_update_succeeded.__get__(fake), events, FakeBox


def test_update_prompt_restart_now_schedules_relaunch_then_closes_and_quits(monkeypatch):
    succeeded, events, box = _prompt_harness(monkeypatch, choose_restart=True)
    succeeded()
    assert events == ["scheduled", "close", "quit"]  # relaunch is queued BEFORE shutdown begins
    assert set(box.last.buttons) == {"Restart Now", "Later"}


def test_update_prompt_later_keeps_running(monkeypatch):
    succeeded, events, _ = _prompt_harness(monkeypatch, choose_restart=False)
    succeeded()
    assert events == []


def test_update_prompt_does_not_quit_if_relaunch_cannot_be_scheduled(monkeypatch):
    succeeded, events, _ = _prompt_harness(monkeypatch, choose_restart=True, schedule_error=OSError("no exec"))
    succeeded()
    assert events == ["warned"]  # told to restart by hand; never quit without a restart queued
