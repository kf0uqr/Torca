"""
AuditLog: a compliance record of who did what over Remote Access.

47 CFR 97.115 only allows a third party ("guest" role) to transmit under
the DIRECT supervision of a control operator -- the blocked-command
entries this writes are exactly what proves that supervision was (or
wasn't) enforced if a complaint ever comes in. Every TX-relevant command
(ptt/CW send/APRS send, plus the kill switch) is logged whether it was
allowed or blocked; freq/mode changes are logged allowed-only, just for
a complete operating record.

One JSONL file per install, written next to the app's own QSettings ini
(same directory QSettings("IcomRadioApp", "RadioControl") already
writes to -- no new path convention invented) so it's easy to find
alongside the rest of this app's config. Also keeps the last 200
entries in memory for the desktop dialog's "recent activity" panel,
which doesn't want to re-read/parse the file on every UI refresh tick.
"""

import collections
import datetime
import json
import pathlib


class AuditLog:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self._recent = collections.deque(maxlen=200)

    @classmethod
    def default(cls):
        """Places remote_audit.log next to the QSettings ini file this
        app already uses for everything else (IcomRadioApp/
        RadioControl) -- PySide6 import kept local so this module stays
        importable (and testable) without a Qt/display environment."""
        from PySide6.QtCore import QSettings

        ini_path = pathlib.Path(QSettings("IcomRadioApp", "RadioControl").fileName())
        return cls(ini_path.parent / "remote_audit.log")

    def log(self, radio_id, role, cmd, allowed, detail=""):
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "radio_id": radio_id,
            "role": role,
            "cmd": cmd,
            "allowed": bool(allowed),
            "detail": detail,
        }
        self._recent.append(entry)
        if self.path is not None:
            # A write failure here (e.g. a read-only/full disk) shouldn't
            # take down the actual radio-control command it's logging --
            # the in-memory `_recent` entry above still lets the desktop
            # panel show it even if the file itself couldn't be written.
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError:
                pass
        return entry

    def recent(self, n=20):
        return list(self._recent)[-n:]
