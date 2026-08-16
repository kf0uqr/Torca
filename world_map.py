"""
The Ham Dashboard's day/night world map: a background coastline image
(downloaded once from Wikimedia Commons and cached locally, falling back
to a plain dark fill if that ever fails) with the night hemisphere
shaded, a lat/long reference grid, the sub-solar point, the operator's
own location, and -- when satellite tracking is on -- satellite markers
and their footprint polygons, all drawn on top.
"""

import datetime
import math
import pathlib
import urllib.error
import urllib.request

from PySide6.QtCore import Qt, QRect, QRectF, QPointF, Signal, QThread
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPainterPath, QImage, QFont
from PySide6.QtWidgets import QWidget, QSizePolicy

from solar_data import _solar_subpoint, _solar_elevation


# Background world map image for WorldMapWidget -- a real equirectangular
# coastline map, downloaded once and cached locally, rather than embedded
# in the source (impractical at any reasonable resolution) or fetched
# fresh every run. Wikimedia's Special:FilePath is a stable redirect to
# whatever the current version of a named file is, so this doesn't depend
# on guessing an internal upload-hash path. CC BY 4.0 -- attributed in
# the map's own corner (see WorldMapWidget.paintEvent).
WORLD_MAP_IMAGE_URL = (
    "https://commons.wikimedia.org/wiki/Special:FilePath/"
    "Blank_Map_of_The_World_Equirectangular_Projection.png"
)
WORLD_MAP_CACHE_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "world_map.png"
WORLD_MAP_ATTRIBUTION = "Map: Wikimedia Commons (CC BY 4.0)"


class WorldMapImageFetcher(QThread):
    """Downloads the background map image once, if not already cached
    locally -- after that, never touches the network again. Runs on its
    own thread since it's a one-time blocking HTTP fetch; the map widget
    itself keeps working (grid-only look, same as before this feature
    existed) regardless of whether this succeeds."""

    image_ready = Signal(str)   # local file path
    failed = Signal(str)

    def run(self):
        if WORLD_MAP_CACHE_PATH.exists():
            self.image_ready.emit(str(WORLD_MAP_CACHE_PATH))
            return
        try:
            WORLD_MAP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Confirmed via a live 403 Forbidden: Wikimedia rejects
            # requests using Python's default urllib User-Agent as a
            # bot-prevention measure. They document requiring a
            # descriptive one instead (meta.wikimedia.org/wiki/
            # User-Agent_policy) -- a plain identifying string is enough
            # to get past this, no API key/registration needed.
            request = urllib.request.Request(
                WORLD_MAP_IMAGE_URL,
                headers={"User-Agent": "IcomRadioControlApp/1.0 (desktop ham radio control application)"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read()
            WORLD_MAP_CACHE_PATH.write_bytes(data)
            self.image_ready.emit(str(WORLD_MAP_CACHE_PATH))
        except Exception as exc:
            self.failed.emit(str(exc))


class WorldMapWidget(QWidget):
    """A day/night map in the spirit of (not a reimplementation of)
    HamClock's map pane: a real equirectangular coastline map as the
    background (downloaded once via WorldMapImageFetcher, cached
    locally; falls back to a plain dark background if that ever fails,
    e.g. no internet on first run), with the night hemisphere shaded,
    a lat/long reference grid, the sub-solar point, and the operator's
    own location if set, drawn on top."""

    satellite_double_clicked = Signal(str)  # satellite name, from the positions passed to set_satellite_positions
    satellite_right_clicked = Signal(str)   # satellite name -- opens Satellite Info (ham_dashboard.py)

    # The background image (and the whole lat/lon grid it's drawn
    # against) is a standard equirectangular projection: 2:1 width:height.
    # Stretching the widget to some other ratio would distort it, so the
    # actual map content is always letterboxed to this ratio within
    # whatever box the widget is given -- see _map_rect().
    MAP_ASPECT_RATIO = 2.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(220)
        # Expanding/Preferred, not Expanding/Expanding -- the old
        # Expanding/Expanding + a stretch factor of 1 in HamClockWindow's
        # layout (the only stretchy item there) meant this widget grabbed
        # *all* extra vertical space in the window, which severely
        # distorted the map once HamClockWindow started occupying a tall/
        # narrow half-screen region (e.g. 960x1200) rather than a wider
        # squarer window -- a naturally 2:1 map stretched to nearly 2:2.5.
        # heightForWidth (below) lets layouts that respect it size this
        # proportionally in the first place; _map_rect()'s letterboxing
        # in paintEvent is the actual guarantee regardless of whether a
        # given layout honors that.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._operator_lat = None
        self._operator_lon = None
        self._operator_label = ""
        self._background_image = None  # QImage, once loaded (or None -- falls back to a plain fill)
        self._satellite_mode = False
        self._satellite_positions = []  # list of {"name", "lat", "lon", "altitude_km", "footprint"}

        self._image_fetcher = WorldMapImageFetcher()
        self._image_fetcher.image_ready.connect(self._on_image_ready)
        self._image_fetcher.failed.connect(self._on_image_failed)
        self._image_fetcher.start()

    def _on_image_ready(self, path):
        image = QImage(path)
        if not image.isNull():
            self._background_image = image
        self.update()

    def _on_image_failed(self, error):
        print(f"[ERROR] Ham Dashboard: world map image download failed ({error}) -- using plain background instead.")
        self.update()

    def set_operator_location(self, lat, lon, label=""):
        self._operator_lat = lat
        self._operator_lon = lon
        self._operator_label = label
        self.update()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return max(self.minimumHeight(), int(width / self.MAP_ASPECT_RATIO))

    def _map_rect(self):
        """The largest MAP_ASPECT_RATIO-shaped rect that fits centered
        within the widget's current size -- everything actually drawn
        (background image, grid, satellites, etc.) goes inside this, not
        the raw self.rect(), so the map itself is never stretched out of
        proportion regardless of what box the surrounding layout ends up
        giving the widget."""
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return QRect(0, 0, w, h)
        if w / self.MAP_ASPECT_RATIO <= h:
            map_w, map_h = w, w / self.MAP_ASPECT_RATIO
        else:
            map_w, map_h = h * self.MAP_ASPECT_RATIO, h
        x0 = (w - map_w) / 2.0
        y0 = (h - map_h) / 2.0
        return QRectF(x0, y0, map_w, map_h)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Letterbox bars (if the widget's actual box isn't exactly
        # MAP_ASPECT_RATIO) in a neutral color matching the rest of the
        # app's dark theme, rather than an unstyled stark black -- then
        # translate into the letterboxed rect's own coordinate space so
        # everything below this can keep using plain (0,0)-origin math
        # against just mw/mh, same as it did against the whole widget
        # before letterboxing existed.
        painter.fillRect(self.rect(), QColor(45, 45, 48))
        map_rect = self._map_rect()
        painter.save()
        painter.translate(map_rect.x(), map_rect.y())
        w, h = map_rect.width(), map_rect.height()

        if self._background_image is not None:
            painter.drawImage(QRectF(0, 0, w, h), self._background_image)
        else:
            painter.fillRect(QRectF(0, 0, w, h), QColor(10, 20, 40))

        now = datetime.datetime.now(datetime.timezone.utc)

        # Night-hemisphere shading -- brute-force grid sampling rather
        # than deriving the terminator's closed-form curve. This redraws
        # once a minute (see HamClockWindow's map_timer), so the cost is
        # a non-issue at this grid resolution.
        lon_step, lat_step = 4, 4
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 140))
        for lon in range(-180, 180, lon_step):
            for lat in range(-90, 90, lat_step):
                if _solar_elevation(lat + lat_step / 2.0, lon + lon_step / 2.0, now) < 0:
                    x = (lon + 180) / 360.0 * w
                    y = (90 - (lat + lat_step)) / 180.0 * h
                    painter.drawRect(QRectF(x, y, lon_step / 360.0 * w + 1, lat_step / 180.0 * h + 1))

        # Lat/long grid every 30 degrees, for orientation.
        painter.setPen(QPen(QColor(60, 80, 110), 1))
        for lon in range(-180, 181, 30):
            x = (lon + 180) / 360.0 * w
            painter.drawLine(QPointF(x, 0), QPointF(x, h))
        for lat in range(-90, 91, 30):
            y = (90 - lat) / 180.0 * h
            painter.drawLine(QPointF(0, y), QPointF(w, y))

        # Equator/prime meridian, slightly brighter for orientation.
        painter.setPen(QPen(QColor(90, 110, 140), 1))
        painter.drawLine(QPointF(w / 2, 0), QPointF(w / 2, h))
        painter.drawLine(QPointF(0, h / 2), QPointF(w, h / 2))

        # Sub-solar point (directly overhead sun).
        decl, sub_lon = _solar_subpoint(now)
        sx = (sub_lon + 180) / 360.0 * w
        sy = (90 - decl) / 180.0 * h
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 210, 60))
        painter.drawEllipse(QPointF(sx, sy), 5, 5)

        # Operator's own location, if set.
        if self._operator_lat is not None and self._operator_lon is not None:
            ox = (self._operator_lon + 180) / 360.0 * w
            oy = (90 - self._operator_lat) / 180.0 * h
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.setBrush(QColor(255, 60, 60))
            painter.drawEllipse(QPointF(ox, oy), 5, 5)
            if self._operator_label:
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(QPointF(ox + 8, oy - 8), self._operator_label)

        # Attribution for the background map image, if it loaded (CC BY
        # 4.0 requires this).
        if self._background_image is not None:
            painter.setPen(QColor(200, 200, 200, 180))
            painter.drawText(QPointF(6, h - 6), WORLD_MAP_ATTRIBUTION)

        if self._satellite_mode:
            for sat in self._satellite_positions:
                self._draw_satellite_footprint(painter, sat, w, h)
            for sat in self._satellite_positions:
                self._draw_satellite_marker(painter, sat, w, h)

        painter.restore()

    def set_satellite_mode(self, enabled):
        self._satellite_mode = enabled
        self.update()

    def set_satellite_positions(self, positions):
        self._satellite_positions = positions
        self.update()

    # Pixel radius around a satellite's marker that still counts as a hit
    # -- markers are drawn at a 4px radius (see _draw_satellite_marker),
    # so this gives some slack for imprecise clicking without the hit
    # areas of nearby satellites overlapping too much.
    _SATELLITE_HIT_RADIUS_PX = 10

    def _satellite_at(self, widget_pos):
        """Returns the name of whichever satellite marker is under
        widget_pos (within _SATELLITE_HIT_RADIUS_PX), or None -- shared
        hit-test behind both the double-click (select/track) and
        right-click (Satellite Info) handlers below."""
        if not self._satellite_mode or not self._satellite_positions:
            return None
        # Markers are drawn in the letterboxed map rect's own (0,0)-origin
        # coordinate space (see paintEvent's painter.translate()) -- shift
        # the click position into that same space before comparing, or
        # every hit-test would be off by the letterbox's offset whenever
        # the widget's box isn't exactly MAP_ASPECT_RATIO.
        map_rect = self._map_rect()
        pos = widget_pos - map_rect.topLeft()
        w, h = map_rect.width(), map_rect.height()
        closest_name, closest_dist = None, None
        for sat in self._satellite_positions:
            x, y = self._lonlat_to_xy(sat["lat"], sat["lon"], w, h)
            dist = math.hypot(x - pos.x(), y - pos.y())
            if dist <= self._SATELLITE_HIT_RADIUS_PX and (closest_dist is None or dist < closest_dist):
                closest_name, closest_dist = sat["name"], dist
        return closest_name

    def mouseDoubleClickEvent(self, event):
        closest_name = self._satellite_at(event.position())
        if closest_name is not None:
            self.satellite_double_clicked.emit(closest_name)

    def mousePressEvent(self, event):
        if event.button() != Qt.RightButton:
            return
        closest_name = self._satellite_at(event.position())
        if closest_name is not None:
            self.satellite_right_clicked.emit(closest_name)

    @staticmethod
    def _lonlat_to_xy(lat, lon, w, h):
        return (lon + 180) / 360.0 * w, (90 - lat) / 180.0 * h

    def _draw_satellite_footprint(self, painter, sat, w, h):
        points = sat.get("footprint") or []
        if len(points) < 3:
            return
        # Split into separate segments wherever the footprint boundary
        # crosses the +/-180 antimeridian -- otherwise a single closed
        # polygon would draw a spurious line straight across the whole
        # map at that crossing.
        segments = [[]]
        prev_lon = None
        for lat, lon in points:
            if prev_lon is not None and abs(lon - prev_lon) > 180:
                segments.append([])
            segments[-1].append((lat, lon))
            prev_lon = lon
        painter.setPen(QPen(QColor(120, 200, 255, 180), 1, Qt.DashLine))
        painter.setBrush(QColor(120, 200, 255, 30))
        for segment in segments:
            if len(segment) < 2:
                continue
            path = QPainterPath()
            x0, y0 = self._lonlat_to_xy(segment[0][0], segment[0][1], w, h)
            path.moveTo(x0, y0)
            for lat, lon in segment[1:]:
                x, y = self._lonlat_to_xy(lat, lon, w, h)
                path.lineTo(x, y)
            painter.drawPath(path)

    def _draw_satellite_marker(self, painter, sat, w, h):
        x, y = self._lonlat_to_xy(sat["lat"], sat["lon"], w, h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 140, 0))
        painter.drawEllipse(QPointF(x, y), 4, 4)

        # Outlined (dark halo + light fill) rather than a flat light
        # color -- a flat color only reads well over the night-shaded
        # hemisphere; over a light daytime landmass it nearly disappears.
        # A stroked QPainterPath keeps it legible over either. Explicit
        # regular-weight font at a modest size, and a thin (not 3px)
        # stroke -- a heavier outline on small map-label text fills in
        # letters' counters (the inside of an "a"/"e"/"o") and runs
        # adjacent letters' outlines together, reading as one bold blob
        # instead of legible text.
        font = QFont(painter.font())
        font.setPointSizeF(9.0)
        font.setBold(False)
        path = QPainterPath()
        path.addText(QPointF(x + 6, y - 6), font, sat["name"])
        painter.setPen(QPen(QColor(15, 15, 20), 1.2))
        painter.setBrush(QColor(255, 230, 190))
        painter.drawPath(path)
