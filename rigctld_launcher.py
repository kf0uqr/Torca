"""
ManagedRigctld: launches and owns a local Hamlib `rigctld` subprocess so
radios rigplane has no native backend for (currently just the truSDX --
see RADIO_PROFILES["truSDX"] in constants.py) can be used with nothing
more than the serial port + baud rate every other USB radio already
asks for. Without this, the operator would have to start and babysit
`rigctld` by hand in a separate terminal before connecting; RadioWorker
runs one instead, scoped to the connection's own lifetime (spawned
alongside it, torn down alongside it -- see radio_worker.py's
_start_managed_rigctld/_teardown_connection).

Binds rigctld to 127.0.0.1 on an OS-assigned ephemeral port (never a
fixed one) so multiple managed radios -- or a managed radio alongside
someone's own manually-run rigctld on the default 4532 -- can't collide.

Model fallback: `hamlib_models` is tried in order (see
RADIO_PROFILES["truSDX"]["hamlib_models"]) because a newer Hamlib model
number (e.g. 2055, the dedicated (tr)uSDX personality) may not exist in
whatever Hamlib build happens to be installed -- confirmed directly that
rigctld responds to an unrecognized -m by printing an error and exiting
within a fraction of a second rather than ever binding the port, which
is exactly the signal used below to move on to the next candidate (a
genuinely bad serial port, by contrast, was confirmed to leave rigctld
running and listening regardless -- it just answers every CAT query
with an I/O error -- so that failure mode surfaces later, through the
normal RigctldClientRadio command path, not here).
"""

import asyncio
import shutil
import socket


class ManagedRigctldError(RuntimeError):
    pass


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ManagedRigctld:
    def __init__(self, serial_port, baud_rate, hamlib_models):
        self._serial_port = serial_port
        self._baud_rate = baud_rate
        self._hamlib_models = hamlib_models
        self._process = None
        self.host = "127.0.0.1"
        self.port = None
        self.model_used = None

    async def start(self, ready_timeout=5.0):
        if shutil.which("rigctld") is None:
            raise ManagedRigctldError(
                "rigctld (Hamlib) isn't installed or isn't on PATH -- install "
                "Hamlib (e.g. `apt install libhamlib-utils` / `brew install "
                "hamlib`) to use this radio."
            )

        last_output = ""
        for model in self._hamlib_models:
            port = _find_free_port()
            process = await asyncio.create_subprocess_exec(
                "rigctld", "-m", str(model), "-r", self._serial_port,
                "-s", str(self._baud_rate), "-t", str(port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            if await self._wait_until_ready(process, port, ready_timeout):
                self._process = process
                self.port = port
                self.model_used = model
                return
            last_output = (await self._drain_output(process)).strip()
            await self._terminate(process)

        tried = ", ".join(str(m) for m in self._hamlib_models)
        detail = f": {last_output}" if last_output else ""
        raise ManagedRigctldError(
            f"Couldn't start rigctld (tried Hamlib model(s) {tried}){detail}"
        )

    async def _wait_until_ready(self, process, port, timeout):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if process.returncode is not None:
                # Exited already (e.g. "Unknown rig num") -- it's never
                # going to start listening, no point continuing to poll.
                return False
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, port), timeout=0.3
                )
            except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.1)
                continue
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        return False

    async def _drain_output(self, process, limit=4096):
        try:
            data = await asyncio.wait_for(process.stdout.read(limit), timeout=1.0)
            return data.decode(errors="replace")
        except Exception:
            return ""

    async def _terminate(self, process):
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def stop(self):
        if self._process is not None:
            await self._terminate(self._process)
            self._process = None
