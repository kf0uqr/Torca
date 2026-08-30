"""
Cloudflare Tunnel management for Remote Access -- exposes the local
Remote Access web server (web_remote/server.py) at a stable hostname on
your own domain (e.g. torca.kf0uqr.com) via a NAMED, DNS-routed
`cloudflared` tunnel that this app creates and owns.

Assumes `cloudflared` is already installed and authenticated (a one-
time `cloudflared tunnel login`, outside this app -- see README.md's
Remote Access section) -- this module only ever creates/manages ONE
tunnel of its own (name configurable, "torca" by default) and writes
its OWN config file under ~/.torca/cloudflared/, entirely separate
from ~/.cloudflared/config.yml or any other tunnel already set up on
this machine (confirmed live: this machine already has an unrelated
tunnel, "torn-war-boss", which none of this touches).

Two pieces, deliberately using two different concurrency patterns
already established elsewhere in this codebase rather than inventing a
third:

- CloudflareSetupWorker: the one-shot "create tunnel / route DNS /
  write config" sequence -- a few blocking `cloudflared` CLI calls (each
  a couple seconds, network-dependent) run off the GUI thread. Same
  "small dedicated QThread for one blocking operation" shape as
  ham_dashboard.py's PskReporterWorker, not the full persistent-
  connection machinery RadioWorker uses.
- CloudflareTunnel: the actual long-lived `cloudflared tunnel run`
  process once set up -- a QProcess-managed subprocess with start()/
  stop()/status()/error() signals, modeled directly on
  direwolf_backend.py's DirewolfBackend (the existing managed-external-
  process pattern in this app), not the fire-and-forget
  subprocess.Popen used for one-shot app launches like WSJT-X.
"""

import json
import pathlib
import shutil
import subprocess

from PySide6.QtCore import QObject, QProcess, QThread, Signal

CONFIG_DIR = pathlib.Path.home() / ".torca" / "cloudflared"
CLOUDFLARED_DIR = pathlib.Path.home() / ".cloudflared"

DEFAULT_TUNNEL_NAME = "torca"
DEFAULT_LOCAL_PORT = 8765


def cloudflared_available() -> bool:
    """True if a `cloudflared` executable is on PATH -- checked before
    offering Remote Access's Cloudflare setup at all, same
    fail-fast-with-a-clear-message reasoning as
    direwolf_backend.direwolf_available()."""
    return shutil.which("cloudflared") is not None


def cloudflared_authenticated() -> bool:
    """True if `cloudflared tunnel login` has already been run for
    SOME Cloudflare account on this machine (cert.pem present) --
    Remote Access's one-time setup needs this to already exist; it's a
    browser-based login flow this app has no reasonable way to drive
    itself, so it's surfaced as a clear instruction instead (see
    README.md's Remote Access section) rather than attempted here."""
    return (CLOUDFLARED_DIR / "cert.pem").exists()


class CloudflareSetupWorker(QThread):
    """Runs the one-time (idempotent -- safe to re-run) `cloudflared`
    setup sequence for one named tunnel: create it if it doesn't
    already exist, route the given hostname's DNS to it, then write
    this app's own config.yml pointing that hostname at the local
    Remote Access web server. Emits finished_ok(tunnel_id) on success,
    failed(message) otherwise -- never raises back into the GUI
    thread."""

    status = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, tunnel_name, hostname, local_port, parent=None):
        super().__init__(parent)
        self.tunnel_name = tunnel_name
        self.hostname = hostname
        self.local_port = local_port

    def run(self):
        try:
            tunnel_id = self._create_or_get_tunnel()
            self._route_dns()
            config_path = self._write_config(tunnel_id)
            self.status.emit(f"Config written to {config_path}.")
            self.finished_ok.emit(tunnel_id)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _run(self, args, timeout=30):
        self.status.emit(f"Running: cloudflared {' '.join(args)}")
        result = subprocess.run(
            ["cloudflared", *args], capture_output=True, text=True, timeout=timeout
        )
        return result

    def _create_or_get_tunnel(self) -> str:
        result = self._run(["tunnel", "create", self.tunnel_name])
        if result.returncode != 0 and "already exists" not in (result.stderr + result.stdout).lower():
            raise RuntimeError(f"cloudflared tunnel create failed: {result.stderr.strip() or result.stdout.strip()}")

        list_result = self._run(["tunnel", "list", "--output", "json"])
        if list_result.returncode != 0:
            raise RuntimeError(f"cloudflared tunnel list failed: {list_result.stderr.strip()}")
        try:
            tunnels = json.loads(list_result.stdout)
        except ValueError as exc:
            raise RuntimeError(f"Couldn't parse `cloudflared tunnel list` output: {exc}")
        for tunnel in tunnels:
            if tunnel.get("name") == self.tunnel_name:
                return tunnel["id"]
        raise RuntimeError(f"Tunnel '{self.tunnel_name}' was created but not found in `cloudflared tunnel list`.")

    def _route_dns(self):
        result = self._run(["tunnel", "route", "dns", self.tunnel_name, self.hostname])
        if result.returncode != 0 and "already exists" not in (result.stderr + result.stdout).lower():
            raise RuntimeError(f"cloudflared tunnel route dns failed: {result.stderr.strip() or result.stdout.strip()}")

    def _write_config(self, tunnel_id: str) -> pathlib.Path:
        credentials_file = CLOUDFLARED_DIR / f"{tunnel_id}.json"
        if not credentials_file.exists():
            raise RuntimeError(
                f"Expected credentials file not found: {credentials_file} -- "
                "`cloudflared tunnel create` should have written this."
            )
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config_path = CONFIG_DIR / f"{self.tunnel_name}.yml"
        content = (
            f"tunnel: {tunnel_id}\n"
            f"credentials-file: {credentials_file}\n"
            "ingress:\n"
            f"  - hostname: {self.hostname}\n"
            f"    service: http://127.0.0.1:{self.local_port}\n"
            "  - service: http_status:404\n"
        )
        config_path.write_text(content)
        return config_path


class CloudflareTunnel(QObject):
    """Manages the long-lived `cloudflared tunnel run` subprocess for
    an already-set-up tunnel (see CloudflareSetupWorker) -- QProcess-
    based start()/stop(), status(str)/error(str) signals, same
    convention as direwolf_backend.DirewolfBackend."""

    status = Signal(str)
    error = Signal(str)

    def __init__(self, tunnel_name, parent=None):
        super().__init__(parent)
        self.tunnel_name = tunnel_name
        self._config_path = CONFIG_DIR / f"{tunnel_name}.yml"
        self._stopping = False

        self._process = QProcess(self)
        self._process.readyReadStandardOutput.connect(self._on_process_output)
        self._process.readyReadStandardError.connect(self._on_process_output)
        self._process.errorOccurred.connect(self._on_process_error)

    def is_running(self) -> bool:
        return self._process.state() != QProcess.NotRunning

    def start(self):
        if not self._config_path.exists():
            self.error.emit(
                f"Cloudflare Tunnel: no config found at {self._config_path} -- "
                "run setup first."
            )
            return
        self._stopping = False
        self._process.start("cloudflared", ["tunnel", "--config", str(self._config_path), "run"])
        self.status.emit(f"Cloudflare Tunnel: starting (config {self._config_path}).")

    def stop(self):
        self._stopping = True
        if self._process.state() != QProcess.NotRunning:
            self._process.terminate()
            if not self._process.waitForFinished(3000):
                self._process.kill()

    def _on_process_output(self):
        text = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        text += bytes(self._process.readAllStandardError()).decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.strip():
                self.status.emit(f"Cloudflare Tunnel: {line.strip()}")

    def _on_process_error(self, _proc_error):
        if self._stopping:
            return  # expected SIGTERM exit from our own stop() -- not a real failure
        self.error.emit(f"Cloudflare Tunnel: process error ({self._process.errorString()}).")
