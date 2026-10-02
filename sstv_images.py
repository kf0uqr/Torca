"""Saved SSTV image gallery: persistent index + file storage for every
completed SSTV decode.

Qt-free, same reasoning as recording.py (only the owning window needs
Qt; this is pure index/file bookkeeping) -- and the same storage
convention as recording.py's RECORDINGS_DIR/RECORDINGS_INDEX_PATH:
everything under ~/.icom_radio_app_cache/, a flat JSON index
(sstv_images.json) plus a subdirectory for the actual PNG files
(sstv_images/*.png).
"""

import json
import pathlib
import uuid

IMAGES_DIR = pathlib.Path.home() / ".icom_radio_app_cache" / "sstv_images"
IMAGES_INDEX_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "sstv_images.json"


def load_images():
    """Same defensive load pattern as recording.py's load_recordings --
    corrupt/missing index just means "no saved images yet", never a
    crash."""
    if IMAGES_INDEX_PATH.exists():
        try:
            data = json.loads(IMAGES_INDEX_PATH.read_text())
            if isinstance(data, list):
                return data
        except (OSError, ValueError):
            pass
    return []


def save_images(entries):
    IMAGES_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGES_INDEX_PATH.write_text(json.dumps(entries, indent=2))


def new_image_path():
    """Allocates a fresh, collision-free .png path under IMAGES_DIR --
    a random filename, not anything derived from the operator-editable
    "name" field or mode, same convention as recording.py's
    new_recording_path (so a later rename in the index can't collide
    with or orphan the file)."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.png"
    return IMAGES_DIR / filename, filename
