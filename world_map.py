"""
The Ham Dashboard's day/night world map: a zoomable OpenStreetMap tile
background (map_tiles.py -- fetched on demand as the viewport needs them,
cached locally) with the night hemisphere shaded via a smooth terminator
curve, the sub-solar point, the operator's own location, and -- when
satellite tracking is on -- satellite markers and their footprint
polygons, all drawn on top.
"""

import datetime
import math

from PySide6.QtCore import Qt, QRectF, QPointF, Signal, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPainterPath, QPolygonF, QFont
from PySide6.QtWidgets import QWidget, QSizePolicy, QToolTip, QPushButton

from solar_data import _solar_subpoint
import map_tiles


class WorldMapWidget(QWidget):
    """A day/night map in the spirit of (not a reimplementation of)
    HamClock's map pane: a zoomable OpenStreetMap tile background
    (map_tiles.py -- fetched on demand, cached locally; unfetched tiles
    draw as a plain placeholder fill until they arrive), with the night
    hemisphere shaded via a smooth terminator curve, the sub-solar point,
    and the operator's own location if set, drawn on top.

    Fills the widget's own natural aspect ratio -- unlike the old static
    equirectangular image (which had to be letterboxed to a fixed 2:1 box
    to avoid visibly distorting it), a real map has no such constraint;
    every other map app just fills its container."""

    satellite_double_clicked = Signal(str)  # satellite name, from the positions passed to set_satellite_positions
    satellite_right_clicked = Signal(str)   # satellite name -- opens Satellite Info (ham_dashboard.py)
    satellite_left_clicked = Signal(str)    # satellite name -- sets the "active" map-highlighted satellite

    # Path direction-arrow animation -- see _advance_path_animation.
    _PATH_ANIMATION_INTERVAL_MS = 60
    _PATH_ANIMATION_SPEED = 0.008  # progress fraction per tick -- one full lap of a shown path in a few seconds

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._operator_lat = None
        self._operator_lon = None
        self._operator_label = ""
        self._tile_cache = map_tiles.TileCache()
        self._tile_fetcher = map_tiles.TileFetcher(self)
        self._tile_fetcher.tile_ready.connect(self._on_tile_ready)
        self._satellite_mode = False
        self._satellite_positions = []  # list of {"name", "lat", "lon", "altitude_km", "footprint"}
        self._qso_markers = []  # list of {"lat", "lon", "band", "time", "callsign"} -- see set_qso_markers
        self._pskreporter_markers = []  # list of {"lat", "lon", "tooltip"} -- see set_pskreporter_markers
        self._pota_markers = []  # list of {"lat", "lon", "tooltip"} -- see set_pota_markers
        self._aprs_markers = []  # list of {"lat", "lon", "tooltip"} -- see set_aprs_markers
        # Hover tooltip needs mouseMoveEvent to fire without a button
        # held down -- off by default on a plain QWidget.
        self.setMouseTracking(True)

        # ---- Zoom/pan ----
        # zoom=1.0 is the original fit-to-widget view (no scaling, pan
        # locked to 0,0 by _clamp_pan -- there's nowhere to pan to
        # without room to scroll into). Content is scaled/panned via a
        # single QPainter transform applied around all the existing
        # drawing code in paintEvent, rather than reworking every
        # individual _lonlat_to_xy call site -- so the map tiles,
        # terminator shading, and every marker all zoom/pan together for
        # free. _pan_x/_pan_y are offsets, in UNZOOMED map-content
        # pixels, of the view center from the map's true center (self.
        # width()/2, self.height()/2) -- see _clamp_pan for why that unit
        # choice makes the valid range shrink cleanly to {0,0} at zoom=1.
        #
        # MAX_ZOOM has to be large enough to actually let effective_zoom
        # (_draw_map_tiles's own log2(w * self._zoom / TILE_SIZE_PX))
        # reach map_tiles.MAX_TILE_ZOOM at all -- since effective_zoom
        # grows with log2(self._zoom), reaching real z19 (building-level)
        # detail from a whole-world starting view genuinely needs a
        # self._zoom on the order of hundreds of thousands (2**19 is
        # already >500,000) -- confirmed by working the formula
        # backwards; an earlier, much smaller constant here (in the
        # tens) silently made it impossible to ever zoom in past roughly
        # z7-9, well short of real street-level tiles, even though
        # nothing about the tile-fetch/draw code itself was wrong. Sized
        # generously for realistic dashboard widget widths (not an
        # exhaustive guarantee at pathologically narrow widths) -- past
        # map_tiles.MAX_TILE_ZOOM this is still just a QPainter
        # magnification multiplier on the same highest-detail tiles,
        # same as any real map app past its own max zoom.
        self.MIN_ZOOM = 1.0
        self.MAX_ZOOM = 1_000_000.0
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

        # ---- +/- zoom buttons ----
        # Plain child QWidgets positioned by hand (not a layout -- they
        # need to float in the map's own top-left corner) -- repositioned
        # in resizeEvent below. A semi-opaque dark background keeps them
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

    def shutdown_tile_fetcher(self):
        """Call from the owning window's closeEvent before it returns --
        the tile fetcher's worker threads are PERSISTENT (unlike the old
        one-shot image fetcher), so they need an explicit stop, not just
        a wait: destroying a still-running QThread aborts the process,
        the same "QThread destroyed while still running" crash this app
        already fixed for every other background worker."""
        self._tile_fetcher.shutdown()

    _ZOOM_BUTTON_FACTOR = 1.4  # per-click step -- a bigger jump than one wheel notch (_ZOOM_STEP), since a click is discrete

    def _on_zoom_in_clicked(self):
        self._zoom_at(QPointF(self.width() / 2.0, self.height() / 2.0), self._ZOOM_BUTTON_FACTOR)

    def _on_zoom_out_clicked(self):
        self._zoom_at(QPointF(self.width() / 2.0, self.height() / 2.0), 1.0 / self._ZOOM_BUTTON_FACTOR)

    def _position_zoom_buttons(self):
        x = self._ZOOM_BUTTON_MARGIN
        y = self._ZOOM_BUTTON_MARGIN
        self.zoom_in_button.move(x, y)
        self.zoom_out_button.move(x, y + self._ZOOM_BUTTON_SIZE + 4)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_zoom_buttons()

    def _advance_path_animation(self):
        # Only worth repainting (a full paintEvent -- map tiles,
        # terminator, every satellite) at this fast a cadence
        # while there's actually an animated path on screen; otherwise
        # this timer is a cheap no-op every tick.
        if not self._satellite_mode or not any(sat.get("path") for sat in self._satellite_positions):
            return
        self._path_animation_progress = (self._path_animation_progress + self._PATH_ANIMATION_SPEED) % 1.0
        self.update()

    def _on_tile_ready(self, z, x, y, path):
        # Just triggers a repaint -- _draw_map_tiles re-checks
        # self._tile_cache.get(...) itself next paintEvent (which now
        # finds this tile decoded and ready, having just been written to
        # disk by the fetch worker), rather than this handler pushing the
        # pixmap in directly.
        self.update()

    def set_operator_location(self, lat, lon, label=""):
        self._operator_lat = lat
        self._operator_lon = lon
        self._operator_label = label
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.fillRect(self.rect(), QColor(45, 45, 48))
        w, h = self.width(), self.height()

        # Zoom/pan transform -- scales/translates everything drawn below
        # this point (map tiles, terminator, every marker), restored
        # before the fixed-position attribution text
        # so that stays put regardless of zoom. See __init__'s comment
        # on _pan_x/_pan_y's units for why this specific translate/
        # scale/translate order is what makes them "offset from center
        # in unzoomed content pixels".
        painter.save()
        painter.translate(w / 2.0, h / 2.0)
        painter.scale(self._zoom, self._zoom)
        painter.translate(-(w / 2.0 + self._pan_x), -(h / 2.0 + self._pan_y))

        self._draw_map_tiles(painter, w, h)

        now = datetime.datetime.now(datetime.timezone.utc)

        self._draw_terminator(painter, w, h, now)

        # Geographic overlays that make sense to zoom WITH the map --
        # coverage circles and ground tracks are real areas/paths over
        # the earth, same as the terminator above.
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

        if self._aprs_markers:
            # Bright red per explicit instruction -- distinguishable from
            # the operator-location marker (also red) by its smaller
            # radius and dark, not white, border, same pen/brush pairing
            # every other spot-category marker above uses.
            painter.setPen(QPen(QColor(15, 15, 20), 1))
            painter.setBrush(QColor(230, 30, 30))
            for spot in self._aprs_markers:
                cx, cy = self._lonlat_to_xy(spot["lat"], spot["lon"], w, h)
                sx, sy = self._content_to_screen(cx, cy, w, h)
                painter.drawEllipse(QPointF(sx, sy), 3.5, 3.5)

        # Attribution -- required by the OSM Tile Usage Policy, must stay
        # visible unconditionally (not gated on a successful fetch, since
        # tiles are always the background now, not an optional extra).
        # Fixed corner label regardless of zoom/pan.
        painter.setPen(QColor(200, 200, 200, 180))
        painter.drawText(QPointF(6, h - 6), map_tiles.MAP_ATTRIBUTION)

    def _draw_map_tiles(self, painter, w, h):
        """Draws every OSM tile overlapping the currently-visible content-
        space rect at the best-available zoom, in CONTENT space (i.e.
        BEFORE the self._zoom painter.scale() transform already active
        around this call magnifies it) -- so a tile's size here is its
        real resolution divided by however far self._zoom has already
        gone, and the outer transform does the rest, exactly like every
        other geographic overlay drawn in this same transform (terminator
        shading, satellite footprints/paths).

        effective_zoom combines how much detail the CURRENT widget size
        already implies (log2(w / TILE_SIZE_PX) -- the zoom level whose
        native tile grid matches content space's own w-pixels-per-360-
        longitude scale) with how far the operator has scaled in beyond
        that (log2(self._zoom)) -- fetching progressively higher-
        resolution tiles as either grows, rather than just letting
        QPainter blur-magnify a fixed-resolution tile set (the exact
        problem this whole feature replaces)."""
        effective_zoom = math.log2(max(w, 1) * self._zoom / map_tiles.TILE_SIZE_PX)
        draw_zoom = int(round(max(map_tiles.MIN_TILE_ZOOM, min(map_tiles.MAX_TILE_ZOOM, effective_zoom))))
        tiles_per_side = 2 ** draw_zoom
        tile_w = w / tiles_per_side

        # Visible content-space rect, inverting the zoom/pan transform via
        # the same helper hit-testing uses -- keeps tile requests/draws
        # limited to what's actually on screen (plus a 1-tile margin for
        # smooth panning) instead of the whole world.
        top_left, _, _ = self._widget_pos_to_content(QPointF(0, 0))
        bottom_right, _, _ = self._widget_pos_to_content(QPointF(self.width(), self.height()))

        x0 = max(0, int(top_left.x() / tile_w) - 1)
        x1 = min(tiles_per_side - 1, int(bottom_right.x() / tile_w) + 1)
        lat_top = map_tiles.content_y_to_lat(top_left.y(), h)
        lat_bottom = map_tiles.content_y_to_lat(bottom_right.y(), h)
        y0 = max(0, map_tiles.lat_to_tile_y(lat_top, draw_zoom) - 1)
        y1 = min(tiles_per_side - 1, map_tiles.lat_to_tile_y(lat_bottom, draw_zoom) + 1)
        if x1 < x0 or y1 < y0:
            return

        painter.fillRect(QRectF(0, 0, w, h), QColor(30, 34, 40))
        for ty in range(y0, y1 + 1):
            lat_top_row = map_tiles.tile_y_to_lat(ty, draw_zoom)
            lat_bottom_row = map_tiles.tile_y_to_lat(ty + 1, draw_zoom)
            y_top = self._lonlat_to_xy(lat_top_row, 0, w, h)[1]
            y_bottom = self._lonlat_to_xy(lat_bottom_row, 0, w, h)[1]
            for tx in range(x0, x1 + 1):
                rect = QRectF(tx * tile_w, y_top, tile_w, y_bottom - y_top)
                pixmap = self._tile_cache.get(draw_zoom, tx, ty)
                if pixmap is not None:
                    painter.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))
                else:
                    self._tile_fetcher.request(draw_zoom, tx, ty)

    # Samples the terminator curve every 2 degrees of longitude -- smooth
    # enough to look like a real curve (not a jagged staircase) at any
    # sane widget size, cheap enough that redrawing once a minute (see
    # HamClockWindow's map_timer) is a non-issue, same "cost is a non-
    # issue at this resolution" reasoning the old per-cell shading used.
    _TERMINATOR_LON_STEP = 2.0

    def _draw_terminator(self, painter, w, h, now):
        """Night-hemisphere shading via the terminator's actual closed-
        form curve (a smooth line, not the old blocky per-cell grid
        sampling) -- the day/night boundary is exactly the locus of
        points where solar elevation is 0, which for a FIXED longitude
        has exactly one latitude solution (solving solar_data.py's own
        sin(elev) = sin(lat)sin(decl) + cos(lat)cos(decl)cos(hour_angle)
        for lat when elev=0 gives tan(lat) = -cos(decl)cos(hour_angle) /
        sin(decl) -- verified numerically against _solar_elevation
        directly). Draws the curve itself as a grey line, and fills
        whichever side is night with a translucent overlay -- the pole
        tilted away from the sun (north when declination is negative,
        south otherwise, since elevation AT a pole simplifies to exactly
        the declination) is always entirely night, so the fill polygon
        is the curve closed off along that pole's own map edge."""
        decl_deg, sub_lon = _solar_subpoint(now)
        decl = math.radians(decl_deg)
        sin_decl = math.sin(decl)
        cos_decl = math.cos(decl)

        points = []
        lon = -180.0
        while lon <= 180.0 + 1e-9:
            hour_angle = math.radians(lon - sub_lon)
            if abs(sin_decl) < 1e-6:
                # Equinox edge case (a couple of days a year) -- the
                # terminator is a near-vertical meridian pair rather than
                # a function of longitude; hugging the nearer pole here
                # is a reasonable degenerate rendering, not a crash.
                term_lat = 90.0 if math.cos(hour_angle) < 0 else -90.0
            else:
                term_lat = math.degrees(math.atan(-cos_decl * math.cos(hour_angle) / sin_decl))
            points.append(self._lonlat_to_xy(term_lat, lon, w, h))
            lon += self._TERMINATOR_LON_STEP

        curve_path = QPainterPath()
        curve_path.moveTo(*points[0])
        for x, y in points[1:]:
            curve_path.lineTo(x, y)

        fill_path = QPainterPath(curve_path)
        if decl_deg < 0:
            # North pole tilted away from the sun -- it's the night side.
            fill_path.lineTo(w, 0)
            fill_path.lineTo(0, 0)
        else:
            fill_path.lineTo(w, h)
            fill_path.lineTo(0, h)
        fill_path.closeSubpath()

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 140))
        painter.drawPath(fill_path)

        painter.setPen(QPen(QColor(160, 160, 160, 210), 1.5))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(curve_path)

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

    def set_aprs_markers(self, markers):
        """markers: list of {"lat", "lon", "tooltip"} -- ham_dashboard.py's
        APRS map button, one entry per station (keyed/deduplicated by
        callsign on the caller's side -- this widget just draws whatever
        list it's given), built directly from decoded APRS position
        report packets (no grid-square conversion needed, packets carry
        real lat/lon already). Same pre-built-tooltip shape as
        set_pskreporter_markers/set_pota_markers. Empty list (the
        button's own OFF state) just stops drawing/hit-testing them."""
        self._aprs_markers = markers
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
        w, h = self.width(), self.height()
        cx = (widget_pos.x() - w / 2.0) / self._zoom + w / 2.0 + self._pan_x
        cy = (widget_pos.y() - h / 2.0) / self._zoom + h / 2.0 + self._pan_y
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

    # Same screen-pixel-radius reasoning as _QSO_HIT_RADIUS_PX.
    _APRS_HIT_RADIUS_PX = 8

    def _aprs_marker_at(self, widget_pos):
        """Same shared hit-test shape as _qso_marker_at, for the APRS
        station markers."""
        if not self._aprs_markers:
            return None
        pos, w, h = self._widget_pos_to_content(widget_pos)
        hit_radius = self._APRS_HIT_RADIUS_PX / self._zoom
        closest, closest_dist = None, None
        for spot in self._aprs_markers:
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
        (blank space and/or the map tiles sliding fully out of view)."""
        w, h = self.width(), self.height()
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
        self._zoom = new_zoom
        self._pan_x = content_pos.x() - w / 2.0 - (widget_pos.x() - w / 2.0) / new_zoom
        self._pan_y = content_pos.y() - h / 2.0 - (widget_pos.y() - h / 2.0) / new_zoom
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
            return
        aprs_spot = self._aprs_marker_at(event.position())
        if aprs_spot is not None:
            QToolTip.showText(event.globalPosition().toPoint(), aprs_spot.get("tooltip", "?"), self)
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
                and self._aprs_marker_at(event.position()) is None
            ):
                # Right-clicking genuinely empty map (no satellite, no
                # QSO/PSKReporter/POTA/APRS marker under the cursor)
                # resets the view -- the only reset affordance this
                # needs, since scrolling back out to MIN_ZOOM is
                # otherwise the only way back once zoomed/panned in.
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
        aprs_spot = self._aprs_marker_at(event.position())
        if aprs_spot is not None:
            QToolTip.showText(event.globalPosition().toPoint(), aprs_spot.get("tooltip", "?"), self)
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
        """Web Mercator, NOT naive equirectangular linear latitude --
        x stays a plain lon-to-w linear scale (longitude IS linear in
        Mercator too), but y now uses Mercator's lat-to-fraction formula,
        scaled to h. Deliberately NOT true square/conformal Mercator (x
        scaled by w, y independently by h, same as the old equirect
        code's own per-axis-independent structure) -- a real textbook
        Mercator projection is square (the same w would drive both axes),
        which would visibly stretch/squash content whenever the widget
        itself isn't square. This is a pragmatic simplification (a
        non-square widget mildly deviates from true-conformal as a
        result) that keeps every zoom/pan/hit-test method in this class
        completely projection-agnostic -- see map_tiles.py's own
        docstring for the fuller reasoning."""
        lat = max(-map_tiles.MERCATOR_LAT_LIMIT, min(map_tiles.MERCATOR_LAT_LIMIT, lat))
        x = (lon + 180.0) / 360.0 * w
        lat_rad = math.radians(lat)
        y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * h
        return x, y

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
