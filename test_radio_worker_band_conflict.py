"""
Wiring test for radio_worker.py's RadioWorker._resolve_receiver_band_
conflict() -- root-caused a real "tuning Main's VFO knob on a 9700
flips the radio to Sub and starts tuning Sub instead" report (plus
"Main can't switch to the band Sub is on") to this method never
reselecting the ORIGINAL receiver as active after temporarily
selecting the OTHER one to park it out of the way. Every caller (the
knob's Main-frequency path, band-button clicks) proceeds with a BARE
set_frequency() call (no receiver kwarg) right after this returns,
which always lands on whichever receiver is genuinely active on the
real radio -- left on the wrong one, that write silently retuned Sub
while Main's own target frequency was never written at all.

Run directly: ./bin/python3 test_radio_worker_band_conflict.py
"""

import asyncio
import threading
import time

from radio_worker import RadioWorker, RECEIVER_MAIN, RECEIVER_SUB


class FakeRadio:
    """Tracks every select_receiver/set_vfo_slot/set_frequency call
    (receiver-addressed or bare) in one ordered list, plus a small
    per-receiver frequency table _resolve_receiver_band_conflict reads
    via get_frequency(receiver=...) to detect the conflict at all."""

    def __init__(self, main_freq_hz, sub_freq_hz):
        self.freqs = {RECEIVER_MAIN: main_freq_hz, RECEIVER_SUB: sub_freq_hz}
        self.calls = []

    async def get_frequency(self, receiver=0):
        return self.freqs[receiver]

    async def select_receiver(self, receiver):
        self.calls.append(("select_receiver", receiver))

    async def set_vfo_slot(self, slot, receiver=0):
        self.calls.append(("set_vfo_slot", slot, receiver))

    async def set_frequency(self, freq_hz, receiver=0):
        self.calls.append(("set_frequency", freq_hz, receiver))
        self.freqs[receiver] = freq_hz


def make_worker(main_freq_hz, sub_freq_hz):
    worker = RadioWorker({"radio_model": "IC-9700"})
    worker.radio = FakeRadio(main_freq_hz, sub_freq_hz)
    worker.is_dual_receiver = True
    worker._active_receiver = RECEIVER_MAIN
    return worker


def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _run(worker, coro):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    worker.loop = loop
    try:
        future = asyncio.run_coroutine_threadsafe(coro(worker), loop)
        assert wait_until(lambda: future.done())
        future.result()  # re-raises if the coroutine itself raised
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
        loop.close()


def test_main_restored_active_after_parking_sub_out_of_the_way():
    # Main on 2m (144 MHz), Sub already on 70cm (440 MHz) -- tuning
    # Main to a 70cm frequency (e.g. the knob crossing a band edge)
    # conflicts with Sub's own current band.
    worker = make_worker(main_freq_hz=144_000_000, sub_freq_hz=440_000_000)

    async def go(w):
        await w._resolve_receiver_band_conflict(RECEIVER_MAIN, 440_500_000)

    _run(worker, go)

    select_calls = [c for c in worker.radio.calls if c[0] == "select_receiver"]
    assert select_calls, "expected select_receiver to be called at all (conflict should have been detected)"
    # Sub gets selected to move it out of the way, but the LAST
    # select_receiver call must restore Main -- that's the actual bug.
    assert select_calls[0] == ("select_receiver", RECEIVER_SUB)
    assert select_calls[-1] == ("select_receiver", RECEIVER_MAIN), select_calls


def test_no_conflict_no_receiver_switch_at_all():
    # Main on 2m, Sub on 23cm -- tuning Main within 2m (or to any band
    # Sub ISN'T on) should never touch select_receiver at all.
    worker = make_worker(main_freq_hz=144_000_000, sub_freq_hz=1_240_000_000)

    async def go(w):
        await w._resolve_receiver_band_conflict(RECEIVER_MAIN, 146_000_000)

    _run(worker, go)

    assert worker.radio.calls == [], worker.radio.calls


def test_reverse_direction_sub_tuning_into_mains_band_restores_sub():
    # The Sub-side equivalent: Sub tuning into Main's own current band
    # must restore SUB as active afterward, not leave Main selected.
    worker = make_worker(main_freq_hz=144_000_000, sub_freq_hz=440_000_000)

    async def go(w):
        await w._resolve_receiver_band_conflict(RECEIVER_SUB, 146_500_000)

    _run(worker, go)

    select_calls = [c for c in worker.radio.calls if c[0] == "select_receiver"]
    assert select_calls[0] == ("select_receiver", RECEIVER_MAIN)
    assert select_calls[-1] == ("select_receiver", RECEIVER_SUB), select_calls


def main():
    tests = [
        test_main_restored_active_after_parking_sub_out_of_the_way,
        test_no_conflict_no_receiver_switch_at_all,
        test_reverse_direction_sub_tuning_into_mains_band_restores_sub,
    ]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    print("ALL BAND-CONFLICT WIRING TESTS PASSED")


if __name__ == "__main__":
    main()
