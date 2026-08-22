"""
Wiring test for radio_worker.py's RadioWorker.recall_memory_snapshot()
-- the "Recall" button's counterpart to the already-existing
capture_memory_snapshot() ("Add Memory"). Confirms it writes VFO A's
and VFO B's frequency/mode, applies the repeater tone settings, and
leaves VFO A selected when done -- against a real running asyncio
event loop (recall_memory_snapshot uses asyncio.run_coroutine_
threadsafe, same as every other RadioWorker command), not a bare
coroutine call.

Run directly: ./bin/python3 test_radio_worker_memories.py
"""

import asyncio
import threading
import time

from radio_worker import RadioWorker


class FakeRadio:
    """Minimal stand-in exposing just enough surface for
    _recall_memory_snapshot: VFO A/B selection, set_frequency,
    set_mode, and the four tone-related setters _set_repeater_tone_
    settings itself already calls. is_dual_receiver stays False (the
    RadioWorker default), so _call_receiver_aware never adds a
    receiver kwarg to set_vfo -- these methods don't need to accept
    one for that call, only set_frequency/set_mode do (called directly,
    not through _call_receiver_aware, matching capture_memory_
    snapshot's own equivalent calls)."""

    def __init__(self):
        self.vfo_calls = []
        self.freq_calls = []
        self.mode_calls = []
        self.tone_freq_calls = []
        self.tsql_freq_calls = []
        self.repeater_tone_calls = []
        self.repeater_tsql_calls = []

    async def set_vfo(self, value):
        self.vfo_calls.append(value)

    async def set_frequency(self, freq_hz, receiver=0):
        self.freq_calls.append((freq_hz, receiver))

    async def set_mode(self, mode, filter_width=None, receiver=0):
        self.mode_calls.append((mode, filter_width, receiver))

    async def set_tone_freq(self, freq_hz, receiver=0):
        self.tone_freq_calls.append(freq_hz)

    async def set_tsql_freq(self, freq_hz, receiver=0):
        self.tsql_freq_calls.append(freq_hz)

    async def set_repeater_tone(self, on, receiver=0):
        self.repeater_tone_calls.append(on)

    async def set_repeater_tsql(self, on, receiver=0):
        self.repeater_tsql_calls.append(on)


def make_worker():
    worker = RadioWorker({})
    worker.radio = FakeRadio()
    worker._control_methods = {"vfo": ("get_vfo", "set_vfo")}
    return worker


def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_recall_writes_both_vfos_tone_and_reselects_a():
    worker = make_worker()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    worker.loop = loop
    try:
        snapshot = {
            "A": {"freq_hz": 146_940_000, "mode": "FM", "filter": None},
            "B": {"freq_hz": 146_340_000, "mode": "FM", "filter": None},
            "tone": {"mode": "tsql", "freq_hz": 100.0},
        }
        worker.recall_memory_snapshot(snapshot)

        assert wait_until(lambda: worker.radio.vfo_calls == ["A", "B", "A"])
        assert worker.radio.freq_calls == [(146_940_000, 0), (146_340_000, 0)]
        assert worker.radio.mode_calls == [("FM", None, 0), ("FM", None, 0)]
        assert worker.radio.tone_freq_calls == [100.0]
        assert worker.radio.tsql_freq_calls == [100.0]
        assert worker.radio.repeater_tone_calls == [False]  # tsql mode -> repeater_tone off
        assert worker.radio.repeater_tsql_calls == [True]   # tsql mode -> tsql on
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
        loop.close()


def test_recall_skips_vfo_b_when_not_captured():
    # A manually-added, non-repeater memory (no real split) leaves VFO
    # B blank -- recall must not write a bogus freq_hz=None.
    worker = make_worker()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    worker.loop = loop
    try:
        snapshot = {
            "A": {"freq_hz": 146_940_000, "mode": "FM", "filter": None},
            "B": {"freq_hz": None, "mode": None, "filter": None},
            "tone": {"mode": "none", "freq_hz": None},
        }
        worker.recall_memory_snapshot(snapshot)

        assert wait_until(lambda: worker.radio.vfo_calls == ["A", "A"])  # B never selected
        assert worker.radio.freq_calls == [(146_940_000, 0)]
        assert worker.radio.repeater_tone_calls == [False]  # "none" -> both off
        assert worker.radio.repeater_tsql_calls == [False]
        assert worker.radio.tone_freq_calls == []  # "none" mode never touches the frequency registers
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
        loop.close()


def main():
    print("--- recall_memory_snapshot writes both VFOs, tone, reselects A (real running loop) ---")
    test_recall_writes_both_vfos_tone_and_reselects_a()
    print("  PASSED\n")

    print("--- recall_memory_snapshot skips VFO B when not captured (real running loop) ---")
    test_recall_skips_vfo_b_when_not_captured()
    print("  PASSED\n")

    print("ALL MEMORY RECALL WIRING TESTS PASSED")


if __name__ == "__main__":
    main()
