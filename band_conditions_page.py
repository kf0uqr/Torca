"""The Band Conditions tab: HF conditions (the existing table), VHF conditions (aurora and
sporadic-E from the same N0NBH feed) with the space-weather readings behind them, and live
pictures of the Sun from NASA's Solar Dynamics Observatory (SDO).

SDO publishes a "latest" JPEG for each instrument/wavelength on sdo.gsfc.nasa.gov, refreshed
every few minutes. The page fetches them (on a worker thread, never the GUI's) when it is
shown and every REFRESH_MINUTES while it stays visible, and says how old each picture is --
because if NASA's "latest" image is not being updated, an old picture of the Sun must not
pass for the current one.
"""

import email.utils
import time
import urllib.request

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QGridLayout,
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

SDO_URL = "https://sdo.gsfc.nasa.gov/assets/img/latest/latest_512_{code}.jpg"
# (code, title, what it shows) -- what an operator looks at for propagation.
SDO_VIEWS = [
    ("0193", "193 Å  corona", "Hot corona. Dark areas are coronal holes: they send fast solar wind that disturbs the geomagnetic field a couple of days later."),
    ("0304", "304 Å  chromosphere", "Cooler gas at the Sun's edge: prominences and the bright patches of flaring active regions."),
    ("HMIIC", "Visible light", "What the eye would see: sunspots, which drive the sunspot number and solar flux."),
    ("HMIB", "Magnetogram", "The Sun's magnetic field (black / white are opposite polarities). Tangled pairs mean flare-prone regions."),
]
REFRESH_MINUTES = 15
MAX_PICTURE_PIXELS = 360    # each of the four pictures is shown no larger than this
STALE_HOURS = 3           # an image older than this gets a warning
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
    image_ready = Signal(str, bytes, object)    # code, JPEG bytes, last-modified epoch (or None)
    failed = Signal(str, str)                   # code, why

    def __init__(self, codes, parent=None):
        super().__init__(parent)
        self._codes = list(codes)

    def run(self):
        for code in self._codes:
            if self.isInterruptionRequested():
                return
            try:
                request = urllib.request.Request(
                    SDO_URL.format(code=code), headers={"User-Agent": "TORCA/1.0 (desktop ham radio control application)"})
                with urllib.request.urlopen(request, timeout=20) as response:
                    data = response.read()
                    modified = parse_last_modified(response.headers.get("Last-Modified"))
            except Exception as exc:
                self.failed.emit(code, str(exc))
                continue
            self.image_ready.emit(code, data, modified)


class BandConditionsPage(QWidget):
    def __init__(self, hf_table, parent=None):
        super().__init__(parent)
        self.hf_table = hf_table
        self.setWindowTitle("Band Conditions")
        self._fetcher = None
        self._last_fetch = 0.0
        self._pixmaps = {}          # code -> QPixmap
        self._modified = {}         # code -> epoch of the picture
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

        # ---- the Sun ----
        self.sun_labels, self.caption_labels = {}, {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        for index, (code, title, tip) in enumerate(SDO_VIEWS):
            picture = QLabel("loading...")
            picture.setAlignment(Qt.AlignCenter)
            picture.setMinimumSize(160, 160)
            picture.setStyleSheet("background: black; color: #666;")
            picture.setToolTip(tip)
            caption = QLabel(title)
            caption.setAlignment(Qt.AlignCenter)
            caption.setToolTip(tip)
            self.sun_labels[code], self.caption_labels[code] = picture, caption
            cell = QVBoxLayout()
            cell.addWidget(picture, 1)
            cell.addWidget(caption)
            grid.addLayout(cell, index // 2, index % 2)
        self.sun_status = QLabel("")
        self.sun_status.setWordWrap(True)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Fetch the latest pictures from NASA now.")
        self.refresh_button.clicked.connect(lambda: self.refresh_sun(force=True))
        sun_header = QHBoxLayout()
        sun_header.addWidget(QLabel("<b>The Sun now</b>  (NASA Solar Dynamics Observatory)"))
        sun_header.addStretch()
        sun_header.addWidget(self.refresh_button)

        for table in (self.hf_table, self.vhf_table):
            self._fit(table)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>HF conditions</b>  (N0NBH)"))
        layout.addWidget(self.hf_table)
        layout.addWidget(QLabel("<b>VHF conditions</b>"))
        layout.addWidget(self.vhf_table)
        layout.addWidget(self.space_weather_label)
        layout.addLayout(sun_header)
        layout.addLayout(grid)
        layout.addWidget(self.sun_status)
        layout.addStretch()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MINUTES * 60 * 1000)
        self._timer.timeout.connect(lambda: self.refresh_sun(force=True))

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
        self.sun_status.setText("Fetching the latest pictures from NASA...")
        self._fetcher = SdoFetcher([code for code, _t, _tip in SDO_VIEWS], self)
        self._fetcher.image_ready.connect(self._on_image)
        self._fetcher.failed.connect(self._on_failed)
        self._fetcher.finished.connect(self._update_status)
        self._fetcher.start()

    def _on_image(self, code, data, modified):
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self._on_failed(code, "NASA sent something that isn't a picture")
            return
        self._pixmaps[code] = pixmap
        self._modified[code] = modified
        self._errors.pop(code, None)
        self._show_pixmap(code)

    def _on_failed(self, code, message):
        self._errors[code] = message
        if code not in self._pixmaps:
            self.sun_labels[code].setText("unavailable")

    def _show_pixmap(self, code):
        pixmap = self._pixmaps.get(code)
        if pixmap is None:
            return
        label = self.sun_labels[code]
        side = max(160, min(MAX_PICTURE_PIXELS, label.width()))
        label.setPixmap(pixmap.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for code in self._pixmaps:
            self._show_pixmap(code)

    def _update_status(self):
        now = time.time()
        ages = [now - t for t in self._modified.values() if t]
        text = ""
        if ages:
            newest, oldest = min(ages), max(ages)
            text = f"Pictures taken {age_text(newest)}" + (f" (the oldest {age_text(oldest)})" if oldest - newest > 900 else "") + "."
            if newest > STALE_HOURS * 3600:
                text += (f"  ⚠ NASA's 'latest' pictures are {age_text(newest)} old, so these may not show the Sun as it is now -- "
                         "its image feed may not be updating.")
        if self._errors:
            text += ("  " if text else "") + f"Couldn't fetch {len(self._errors)} picture(s): {next(iter(self._errors.values()))}."
        self.sun_status.setText(text or "No pictures yet.")
        for code, (_c, title, _t) in zip((c for c, _t, _tip in SDO_VIEWS), SDO_VIEWS):
            modified = self._modified.get(code)
            self.caption_labels[code].setText(f"{title}  ·  {age_text(now - modified)}" if modified else title)

    def closeEvent(self, event):
        if self._fetcher is not None:
            self._fetcher.requestInterruption()
            self._fetcher.wait(3000)
        super().closeEvent(event)
