"""
Wiring test for radio_worker.py's RTTY decode attach/detach trio
(start_rtty_decode/stop_rtty_decode/_attach_rtty_decode/
_detach_rtty_decode) -- plumbing only, does NOT re-test rtty.py's own
DSP correctness (see test_rtty.py for that).

rigplane's SerialMockRadio has no audio surface at all (start_rx/
stop_rx don't exist on it -- confirmed by reading serial_stub.py
directly), so a small local fake radio is used instead, per the
approved plan.

Run directly: ./bin/python3 test_radio_worker_rtty.py
"""

import asyncio
import sys
import threading

from radio_worker import RadioWorker


class FakeRadio:
    """Minimal stand-in exposing only start_rx/stop_rx -- enough for
    RadioWorker._attach_rtty_decode's direct-tap branch."""

    def __init__(self):
        self.rx_callback = None
        self.start_rx_calls = 0
        self.stop_rx_calls = 0

    async def start_rx(self, callback):
        self.rx_callback = callback
        self.start_rx_calls += 1

    async def stop_rx(self):
        self.rx_callback = None
        self.stop_rx_calls += 1


class FakeBridge:
    """Minimal stand-in for AudioBridge -- only the two methods
    _attach_rtty_decode/_detach_rtty_decode actually call."""

    def __init__(self, has_rx=True):
        self._has_rx = has_rx
        self.extra_rx_callback = None

    def has_rx_stream(self):
        return self._has_rx

    def set_extra_rx_callback(self, callback):
        self.extra_rx_callback = callback


def make_worker():
    worker = RadioWorker({})
    worker.radio = FakeRadio()
    worker.audio_bridge = None
    return worker


def test_direct_tap_path():
    print("--- direct-tap path (no bridge holding RX) ---")
    worker = make_worker()
    received = []
    worker._rtty_decode_callback = lambda b: received.append(b)

    asyncio.run(worker._attach_rtty_decode())
    assert worker._rtty_decode_via_bridge is False
    assert worker.radio.start_rx_calls == 1
    assert worker.radio.rx_callback == worker._on_rtty_decode_frame

    worker._on_rtty_decode_frame(b"\x01\x02\x03\x04")
    assert received == [b"\x01\x02\x03\x04"]

    asyncio.run(worker._detach_rtty_decode())
    assert worker.radio.stop_rx_calls == 1
    print("  PASSED\n")


def test_bridge_piggyback_path():
    print("--- bridge-piggyback path (bridge already holds RX) ---")
    worker = make_worker()
    worker.audio_bridge = FakeBridge(has_rx=True)
    received = []
    worker._rtty_decode_callback = lambda b: received.append(b)

    asyncio.run(worker._attach_rtty_decode())
    assert worker._rtty_decode_via_bridge is True
    assert worker.audio_bridge.extra_rx_callback == worker._on_rtty_decode_frame_from_bridge
    assert worker.radio.start_rx_calls == 0, "bridge path must not steal the direct tap"

    worker._on_rtty_decode_frame_from_bridge(b"\xaa\xbb")
    assert received == [b"\xaa\xbb"]

    asyncio.run(worker._detach_rtty_decode())
    assert worker.audio_bridge.extra_rx_callback is None
    assert worker.radio.stop_rx_calls == 0, "bridge path must never call radio.stop_rx()"
    print("  PASSED\n")


def test_no_bridge_rx_stream_falls_back_to_direct_tap():
    print("--- bridge exists but holds no RX stream -> still direct-tap ---")
    worker = make_worker()
    worker.audio_bridge = FakeBridge(has_rx=False)  # e.g. a TX-only (mic-only) bridge
    asyncio.run(worker._attach_rtty_decode())
    assert worker._rtty_decode_via_bridge is False
    assert worker.radio.start_rx_calls == 1
    assert worker.audio_bridge.extra_rx_callback is None
    print("  PASSED\n")


def test_start_stop_rtty_decode_thread_safe_wrappers():
    """Exercises the actual public entry points (start_rtty_decode/
    stop_rtty_decode), which dispatch via
    asyncio.run_coroutine_threadsafe(..., self.loop) -- requires a real
    running loop on another thread, same as production usage (GUI
    thread calling into RadioWorker's asyncio-loop thread)."""
    print("--- start_rtty_decode/stop_rtty_decode (real running loop) ---")
    worker = make_worker()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    worker.loop = loop
    try:
        received = []
        worker.start_rtty_decode(lambda b: received.append(b))

        def wait_until(pred, timeout=2.0):
            import time
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if pred():
                    return True
                time.sleep(0.01)
            return False

        assert wait_until(lambda: worker.radio.start_rx_calls == 1)
        assert worker._rtty_decode_callback is not None

        worker._on_rtty_decode_frame(b"\x01\x02")
        assert wait_until(lambda: received == [b"\x01\x02"])

        worker.stop_rtty_decode()
        assert wait_until(lambda: worker.radio.stop_rx_calls == 1)
        assert wait_until(lambda: worker._rtty_decode_callback is None)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
        loop.close()
    print("  PASSED\n")


def main():
    ok = True
    try:
        test_direct_tap_path()
        test_bridge_piggyback_path()
        test_no_bridge_rx_stream_falls_back_to_direct_tap()
        test_start_stop_rtty_decode_thread_safe_wrappers()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        ok = False
    if ok:
        print("ALL RTTY WIRING TESTS PASSED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
