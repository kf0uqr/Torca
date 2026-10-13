"""The stations this station has heard itself (from the FT8/FT4 and WSPR tools), kept on disk for
24 hours so the PSKReporter map still shows them after a restart -- and when PSKReporter's own query
service, which can hold less than its website shows, has nothing to give back.

One small JSON file in the app's cache folder; spots older than MAX_AGE_SECONDS are dropped, and only
the most recent MAX_SPOTS are kept.
"""

import json
import os
import pathlib
import time

PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "heard_spots.json"
MAX_AGE_SECONDS = 86400
MAX_SPOTS = 5000

_FIELDS = ("callsign", "locator", "frequency_hz", "band", "mode", "snr", "flow_start_seconds", "direction")


def load(path=None, now=None):
    """The saved spots (a list of dicts with a "raw" {} each), minus any that are too old."""
    now = time.time() if now is None else now
    try:
        data = json.loads(pathlib.Path(path or PATH).read_text())
    except (OSError, ValueError):
        return []
    spots = []
    for item in data.get("spots", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or not all(k in item for k in _FIELDS):
            continue
        if not isinstance(item["flow_start_seconds"], (int, float)) or item["flow_start_seconds"] < now - MAX_AGE_SECONDS:
            continue
        spot = {k: item[k] for k in _FIELDS}
        spot["raw"] = {}
        spots.append(spot)
    return spots[-MAX_SPOTS:]


def save(spots, path=None, now=None):
    """Writes the spots atomically (a temp file, then a rename). Returns True on success."""
    now = time.time() if now is None else now
    path = pathlib.Path(path or PATH)
    recent = sorted((s for s in spots if s["flow_start_seconds"] >= now - MAX_AGE_SECONDS),
                    key=lambda s: s["flow_start_seconds"])[-MAX_SPOTS:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps({"spots": [{k: s[k] for k in _FIELDS} for s in recent]}))
        os.replace(temp, path)
        return True
    except OSError as exc:
        print(f"[ERROR] Heard stations: couldn't save ({exc}).")
        return False
