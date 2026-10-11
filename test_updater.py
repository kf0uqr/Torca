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
        _update_progress_dialog=None,  # no progress dialog open
    )
    fake._close_update_progress_dialog = cls._close_update_progress_dialog.__get__(fake)
    fake._action_parent = lambda: fake  # dialogs open over the window an Action was launched from
    fake._action_parent = lambda: fake      # dialogs open over the window an Action was launched from; here, just this
    fake.window = lambda: fake  # unhosted: the dashboard is its own top-level window
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


# ---- update progress dialog ----

def _qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_progress_dialog_shows_status_and_an_animated_busy_bar():
    _qapp()
    from update_progress_dialog import UpdateProgressDialog
    dialog = UpdateProgressDialog()
    dialog.show()
    assert dialog.status_label.text() == "Starting..."
    assert (dialog.bar.minimum(), dialog.bar.maximum()) == (0, 0)        # indeterminate: keeps animating
    dialog.set_status("Pulling the latest changes...")
    assert dialog.status_label.text() == "Pulling the latest changes..."
    assert dialog._timer.isActive()
    dialog.finish()
    assert not dialog._timer.isActive() and not dialog.isVisible()


def test_progress_dialog_cannot_be_dismissed_until_the_update_ends():
    _qapp()
    from PySide6.QtGui import QCloseEvent
    from update_progress_dialog import UpdateProgressDialog
    dialog = UpdateProgressDialog()
    dialog.show()
    dialog.reject()                                  # what Esc does
    assert dialog.isVisible()
    event = QCloseEvent()
    dialog.closeEvent(event)                         # window-manager close
    assert not event.isAccepted() and dialog.isVisible()
    dialog.finish()
    assert not dialog.isVisible()


def test_progress_dialog_elapsed_clock_formats_minutes_and_seconds():
    from update_progress_dialog import format_elapsed
    assert [format_elapsed(s) for s in (0, 5, 59, 60, 61, 125, -3)] == ["0:00", "0:05", "0:59", "1:00", "1:01", "2:05", "0:00"]


def test_dashboard_update_flow_opens_progress_dialog_relays_status_and_closes_it(monkeypatch):
    import types
    _qapp()
    import ham_dashboard
    from update_progress_dialog import UpdateProgressDialog

    class FakeWorker:
        def __init__(self, parent=None):
            self.status = types.SimpleNamespace(connect=lambda f: setattr(self, "on_status", f))
            self.succeeded = types.SimpleNamespace(connect=lambda f: setattr(self, "on_succeeded", f))
            self.failed = types.SimpleNamespace(connect=lambda f: setattr(self, "on_failed", f))
        def start(self): pass

    monkeypatch.setattr(ham_dashboard, "UpdatePerformWorker", FakeWorker)
    cls = ham_dashboard.HamClockWindow
    for outcome in ("succeeded", "failed"):
        events = []
        fake = types.SimpleNamespace(
            _update_progress_dialog=None, _update_perform_worker=None,
            update_button=types.SimpleNamespace(setEnabled=lambda *_: None, setText=lambda *_: None),
        )
        fake._on_update_status = cls._on_update_status.__get__(fake)
        fake._close_update_progress_dialog = cls._close_update_progress_dialog.__get__(fake)
        fake._action_parent = lambda: fake
        fake._restart_application = lambda: events.append("restart")
        fake._on_update_succeeded = lambda: (fake._close_update_progress_dialog(), events.append("prompt"))
        fake._on_update_failed = lambda m: (fake._close_update_progress_dialog(), events.append("error"))
        parent = None
        monkeypatch.setattr(ham_dashboard, "UpdateProgressDialog", lambda p=None: UpdateProgressDialog())
        cls._start_update(fake)
        dialog = fake._update_progress_dialog
        assert isinstance(dialog, UpdateProgressDialog) and dialog.isVisible()   # appears as soon as "Yes" is chosen
        fake._update_perform_worker.on_status("Installing any new dependencies...")
        assert dialog.status_label.text() == "Installing any new dependencies..."
        (fake._update_perform_worker.on_succeeded() if outcome == "succeeded"
         else fake._update_perform_worker.on_failed("boom"))
        assert not dialog.isVisible() and fake._update_progress_dialog is None   # gone BEFORE the prompt / error box
        assert events == (["prompt"] if outcome == "succeeded" else ["error"])


# ---- rebuilding the FT8 decoder on a git-checkout update ----

def test_update_rebuilds_the_ft8_decoder_best_effort(tmp_path, monkeypatch):
    import updater
    (tmp_path / "vendor" / "ft8_lib").mkdir(parents=True)
    (tmp_path / "vendor" / "ft8_lib" / "Makefile").write_text("decode_ft8:\n")
    monkeypatch.setattr(updater, "APP_DIR", tmp_path)
    monkeypatch.setattr(updater.shutil, "which", lambda name: "/usr/bin/make")
    calls, messages = [], []
    monkeypatch.setattr(updater, "_run", lambda args, cwd=None, timeout=30: calls.append((args, cwd)))
    updater._rebuild_ft8_decoder(messages.append)
    assert calls == [(["make", "decode_ft8", "gen_ft8"], tmp_path / "vendor" / "ft8_lib")]
    assert messages == ["Building the FT8 decoder..."]


def test_ft8_rebuild_is_skipped_without_make_or_the_source_and_never_fails_the_update(tmp_path, monkeypatch):
    import updater
    monkeypatch.setattr(updater, "APP_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(updater, "_run", lambda *a, **k: calls.append(1))
    updater._rebuild_ft8_decoder(lambda m: None)                       # no vendor/ft8_lib at all
    (tmp_path / "vendor" / "ft8_lib").mkdir(parents=True)
    (tmp_path / "vendor" / "ft8_lib" / "Makefile").write_text("x:\n")
    monkeypatch.setattr(updater.shutil, "which", lambda name: None)    # and no make
    updater._rebuild_ft8_decoder(lambda m: None)
    assert calls == []

    monkeypatch.setattr(updater.shutil, "which", lambda name: "/usr/bin/make")
    def failing(*args, **kwargs):
        raise updater.subprocess.CalledProcessError(2, "make")
    monkeypatch.setattr(updater, "_run", failing)
    updater._rebuild_ft8_decoder(lambda m: None)                       # a failed build must not raise


def test_progress_dialog_wrapped_text_is_never_clipped():
    app = _qapp()
    from update_progress_dialog import UpdateProgressDialog
    dialog = UpdateProgressDialog()
    dialog.show()
    dialog.set_status("Installing Python dependencies, this can take a minute or two on a slow connection. " * 2)
    app.processEvents()
    for label in (dialog.status_label, dialog.hint_label):
        assert label.height() >= label.heightForWidth(label.width()), label.text()
    assert dialog.hint_label.geometry().bottom() <= dialog.rect().bottom()
    dialog.finish()
