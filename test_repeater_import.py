"""Tests for repeater_import.py.

test_real_repeaterbook_header_row_confirmed_by_operator is the load-
bearing one: it uses the EXACT header row a real operator pasted from
their own downloaded RepeaterBook CSV ("Output Freq, Input Freq,
Offset, Uplink Tone, Downlink Tone, Call, Location, County, State,
Modes, Digital Access") -- the earlier version of this module was
tuned to RepeaterBook's Export API documentation instead (e.g. "PL/
CTCSS Uplink"), which never matched this real file's actual "Uplink
Tone" column at all, so tone silently never imported. This test is
the regression guard for that specific bug.

Other tests cover the alias-matching logic more generally, plus a
real-data case (test_fixtures/icom_repeater_sample.csv, two rows taken
verbatim from Icom's own official D-STAR repeater list download, not
synthesized) confirming the alias-based matching also tolerates a
differently-shaped CSV without being the design target.

Per explicit instruction, parse_repeater_csv imports the ENTIRE file
unfiltered -- no band restriction, no location filtering."""

import pathlib
import tempfile

import repeater_import

_FIXTURES_DIR = pathlib.Path(__file__).parent / "test_fixtures"


def _write_csv(text):
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(text)
        return f.name


def test_real_repeaterbook_header_row_confirmed_by_operator():
    csv_text = (
        "Output Freq,Input Freq,Offset,Uplink Tone,Downlink Tone,Call,Location,County,State,Modes,Digital Access\n"
        "146.940000,146.340000,-0.600000,100.0,100.0,W1ABC,Anytown,Some County,MA,\"FM, DMR\",\n"
    )
    path = _write_csv(csv_text)
    try:
        repeaters = repeater_import.parse_repeater_csv(path)
        assert len(repeaters) == 1
        r = repeaters[0]
        assert r["callsign"] == "W1ABC"
        assert r["output_freq_hz"] == 146_940_000
        assert r["input_freq_hz"] == 146_340_000
        assert r["ctcss_hz"] == 100.0, r  # the actual bug: this used to be None every time
        assert r["location"] == "Anytown, MA"
        assert r["modes"] == "FM, DMR"
    finally:
        pathlib.Path(path).unlink()


def test_location_falls_back_to_whichever_of_city_state_is_present():
    path = _write_csv("Call,Output Freq,Location\nW1ABC,146.940000,Anytown\n")
    try:
        assert repeater_import.parse_repeater_csv(path)[0]["location"] == "Anytown"
    finally:
        pathlib.Path(path).unlink()
    path = _write_csv("Call,Output Freq,State\nW1ABC,146.940000,MA\n")
    try:
        assert repeater_import.parse_repeater_csv(path)[0]["location"] == "MA"
    finally:
        pathlib.Path(path).unlink()
    path = _write_csv("Call,Output Freq\nW1ABC,146.940000\n")
    try:
        assert repeater_import.parse_repeater_csv(path)[0]["location"] == ""
    finally:
        pathlib.Path(path).unlink()


def test_icom_dstar_csv_real_fixture():
    repeaters = repeater_import.parse_repeater_csv(_FIXTURES_DIR / "icom_repeater_sample.csv")
    assert len(repeaters) == 2, repeaters

    claremore = next(r for r in repeaters if "AE5ME" in r["callsign"])
    assert claremore["output_freq_hz"] == 444_350_000
    assert claremore["input_freq_hz"] == 449_350_000  # 444.35 + 5.0 (DUP+)
    assert claremore["mode"] == "DV"
    assert claremore["location"] == "Claremore, Oklahoma"  # Name + Sub Name

    altus = next(r for r in repeaters if "AJ5Q" in r["callsign"])
    assert altus["output_freq_hz"] == 442_225_000
    assert altus["input_freq_hz"] == 447_225_000  # 442.225 + 5.0 (DUP+)


def test_dup_minus_sign_subtracts_offset():
    path = _write_csv("Callsign,Frequency,Dup,Offset\nK1XYZ,145.150000,DUP-,0.600000\n")
    try:
        repeaters = repeater_import.parse_repeater_csv(path)
        assert len(repeaters) == 1
        assert repeaters[0]["input_freq_hz"] == 144_550_000  # 145.15 - 0.6
    finally:
        pathlib.Path(path).unlink()


def test_all_bands_included_unfiltered():
    # Per explicit instruction: import the entire CSV, no band
    # restriction -- 33cm and 10m rows (previously excluded) must now
    # come through just like 2m/70cm.
    csv_text = (
        "Callsign,Frequency\n"
        "N0HAM,927.000000\n"   # 33cm
        "N0HAM2,29.600000\n"   # 10m
        "N0HAM3,146.940000\n"  # 2m
    )
    path = _write_csv(csv_text)
    try:
        repeaters = repeater_import.parse_repeater_csv(path)
        assert {r["callsign"] for r in repeaters} == {"N0HAM", "N0HAM2", "N0HAM3"}
    finally:
        pathlib.Path(path).unlink()


def test_no_frequency_column_returns_empty_not_error():
    path = _write_csv("Callsign,City\nW1ABC,Anytown\n")
    try:
        assert repeater_import.parse_repeater_csv(path) == []
    finally:
        pathlib.Path(path).unlink()


def test_row_with_unparseable_frequency_skipped():
    path = _write_csv("Callsign,Frequency\nW1ABC,not-a-number\nW1DEF,146.520000\n")
    try:
        repeaters = repeater_import.parse_repeater_csv(path)
        assert len(repeaters) == 1
        assert repeaters[0]["callsign"] == "W1DEF"
    finally:
        pathlib.Path(path).unlink()


if __name__ == "__main__":
    tests = [
        test_real_repeaterbook_header_row_confirmed_by_operator,
        test_location_falls_back_to_whichever_of_city_state_is_present,
        test_icom_dstar_csv_real_fixture,
        test_dup_minus_sign_subtracts_offset,
        test_all_bands_included_unfiltered,
        test_no_frequency_column_returns_empty_not_error,
        test_row_with_unparseable_frequency_skipped,
    ]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    print("All repeater_import tests passed.")
