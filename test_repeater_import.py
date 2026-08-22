"""Tests for repeater_import.py -- primarily a synthetic RepeaterBook-
style export (that provider's exact downloadable-CSV column names
weren't independently verified against a real downloaded file, so this
covers the alias-matching logic rather than claiming byte-exact
provider fidelity), plus a real-data regression case (test_fixtures/
icom_repeater_sample.csv, two rows taken verbatim from Icom's own
official D-STAR repeater list download, not synthesized) confirming
the alias-based column matching also tolerates a differently-shaped
CSV without being the design target."""

import pathlib
import tempfile

import repeater_import

_FIXTURES_DIR = pathlib.Path(__file__).parent / "test_fixtures"


def test_icom_dstar_csv_real_fixture():
    repeaters = repeater_import.parse_repeater_csv(_FIXTURES_DIR / "icom_repeater_sample.csv")
    assert len(repeaters) == 2, repeaters

    claremore = next(r for r in repeaters if "AE5ME" in r["callsign"])
    assert claremore["output_freq_hz"] == 444_350_000
    assert claremore["input_freq_hz"] == 449_350_000  # 444.35 + 5.0 (DUP+)
    assert claremore["band"] == "70cm"
    assert claremore["mode"] == "DV"
    assert claremore["lat"] == 36.32
    assert claremore["lon"] == -95.63

    altus = next(r for r in repeaters if "AJ5Q" in r["callsign"])
    assert altus["output_freq_hz"] == 442_225_000
    assert altus["input_freq_hz"] == 447_225_000  # 442.225 + 5.0 (DUP+)
    assert altus["band"] == "70cm"


def test_repeaterbook_style_dup_minus_offset():
    # RepeaterBook-style: signed "Input Frequency" given directly
    # (no separate Dup/Offset split) -- confirmed against that
    # provider's own documented Export API field list (Frequency,
    # Input Frequency) even though the exact downloadable-CSV header
    # spelling wasn't independently verified.
    csv_text = (
        "Callsign,Frequency,Input Frequency,PL/CTCSS Uplink,City,Operating Mode,Latitude,Longitude\n"
        "W1ABC,146.940000,146.340000,100.0,Anytown,FM,42.0,-71.0\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv_text)
        path = f.name
    try:
        repeaters = repeater_import.parse_repeater_csv(path)
        assert len(repeaters) == 1
        r = repeaters[0]
        assert r["callsign"] == "W1ABC"
        assert r["output_freq_hz"] == 146_940_000
        assert r["input_freq_hz"] == 146_340_000
        assert r["ctcss_hz"] == 100.0
        assert r["city"] == "Anytown"
        assert r["band"] == "2m"
    finally:
        pathlib.Path(path).unlink()


def test_dup_minus_sign_subtracts_offset():
    csv_text = (
        "Callsign,Frequency,Dup,Offset,Latitude,Longitude\n"
        "K1XYZ,145.150000,DUP-,0.600000,40.0,-75.0\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv_text)
        path = f.name
    try:
        repeaters = repeater_import.parse_repeater_csv(path)
        assert len(repeaters) == 1
        assert repeaters[0]["input_freq_hz"] == 144_550_000  # 145.15 - 0.6
    finally:
        pathlib.Path(path).unlink()


def test_non_2m_70cm_frequency_excluded():
    csv_text = (
        "Callsign,Frequency,Latitude,Longitude\n"
        "N0HAM,927.000000,39.0,-95.0\n"  # 33cm -- out of scope
        "N0HAM2,29.600000,39.0,-95.0\n"  # 10m -- out of scope
    )
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv_text)
        path = f.name
    try:
        assert repeater_import.parse_repeater_csv(path) == []
    finally:
        pathlib.Path(path).unlink()


def test_no_frequency_column_returns_empty_not_error():
    csv_text = "Callsign,City\nW1ABC,Anytown\n"
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv_text)
        path = f.name
    try:
        assert repeater_import.parse_repeater_csv(path) == []
    finally:
        pathlib.Path(path).unlink()


def test_filter_by_distance_keeps_near_drops_far():
    repeaters = repeater_import.parse_repeater_csv(_FIXTURES_DIR / "icom_repeater_sample.csv")
    # Claremore, OK (36.32, -95.63) itself -- radius 10km should keep
    # only Claremore, not Altus (~400km away).
    nearby = repeater_import.filter_by_distance(repeaters, 36.32, -95.63, radius_km=10)
    assert len(nearby) == 1
    assert "AE5ME" in nearby[0]["callsign"]
    # "lat"/"lon" must be stripped -- memories_window.py's _repeater_to_entry doesn't expect them.
    assert "lat" not in nearby[0] and "lon" not in nearby[0]


def test_filter_by_distance_drops_entries_with_no_coordinates():
    repeaters = [{"callsign": "NOCOORD", "output_freq_hz": 146_000_000, "input_freq_hz": 146_000_000,
                  "mode": "FM", "band": "2m", "city": "", "ctcss_hz": None, "lat": None, "lon": None}]
    assert repeater_import.filter_by_distance(repeaters, 36.32, -95.63, radius_km=1000) == []


if __name__ == "__main__":
    tests = [
        test_icom_dstar_csv_real_fixture,
        test_repeaterbook_style_dup_minus_offset,
        test_dup_minus_sign_subtracts_offset,
        test_non_2m_70cm_frequency_excluded,
        test_no_frequency_column_returns_empty_not_error,
        test_filter_by_distance_keeps_near_drops_far,
        test_filter_by_distance_drops_entries_with_no_coordinates,
    ]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    print("All repeater_import tests passed.")
