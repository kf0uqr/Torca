"""
Raster tile fetching/caching for world_map.py's background map --
standard slippy-map z/x/y tiles, free forever, no API key/signup. Two
layers are supported, selected by world_map.py's own layer toggle
button, each with its own URL template/cache directory/attribution but
sharing the exact same z/x/y addressing and fetch/cache machinery below
(both are plumbed through everywhere as a "layer" string, "osm" or
"satellite"):

- "osm": OpenStreetMap (https://tile.openstreetmap.org), per the OSM
  Tile Usage Policy (https://operations.osmfoundation.org/policies/tiles/):
  HTTPS-only, a distinctive User-Agent, local disk caching explicitly
  encouraged, "© OpenStreetMap contributors" attribution required and
  must stay visible, only viewport-driven fetching (no pre-seeding/
  bulk/offline-archive downloading -- TileFetcher only ever fetches
  tiles world_map.py's own paintEvent actually asked for, nothing
  more).
- "satellite": Esri World Imagery (server.arcgisonline.com), the same
  free, key-less, standard-zxy tile service used by countless other
  open-source mapping tools (Leaflet-providers' "Esri.WorldImagery",
  JOSM, QGIS's default satellite basemap, etc.) under Esri's terms for
  use in third-party applications, provided attribution is shown --
  handled the same way as OSM's own attribution requirement above.

Pure projection helpers here mirror world_map.py's own WorldMapWidget.
_lonlat_to_xy (Web Mercator lat-to-y fraction, scaled to the widget's own
h rather than true square/conformal Mercator -- see that method's own
docstring for why) -- only used here for the reverse direction (a
content-space y back to a latitude, then to a tile row), since every
other overlay in world_map.py projects forward via _lonlat_to_xy directly
and never needs to go through this module at all.

Networking follows this app's one and only established idiom (confirmed
by reading every other network-fetching class in this codebase -- pota.py,
pskreporter.py, qrz_logbook.py, world_map.py's own former
WorldMapImageFetcher): a QThread subclass doing a plain blocking
urllib.request.urlopen() call. No `requests`, no QNetworkAccessManager
anywhere in this app -- TileFetcher below is the same idiom, just wrapped
in a persistent (not one-shot) worker pair pulling from a shared queue,
since panning/zooming needs many small fetches over a session's lifetime,
not one.
"""

import math
import pathlib
import queue
import threading
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QPixmap

TILE_SIZE_PX = 256
MIN_TILE_ZOOM = 0
# 19 is standard tile.openstreetmap.org's own actual max zoom (building/
# street level) -- per explicit instruction, raised from an initial,
# more conservative 15 (city-block level). Esri World Imagery's own
# max LOD is also 19 in well-covered areas (it transparently serves
# upsampled lower-resolution imagery elsewhere, never a blank/missing
# tile), so the same cap is reused for both layers rather than adding
# a second one.
MAX_TILE_ZOOM = 19

DEFAULT_LAYER = "osm"

_TILE_URL_TEMPLATES = {
    "osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    # Note the {y}/{x} order (not {x}/{y}) -- ArcGIS REST tile
    # endpoints address tiles row-before-column, the reverse of OSM's
    # own convention; confirmed against Leaflet-providers' own
    # "Esri.WorldImagery" entry, the same URL used there.
    "satellite": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
}
_TILE_CACHE_DIRS = {
    "osm": pathlib.Path.home() / ".icom_radio_app_cache" / "osm_tiles",
    "satellite": pathlib.Path.home() / ".icom_radio_app_cache" / "satellite_tiles",
}
_MAP_ATTRIBUTIONS = {
    "osm": "Map data: © OpenStreetMap contributors",
    "satellite": "Imagery: © Esri, Maxar, Earthstar Geographics, and the GIS User Community",
}

# Plain top-level names for the default ("osm") layer's own values --
# kept since most of this module (and test_map_tiles.py) only ever
# deals with the one map, never a specific tile source.
TILE_URL_TEMPLATE = _TILE_URL_TEMPLATES["osm"]
TILE_CACHE_DIR = _TILE_CACHE_DIRS["osm"]
MAP_ATTRIBUTION = _MAP_ATTRIBUTIONS["osm"]


def attribution_for(layer):
    return _MAP_ATTRIBUTIONS.get(layer, MAP_ATTRIBUTION)

# Standard Web Mercator cutoff -- the projection's y value diverges to
# infinity at the true poles, so every slippy-map implementation clamps
# to this latitude instead (the point where a full 2**zoom tile pyramid's
# square content exactly reaches top/bottom).
MERCATOR_LAT_LIMIT = 85.05112878

# Two persistent fetch threads, not more -- a conservative concurrency
# matching common slippy-map client conventions, in the spirit of OSM's
# "reasonable use" policy even though no exact number is mandated there.
_FETCH_THREAD_COUNT = 2


# QPixmap's file loader picks a decoder primarily off the file
# extension, so each layer's cached tiles need the extension matching
# what that service actually serves -- OSM tiles are PNG, but the
# World_Imagery MapServer's own default tile format is JPEG (confirmed
# via its REST service description); saving satellite tiles as ".png"
# would silently fail to decode despite a byte-for-byte successful
# download.
_TILE_FILE_EXTENSIONS = {"osm": "png", "satellite": "jpg"}


def _tile_cache_path(layer, z, x, y):
    cache_dir = _TILE_CACHE_DIRS.get(layer, TILE_CACHE_DIR)
    ext = _TILE_FILE_EXTENSIONS.get(layer, "png")
    return cache_dir / str(z) / str(x) / f"{y}.{ext}"


def content_y_to_lat(y, h):
    """Inverse of WorldMapWidget._lonlat_to_xy's y formula (Web Mercator,
    scaled to h) -- given a content-space y coordinate, returns the
    latitude it corresponds to. Only used by _draw_map_tiles to figure out
    which tile rows are actually visible; every other place in world_map.py
    projects forward (lat/lon -> xy) directly and never needs this."""
    frac = 1.0 - 2.0 * (y / h)
    return math.degrees(math.atan(math.sinh(frac * math.pi)))


def lat_to_tile_y(lat, zoom):
    """Standard slippy-map lat -> tile row formula."""
    lat = max(-MERCATOR_LAT_LIMIT, min(MERCATOR_LAT_LIMIT, lat))
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    return int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)


def tile_y_to_lat(ty, zoom):
    """Standard slippy-map tile row -> lat formula (the top latitude of
    that row; the row's bottom is tile_y_to_lat(ty + 1, zoom))."""
    n = 2 ** zoom
    frac = 1.0 - 2.0 * ty / n
    return math.degrees(math.atan(math.sinh(frac * math.pi)))


class TileCache:
    """In-memory LRU of decoded QPixmap tiles, backed by an on-disk cache
    (per-layer directory, see _TILE_CACHE_DIRS) that persists across
    runs. Bounded in-memory size -- a full session of panning/zooming
    could otherwise accumulate an unbounded number of decoded pixmaps;
    eviction just means the next get() for that tile re-decodes from
    the (still-present) disk file, cheap, not a re-fetch. Shared by
    both layers -- keys carry the layer so switching layers can never
    collide two different images onto the same (z, x, y)."""

    _MAX_CACHED_PIXMAPS = 400

    def __init__(self):
        self._pixmaps = {}  # (layer, z, x, y) -> QPixmap, insertion-ordered (see _touch)

    def get(self, layer, z, x, y):
        key = (layer, z, x, y)
        pixmap = self._pixmaps.get(key)
        if pixmap is not None:
            self._touch(key)
            return pixmap
        path = _tile_cache_path(layer, z, x, y)
        if not path.exists():
            return None
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return None
        self._pixmaps[key] = pixmap
        self._touch(key)
        self._evict_if_needed()
        return pixmap

    def _touch(self, key):
        # Re-insert to move it to the end -- dict preserves insertion
        # order, so the front is always the least-recently-used entry.
        pixmap = self._pixmaps.pop(key)
        self._pixmaps[key] = pixmap

    def _evict_if_needed(self):
        while len(self._pixmaps) > self._MAX_CACHED_PIXMAPS:
            oldest_key = next(iter(self._pixmaps))
            del self._pixmaps[oldest_key]

    def has_disk_copy(self, layer, z, x, y):
        return _tile_cache_path(layer, z, x, y).exists()


class _TileFetchThread(QThread):
    """One persistent worker pulling (z, x, y) requests off a shared
    queue.Queue and fetching each with a plain blocking urllib call --
    same networking idiom as every other fetch in this app, just looped
    instead of one-shot. No reliable in-flight HTTP cancellation exists
    anywhere in this codebase's established idiom (confirmed against
    world_map.py's own former WorldMapImageFetcher, which documents the
    same limitation) -- stop() only takes effect between requests, via
    the queue.get() timeout below."""

    tile_ready = Signal(str, int, int, int, str)  # layer, z, x, y, local file path

    def __init__(self, work_queue, parent=None):
        super().__init__(parent)
        self._queue = work_queue
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def run(self):
        while not self._stop_requested:
            try:
                layer, z, x, y = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._fetch_one(layer, z, x, y)
            except Exception as exc:
                print(f"[ERROR] Ham Dashboard: map tile fetch failed ({layer} {z}/{x}/{y}): {exc}")

    def _fetch_one(self, layer, z, x, y):
        path = _tile_cache_path(layer, z, x, y)
        if not path.exists():
            url_template = _TILE_URL_TEMPLATES.get(layer, TILE_URL_TEMPLATE)
            url = url_template.format(z=z, x=x, y=y)
            # Same User-Agent-policy reasoning as world_map.py's former
            # WorldMapImageFetcher (meta.wikimedia.org's policy doc) --
            # OSM's own Tile Usage Policy makes the identical requirement:
            # a clear, distinctive User-Agent naming the app. Applied to
            # both layers, not just OSM -- good practice regardless of
            # whether Esri's own terms mandate it.
            request = urllib.request.Request(
                url, headers={"User-Agent": "IcomRadioControlApp/1.0 (desktop ham radio control application)"},
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.tile_ready.emit(layer, z, x, y, str(path))


class TileFetcher(QObject):
    """Owns _FETCH_THREAD_COUNT persistent worker threads sharing one
    request queue. request(layer, z, x, y) is safe to call repeatedly
    for the same tile (e.g. every paintEvent while it's still loading)
    -- already cached-on-disk tiles short-circuit straight to
    tile_ready with no network touch at all (mirrors WorldMapImageFetcher.
    run()'s own cache-hit shape), and already-queued/in-flight tiles are
    silently ignored rather than re-queued. One shared queue/pending-set
    for both layers -- worker threads just fetch whatever's in the
    request tuple, layer included, so no per-layer fetcher instance is
    needed."""

    tile_ready = Signal(str, int, int, int, str)  # layer, z, x, y, local file path

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = queue.Queue()
        self._pending_lock = threading.Lock()
        self._pending = set()  # (layer, z, x, y) currently queued or being fetched
        self._workers = []
        for _ in range(_FETCH_THREAD_COUNT):
            worker = _TileFetchThread(self._queue, self)
            worker.tile_ready.connect(self._on_worker_tile_ready)
            worker.start()
            self._workers.append(worker)

    def request(self, layer, z, x, y):
        key = (layer, z, x, y)
        path = _tile_cache_path(layer, z, x, y)
        if path.exists():
            self.tile_ready.emit(layer, z, x, y, str(path))
            return
        with self._pending_lock:
            if key in self._pending:
                return
            self._pending.add(key)
        self._queue.put(key)

    def _on_worker_tile_ready(self, layer, z, x, y, path):
        with self._pending_lock:
            self._pending.discard((layer, z, x, y))
        self.tile_ready.emit(layer, z, x, y, path)

    def shutdown(self, timeout_ms=3000):
        """Call from the owning window's closeEvent before it returns --
        without this, a persistent worker thread destroyed while still
        running aborts the process (the exact "QThread destroyed while
        still running" crash this app already fixed for every other
        background worker; a persistent tile fetcher needs the same
        discipline, not just the one-shot fetchers that already have it)."""
        for worker in self._workers:
            worker.stop()
        for worker in self._workers:
            worker.wait(timeout_ms)
