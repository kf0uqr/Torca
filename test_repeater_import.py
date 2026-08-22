"""Tests for repeater_import.py -- primarily a synthetic RepeaterBook-
style export (that provider's exact downloadable-CSV column names
weren't independently verified against a real downloaded file, so this
covers the alias-matching logic rather than claiming byte-exact
provider fidelity), plus a real-data regression case (test_fixtures/
icom_repeater_sample.csv, two rows taken verbatim from Icom's own
official D-STAR repeater list download, not synthesized) confirming
the alias-based column matching also tolerates a differently-shaped
CSV without being the design target.

Per explicit instruction, parse_repeater_csv imports the ENTIRE file
unfiltered -- no band restriction, no location filtering -- so these
tests confirm that directly (e.g. test_all_bands_included_unfiltered),
not just the column-matching logic."""

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
    assert claremore["mode"] == "DV"

    altus = next(r for r in repeaters if "AJ5Q" in r["callsign"])
    assert altus["output_freq_hz"] == 442_225_000
    assert altus["input_freq_hz"] == 447_225_000  # 442.225 + 5.0 (DUP+)


def test_repeaterbook_style_dup_minus_offset():
    # RepeaterBook-style: signed "Input Frequency" given directly
    # (no separate Dup/Offset split) -- confirmed against that
    # provider's own documented Export API field list (Frequency,
    # Input Frequency) even though the exact downloadable-CSV header
    # spelling wasn't independently verified.
    csv_text = (
        "Callsign,Frequency,Input Frequency,PL/CTCSS Uplink,City,Operating Mode\n"
        "W1ABC,146.940000,146.340000,100.0,Anytown,FM\n"
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
    finally:
        pathlib.Path(path).unlink()


def test_dup_minus_sign_subtracts_offset():
    csv_text = (
        "Callsign,Frequency,Dup,Offset\n"
        "K1XYZ,145.150000,DUP-,0.600000\n"
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
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv_text)
        path = f.name
    try:
        repeaters = repeater_import.parse_repeater_csv(path)
        assert {r["callsign"] for r in repeaters} == {"N0HAM", "N0HAM2", "N0HAM3"}
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


def test_row_with_unparseable_frequency_skipped():
    csv_text = "Callsign,Frequency\nW1ABC,not-a-number\nW1DEF,146.520000\n"
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv_text)
        path = f.name
    try:
        repeaters = repeater_import.parse_repeater_csv(path)
        assert len(repeaters) == 1
        assert repeaters[0]["callsign"] == "W1DEF"
    finally:
        pathlib.Path(path).unlink()


if __name__ == "__main__":
    tests = [
        test_icom_dstar_csv_real_fixture,
        test_repeaterbook_style_dup_minus_offset,
        test_dup_minus_sign_subtracts_offset,
        test_all_bands_included_unfiltered,
        test_no_frequency_column_returns_empty_not_error,
        test_row_with_unparseable_frequency_skipped,
    ]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    print("All repeater_import tests passed.")
