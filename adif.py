"""
ADIF (Amateur Data Interchange Format) encode/decode -- the log book's
local file format (qso_log.py's qsolog.adi) and the wire format QRZ's
Logbook API (qrz_logbook.py) both speak. Pure, no Qt dependency, so
it's independently testable and reusable by the planned WSJT-X UDP
listener (a later phase), which receives a just-logged QSO record over
the wire in exactly this format ("Logged ADIF" UDP message).

ADIF field syntax: <FIELDNAME:LENGTH[:TYPE]>value -- LENGTH is the byte
length of value (not counting the enclosing <...>); TYPE is optional
and only matters for a handful of ADIF-defined types this app doesn't
need to distinguish (plain string round-tripping is enough here). A
record ends with a bare <eor> tag; a file may start with an optional
header ending in <eoh> -- this app always writes one (see
write_adif_log), and must tolerate one when reading QRZ's FETCH
response or a real ADIF file from elsewhere.
"""

import pathlib

from constants import HF_6M_BANDS, VHF_UHF_BANDS

# Matches ANY ADIF tag -- a length-prefixed field (<NAME:LEN> or
# <NAME:LEN:TYPE>) or a bare tag (<eor>, <eoh>). Deliberately a single
# regex covering both shapes so parse_adif_records's manual scan (see
# below) never has to guess which kind of tag it just found.
import re

_TAG_RE = re.compile(r"<(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?::(?P<length>\d+)(?::[^>]*)?)?>", re.IGNORECASE)


def format_adif_record(fields: dict) -> str:
    """Encodes a {ADIF_FIELD: value} dict as one ADIF record ending in
    <eor> (no trailing newline -- callers join multiple records
    themselves, see write_adif_log). Field names are upper-cased (ADIF
    convention); empty/None values are skipped entirely -- an omitted
    field, not an empty one, matching how optional ADIF fields are
    normally written."""
    parts = []
    for name, value in fields.items():
        if value is None:
            continue
        text = str(value)
        if not text:
            continue
        parts.append(f"<{name.upper()}:{len(text.encode('utf-8'))}>{text}")
    parts.append("<eor>")
    return " ".join(parts)


def parse_adif_records(text: str) -> list:
    """Parses an ADIF file/fragment into a list of {ADIF_FIELD: value}
    dicts, one per <eor>-terminated record.

    Scans manually (find the next '<', match a tag exactly there, then
    jump straight past that tag's declared-length value) rather than
    with a single-pass regex search -- a field's VALUE can itself
    contain '<' characters (e.g. a free-text COMMENT), and jumping by
    the declared length is the only way to skip over those safely
    instead of misreading them as the next tag.

    Tolerates (and discards) an optional file header -- everything
    before the first <eoh>, if any; QRZ's FETCH response and any real-
    world ADIF file may include one, this app's own writes always do
    (see write_adif_log)."""
    records = []
    current = {}
    pos = 0
    n = len(text)
    while pos < n:
        lt = text.find("<", pos)
        if lt == -1:
            break
        match = _TAG_RE.match(text, lt)
        if match is None:
            pos = lt + 1  # a stray '<' not forming a valid tag -- skip past it
            continue
        name = match.group("name").upper()
        length_str = match.group("length")
        tag_end = match.end()
        if name == "EOH":
            current = {}  # discard anything collected before the header ends
            pos = tag_end
            continue
        if name == "EOR":
            records.append(current)
            current = {}
            pos = tag_end
            continue
        if length_str is None:
            pos = tag_end  # a bare/unknown tag with no length -- nothing to consume as a value
            continue
        length = int(length_str)
        current[name] = text[tag_end:tag_end + length]
        pos = tag_end + length
    return records


# Written at the top of every file this app saves (write_adif_log) --
# not required by the ADIF spec (a header is optional), but keeps
# qsolog.adi a genuinely conventional, portable ADIF file that imports
# cleanly into other loggers, not just something this app itself reads.
ADIF_HEADER = (
    "TORCA QSO Log\n"
    "<adif_ver:5>3.1.4\n"
    "<programid:5>TORCA\n"
    "<eoh>\n\n"
)


def read_adif_log(path) -> list:
    """Loads an ADIF file into a list of QSO dicts. Same never-crash
    convention as satellite_tracking.py's load_satellite_data: missing
    file or any read/parse failure returns [] with an [ERROR] print,
    rather than raising."""
    path = pathlib.Path(path)
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except Exception as exc:
        print(f"[ERROR] QSO Log: couldn't read {path} ({exc}); starting with an empty log.")
        return []
    try:
        return parse_adif_records(text)
    except Exception as exc:
        print(f"[ERROR] QSO Log: couldn't parse {path} ({exc}); starting with an empty log.")
        return []


def write_adif_log(path, records) -> None:
    """Saves a list of QSO dicts as an ADIF file, overwriting whatever
    was there -- same "load whole list into memory, mutate, write whole
    file back" idiom as satellite_tracking.py's save_satellite_data."""
    path = pathlib.Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [format_adif_record(record) for record in records]
        body = ADIF_HEADER + "\n".join(lines) + ("\n" if lines else "")
        path.write_text(body)
    except Exception as exc:
        print(f"[ERROR] QSO Log: couldn't save {path} ({exc}).")


# Maps this app's own radio mode strings (CONTROL_DEFINITIONS["mode"]
# ["options"] in constants.py -- confirmed via rigplane's set_mode()
# runtime error message: LSB, USB, AM, CW, RTTY, FM, WFM, CW_R, RTTY_R,
# DV) to valid ADIF 3.1.4 MODE (and, where needed, SUBMODE) values.
# LSB/USB both map to ADIF's MODE=SSB (ADIF has no separate top-level
# USB/LSB mode -- sideband is conventionally implied by band, same as
# every other ADIF-writing logger does it). DV (D-STAR digital voice)
# maps to MODE=DIGITALVOICE/SUBMODE=DSTAR, not the older bare
# MODE=DSTAR -- confirmed DSTAR was deprecated as a standalone Mode in
# a later ADIF revision in favor of DIGITALVOICE+SUBMODE, so this
# targets the current form rather than a deprecated one.
RADIO_MODE_TO_ADIF = {
    "LSB": {"MODE": "SSB"},
    "USB": {"MODE": "SSB"},
    "AM": {"MODE": "AM"},
    "CW": {"MODE": "CW"},
    "CW_R": {"MODE": "CW"},
    "RTTY": {"MODE": "RTTY"},
    "RTTY_R": {"MODE": "RTTY"},
    "FM": {"MODE": "FM"},
    "WFM": {"MODE": "FM"},
    "DV": {"MODE": "DIGITALVOICE", "SUBMODE": "DSTAR"},
}


def adif_mode_fields(radio_mode: str) -> dict:
    """Returns the {"MODE": ..., "SUBMODE": ...} (SUBMODE only when
    applicable) to merge into a QSO record for this app's own radio
    mode string, or {} if it isn't one of the known/mapped values (the
    caller should leave MODE unset rather than guess)."""
    return dict(RADIO_MODE_TO_ADIF.get(radio_mode, {}))


# constants.py's HF_6M_BANDS/VHF_UHF_BANDS band edges are already in
# ADIF's own lowercase BAND enum convention ("160m", "2m", "70cm", ...)
# -- reused directly here rather than duplicating a second band-edge
# table just for ADIF.
_ALL_BANDS_HZ = HF_6M_BANDS + VHF_UHF_BANDS


def band_for_freq_hz(freq_hz) -> str:
    """Derives the ADIF BAND value for a frequency in Hz. Returns None
    if freq_hz is None or falls outside every band this app knows
    about (out-of-band, or the radio hasn't reported a frequency
    yet) -- callers should leave BAND unset rather than guess."""
    if freq_hz is None:
        return None
    for label, low_hz, high_hz in _ALL_BANDS_HZ:
        if low_hz <= freq_hz <= high_hz:
            return label
    return None


def grid_square_to_latlon(grid: str):
    """Converts a Maidenhead grid locator (ADIF GRIDSQUARE, e.g. "EM12"
    or "EM12ab") to (lat, lon) at the CENTER of the cell it identifies
    -- the standard field/square/subsquare encoding (2 letters: 20deg
    lon x 10deg lat field; +2 digits: 2deg lon x 1deg lat square; +2
    more letters: 5min lon x 2.5min lat subsquare). Returns None for
    anything shorter than 4 characters or that doesn't parse as a valid
    locator -- callers should skip that QSO rather than guess/plot it
    at 0,0. Used by ham_dashboard.py's QSO map overlay, the only
    consumer of location data this log format actually has (no raw
    lat/lon field is logged per-QSO)."""
    if not grid:
        return None
    grid = grid.strip().upper()
    if len(grid) < 4:
        return None
    try:
        lon = (ord(grid[0]) - ord("A")) * 20 - 180
        lat = (ord(grid[1]) - ord("A")) * 10 - 90
        lon += int(grid[2]) * 2
        lat += int(grid[3]) * 1
        if len(grid) >= 6 and grid[4].isalpha() and grid[5].isalpha():
            lon += (ord(grid[4]) - ord("A")) * (2 / 24)
            lat += (ord(grid[5]) - ord("A")) * (1 / 24)
            lon += (2 / 24) / 2
            lat += (1 / 24) / 2
        else:
            lon += 1.0
            lat += 0.5
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            return None
        return lat, lon
    except (ValueError, IndexError):
        return None
