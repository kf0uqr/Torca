"""Pure-logic tests for pota.py's park-directory helpers -- no network,
no Qt. Live-fetch behavior (fetch_pota_programs/fetch_program_parks/
fetch_park_details) is verified manually against the real API, not
here (mirrors map_tiles.py's own test file, which keeps its automated
tests offline for the same reason)."""

import json

import pota


def test_haversine_known_distance_berlin_paris():
    # Real-world distance is ~878km -- allow a few km of slack for the
    # haversine formula's own spherical-Earth approximation.
    d = pota.haversine_km(52.5200, 13.4050, 48.8566, 2.3522)
    assert 850 < d < 900, f"expected ~878km, got {d}"


def test_haversine_same_point_is_zero():
    d = pota.haversine_km(40.0, -105.0, 40.0, -105.0)
    assert abs(d) < 1e-9


def test_haversine_antipodal_is_half_earth_circumference():
    d = pota.haversine_km(0.0, 0.0, 0.0, 180.0)
    expected = math_pi_earth_half_circumference()
    assert abs(d - expected) < 1.0


def math_pi_earth_half_circumference():
    import math
    return math.pi * pota._EARTH_RADIUS_KM


def test_parks_cache_path_scheme():
    path = pota._parks_cache_path("US")
    assert path == pota._PARKS_CACHE_DIR / "US.json"


def test_parks_cache_round_trip():
    test_prefix = "TESTPROGRAM"
    parks = [{"reference": "TEST-0001", "name": "Test Park", "lat": 1.0, "lon": 2.0}]
    try:
        pota.save_program_parks_cache(test_prefix, parks)
        loaded = pota.load_cached_program_parks(test_prefix)
        assert loaded == parks
    finally:
        path = pota._parks_cache_path(test_prefix)
        if path.exists():
            path.unlink()


def test_parks_cache_stale_returns_none():
    test_prefix = "TESTPROGRAM_STALE"
    path = pota._parks_cache_path(test_prefix)
    try:
        pota._PARKS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([{"reference": "X"}]))
        import os
        import time
        old_time = time.time() - pota._PARKS_CACHE_MAX_AGE_S - 3600
        os.utime(path, (old_time, old_time))
        assert pota.load_cached_program_parks(test_prefix) is None
    finally:
        if path.exists():
            path.unlink()


def test_parks_cache_missing_returns_none():
    assert pota.load_cached_program_parks("NO_SUCH_PROGRAM_EVER") is None


if __name__ == "__main__":
    tests = [
        test_haversine_known_distance_berlin_paris,
        test_haversine_same_point_is_zero,
        test_haversine_antipodal_is_half_earth_circumference,
        test_parks_cache_path_scheme,
        test_parks_cache_round_trip,
        test_parks_cache_stale_returns_none,
        test_parks_cache_missing_returns_none,
    ]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    print("All pota tests passed.")
