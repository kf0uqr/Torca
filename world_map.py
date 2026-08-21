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

from PySide6.QtCore import Qt, QRect, QRectF, QPointF, Signal, QThread, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPainterPath, QPolygonF, QImage, QFont
from PySide6.QtWidgets import QWidget, QSizePolicy, QToolTip, QPushButton

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

# Despite its filename/description, the actual fetched file (7194x3386
# as of this writing) is NOT a true pole-to-pole -90..+90 equirectangular
# render -- confirmed empirically by cross-referencing known ruler-
# straight political borders at fixed latitudes against their real
# pixel rows in this specific file: the 49th-parallel US/Canada border
# (its Lake of the Woods "Northwest Angle" notch is unmistakable) sits
# at row 680, and the Egypt-Sudan border (dead straight at 22N, with
# its Halaib Triangle corner) sits at row 1203 -- not the rows a naive
# full -90/+90 span would predict (771 and 1279 respectively, ~90px/
# ~5deg off). Fitting a line through those two points gives this
# image's TRUE top/bottom latitude bounds: row 0 is actually about
# +84.1 (missing the northernmost polar cap entirely) and its last row
# overshoots to about -90.6 (a few rows of plain ocean fill past the
# real South Pole, not real geography). Horizontal (longitude)
# placement WAS confirmed correct (land reaches to within a couple
# pixels of both the left and right edges, as a genuine -180/+180 span
# should) -- only vertical placement needed correcting.
#
# _draw_background_image below uses this to draw the image at its
# actual position/scale instead of naively stretching it to fill the
# full latitude range -- everything else (the grid, terminator,
# satellite/QSO markers, operator location) was already using the
# correct/real -180..180 / -90..90 math the whole time; it was only
# ever this raster image that didn't line up with it. Guarded by an
# exact pixel-dimension match so a differently-sized image (e.g. if
# Wikimedia ever serves an edited/replaced file at that same URL, per
# WORLD_MAP_IMAGE_URL's own docstring about it being a "stable
# redirect to whatever the CURRENT version is") falls back to the
# naive full -90/+90 assumption instead of applying a now-wrong
# correction blindly.
_CALIBRATED_IMAGE_SIZE = (7194, 3386)
_CALIBRATED_LAT_TOP = 84.109
_CALIBRATED_LAT_BOTTOM = -90.647


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
    satellite_left_clicked = Signal(str)    # satellite name -- sets the "active" map-highlighted satellite

    # The background image (and the whole lat/lon grid it's drawn
    # against) is a standard equirectangular projection: 2:1 width:height.
    # Stretching the widget to some other ratio would distort it, so the
    # actual map content is always letterboxed to this ratio within
    # whatever box the widget is given -- see _map_rect().
    MAP_ASPECT_RATIO = 2.0

    # Path direction-arrow animation -- see _advance_path_animation.
    _PATH_ANIMATION_INTERVAL_MS = 60
    _PATH_ANIMATION_SPEED = 0.008  # progress fraction per tick -- one full lap of a shown path in a few seconds

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
        self._qso_markers = []  # list of {"lat", "lon", "band", "time", "callsign"} -- see set_qso_markers
        self._pskreporter_markers = []  # list of {"lat", "lon", "tooltip"} -- see set_pskreporter_markers
        self._pota_markers = []  # list of {"lat", "lon", "tooltip"} -- see set_pota_markers
        # Hover tooltip needs mouseMoveEvent to fire without a button
        # held down -- off by default on a plain QWidget.
        self.setMouseTracking(True)

        # ---- Zoom/pan ----
        # zoom=1.0 is the original fit-to-widget view (no scaling, pan
        # locked to 0,0 by _clamp_pan -- there's nowhere to pan to
        # without room to scroll into). Content is scaled/panned via a
        # single QPainter transform applied around all the existing
        # drawing code in paintEvent, rather than reworking every
        # individual _lonlat_to_xy call site -- so the background image,
        # grid, terminator shading, and every marker all zoom/pan
        # together for free. _pan_x/_pan_y are offsets, in UNZOOMED
        # map-content pixels, of the view center from the map's true
        # center (map_rect's own w/2, h/2) -- see _clamp_pan for why
        # that unit choice makes the valid range shrink cleanly to
        # {0,0} at zoom=1.
        self.MIN_ZOOM = 1.0
        self.MAX_ZOOM = 12.0
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        # Left-button drag-to-pan vs. click (satellite select / QSO
        # tooltip) disambiguation -- see mousePressEvent/mouseMoveEvent/
        # mouseReleaseEvent. Click behavior is decided on RELEASE now
        # (was on press, before panning existed): only fires if the
        # press-to-release movement never exceeded _DRAG_THRESHOLD_PX.
        self._drag_start_pos = None
        self._drag_start_pan = (0.0, 0.0)
        self._drag_active = False
        self._DRAG_THRESHOLD_PX = 4

        # Animated arrow along each shown ground track (sat["path"]),
        # indicating direction of travel -- a single shared progress
        # value (0.0-1.0, wrapping) rather than one per satellite, so
        # every visible path's arrow moves at the same wall-clock pace
        # even though their real ground speeds differ slightly.
        # ground_track_points() (satellite_tracking.py) orders points
        # chronologically (past -> future), so walking progress 0->1
        # through a path's point list is the same direction the
        # satellite is actually travelling.
        self._path_animation_progress = 0.0
        self._path_animation_timer = QTimer(self)
        self._path_animation_timer.timeout.connect(self._advance_path_animation)
        self._path_animation_timer.start(self._PATH_ANIMATION_INTERVAL_MS)

        self._image_fetcher = WorldMapImageFetcher()
        self._image_fetcher.image_ready.connect(self._on_image_ready)
        self._image_fetcher.failed.connect(self._on_image_failed)
        self._image_fetcher.start()

        # ---- +/- zoom buttons ----
        # Plain child QWidgets positioned by hand (not a layout -- they
        # need to float in the map's own top-left corner, tracking
        # _map_rect() rather than the widget's own rect, since letterbox
        # bars mean those aren't the same point whenever the widget's
        # box isn't exactly MAP_ASPECT_RATIO) -- repositioned in
        # resizeEvent below. A semi-opaque dark background keeps them
        # legible over either the day or night hemisphere.
        button_style = (
            "QPushButton { background-color: rgba(30, 30, 34, 200); color: white; "
            "border: 1px solid rgba(200, 200, 200, 120); border-radius: 4px; "
            "font-weight: bold; font-size: 14px; }"
            "QPushButton:hover { background-color: rgba(60, 60, 66, 220); }"
            "QPushButton:pressed { background-color: rgba(90, 90, 98, 230); }"
        )
        self._ZOOM_BUTTON_SIZE = 26
        self._ZOOM_BUTTON_MARGIN = 8
        self.zoom_in_button = QPushButton("+", self)
        self.zoom_in_button.setFixedSize(self._ZOOM_BUTTON_SIZE, self._ZOOM_BUTTON_SIZE)
        self.zoom_in_button.setStyleSheet(button_style)
        self.zoom_in_button.setToolTip("Zoom in")
        self.zoom_in_button.clicked.connect(self._on_zoom_in_clicked)
        self.zoom_out_button = QPushButton("−", self)  # proper minus sign, not a hyphen
        self.zoom_out_button.setFixedSize(self._ZOOM_BUTTON_SIZE, self._ZOOM_BUTTON_SIZE)
        self.zoom_out_button.setStyleSheet(button_style)
        self.zoom_out_button.setToolTip("Zoom out")
        self.zoom_out_button.clicked.connect(self._on_zoom_out_clicked)
        self._position_zoom_buttons()

    def wait_for_pending_image_fetch(self, timeout_ms=21000):
        """Call from the owning window's closeEvent before it returns
        -- _image_fetcher is a one-shot QThread with no interruption
        support at all (nothing to check between steps: it's cache-hit-
        instant, or one single blocking urllib call up to its own 20s
        timeout, never a loop), so unlike SolarDataWorker there's no
        cooperative flag to set here, only waiting it out. Only matters
        on a fresh install/cache miss -- once WORLD_MAP_CACHE_PATH
        exists (true after the very first successful run), this
        returns instantly every time after. Default timeout covers
        that one urllib call's own 20s bound with a small margin."""
        self._image_fetcher.wait(timeout_ms)

    _ZOOM_BUTTON_FACTOR = 1.4  # per-click step -- a bigger jump than one wheel notch (_ZOOM_STEP), since a click is discrete

    def _on_zoom_in_clicked(self):
        self._zoom_at(self._map_rect().center(), self._ZOOM_BUTTON_FACTOR)

    def _on_zoom_out_clicked(self):
        self._zoom_at(self._map_rect().center(), 1.0 / self._ZOOM_BUTTON_FACTOR)

    def _position_zoom_buttons(self):
        map_rect = self._map_rect()
        x = int(map_rect.x()) + self._ZOOM_BUTTON_MARGIN
        y = int(map_rect.y()) + self._ZOOM_BUTTON_MARGIN
        self.zoom_in_button.move(x, y)
        self.zoom_out_button.move(x, y + self._ZOOM_BUTTON_SIZE + 4)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_zoom_buttons()

    def _advance_path_animation(self):
        # Only worth repainting (a full paintEvent -- background image
        # blit, grid, terminator, every satellite) at this fast a cadence
        # while there's actually an animated path on screen; otherwise
        # this timer is a cheap no-op every tick.
        if not self._satellite_mode or not any(sat.get("path") for sat in self._satellite_positions):
            return
        self._path_animation_progress = (self._path_animation_progress + self._PATH_ANIMATION_SPEED) % 1.0
        self.update()

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
        # Clip to the letterboxed map rect itself (not just the widget's
        # own rect, already the default) -- without this, zoomed-in
        # content spills into the letterbox bars whenever the widget's
        # box isn't exactly MAP_ASPECT_RATIO, since the zoom transform
        # below can make drawn content larger than map_rect.
        painter.setClipRect(QRectF(0, 0, w, h))

        # Zoom/pan transform -- scales/translates everything drawn below
        # this point (background image, grid, terminator, every
        # marker), restored before the fixed-position attribution text
        # so that stays put regardless of zoom. See __init__'s comment
        # on _pan_x/_pan_y's units for why this specific translate/
        # scale/translate order is what makes them "offset from center
        # in unzoomed content pixels".
        painter.save()
        painter.translate(w / 2.0, h / 2.0)
        painter.scale(self._zoom, self._zoom)
        painter.translate(-(w / 2.0 + self._pan_x), -(h / 2.0 + self._pan_y))

        if self._background_image is not None:
            self._draw_background_image(painter, w, h)
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

        # Geographic overlays that make sense to zoom WITH the map --
        # coverage circles and ground tracks are real areas/paths over
        # the earth, same as the grid/terminator above.
        if self._satellite_mode:
            for sat in self._satellite_positions:
                self._draw_satellite_footprint(painter, sat, w, h)
            for sat in self._satellite_positions:
                self._draw_satellite_path(painter, sat, w, h)

        painter.restore()  # undo the zoom/pan transform

        # Point markers and their text labels -- the sub-solar point,
        # operator location, satellite markers/names, and QSO dots --
        # are deliberately drawn AFTER restoring the zoom transform,
        # at their correctly zoomed/panned SCREEN position (via
        # _content_to_screen) but with a FIXED, unscaled size. Per
        # explicit feedback: these ballooning to the same degree as the
        # background map when zoomed in made them harder to read, not
        # easier -- a location marker only needs to be "found", not
        # "measured", so it doesn't need to get bigger just because the
        # map under it did.

        # Sub-solar point (directly overhead sun).
        decl, sub_lon = _solar_subpoint(now)
        cx, cy = self._lonlat_to_xy(decl, sub_lon, w, h)
        sx, sy = self._content_to_screen(cx, cy, w, h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 210, 60))
        painter.drawEllipse(QPointF(sx, sy), 5, 5)

        # Operator's own location, if set.
        if self._operator_lat is not None and self._operator_lon is not None:
            cx, cy = self._lonlat_to_xy(self._operator_lat, self._operator_lon, w, h)
            ox, oy = self._content_to_screen(cx, cy, w, h)
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.setBrush(QColor(255, 60, 60))
            painter.drawEllipse(QPointF(ox, oy), 5, 5)
            if self._operator_label:
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(QPointF(ox + 8, oy - 8), self._operator_label)

        if self._satellite_mode:
            for sat in self._satellite_positions:
                cx, cy = self._lonlat_to_xy(sat["lat"], sat["lon"], w, h)
                sx, sy = self._content_to_screen(cx, cy, w, h)
                self._draw_satellite_marker(painter, sat, sx, sy)

        if self._qso_markers:
            painter.setPen(QPen(QColor(15, 15, 20), 1))
            painter.setBrush(QColor(80, 230, 130))
            for qso in self._qso_markers:
                cx, cy = self._lonlat_to_xy(qso["lat"], qso["lon"], w, h)
                sx, sy = self._content_to_screen(cx, cy, w, h)
                painter.drawEllipse(QPointF(sx, sy), 3.5, 3.5)

        if self._pskreporter_markers:
            # Deliberately a distinct hue from every other marker
            # category (green QSOs, yellow/orange satellites, red
            # operator location, teal POTA spots) -- per explicit
            # instruction, so every spot category stays visually
            # distinguishable from every other at a glance.
            painter.setPen(QPen(QColor(15, 15, 20), 1))
            painter.setBrush(QColor(190, 110, 255))
            for spot in self._pskreporter_markers:
                cx, cy = self._lonlat_to_xy(spot["lat"], spot["lon"], w, h)
                sx, sy = self._content_to_screen(cx, cy, w, h)
                painter.drawEllipse(QPointF(sx, sy), 3.5, 3.5)

        if self._pota_markers:
            painter.setPen(QPen(QColor(15, 15, 20), 1))
            painter.setBrush(QColor(0, 200, 200))
            for spot in self._pota_markers:
                cx, cy = self._lonlat_to_xy(spot["lat"], spot["lon"], w, h)
                sx, sy = self._content_to_screen(cx, cy, w, h)
                painter.drawEllipse(QPointF(sx, sy), 3.5, 3.5)

        # Attribution for the background map image, if it loaded (CC BY
        # 4.0 requires this) -- fixed corner label regardless of zoom/pan.
        if self._background_image is not None:
            painter.setPen(QColor(200, 200, 200, 180))
            painter.drawText(QPointF(6, h - 6), WORLD_MAP_ATTRIBUTION)

        painter.restore()

    def _draw_background_image(self, painter, w, h):
        """Draws self._background_image into the (0,0,w,h) content
        rect -- but at the image's OWN true lat/lon bounds (see
        _CALIBRATED_LAT_TOP/_CALIBRATED_LAT_BOTTOM's module-level
        comment) rather than naively stretching it to fill the full
        -90..+90 range, which left it visibly misaligned from the grid
        and every marker (all of which already use the correct/real
        latitude math). A solid ocean-color base fill covers the small
        gap this leaves near the North Pole (this image's content
        doesn't reach that far), which otherwise would have shown
        whatever's behind the map (the widget's own dark background)
        instead of more ocean."""
        image = self._background_image
        if (image.width(), image.height()) == _CALIBRATED_IMAGE_SIZE:
            lat_top, lat_bottom = _CALIBRATED_LAT_TOP, _CALIBRATED_LAT_BOTTOM
        else:
            lat_top, lat_bottom = 90.0, -90.0
        y0 = (90.0 - lat_top) / 180.0 * h
        y1 = (90.0 - lat_bottom) / 180.0 * h
        painter.fillRect(QRectF(0, 0, w, h), QColor(72, 114, 255))
        painter.drawImage(QRectF(0, y0, w, y1 - y0), image)

    def set_satellite_mode(self, enabled):
        self._satellite_mode = enabled
        self.update()

    def set_satellite_positions(self, positions):
        self._satellite_positions = positions
        self.update()

    def set_qso_markers(self, markers):
        """markers: list of {"lat", "lon", "band", "time", "callsign"}
        -- ham_dashboard.py's QSO map button, converted from the log's
        stored grid squares (adif.grid_square_to_latlon). Empty list
        (the button's own OFF state) just stops drawing/hit-testing
        them, same shape as set_satellite_positions([])."""
        self._qso_markers = markers
        self.update()

    def set_pskreporter_markers(self, markers):
        """markers: list of {"lat", "lon", "tooltip"} -- ham_dashboard.py's
        PSKReporter map button, converted from PSKReporter spots
        (pskreporter.fetch_pskreporter_spots), converted from each
        spot's locator (adif.grid_square_to_latlon). Unlike QSO/
        satellite markers, the tooltip text is pre-built by the caller
        (not band/time/callsign fields the widget formats itself) --
        PSKReporter spots carry a variably-shaped bag of extra fields
        (region, DXCC, LoTW upload date, ...) that this widget has no
        reason to know the specific names of; ham_dashboard.py owns
        deciding what "all the pskreporter data for that spot" means.
        Empty list (the button's own OFF state) just stops drawing/
        hit-testing them, same shape as set_qso_markers([])."""
        self._pskreporter_markers = markers
        self.update()

    def set_pota_markers(self, markers):
        """markers: list of {"lat", "lon", "tooltip"} -- ham_dashboard.py's
        POTA map button, converted from pota.fetch_pota_spots() (which
        already includes latitude/longitude directly, no grid-square
        conversion needed). Same pre-built-tooltip shape as
        set_pskreporter_markers, same reasoning: POTA spots carry their
        own bag of fields (park name/reference, spotter, comments,
        count, ...) this widget doesn't need to know the shape of."""
        self._pota_markers = markers
        self.update()

    # Pixel radius around a QSO marker that still counts as a hover/
    # click hit -- markers are drawn at a 3.5px radius (see paintEvent),
    # matching the same slack-for-imprecision reasoning as satellite
    # markers' own _SATELLITE_HIT_RADIUS_PX. Measured in SCREEN pixels
    # (see _widget_pos_to_content) so the hit area doesn't effectively
    # shrink to nothing when zoomed out, or balloon when zoomed in.
    _QSO_HIT_RADIUS_PX = 8

    def _widget_pos_to_content(self, widget_pos):
        """Converts a widget-space position (e.g. from a mouse event)
        into the map's unzoomed content-pixel space -- the same space
        _lonlat_to_xy already returns coordinates in -- by inverting
        paintEvent's zoom/pan transform. Every hit-test (satellites, QSO
        markers) compares against content-space coordinates via this,
        rather than transforming each marker's position individually,
        since there's only ever one cursor position to convert per
        hit-test call. Returns (content_pos, w, h)."""
        map_rect = self._map_rect()
        local = widget_pos - map_rect.topLeft()
        w, h = map_rect.width(), map_rect.height()
        cx = (local.x() - w / 2.0) / self._zoom + w / 2.0 + self._pan_x
        cy = (local.y() - h / 2.0) / self._zoom + h / 2.0 + self._pan_y
        return QPointF(cx, cy), w, h

    def _content_to_screen(self, cx, cy, w, h):
        """Inverse of _widget_pos_to_content's math: converts a
        content-space (unzoomed, _lonlat_to_xy) coordinate into where
        it currently lands on screen (still in the map_rect's own
        (0,0)-origin frame, i.e. the frame paintEvent is in right after
        restoring the zoom transform). Used to position fixed-size
        markers/labels at the correct zoomed/panned spot without
        drawing them WITH the zoom scale active (which would also scale
        their size -- see paintEvent's comment on why that's undesirable)."""
        sx = w / 2.0 + (cx - (w / 2.0 + self._pan_x)) * self._zoom
        sy = h / 2.0 + (cy - (h / 2.0 + self._pan_y)) * self._zoom
        return sx, sy

    def _qso_marker_at(self, widget_pos):
        """Returns whichever QSO marker dict is under widget_pos
        (within _QSO_HIT_RADIUS_PX screen pixels), or None -- same
        shared hit-test shape as _satellite_at, used by both the hover
        and click tooltip paths below."""
        if not self._qso_markers:
            return None
        pos, w, h = self._widget_pos_to_content(widget_pos)
        hit_radius = self._QSO_HIT_RADIUS_PX / self._zoom  # screen px -> content px
        closest, closest_dist = None, None
        for qso in self._qso_markers:
            x, y = self._lonlat_to_xy(qso["lat"], qso["lon"], w, h)
            dist = math.hypot(x - pos.x(), y - pos.y())
            if dist <= hit_radius and (closest_dist is None or dist < closest_dist):
                closest, closest_dist = qso, dist
        return closest

    @staticmethod
    def _qso_tooltip_text(qso):
        return f"{qso.get('callsign', '?')}\n{qso.get('band', '?')}\n{qso.get('time', '?')}"

    # Same screen-pixel-radius reasoning as _QSO_HIT_RADIUS_PX.
    _PSKREPORTER_HIT_RADIUS_PX = 8

    def _pskreporter_marker_at(self, widget_pos):
        """Same shared hit-test shape as _qso_marker_at, for the
        PSKReporter spot markers."""
        if not self._pskreporter_markers:
            return None
        pos, w, h = self._widget_pos_to_content(widget_pos)
        hit_radius = self._PSKREPORTER_HIT_RADIUS_PX / self._zoom
        closest, closest_dist = None, None
        for spot in self._pskreporter_markers:
            x, y = self._lonlat_to_xy(spot["lat"], spot["lon"], w, h)
            dist = math.hypot(x - pos.x(), y - pos.y())
            if dist <= hit_radius and (closest_dist is None or dist < closest_dist):
                closest, closest_dist = spot, dist
        return closest

    # Same screen-pixel-radius reasoning as _QSO_HIT_RADIUS_PX.
    _POTA_HIT_RADIUS_PX = 8

    def _pota_marker_at(self, widget_pos):
        """Same shared hit-test shape as _qso_marker_at, for the POTA
        spot markers."""
        if not self._pota_markers:
            return None
        pos, w, h = self._widget_pos_to_content(widget_pos)
        hit_radius = self._POTA_HIT_RADIUS_PX / self._zoom
        closest, closest_dist = None, None
        for spot in self._pota_markers:
            x, y = self._lonlat_to_xy(spot["lat"], spot["lon"], w, h)
            dist = math.hypot(x - pos.x(), y - pos.y())
            if dist <= hit_radius and (closest_dist is None or dist < closest_dist):
                closest, closest_dist = spot, dist
        return closest

    # Pixel radius around a satellite's marker that still counts as a hit
    # -- markers are drawn at a 4px radius (see _draw_satellite_marker),
    # so this gives some slack for imprecise clicking without the hit
    # areas of nearby satellites overlapping too much. Screen pixels,
    # same reasoning as _QSO_HIT_RADIUS_PX above.
    _SATELLITE_HIT_RADIUS_PX = 10

    def _satellite_at(self, widget_pos):
        """Returns the name of whichever satellite marker is under
        widget_pos (within _SATELLITE_HIT_RADIUS_PX screen pixels), or
        None -- shared hit-test behind both the double-click (select/
        track) and right-click (Satellite Info) handlers below."""
        if not self._satellite_mode or not self._satellite_positions:
            return None
        pos, w, h = self._widget_pos_to_content(widget_pos)
        hit_radius = self._SATELLITE_HIT_RADIUS_PX / self._zoom
        closest_name, closest_dist = None, None
        for sat in self._satellite_positions:
            x, y = self._lonlat_to_xy(sat["lat"], sat["lon"], w, h)
            dist = math.hypot(x - pos.x(), y - pos.y())
            if dist <= hit_radius and (closest_dist is None or dist < closest_dist):
                closest_name, closest_dist = sat["name"], dist
        return closest_name

    def _clamp_pan(self):
        """Keeps the view center's pan offset within whatever range
        still shows real content on every edge -- at zoom=1 the valid
        range is exactly {0,0} (fit-to-widget, nowhere to pan), and it
        widens as zoom increases (more of the oversized content is
        available to scroll to). Without this, dragging or zooming near
        an edge could push the visible viewport off the map entirely
        (blank space and/or the background image sliding fully out of
        the clip rect)."""
        map_rect = self._map_rect()
        w, h = map_rect.width(), map_rect.height()
        if self._zoom <= 1.0:
            self._pan_x = 0.0
            self._pan_y = 0.0
            return
        max_pan_x = (w / 2.0) * (1.0 - 1.0 / self._zoom)
        max_pan_y = (h / 2.0) * (1.0 - 1.0 / self._zoom)
        self._pan_x = max(-max_pan_x, min(max_pan_x, self._pan_x))
        self._pan_y = max(-max_pan_y, min(max_pan_y, self._pan_y))

    def _zoom_at(self, widget_pos, factor):
        """Multiplies the zoom level by factor (clamped to [MIN_ZOOM,
        MAX_ZOOM]), adjusting pan so the content point currently under
        widget_pos stays under it -- the usual "zoom toward the cursor"
        map UX, rather than always zooming toward the view center."""
        content_pos, w, h = self._widget_pos_to_content(widget_pos)
        if w <= 0 or h <= 0:
            return
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, self._zoom * factor))
        if new_zoom == self._zoom:
            return
        map_rect = self._map_rect()
        local = widget_pos - map_rect.topLeft()
        self._zoom = new_zoom
        self._pan_x = content_pos.x() - w / 2.0 - (local.x() - w / 2.0) / new_zoom
        self._pan_y = content_pos.y() - h / 2.0 - (local.y() - h / 2.0) / new_zoom
        self._clamp_pan()
        self.update()

    def wheelEvent(self, event):
        # angleDelta().y() is in eighths of a degree, +/-120 per typical
        # notch -- one notch is one ZOOM_STEP application.
        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            return
        self._zoom_at(event.position(), self._ZOOM_STEP ** steps)
        event.accept()

    _ZOOM_STEP = 1.15

    def mouseMoveEvent(self, event):
        if self._drag_start_pos is not None and (event.buttons() & Qt.LeftButton):
            delta = event.position() - self._drag_start_pos
            if not self._drag_active and delta.manhattanLength() > self._DRAG_THRESHOLD_PX:
                self._drag_active = True
            if self._drag_active:
                # Converts a SCREEN-pixel drag delta into content-space
                # pan -- see _clamp_pan's docstring for _pan_x/_pan_y's
                # units (unzoomed content pixels), so a drag of d screen
                # px needs to move pan by d/zoom to keep the dragged
                # point following the cursor exactly.
                self._pan_x = self._drag_start_pan[0] - delta.x() / self._zoom
                self._pan_y = self._drag_start_pan[1] - delta.y() / self._zoom
                self._clamp_pan()
                QToolTip.hideText()
                self.update()
                return
        qso = self._qso_marker_at(event.position())
        if qso is not None:
            QToolTip.showText(event.globalPosition().toPoint(), self._qso_tooltip_text(qso), self)
            return
        spot = self._pskreporter_marker_at(event.position())
        if spot is not None:
            QToolTip.showText(event.globalPosition().toPoint(), spot.get("tooltip", "?"), self)
            return
        pota_spot = self._pota_marker_at(event.position())
        if pota_spot is not None:
            QToolTip.showText(event.globalPosition().toPoint(), pota_spot.get("tooltip", "?"), self)
        else:
            QToolTip.hideText()

    def mouseDoubleClickEvent(self, event):
        closest_name = self._satellite_at(event.position())
        if closest_name is not None:
            self.satellite_double_clicked.emit(closest_name)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Click-vs-drag is decided on RELEASE (see mouseReleaseEvent)
            # -- just record where the press started here.
            self._drag_start_pos = event.position()
            self._drag_start_pan = (self._pan_x, self._pan_y)
            self._drag_active = False
            return
        if event.button() == Qt.RightButton:
            closest_name = self._satellite_at(event.position())
            if closest_name is not None:
                self.satellite_right_clicked.emit(closest_name)
            elif (
                self._qso_marker_at(event.position()) is None
                and self._pskreporter_marker_at(event.position()) is None
                and self._pota_marker_at(event.position()) is None
            ):
                # Right-clicking genuinely empty map (no satellite, no
                # QSO/PSKReporter/POTA marker under the cursor) resets
                # the view -- the only reset affordance this needs,
                # since scrolling back out to MIN_ZOOM is otherwise the
                # only way back once zoomed/panned in.
                self._zoom = self.MIN_ZOOM
                self._pan_x = 0.0
                self._pan_y = 0.0
                self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        was_dragging = self._drag_active
        self._drag_start_pos = None
        self._drag_active = False
        if was_dragging:
            return  # just finished a pan -- not a click
        qso = self._qso_marker_at(event.position())
        if qso is not None:
            # Click shows the same tooltip hovering already would --
            # explicitly asked for as an alternative to hovering, not
            # just a side effect of it.
            QToolTip.showText(event.globalPosition().toPoint(), self._qso_tooltip_text(qso), self)
            return
        spot = self._pskreporter_marker_at(event.position())
        if spot is not None:
            QToolTip.showText(event.globalPosition().toPoint(), spot.get("tooltip", "?"), self)
            return
        pota_spot = self._pota_marker_at(event.position())
        if pota_spot is not None:
            QToolTip.showText(event.globalPosition().toPoint(), pota_spot.get("tooltip", "?"), self)
            return
        closest_name = self._satellite_at(event.position())
        if closest_name is not None:
            # Also fires as the first release of a double-click (Qt sends
            # press/release/press/doubleClick/release for one) -- that's
            # fine, ham_dashboard.py's double-click handler sets the same
            # "active" state itself, so this is just redundant, not wrong.
            self.satellite_left_clicked.emit(closest_name)

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
        if sat.get("active"):
            painter.setPen(QPen(QColor(255, 230, 0, 220), 1.5, Qt.DashLine))
            painter.setBrush(QColor(255, 230, 0, 45))
        else:
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

    def _draw_satellite_path(self, painter, sat, w, h):
        """Draws the satellite's orbit ground track -- present in
        sat["path"] either because it's opt-in per satellite (see
        ham_dashboard.py's "Show Orbit Path" right-click toggle) or
        because this is the currently map-"active" satellite (always
        shown while active, regardless of its own Show Orbit Path
        setting -- see ham_dashboard.py's _update_satellite_positions)."""
        points = sat.get("path") or []
        if len(points) < 2:
            return
        # Same antimeridian-crossing segment split as
        # _draw_satellite_footprint, just for an open polyline instead
        # of a closed/filled polygon -- otherwise the ground track would
        # draw a spurious line straight across the map at each crossing.
        segments = [[]]
        prev_lon = None
        for lat, lon in points:
            if prev_lon is not None and abs(lon - prev_lon) > 180:
                segments.append([])
            segments[-1].append((lat, lon))
            prev_lon = lon
        if sat.get("active"):
            painter.setPen(QPen(QColor(255, 230, 0, 220), 2.5, Qt.SolidLine))
        else:
            painter.setPen(QPen(QColor(255, 140, 0, 160), 1.5, Qt.SolidLine))
        painter.setBrush(Qt.NoBrush)
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
        self._draw_path_direction_arrow(painter, points, w, h, sat.get("active"))

    def _draw_path_direction_arrow(self, painter, points, w, h, active):
        """A small arrowhead that animates along the ground track
        (self._path_animation_progress, 0.0-1.0) to show which way the
        satellite is actually moving -- points is already ordered
        starting at the satellite's current position and running one
        full orbit into the future (ground_track_points), so walking
        it start-to-end is the real direction of travel."""
        segment_count = len(points) - 1
        if segment_count < 1:
            return
        position = self._path_animation_progress * segment_count
        index = min(int(position), segment_count - 1)
        frac = position - index
        lat0, lon0 = points[index]
        lat1, lon1 = points[index + 1]
        if abs(lon1 - lon0) > 180:
            return  # crosses the antimeridian -- skip this frame rather than draw a spurious jump
        x0, y0 = self._lonlat_to_xy(lat0, lon0, w, h)
        x1, y1 = self._lonlat_to_xy(lat1, lon1, w, h)
        x = x0 + (x1 - x0) * frac
        y = y0 + (y1 - y0) * frac
        angle = math.atan2(y1 - y0, x1 - x0)
        size = 7.0 if active else 5.0
        spread = 2.6  # radians off the heading for each back corner -- a narrow-ish forward-pointing triangle
        tip = QPointF(x + size * math.cos(angle), y + size * math.sin(angle))
        left = QPointF(x + size * math.cos(angle + spread), y + size * math.sin(angle + spread))
        right = QPointF(x + size * math.cos(angle - spread), y + size * math.sin(angle - spread))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 235) if active else QColor(255, 200, 120, 210))
        painter.drawPolygon(QPolygonF([tip, left, right]))

    def _draw_satellite_marker(self, painter, sat, x, y):
        """x, y: already-computed SCREEN-space position (see paintEvent
        -- deliberately not content-space + w/h like the footprint/path
        drawing methods, since this marker+label is drawn AFTER the
        zoom transform is restored, at a fixed unscaled size)."""
        if sat.get("active"):
            painter.setPen(QPen(QColor(255, 255, 255, 220), 1))
            painter.setBrush(QColor(255, 230, 0))
            painter.drawEllipse(QPointF(x, y), 6, 6)
        else:
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
