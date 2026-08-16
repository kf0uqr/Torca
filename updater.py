"""
Checks GitHub for a newer commit than what's currently running, and
can update the app in place. No Qt/threading here -- ham_dashboard.py
wraps check_for_update()/perform_update() in a QThread since both do
real network/subprocess I/O; this module is plain, independently
testable Python.

Two fundamentally different ways this app can be running, so the
update mechanics differ (see detect_mode()):

- "dev": this file's own directory has a .git checkout (e.g. running
  directly via `python main.py` from a git clone, same as this whole
  session's own dev environment). Update = a plain `git pull` in
  place, then reinstalling dependencies with whichever Python
  interpreter is actually running (sys.executable) -- correct
  regardless of which venv that happens to be, matching this
  codebase's "checkout doubles as its own venv" convention.
- "installed": no .git here, but the directory IS writable by the
  current user -- an install.sh install, one made (or updated) since
  install.sh started chown'ing /opt/radione to the installing user
  specifically so this doesn't need root. Update = git clone/refresh
  a small cache checkout under ~/.icom_radio_app_cache, then run THAT
  checkout's own install.sh (no sudo -- see install.sh's own docstring
  for why that's safe here) to refresh app files + venv + dependencies
  in place.
- "unsupported": neither of the above -- e.g. an install from before
  this feature existed, still root-owned, or a copy with no git
  history to compare against at all. Reported clearly rather than
  attempting a partial update and leaving things broken.

Either way, the CURRENTLY RUNNING process keeps executing its already-
loaded old code after a successful update -- there's no in-place hot
reload here, the caller needs to tell the operator to restart.
"""

import os
import pathlib
import shutil
import subprocess
import sys

APP_DIR = pathlib.Path(__file__).resolve().parent
GITHUB_REPO_URL = "https://github.com/kf0uqr/irc.git"
GITHUB_BRANCH = "main"
UPDATE_CACHE_DIR = pathlib.Path.home() / ".icom_radio_app_cache" / "radione-src"

MODE_DEV = "dev"
MODE_INSTALLED = "installed"
MODE_UNSUPPORTED = "unsupported"


class UpdateCheckResult:
    def __init__(self, mode, current_commit, latest_commit):
        self.mode = mode
        self.current_commit = current_commit  # None if unknown (e.g. marker missing)
        self.latest_commit = latest_commit

    @property
    def update_available(self):
        """True only when both commits are known AND differ -- an
        unknown current version (e.g. an install predating the
        .install_commit marker) is deliberately NOT reported as
        available, since we can't actually tell -- see
        version_unknown."""
        return self.current_commit is not None and self.current_commit != self.latest_commit

    @property
    def version_unknown(self):
        return self.current_commit is None


def _run(args, cwd=None, timeout=30):
    return subprocess.run(
        args, cwd=cwd, timeout=timeout, check=True,
        capture_output=True, text=True,
    )


def _require_git():
    if shutil.which("git") is None:
        raise RuntimeError("git is required to check for or perform updates, but wasn't found on this system.")


def _current_commit_dev():
    result = _run(["git", "rev-parse", "HEAD"], cwd=APP_DIR)
    return result.stdout.strip()


def _installed_commit_marker():
    marker = APP_DIR / ".install_commit"
    if marker.exists():
        commit = marker.read_text().strip()
        return commit or None
    return None


def _latest_remote_commit():
    result = _run(["git", "ls-remote", GITHUB_REPO_URL, f"refs/heads/{GITHUB_BRANCH}"], timeout=15)
    line = result.stdout.strip()
    if not line:
        raise RuntimeError("Couldn't read the latest commit from GitHub (empty response).")
    return line.split()[0]


def detect_mode():
    if (APP_DIR / ".git").is_dir():
        return MODE_DEV
    if os.access(APP_DIR, os.W_OK):
        return MODE_INSTALLED
    return MODE_UNSUPPORTED


def check_for_update():
    """Raises on failure (no network, git missing, GitHub unreachable,
    etc.) -- callers catch and report, same convention as every other
    external fetch in this app (pota.py, contests.py, ...)."""
    _require_git()
    mode = detect_mode()
    latest = _latest_remote_commit()
    if mode == MODE_DEV:
        current = _current_commit_dev()
    elif mode == MODE_INSTALLED:
        current = _installed_commit_marker()
    else:
        current = None
    return UpdateCheckResult(mode, current, latest)


def perform_update(status_callback=lambda message: None):
    """Raises on failure -- callers catch and report. status_callback
    is invoked with a short human-readable string at each stage, for a
    caller to relay to the operator (e.g. a progress dialog)."""
    _require_git()
    mode = detect_mode()

    if mode == MODE_DEV:
        status_callback("Pulling the latest changes...")
        _run(["git", "pull", "--ff-only", "origin", GITHUB_BRANCH], cwd=APP_DIR, timeout=60)
        status_callback("Installing any new dependencies...")
        _run(
            [sys.executable, "-m", "pip", "install", "-r", str(APP_DIR / "requirements.txt")],
            cwd=APP_DIR, timeout=300,
        )
        status_callback("Updated -- restart Radione to use the new version.")
        return

    if mode == MODE_INSTALLED:
        status_callback("Fetching the latest source...")
        if (UPDATE_CACHE_DIR / ".git").is_dir():
            _run(["git", "fetch", "origin", GITHUB_BRANCH], cwd=UPDATE_CACHE_DIR, timeout=60)
            _run(["git", "reset", "--hard", f"origin/{GITHUB_BRANCH}"], cwd=UPDATE_CACHE_DIR, timeout=30)
        else:
            if UPDATE_CACHE_DIR.exists():
                shutil.rmtree(UPDATE_CACHE_DIR)
            UPDATE_CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
            _run(
                ["git", "clone", "--branch", GITHUB_BRANCH, "--depth", "1", GITHUB_REPO_URL, str(UPDATE_CACHE_DIR)],
                timeout=120,
            )
        status_callback("Reinstalling to /opt/radione (this may take a minute)...")
        # No sudo: /opt/radione is writable by this user (install.sh's
        # own chown, done at first install specifically for this) --
        # install.sh itself only re-requires root if that's NOT true
        # (see its own docstring).
        _run(["bash", "install.sh"], cwd=UPDATE_CACHE_DIR, timeout=300)
        status_callback("Updated -- restart Radione to use the new version.")
        return

    raise RuntimeError(
        f"This installation can't be self-updated: {APP_DIR} has no .git checkout and isn't "
        "writable by the current user (likely an install from before this feature existed, "
        "or one still owned by root). Re-run install.sh by hand instead: "
        "sudo ./install.sh (after a fresh git pull/clone)."
    )
