"""Pure-logic tests for map_tiles.py's projection/cache-path helpers --
no Qt, no network. See world_map_smoke_test.py (manual, run separately)
for the QWidget/live-fetch integration checks."""

import math

from map_tiles import (
    content_y_to_lat, lat_to_tile_y, tile_y_to_lat, _tile_cache_path,
    MERCATOR_LAT_LIMIT, TILE_CACHE_DIR,
)


def test_content_y_to_lat_center_is_equator():
    h = 1000.0
    lat = content_y_to_lat(h / 2.0, h)
    assert abs(lat) < 1e-9, f"expected ~0, got {lat}"


def test_content_y_to_lat_top_and_bottom_near_mercator_limit():
    h = 1000.0
    lat_top = content_y_to_lat(0.0, h)
    lat_bottom = content_y_to_lat(h, h)
    assert lat_top > 89.9 or lat_top == math.inf or lat_top > 85.0, f"top lat too low: {lat_top}"
    assert lat_bottom < -85.0, f"bottom lat too high: {lat_bottom}"


def test_lat_to_tile_y_equator_is_center_row():
    for zoom in (0, 3, 8):
        n = 2 ** zoom
        ty = lat_to_tile_y(0.0, zoom)
        assert ty == n // 2, f"zoom {zoom}: expected {n // 2}, got {ty}"


def test_lat_to_tile_y_clamps_past_mercator_limit():
    # A latitude past the Mercator cutoff must not raise (math domain
    # error from tan/cos going singular at true +/-90) -- must clamp.
    ty_north = lat_to_tile_y(89.9, 5)
    ty_north_clamped = lat_to_tile_y(MERCATOR_LAT_LIMIT, 5)
    assert ty_north == ty_north_clamped
    ty_south = lat_to_tile_y(-89.9, 5)
    assert ty_south == lat_to_tile_y(-MERCATOR_LAT_LIMIT, 5)


def test_tile_y_to_lat_round_trips_lat_to_tile_y():
    zoom = 6
    for lat in (-80.0, -30.0, 0.0, 10.0, 60.0, 84.0):
        ty = lat_to_tile_y(lat, zoom)
        # tile_y_to_lat(ty) is that row's TOP edge -- the original lat
        # should fall between this row's top and the next row's top
        # (rows get taller near the poles under Mercator, i.e. top >
        # next-top always, both decreasing as ty increases).
        row_top = tile_y_to_lat(ty, zoom)
        row_bottom = tile_y_to_lat(ty + 1, zoom)
        assert row_bottom <= lat <= row_top, f"lat {lat} not within row {ty}'s bounds ({row_bottom}, {row_top})"


def test_tile_y_to_lat_zero_row_is_near_top():
    # Row 0 at any zoom starts at the Mercator limit (the whole tile
    # pyramid's own top edge).
    for zoom in (0, 4, 10):
        assert abs(tile_y_to_lat(0, zoom) - MERCATOR_LAT_LIMIT) < 1e-6


def test_tile_cache_path_scheme():
    path = _tile_cache_path(7, 3, 5)
    assert path == TILE_CACHE_DIR / "7" / "3" / "5.png"


def test_tile_cache_path_unique_per_coordinate():
    paths = {_tile_cache_path(z, x, y) for z, x, y in [(1, 0, 0), (1, 1, 0), (1, 0, 1), (2, 0, 0)]}
    assert len(paths) == 4


if __name__ == "__main__":
    tests = [
        test_content_y_to_lat_center_is_equator,
        test_content_y_to_lat_top_and_bottom_near_mercator_limit,
        test_lat_to_tile_y_equator_is_center_row,
        test_lat_to_tile_y_clamps_past_mercator_limit,
        test_tile_y_to_lat_round_trips_lat_to_tile_y,
        test_tile_y_to_lat_zero_row_is_near_top,
        test_tile_cache_path_scheme,
        test_tile_cache_path_unique_per_coordinate,
    ]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    print("All map_tiles tests passed.")
