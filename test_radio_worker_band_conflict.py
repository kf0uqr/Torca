"""
Wiring tests for radio_worker.py's Main-vs-Sub band-conflict handling
-- two fixes for the same underlying report ("tuning Main's VFO knob
on a 9700 flips the radio to Sub and starts tuning Sub instead" / "Main
can't switch to the band Sub is on"):

1. RadioWorker._resolve_receiver_band_conflict() now always reselects
   the ORIGINAL receiver as active before returning, after temporarily
   selecting the OTHER one to park it out of the way (parking requires
   making it active first -- rigplane has no other way to write its
   VFO/frequency).
2. That alone turned out to still not be reliable on real hardware --
   a bare set_frequency() call (no receiver kwarg) depends on the
   radio having ALREADY finished switching back to Main by the time it
   runs, immediately after (1)'s own select_receiver() call, with no
   guarantee either way. Every set_frequency() call that's
   conceptually "always Main" now passes receiver=RECEIVER_MAIN
   explicitly instead -- confirmed via rigplane's own source
   (runtime/radio.py) that receiver=RECEIVER_MAIN routes through a
   completely different, unconditional internal path
   (_set_frequency_main) that doesn't depend on which receiver the
   radio currently considers "active" at all, sidestepping the
   ordering question entirely instead of trying to win the race.

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


def test_set_frequency_explicitly_addresses_main_after_conflict():
    # The actual knob path (RadioWorker._set_frequency, public
    # set_frequency()) -- not just _resolve_receiver_band_conflict in
    # isolation. Confirms the FINAL write (the one that actually
    # matters) is explicitly receiver=RECEIVER_MAIN, not a bare call
    # that would depend on select_receiver's own restore having
    # already landed on real hardware.
    worker = make_worker(main_freq_hz=144_000_000, sub_freq_hz=440_000_000)

    async def go(w):
        await w._set_frequency(440_500_000, check_conflict=True)

    _run(worker, go)

    freq_calls = [c for c in worker.radio.calls if c[0] == "set_frequency"]
    assert freq_calls, worker.radio.calls
    assert freq_calls[-1] == ("set_frequency", 440_500_000, RECEIVER_MAIN), freq_calls


def test_select_band_low_edge_fallback_explicitly_addresses_main():
    # _select_band's low-edge fallback (band-stacking unavailable/
    # excluded) for Main -- same explicit-addressing fix, different
    # call site.
    worker = make_worker(main_freq_hz=144_000_000, sub_freq_hz=440_000_000)

    async def go(w):
        await w._select_band("70cm", 440_000_000, receiver=None)

    _run(worker, go)

    freq_calls = [c for c in worker.radio.calls if c[0] == "set_frequency"]
    assert freq_calls, worker.radio.calls
    assert freq_calls[-1] == ("set_frequency", 440_000_000, RECEIVER_MAIN), freq_calls


def main():
    tests = [
        test_main_restored_active_after_parking_sub_out_of_the_way,
        test_no_conflict_no_receiver_switch_at_all,
        test_reverse_direction_sub_tuning_into_mains_band_restores_sub,
        test_set_frequency_explicitly_addresses_main_after_conflict,
        test_select_band_low_edge_fallback_explicitly_addresses_main,
    ]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    print("ALL BAND-CONFLICT WIRING TESTS PASSED")


if __name__ == "__main__":
    main()
