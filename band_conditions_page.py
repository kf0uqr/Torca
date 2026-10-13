"""The Band Conditions tab: HF conditions (the existing table), VHF conditions (aurora and
sporadic-E from the same N0NBH feed) with the space-weather readings behind them, and live
pictures of the Sun from NASA's Solar Dynamics Observatory (SDO).

The pictures come from Helioviewer (api.helioviewer.org, a NASA/ESA service that serves SDO's
newest images within minutes of them being taken): the page asks which image is the newest for
each instrument, then fetches a picture of it. If Helioviewer can't be reached it falls back to
the "latest" JPEGs on sdo.gsfc.nasa.gov -- which can go stale (they once sat three weeks old).
Either way the page fetches on a worker thread, never the GUI's, when it is shown and every
REFRESH_MINUTES while it stays visible, and says how old each picture is and where it came
from, so an old picture of the Sun can't pass for the current one.
"""

import calendar
import email.utils
import json
import time
import urllib.request

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import solar_data

HELIOVIEWER_URL = "https://api.helioviewer.org/v2"
# Helioviewer's source ids for each view.
HELIOVIEWER_SOURCES = {"0094": 8, "0131": 9, "0171": 10, "0193": 11, "0211": 12, "0304": 13, "0335": 14, "HMIIC": 18, "HMIB": 19}
SDO_URL = "https://sdo.gsfc.nasa.gov/assets/img/latest/latest_512_{code}.jpg"   # the fallback
# (code, title, what it shows), in the order the arrow buttons step through them.
SDO_VIEWS = [
    ("0193", "193 Å  corona", "Hot corona (about 1.2 million K). Dark areas are coronal holes: they send fast solar wind that disturbs the geomagnetic field a couple of days later."),
    ("0304", "304 Å  chromosphere", "Cooler gas (about 50,000 K) at the Sun's edge: prominences and the bright patches of flaring active regions."),
    ("HMIIC", "Visible light", "What the eye would see: sunspots, which drive the sunspot number and solar flux."),
    ("HMIB", "Magnetogram", "The Sun's magnetic field (black and white are opposite polarities). Tangled pairs mean flare-prone regions."),
    ("0171", "171 Å  quiet corona", "Corona at about 600,000 K: the loops of magnetic field that trace out active regions."),
    ("0211", "211 Å  active regions", "Corona at about 2 million K: shows active regions and coronal holes."),
    ("0335", "335 Å  active regions", "Corona at about 2.5 million K: the hottest, brightest parts of active regions."),
    ("0094", "94 Å  flaring regions", "Corona at about 6 million K: lights up during flares."),
    ("0131", "131 Å  flares", "Flaring plasma at about 10 million K: the fastest way to see a flare in progress."),
]
REFRESH_MINUTES = 15
MAX_PICTURE_PIXELS = 512    # the picture is shown no larger than this
STALE_HOURS = 3           # an AIA image older than this gets a warning
STALE_HOURS_HMI = 12      # the HMI ones normally run hours behind
_NO_DATA = "--"

_COLORS = {
    "good": QColor(40, 130, 60),
    "fair": QColor(160, 140, 30),
    "poor": QColor(140, 50, 50),
    "band closed": QColor(70, 70, 70),
}


def age_text(seconds):
    seconds = max(int(seconds), 0)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{seconds // 86400} days ago"


def parse_last_modified(value):
    """HTTP Last-Modified header -> epoch seconds, or None."""
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None


class SdoFetcher(QThread):
    """Fetches the SDO pictures one after another; each is announced as it arrives."""
    image_ready = Signal(str, bytes, object, str)    # code, image bytes, when it was taken (epoch or None), where from
    failed = Signal(str, str)                   # code, why

    def __init__(self, codes, parent=None):
        super().__init__(parent)
        self._codes = list(codes)

    def run(self):
        for code in self._codes:
            if self.isInterruptionRequested():
                return
            problems = []
            for source, fetch in (("Helioviewer", self._from_helioviewer), ("NASA", self._from_nasa)):
                try:
                    data, taken = fetch(code)
                except Exception as exc:
                    problems.append(f"{source}: {exc}")
                    continue
                self.image_ready.emit(code, data, taken, source)
                break
            else:
                self.failed.emit(code, "; ".join(problems))

    @staticmethod
    def _get(url):
        request = urllib.request.Request(url, headers={"User-Agent": "TORCA/1.0 (desktop ham radio control application)"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(), response.headers

    def _from_helioviewer(self, code):
        """The newest SDO image for this view, as a 512 px picture of the whole Sun, and when it was taken."""
        source = HELIOVIEWER_SOURCES[code]
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        info = json.loads(self._get(f"{HELIOVIEWER_URL}/getClosestImage/?date={now}&sourceId={source}")[0])
        taken_text = info["date"]                                  # "2026-10-13 08:47:05" (UTC)
        taken = calendar.timegm(time.strptime(taken_text, "%Y-%m-%d %H:%M:%S"))
        stamp = taken_text.replace(" ", "T") + "Z"
        data, _ = self._get(
            f"{HELIOVIEWER_URL}/takeScreenshot/?date={stamp}&imageScale=4.8&layers=[{source},1,100]"
            "&x0=0&y0=0&width=512&height=512&display=true&watermark=false")
        return data, taken

    def _from_nasa(self, code):
        data, headers = self._get(SDO_URL.format(code=code))
        return data, parse_last_modified(headers.get("Last-Modified"))


class BandConditionsPage(QWidget):
    def __init__(self, hf_table, parent=None):
        super().__init__(parent)
        self.hf_table = hf_table
        self.setWindowTitle("Band Conditions")
        self._fetcher = None
        self._last_fetch = 0.0
        self._pixmaps = {}          # code -> QPixmap
        self._modified = {}         # code -> epoch of the picture
        self._sources = {}          # code -> "Helioviewer" or "NASA"
        self._errors = {}

        # ---- VHF ----
        self.vhf_table = QTableWidget(len(solar_data.VHF_CONDITIONS), 1)
        self.vhf_table.setHorizontalHeaderLabels(["Now"])
        self.vhf_table.setVerticalHeaderLabels([label for _, label in solar_data.VHF_CONDITIONS])
        self.vhf_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.vhf_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.vhf_table.setSelectionMode(QTableWidget.NoSelection)
        for row in range(len(solar_data.VHF_CONDITIONS)):
            item = QTableWidgetItem(_NO_DATA)
            item.setTextAlignment(Qt.AlignCenter)
            self.vhf_table.setItem(row, 0, item)
        self.vhf_table.setToolTip("Calculated VHF conditions from N0NBH (hamqsl.com). 'Band Closed' means the phenomenon is not expected now.")
        self.space_weather_label = QLabel("")
        self.space_weather_label.setWordWrap(True)
        self.space_weather_label.setStyleSheet("color: #aaa;")

        # ---- the Sun: one picture, with arrows to step through the views ----
        self._index = 0
        self.picture = QLabel("loading...")
        self.picture.setAlignment(Qt.AlignCenter)
        self.picture.setStyleSheet("background: black; color: #666;")
        self.title_label = QLabel("")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setStyleSheet("font-weight: bold;")
        self.detail_label = QLabel("")
        self.detail_label.setAlignment(Qt.AlignCenter)
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("color: #aaa;")
        self.prev_button = QPushButton("◀")
        self.next_button = QPushButton("▶")
        for button, tip in ((self.prev_button, "Previous picture"), (self.next_button, "Next picture")):
            button.setToolTip(tip)
            button.setFixedWidth(48)
        self.prev_button.clicked.connect(lambda: self.step(-1))
        self.next_button.clicked.connect(lambda: self.step(1))
        self.sun_status = QLabel("")
        self.sun_status.setWordWrap(True)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Fetch the latest pictures now.")
        self.refresh_button.clicked.connect(lambda: self.refresh_sun(force=True))
        sun_header = QHBoxLayout()
        sun_header.addWidget(QLabel("<b>The Sun now</b>  (NASA Solar Dynamics Observatory)"))
        sun_header.addStretch()
        sun_header.addWidget(self.refresh_button)
        viewer = QHBoxLayout()
        viewer.addStretch(1)
        viewer.addWidget(self.prev_button)
        viewer.addWidget(self.picture)
        viewer.addWidget(self.next_button)
        viewer.addStretch(1)

        for table in (self.hf_table, self.vhf_table):
            self._fit(table)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>HF conditions</b>  (N0NBH)"))
        layout.addWidget(self.hf_table)
        layout.addWidget(QLabel("<b>VHF conditions</b>"))
        layout.addWidget(self.vhf_table)
        layout.addWidget(self.space_weather_label)
        layout.addLayout(sun_header)
        layout.addLayout(viewer)
        layout.addWidget(self.title_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.sun_status)
        layout.addStretch()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MINUTES * 60 * 1000)
        self._timer.timeout.connect(lambda: self.refresh_sun(force=True))
        self._show_current()

    # ---- VHF ----

    @staticmethod
    def _fit(table):
        """Exactly as tall as its rows (no scroll bar, no blank space): these are small fixed tables."""
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.resizeRowsToContents()
        table.setFixedHeight(table.horizontalHeader().height() + table.verticalHeader().length() + 4)

    def update_vhf(self, conditions):
        """`conditions` is solar_data.fetch_band_conditions()'s dict."""
        for row, (key, _label) in enumerate(solar_data.VHF_CONDITIONS):
            value = conditions.get((key[0], key[1]), _NO_DATA)
            item = self.vhf_table.item(row, 0)
            item.setText(value)
            color = _COLORS.get(value.strip().lower())
            if color:
                item.setBackground(color)
                item.setForeground(QColor(255, 255, 255))
        extra = lambda tag: conditions.get(("extra", tag), "").strip()
        parts = []
        if extra("aurora"):
            parts.append(f"Aurora index {extra('aurora')}" + (f" (reaching {extra('latdegree')}° latitude)" if extra("latdegree") else ""))
        if extra("solarwind"):
            parts.append(f"solar wind {extra('solarwind')} km/s")
        if extra("magneticfield"):
            parts.append(f"Bz {extra('magneticfield')} nT")
        for tag, label in (("kindex", "K"), ("aindex", "A"), ("xray", "X-ray"), ("protonflux", "protons")):
            if extra(tag):
                parts.append(f"{label} {extra(tag)}")
        self.space_weather_label.setText("  ·  ".join(parts))

    # ---- SDO pictures ----

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_sun()
        self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def refresh_sun(self, force=False):
        if self._fetcher is not None and self._fetcher.isRunning():
            return
        if not force and time.time() - self._last_fetch < REFRESH_MINUTES * 60 and self._pixmaps:
            return
        self._last_fetch = time.time()
        self.sun_status.setText("Fetching the latest pictures of the Sun...")
        order = SDO_VIEWS[self._index:] + SDO_VIEWS[:self._index]       # the one on screen first, then the rest
        self._fetcher = SdoFetcher([code for code, _t, _tip in order], self)
        self._fetcher.image_ready.connect(self._on_image)
        self._fetcher.failed.connect(self._on_failed)
        self._fetcher.finished.connect(self._show_current)
        self._fetcher.start()

    def step(self, direction):
        """Show the next (+1) or previous (-1) picture, wrapping round."""
        self._index = (self._index + direction) % len(SDO_VIEWS)
        self._show_current()

    @property
    def current_code(self):
        return SDO_VIEWS[self._index][0]

    def _on_image(self, code, data, modified, source="NASA"):
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self._on_failed(code, f"{source} sent something that isn't a picture")
            return
        self._pixmaps[code] = pixmap
        self._modified[code] = modified
        self._sources[code] = source
        self._errors.pop(code, None)
        if code == self.current_code:
            self._show_current()

    def _on_failed(self, code, message):
        self._errors[code] = message
        if code == self.current_code:
            self._show_current()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._show_current()

    def _show_current(self):
        """Puts the selected view on screen with its title, age, source and a warning if it is old."""
        code, title, tip = SDO_VIEWS[self._index]
        self.title_label.setText(f"{title}   ({self._index + 1} of {len(SDO_VIEWS)})")
        self.picture.setToolTip(tip)
        pixmap = self._pixmaps.get(code)
        side = max(280, min(MAX_PICTURE_PIXELS, self.width() - 140))     # a square, as big as fits
        self.picture.setFixedSize(side, side)
        if pixmap is not None:
            self.picture.setPixmap(pixmap.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.picture.setPixmap(QPixmap())
            self.picture.setText("unavailable" if code in self._errors else "loading...")
        detail = tip
        modified = self._modified.get(code)
        if pixmap is not None and modified:
            age = time.time() - modified
            detail += f"\nTaken {age_text(age)}, from {self._sources.get(code, '?')}."
            limit = STALE_HOURS_HMI if code.startswith("HMI") else STALE_HOURS
            if age > limit * 3600:
                detail += f"  ⚠ That is {age_text(age)} -- the image feed may not be updating, so this may not show the Sun as it is now."
        elif code in self._errors:
            detail += f"\nCouldn't fetch it: {self._errors[code]}"
        self.detail_label.setText(detail)
        if self._fetcher is not None and self._fetcher.isFinished():
            self.sun_status.setText("" if self._pixmaps else "No pictures of the Sun could be fetched.")

    def closeEvent(self, event):
        if self._fetcher is not None:
            self._fetcher.requestInterruption()
            self._fetcher.wait(3000)
        super().closeEvent(event)
