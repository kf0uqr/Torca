"""
Two independent pieces that let external CAT-aware apps (WSJT-X, JTDX,
fldigi, etc.) work with this app's radio connection: a "Launch WSJT-X"
helper (finds/remembers the executable, launches it into its own
isolated --rig-name profile) and RigctldServer, a minimal Hamlib
rigctld-compatible TCP server so those apps can drive the radio through
this app's existing connection instead of opening their own.
"""

import os
import platform
import shutil
import subprocess

from PySide6.QtNetwork import QHostAddress, QTcpServer

# a file we don't own (unlike a wrong guess in our own CONTROL_DEFINITIONS,
# which just shows an error). Instead: launch WSJT-X into an entirely
# separate, isolated profile using its confirmed --rig-name=NAME option
# (documented in WSJT-X's own man page). First launch is a blank profile
# needing one-time setup; every launch after that reuses the same profile
# with those settings already in place -- this never touches the user's
# main/default WSJT-X profile at all.
WSJTX_RIG_NAME = "RadioApp"

# Common install locations to check before asking the user to browse.
# Not exhaustive -- package managers/custom installs vary -- just a
# starting guess per platform.
_WSJTX_CANDIDATE_PATHS = {
    "Windows": [
        r"C:\WSJT-X\bin\wsjtx.exe",
        r"C:\Program Files\WSJT-X\wsjtx.exe",
        r"C:\Program Files (x86)\WSJT-X\wsjtx.exe",
    ],
    "Darwin": [
        "/Applications/WSJT-X.app/Contents/MacOS/wsjtx",
    ],
    "Linux": [
        "/usr/bin/wsjtx",
        "/usr/local/bin/wsjtx",
    ],
}


def find_wsjtx_executable():
    """Best-effort auto-detection: checks the PATH, then common per-OS
    install locations. Returns a path string or None if nothing was found
    -- callers should fall back to asking the user to browse for it."""
    on_path = shutil.which("wsjtx")
    if on_path:
        return on_path
    for candidate in _WSJTX_CANDIDATE_PATHS.get(platform.system(), []):
        if os.path.isfile(candidate):
            return candidate
    return None


def launch_wsjtx(executable_path):
    """Launches WSJT-X into its own isolated --rig-name profile (see
    WSJTX_RIG_NAME above). Raises OSError/subprocess errors on failure --
    callers should catch and report those."""
    subprocess.Popen([executable_path, f"--rig-name={WSJTX_RIG_NAME}"])


# ==================== rigctld ("NET rigctl") server ====================
#
# A minimal Hamlib rigctld-compatible TCP server, so other CAT-aware apps
# (WSJT-X, JTDX, fldigi, ...) can drive THIS app's already-open radio
# connection -- selecting rig model 2 ("Hamlib NET rigctl") pointed at
# 127.0.0.1:4532 -- instead of needing their own separate CAT connection
# to the radio (which the radio's CI-V/serial port often can't share
# with two programs at once anyway).
#
# Protocol confirmed against Hamlib's own rigctld documentation: plain
# ASCII, newline-terminated commands; lowercase = get, uppercase = set;
# "RPRT x\n" (x=0 for success) replies to set commands; get commands
# return one value per line. Default port 4532 is Hamlib's documented
# default for "NET rigctl".
#
# The \dump_state handshake (WSJT-X's Hamlib backend sends this
# immediately on connect and requires a correctly-shaped response before
# it will proceed -- a real bug in another rigctld-compatible server,
# AetherSDR GitHub issue #63, shows what a malformed one does: 9-55s
# hangs or outright "IO error" connection failures) is now built by
# tracing Hamlib's actual netrigctl_open() source line-by-line (see
# RigctldServer._dump_state_response()'s docstring for the confirmed
# field sequence) rather than reconstructed from documentation alone. An
# earlier version of this got two things wrong that source-tracing
# caught: it omitted a required ITU-region line (shifting every
# subsequent field by one position) and sent trailing lines that were
# never supposed to be there for protocol version 0. What's still
# inferred rather than directly confirmed: the exact end-of-list
# sentinel values (RIG_IS_FRNG_END etc.) -- based on long-established
# Hamlib convention, not the literal macro source.
RIGCTLD_DEFAULT_PORT = 4532


class RigctldServer(QTcpServer):
    """Runs on the GUI thread -- QTcpServer/QTcpSocket are event-driven
    via Qt signals, so no separate thread is needed. Talks to the actual
    radio only through the callback functions passed in, reusing
    RadioWindow's existing worker/widget plumbing rather than
    duplicating radio-control logic."""

    def __init__(self, get_freq, set_freq, get_mode, set_mode, get_ptt, set_ptt,
                 port=RIGCTLD_DEFAULT_PORT, parent=None):
        super().__init__(parent)
        self._get_freq = get_freq
        self._set_freq = set_freq
        self._get_mode = get_mode
        self._set_mode = set_mode
        self._get_ptt = get_ptt
        self._set_ptt = set_ptt
        self._port = port
        self._buffers = {}  # QTcpSocket -> bytearray, for partial-line buffering
        self.newConnection.connect(self._on_new_connection)

    def start(self):
        if not self.listen(QHostAddress.AnyIPv4, self._port):
            raise RuntimeError(f"Couldn't listen on TCP port {self._port}: {self.errorString()}")

    def stop(self):
        for sock in list(self._buffers):
            sock.disconnectFromHost()
        self._buffers.clear()
        self.close()

    def _on_new_connection(self):
        while self.hasPendingConnections():
            sock = self.nextPendingConnection()
            self._buffers[sock] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(lambda s=sock: self._on_disconnected(s))

    def _on_disconnected(self, sock):
        self._buffers.pop(sock, None)
        sock.deleteLater()

    def _on_ready_read(self, sock):
        if sock not in self._buffers:
            return
        self._buffers[sock] += bytes(sock.readAll())
        while b"\n" in self._buffers[sock]:
            line, _sep, rest = self._buffers[sock].partition(b"\n")
            self._buffers[sock] = bytearray(rest)
            self._handle_command(sock, line.decode("utf-8", errors="replace").strip())

    def _handle_command(self, sock, line):
        if not line:
            return
        long_form = line.startswith("\\")
        body = line[1:] if long_form else line
        cmd = body.split(None, 1)[0] if long_form else body[:1]
        args_text = body[len(cmd):].strip() if long_form else body[1:].strip()
        args = args_text.split()

        try:
            if cmd in ("f", "get_freq"):
                self._respond(sock, f"{int(self._get_freq())}\n")
            elif cmd in ("F", "set_freq"):
                self._set_freq(float(args[0]))
                self._respond(sock, "RPRT 0\n")
            elif cmd in ("m", "get_mode"):
                mode, passband = self._get_mode()
                self._respond(sock, f"{mode}\n{passband}\n")
            elif cmd in ("M", "set_mode"):
                self._set_mode(args[0])
                self._respond(sock, "RPRT 0\n")
            elif cmd in ("t", "get_ptt"):
                self._respond(sock, f"{1 if self._get_ptt() else 0}\n")
            elif cmd in ("T", "set_ptt"):
                self._set_ptt(args[0] not in ("0", ""))
                self._respond(sock, "RPRT 0\n")
            elif cmd in ("v", "get_vfo"):
                self._respond(sock, "VFOA\n")
            elif cmd in ("V", "set_vfo"):
                self._respond(sock, "RPRT 0\n")  # accepted, no-op -- single-VFO from rigctld's perspective
            elif cmd == "chk_vfo":
                self._respond(sock, "CHKVFO 0\n")
            elif cmd == "dump_state":
                self._respond(sock, self._dump_state_response())
            else:
                self._respond(sock, "RPRT -11\n")  # -RIG_ENAVAIL: not implemented
        except Exception:
            self._respond(sock, "RPRT -1\n")  # -RIG_EINVAL: malformed args or callback failure

    @staticmethod
    def _respond(sock, text):
        sock.write(text.encode("utf-8"))

    @staticmethod
    def _dump_state_response():
        """Field sequence traced directly from Hamlib's own netrigctl_open()
        source (rigs/dummy/netrigctl.c) -- this is what WSJT-X's "Hamlib
        NET rigctl" backend actually parses, line by line, in this exact
        order:
          1. protocol version (int) -- 0 here
          2. one line that netrigctl_open() reads but never uses for
             anything (confirmed in the source: a second read_string()
             call whose result is simply overwritten by the next one)
          3. ITU region (int)
          4+. rx_range_list entries: "startf endf modes(hex) low_power
             high_power vfo(hex) ant(hex)", terminated by an all-zero line
          N+. tx_range_list entries, same shape, same all-zero terminator
          N+. tuning_steps entries: "modes(hex) step_hz", terminated by a
             "0 0" line
          N+. filters entries: "modes(hex) width_hz" (width must be
             nonzero or it reads as the terminator), terminated by "0 0"
          then six more single-value lines in this exact order: max_rit,
          max_xit, max_ifshift, announces, preamp list, attenuator list,
          has_get_func, has_set_func, has_get_level, has_set_level,
          has_get_parm, has_set_parm.

        Critically: since protocol version is 0, netrigctl_open() returns
        successfully immediately after has_set_parm and reads NOTHING
        further -- any extra trailing lines would sit unread in the
        socket and corrupt the next command's response. An earlier
        version of this method got this wrong two ways: it omitted the
        ITU region line entirely (shifting every field after it by one
        position) and included four extra trailing lines that were never
        supposed to be sent for protocol version 0.

        One thing NOT directly confirmed from the source (it's inferred
        from long-established, widely-documented Hamlib convention): the
        exact end-of-list sentinel checks (RIG_IS_FRNG_END/_TS_END/
        _FLT_END) -- believed to be startf==0&&endf==0 for ranges, step==0
        for tuning steps, and width==0 for filters, which is why the
        filter entry below uses a nonzero width."""
        return (
            "0\n"                                                             # protocol version
            "2\n"                                                             # unused line (confirmed discarded by the client)
            "0\n"                                                             # ITU region
            "150000.000000 1500000000.000000 0x1ff -1 -1 0x10000003 0x3\n"    # rx range
            "0 0 0 0 0 0 0\n"                                                 # rx range list terminator
            "150000.000000 1500000000.000000 0x1ff -1 -1 0x10000003 0x3\n"    # tx range
            "0 0 0 0 0 0 0\n"                                                 # tx range list terminator
            "0x1ff 1\n"                                                       # tuning step
            "0 0\n"                                                           # tuning step list terminator
            "0x1ff 2400\n"                                                    # filter (2400 Hz -- nonzero, so not read as the terminator)
            "0 0\n"                                                           # filter list terminator
            "0\n"                                                             # max_rit
            "0\n"                                                             # max_xit
            "0\n"                                                             # max_ifshift
            "0\n"                                                             # announces
            "0\n"                                                             # preamp list
            "0\n"                                                             # attenuator list
            "0\n"                                                             # has_get_func
            "0\n"                                                             # has_set_func
            "0\n"                                                             # has_get_level
            "0\n"                                                             # has_set_level
            "0\n"                                                             # has_get_parm
            "0\n"                                                             # has_set_parm -- last line read when protocol version is 0
        )
