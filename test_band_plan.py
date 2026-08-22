"""Pure-logic tests for band_plan.py -- mostly internal-consistency
checks on the transcribed FCC data (contiguous, no gaps/overlaps,
valid mode/license keys) plus a few spot-checks of well-known
boundaries against 47 CFR 97.301/97.305 (see band_plan.py's own
docstring for the exact sourcing)."""

import band_plan


def test_every_band_segment_list_is_contiguous_and_covers_the_band():
    for band in band_plan.BANDS:
        segments = band["segments"]
        assert segments, band["name"]
        for s1, e1, _, _ in segments:
            assert s1 < e1, f"{band['name']}: zero/negative-width segment at {s1}"
        if band.get("channelized"):
            continue  # real gaps between discrete channels -- see band_plan.py's own docstring
        assert segments[0][0] == band["start_hz"], f"{band['name']}: gap before first segment"
        assert segments[-1][1] == band["end_hz"], f"{band['name']}: gap after last segment"
        for (s1, e1, _, _), (s2, _, _, _) in zip(segments, segments[1:]):
            assert e1 == s2, f"{band['name']}: gap/overlap between segments at {e1} vs {s2}"


def test_every_segment_uses_a_known_mode_and_license():
    for band in band_plan.BANDS:
        for _, _, mode, min_license in band["segments"]:
            assert mode in band_plan.MODE_LABELS, f"{band['name']}: unknown mode {mode}"
            assert min_license in band_plan.LICENSE_CLASSES, f"{band['name']}: unknown license {min_license}"


def test_bands_are_sorted_and_non_overlapping():
    for (b1, b2) in zip(band_plan.BANDS, band_plan.BANDS[1:]):
        assert b1["end_hz"] <= b2["start_hz"], f"{b1['name']} overlaps {b2['name']}"


def test_license_rank_is_ascending_privilege_order():
    assert band_plan.license_rank("Technician") < band_plan.license_rank("General")
    assert band_plan.license_rank("General") < band_plan.license_rank("Advanced")
    assert band_plan.license_rank("Advanced") < band_plan.license_rank("Extra")


def test_40m_extra_only_sliver_below_7025():
    # 97.301(a)/(b): only Extra gets 7.000-7.025 MHz.
    hits = band_plan.segments_in_range(7_010_000, 7_010_000)
    assert len(hits) == 1
    _, start, end, mode, min_license = hits[0]
    assert (start, end) == (7_000_000, 7_025_000)
    assert mode == "cw_data"
    assert min_license == "Extra"


def test_40m_general_phone_above_7175():
    hits = band_plan.segments_in_range(7_200_000, 7_200_000)
    assert len(hits) == 1
    _, start, end, mode, min_license = hits[0]
    assert (start, end) == (7_175_000, 7_300_000)
    assert mode == "phone"
    assert min_license == "General"


def test_30m_is_data_only_no_phone_ever():
    # 30m is a WARC band shared with fixed services -- FCC never
    # authorizes phone there for anyone, at any class.
    hits = band_plan.segments_in_range(10_100_000, 10_150_000)
    assert len(hits) == 1
    assert hits[0][3] == "cw_data"


def test_6m_bottom_100khz_is_cw_only():
    hits = band_plan.segments_in_range(50_050_000, 50_050_000)
    assert len(hits) == 1
    assert hits[0][3] == "cw"
    hits = band_plan.segments_in_range(50_150_000, 50_150_000)
    assert len(hits) == 1
    assert hits[0][3] == "all"


def test_segments_in_range_empty_between_bands():
    # 4.000-5.330 MHz has no amateur allocation at all (between 75m and 60m).
    hits = band_plan.segments_in_range(4_100_000, 4_200_000)
    assert hits == []


def test_segments_in_range_spans_multiple_bands():
    # A wide span covering all of 20m plus a slice of 17m either side.
    hits = band_plan.segments_in_range(13_900_000, 18_200_000)
    band_names = {h[0] for h in hits}
    assert "20m" in band_names
    assert "17m" in band_names


def test_10m_technician_gets_cw_and_phone_up_to_28_5():
    hits = band_plan.segments_in_range(28_000_000, 28_499_000)
    assert all(h[4] == "Technician" for h in hits)
    hits_above = band_plan.segments_in_range(28_600_000, 28_600_000)
    assert hits_above[0][4] == "General"


if __name__ == "__main__":
    tests = [
        test_every_band_segment_list_is_contiguous_and_covers_the_band,
        test_every_segment_uses_a_known_mode_and_license,
        test_bands_are_sorted_and_non_overlapping,
        test_license_rank_is_ascending_privilege_order,
        test_40m_extra_only_sliver_below_7025,
        test_40m_general_phone_above_7175,
        test_30m_is_data_only_no_phone_ever,
        test_6m_bottom_100khz_is_cw_only,
        test_segments_in_range_empty_between_bands,
        test_segments_in_range_spans_multiple_bands,
        test_10m_technician_gets_cw_and_phone_up_to_28_5,
    ]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    print("All band_plan tests passed.")
