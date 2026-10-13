"""VOACAP propagation prediction for the map: how likely is each place on Earth to be reachable
from your station, on a chosen HF band, right now.

VOACAP is the US government (NTIA/ITS) HF propagation model that HamClock and the other
propagation tools are built on. TORCA does not contain it: it runs `voacapl`, the Linux build of
it (Debian/Ubuntu: `sudo apt install voacapl`, which also brings its data files), as a separate
program. For each request TORCA writes VOACAP's "area" input (your location as the transmitter,
one frequency, the current UTC hour, month and sunspot number, your power and antenna gain, the
signal-to-noise a mode needs), runs it on a private copy of VOACAP's data folder, reads back the
predicted CIRCUIT RELIABILITY (0-1: the share of days in the month, at this hour, that the
signal is strong enough) for a grid covering the world, and paints it on the map.

Everything here except running voacapl is plain Python, so it is tested without it.
"""

import datetime
import math
import os
import pathlib
import shutil
import subprocess

import numpy as np
from PySide6.QtCore import QThread, Signal

# Representative frequency (MHz) for each band; VOACAP covers 2-30 MHz.
BANDS = {"80m": 3.6, "60m": 5.357, "40m": 7.1, "30m": 10.12, "20m": 14.1, "17m": 18.1,
         "15m": 21.1, "12m": 24.9, "10m": 28.4}
# Signal-to-noise (dB in a 1 Hz bandwidth) each kind of operating needs to be usable -- the weakest
# signal you can work, as a figure that does not depend on the receive bandwidth: what the mode needs
# in its own bandwidth plus 10*log10(bandwidth). SSB: about 13 dB in 3 kHz (+35); CW: about 3 dB in
# 500 Hz (+27); FT8: -21 dB in 2.5 kHz (+34 = 13). VOACAP reports its SNR the same way (per 1 Hz).
MODES = {"SSB voice": 48, "CW": 30, "FT8 / weak-signal digital": 13}
# Man-made noise at the receiving end, as VOACAP's figure (dBW in 1 Hz at 3 MHz): the higher, the quieter.
NOISE = {"Business / industrial": 140, "Residential": 145, "Rural": 150}
DEFAULT_NOISE = "Residential"

DEFAULT_POWER_W = 100
DEFAULT_GAIN_DBI = 3.0     # a typical wire or vertical, about 3 dBi
GRID_SIZE = 90               # points across the world each way (about 2 degrees)
IMAGE_SIZE = (720, 360)      # the picture drawn on the map (a Web Mercator map covers 360 x 170.1 degrees)
MIN_TAKEOFF_DEG = 3.0

SOURCE_DIRS = ("/usr/share/voacapl/itshfbc", "/usr/local/share/voacapl/itshfbc")
WORK_DIR = pathlib.Path.home() / ".icom_radio_app_cache" / "voacap"
ANTENNA_FILE = "torca_flat"       # written into the private copy: a constant-gain antenna


def voacapl_path():
    found = shutil.which("voacapl")
    return pathlib.Path(found) if found else None


def source_dir():
    for candidate in SOURCE_DIRS:
        if (pathlib.Path(candidate) / "coeffs").is_dir():
            return pathlib.Path(candidate)
    return None


def is_available():
    return voacapl_path() is not None and source_dir() is not None


INSTALL_HINT = "Install VOACAP with: sudo apt install voacapl   (Debian/Ubuntu; it also installs its data files)"


def prepare_workdir(work=None, source=None):
    """A private copy of VOACAP's data folder (it writes its results inside it), made once. Returns its path."""
    work = pathlib.Path(work or WORK_DIR) / "itshfbc"
    source = pathlib.Path(source or source_dir() or "")
    if not (work / "coeffs").is_dir():
        if not (source / "coeffs").is_dir():
            raise RuntimeError("VOACAP's data files were not found. " + INSTALL_HINT)
        work.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, work, dirs_exist_ok=True)
    (work / "areadata").mkdir(exist_ok=True)
    return work


def _flat_antenna(gain_dbi):
    """An antenna file with the same gain at every angle (VOACAP's table format: 91 elevation values)."""
    rows = ["     .0" * 10 for _ in range(9)] + ["     .0"]
    return ("Flat gain antenna written by TORCA\r\n 3     3 parameters\r\n"
            f"{gain_dbi:6.2f}  [ 1] Max Gain dBi..:\r\n  11    [ 2] Antenna Type..: 91 values gain in elevation angle follows\r\n"
            "  0.0   [ 3] Efficiency (for IONCAP)\r\n" + "\r\n".join(rows) + "\r\n")


def _lat_text(lat):
    return f"{abs(lat):.2f}{'N' if lat >= 0 else 'S'}"


def _lon_text(lon):
    return f"{abs(lon):.2f}{'E' if lon >= 0 else 'W'}"


def build_deck(lat, lon, freq_mhz, utc_hour, month, ssn, power_w=DEFAULT_POWER_W, required_snr=48, grid_size=GRID_SIZE,
               noise=NOISE[DEFAULT_NOISE]):
    """The text of VOACAP's area input for one frequency / hour. (Fixed-column Fortran: the layout
    matters, and is the one VOACAP's own sample files use.)"""
    antenna = f"[default /{ANTENNA_FILE:<12}]"
    lines = [
        "Model    :VOACAP",
        "Colors   :Black    :Blue     :Ignore   :Ignore   :Red      :Black with shading",
        "Cities   :Receive.cty",
        "Nparms   :    4",
        "Parameter:MUF      0",
        "Parameter:DBU      0",
        "Parameter:SNRxx    0",
        "Parameter:REL      0",
        "Transmit :%10s%10s%20s %5s" % (_lat_text(lat), _lon_text(lon), "Home", "Short"),
        "Pcenter  : %s %9s   %s" % (_lat_text(lat), _lon_text(lon), "Home"),
        "Area     :%10.1f%10.1f%10.1f%10.1f" % (-180.0, 180.0, -90.0, 90.0),
        "Gridsize :  %3d    1" % grid_size,
        "Method   :   30",
        "Coeffs   :CCIR",
        "Months   :" + "%7.2f" % month + "%7.2f" * 8 % ((0.0,) * 8),
        "Ssns     :" + "%7d" % round(ssn) + "%7d" * 8 % ((0,) * 8),
        "Hours    :" + "%7d" % utc_hour + "%7d" * 8 % ((0,) * 8),
        "Freqs    :" + "%7.3f" % freq_mhz + "%7.3f" * 8 % ((0.0,) * 8),
        "System   :%5d%10.3f%5d%5d%10.3f%10.3f" % (noise, MIN_TAKEOFF_DEG, 90, required_snr, 3.0, 0.1),
        "Fprob    : 1.00 1.00 1.00 0.00",
        f"Rec Ants :{antenna}  gain=   0.0   0.0",
        f"Tx Ants  :{antenna}  0.000   0.0%10.4f" % (power_w / 1000.0),
    ]
    return "\n".join(lines) + "\n"


class Grid:
    """VOACAP's answer: reliability (0-1) on a lat/lon grid covering the world."""

    def __init__(self, lats, lons, reliability, muf):
        self.lats, self.lons, self.reliability, self.muf = lats, lons, reliability, muf

    def sample(self, lat, lon):
        """Bilinear reliability at arrays of latitude / longitude (degrees)."""
        return _bilinear(self.reliability, self.lats, self.lons, lat, lon)


def parse_output(text):
    """Reads a voacapl area result (.vg1) into a Grid, or raises ValueError."""
    cells = {}
    for line in text.splitlines():
        if len(line) < 104 or not line[:3].strip().isdigit():
            continue
        try:
            x, y = int(line[0:3]), int(line[3:6])
            lat, lon = float(line[6:16]), float(line[16:26])
            muf, rel = float(line[26:32]), float(line[98:104])
        except ValueError:
            continue
        cells[(y, x)] = (lat, lon, muf, rel)
    if not cells:
        raise ValueError("VOACAP produced no results")
    ny, nx = max(y for y, _ in cells), max(x for _, x in cells)
    reliability, mufs = np.zeros((ny, nx)), np.zeros((ny, nx))
    lats, lons = np.zeros(ny), np.zeros(nx)
    for (y, x), (lat, lon, muf, rel) in cells.items():
        reliability[y - 1, x - 1], mufs[y - 1, x - 1] = rel, muf
        lats[y - 1] = lat
        if abs(lat) < 89.0:              # at the poles the longitude column holds a placeholder
            lons[x - 1] = lon
    if nx > 1 and not np.any(lons):
        raise ValueError("VOACAP's result had no longitudes")
    return Grid(lats, lons, reliability, mufs)


def _bilinear(values, lats, lons, lat, lon):
    lat, lon = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
    fy = np.interp(lat, lats, np.arange(len(lats)))
    fx = np.interp(lon, lons, np.arange(len(lons)))
    y0, x0 = np.clip(np.floor(fy).astype(int), 0, len(lats) - 2), np.clip(np.floor(fx).astype(int), 0, len(lons) - 2)
    ty, tx = fy - y0, fx - x0
    top = values[y0, x0] * (1 - tx) + values[y0, x0 + 1] * tx
    bottom = values[y0 + 1, x0] * (1 - tx) + values[y0 + 1, x0 + 1] * tx
    return top * (1 - ty) + bottom * ty


def color_for(reliability):
    """RGBA bytes (arrays) for reliability 0-1: nothing where there is no chance, then red -> yellow -> green."""
    r = np.clip(np.asarray(reliability, dtype=float), 0.0, 1.0)
    red = np.where(r < 0.5, 215, 215 - (r - 0.5) * 2 * 175)
    green = np.where(r < 0.5, 40 + r * 2 * 170, 210)
    blue = np.full_like(r, 40)
    alpha = np.where(r < 0.03, 0, 70 + 110 * r)
    return np.stack([red, green, blue, alpha], axis=-1).astype(np.uint8)


def render_rgba(grid, width=IMAGE_SIZE[0], height=IMAGE_SIZE[1]):
    """The grid as an RGBA picture in the map's own projection (Web Mercator rows), as bytes."""
    lon = -180.0 + (np.arange(width) + 0.5) / width * 360.0
    y = (np.arange(height) + 0.5) / height
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1.0 - 2.0 * y))))
    rel = grid.sample(lat[:, None] * np.ones((1, width)), lon[None, :] * np.ones((height, 1)))
    return np.ascontiguousarray(color_for(rel)).tobytes()


def run_area(lat, lon, freq_mhz, utc_hour, month, ssn, power_w=DEFAULT_POWER_W, gain_dbi=DEFAULT_GAIN_DBI,
             required_snr=48, grid_size=GRID_SIZE, noise=NOISE[DEFAULT_NOISE], work=None, binary=None, timeout=120):
    """Runs voacapl for one frequency/hour and returns the Grid. Raises RuntimeError with a plain reason."""
    binary = binary or voacapl_path()
    if binary is None:
        raise RuntimeError("VOACAP (voacapl) is not installed. " + INSTALL_HINT)
    root = prepare_workdir(work)
    (root / "antennas" / "default" / ANTENNA_FILE).write_bytes(_flat_antenna(gain_dbi).encode("ascii"))
    name = "torca"
    for stale in (root / "areadata").glob(f"{name}.*"):
        stale.unlink()
    (root / "areadata" / f"{name}.voa").write_text(
        build_deck(lat, lon, freq_mhz, utc_hour, month, ssn, power_w, required_snr, grid_size, noise))
    try:
        result = subprocess.run([str(binary), str(root), "area", "calc", f"{name}.voa"],
                                capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"VOACAP could not be run: {exc}") from exc
    output = root / "areadata" / f"{name}.vg1"
    if result.returncode != 0 or not output.is_file():
        tail = (result.stderr or result.stdout).strip().splitlines()[:2]
        raise RuntimeError("VOACAP failed: " + " ".join(tail))
    try:
        return parse_output(output.read_text(errors="replace"))
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def request_key(band, mode, power_w, gain_dbi, noise_name, lat, lon, ssn, when):
    """What a result depends on -- the same key means the same picture (so it can be reused)."""
    return (band, mode, int(power_w), round(float(gain_dbi), 1), noise_name, round(lat, 1), round(lon, 1), round(ssn),
            when.year, when.month, when.hour)


class VoacapWorker(QThread):
    done = Signal(object, object)    # key, Grid
    failed = Signal(object, str)     # key, why

    def __init__(self, key, params, parent=None):
        super().__init__(parent)
        self.key, self.params = key, params

    def run(self):
        try:
            self.done.emit(self.key, run_area(**self.params))
        except Exception as exc:          # a problem here must never take the app down
            self.failed.emit(self.key, str(exc))


def current_params(band, mode, power_w, gain_dbi, noise_name, lat, lon, ssn, now=None):
    """The run_area() arguments (and cache key) for `band` at the current UTC hour."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    params = dict(lat=lat, lon=lon, freq_mhz=BANDS[band], utc_hour=now.hour, month=now.month, ssn=ssn,
                  power_w=power_w, gain_dbi=gain_dbi, required_snr=MODES[mode], noise=NOISE[noise_name])
    return request_key(band, mode, power_w, gain_dbi, noise_name, lat, lon, ssn, now), params
