"""
RemoteWebServer: runs the Remote Access FastAPI app (app.py) inside its
own asyncio event loop on a dedicated QThread -- the exact same shape
radio_worker.py's RadioWorker already uses (a QThread that owns a
private asyncio loop in run(), stopped by scheduling a callback onto
that loop from another thread rather than touching it directly).

Binds to 127.0.0.1 ONLY, never 0.0.0.0 -- this is meant to be reached
from the internet exclusively through a Cloudflare Tunnel
(cloudflare_tunnel.py), whose local origin connection is itself
loopback-only. Nothing about this class opens the port to the LAN/WAN.

uvicorn.Server.serve() is safe to call from a non-main thread as-is:
its own capture_signals() (confirmed by reading uvicorn 0.52's source)
already checks threading.current_thread() is threading.main_thread()
and skips installing OS signal handlers when it isn't -- no extra
config needed here to avoid the usual "signal only works in main
thread" crash.
"""

import asyncio

from PySide6.QtCore import QThread, Signal
import uvicorn

from web_remote.app import create_app


class RemoteWebServer(QThread):
    started = Signal()
    stopped = Signal()
    error = Signal(str)

    def __init__(self, dashboard, host="127.0.0.1", port=8765, token=None, parent=None):
        super().__init__(parent)
        self.dashboard = dashboard
        self.host = host
        self.port = port
        self.token = token
        self.loop = None
        self._uvicorn_server = None

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        app = create_app(self.dashboard, self.token)
        config = uvicorn.Config(app, host=self.host, port=self.port, loop="none", log_level="warning")
        self._uvicorn_server = uvicorn.Server(config)
        try:
            self.loop.run_until_complete(self._serve())
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.loop.close()
            self.loop = None

    async def _serve(self):
        self.started.emit()
        try:
            await self._uvicorn_server.serve()
        finally:
            self.stopped.emit()

    def stop(self):
        """Thread-safe: call from the GUI thread. Blocks (briefly) for
        the server loop to actually wind down, same "wait for the
        thread to really finish" convention as
        direwolf_backend.py._process.waitForFinished()."""
        loop = self.loop
        server = self._uvicorn_server
        if loop is None or server is None:
            return
        loop.call_soon_threadsafe(setattr, server, "should_exit", True)
        self.wait(5000)
