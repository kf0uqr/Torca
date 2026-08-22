"""
Wiring test for radio_worker.py's RadioWorker._setup_meters() power-
meter display_max fix -- root-caused a real "app shows ~55W, radio's
actually transmitting ~5W" report on an IC-705 to METER_DEFINITIONS'
hardcoded display_max=100 (correct for a 100W-class rig like the
IC-7300/9700, wrong for a QRP-max radio). Confirms _setup_meters now
takes display_max from the connected radio's own rigplane profile
(radio.profile.max_watts) when the power meter is in "linear" (raw_255)
mode, instead of the one-size-fits-all constants.py default.

Run directly: ./bin/python3 test_radio_worker_meters.py
"""

from radio_worker import RadioWorker


class FakeProfile:
    def __init__(self, max_watts):
        self.max_watts = max_watts


class FakeRadio:
    """Exposes just enough surface for _setup_meters(): get_power_meter
    (so the "power" meter type resolves at all), native_power_unit, and
    profile.max_watts -- everything else METER_DEFINITIONS asks about
    (s_meter/swr/alc/...) simply won't resolve, which _setup_meters
    already tolerates (reports them as "unavailable", doesn't raise)."""

    def __init__(self, native_power_unit, max_watts):
        self.native_power_unit = native_power_unit
        self.profile = FakeProfile(max_watts)

    async def get_power_meter(self):
        return 140  # arbitrary raw byte, not exercised by this test


def make_worker(native_power_unit, max_watts):
    worker = RadioWorker({})
    worker.radio = FakeRadio(native_power_unit, max_watts)
    return worker


def test_ic705_style_radio_gets_10w_display_max():
    worker = make_worker("raw_255", 10)
    worker._setup_meters()
    assert worker._meter_definitions["power"]["kind"] == "linear"
    assert worker._meter_definitions["power"]["display_max"] == 10, worker._meter_definitions["power"]


def test_ic7300_style_radio_gets_100w_display_max():
    worker = make_worker("raw_255", 100)
    worker._setup_meters()
    assert worker._meter_definitions["power"]["display_max"] == 100, worker._meter_definitions["power"]


def test_raw_140_of_255_now_reads_close_to_5_5w_on_a_10w_radio():
    # The exact real-world numbers from the bug report: app showed
    # ~55W (raw/255*100, the old hardcoded ceiling) while the radio was
    # actually outputting ~5W. raw≈140 backs out of the OLD formula
    # (140/255*100≈54.9); the FIXED formula against this radio's real
    # 10W ceiling should land close to the reported actual power.
    worker = make_worker("raw_255", 10)
    worker._setup_meters()
    display_max = worker._meter_definitions["power"]["display_max"]
    raw = 140
    watts = (raw / 255) * display_max
    assert 5.0 <= watts <= 6.0, watts


def test_direct_native_watts_radio_is_untouched():
    # A radio reporting native watts already ("direct" kind) has no
    # display_max scaling applied at all -- confirm the fix's new code
    # path doesn't clobber that case (max_watts is only consulted for
    # "linear" kind).
    worker = make_worker("watts", 100)
    worker._setup_meters()
    assert worker._meter_definitions["power"]["kind"] == "direct"
    # display_max stays whatever METER_DEFINITIONS' own default is --
    # irrelevant for "direct" (see widgets.py/constants.py: "direct"
    # kind doesn't consult display_max/raw_max at all), just confirming
    # no crash and no unexpected mutation happened here.
    assert "display_max" in worker._meter_definitions["power"]


def test_missing_max_watts_falls_back_to_original_default():
    # A radio/profile with no max_watts at all (getattr returns None)
    # must not crash and must leave the original constants.py default
    # in place, rather than e.g. setting display_max=None.
    worker = make_worker("raw_255", None)
    worker._setup_meters()
    assert worker._meter_definitions["power"]["display_max"] == 100


if __name__ == "__main__":
    tests = [
        test_ic705_style_radio_gets_10w_display_max,
        test_ic7300_style_radio_gets_100w_display_max,
        test_raw_140_of_255_now_reads_close_to_5_5w_on_a_10w_radio,
        test_direct_native_watts_radio_is_untouched,
        test_missing_max_watts_falls_back_to_original_default,
    ]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    print("All radio_worker meter tests passed.")
