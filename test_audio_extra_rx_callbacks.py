"""
Tests AudioBridge's extra-RX-callback mechanism directly -- the actual
fix behind this phase's prerequisite change (audio.py, see its own
docstring on _extra_rx_callbacks): a single overwriting slot meant
starting a second digital-mode decoder (or an RX audio stream to a
browser) while one was already running silently stole the callback
from the first, with no error. Constructs a bare AudioBridge instance
via object.__new__ (bypassing __init__, which needs a real radio/
sounddevice setup) and sets only the handful of attributes
_on_rx_audio/add_extra_rx_callback/remove_extra_rx_callback actually
touch -- exercises the real production methods, not a reimplementation
of their logic.

Run directly: ./bin/python3 test_audio_extra_rx_callbacks.py
"""

import queue
import sys

from audio import AudioBridge


def make_bare_bridge():
    bridge = object.__new__(AudioBridge)
    bridge._extra_rx_callbacks = []
    bridge._rx_stereo = False
    bridge._rx_stereo_remainder = b""
    bridge._rx_unrecognized_reported = False
    bridge._rx_chunk_count = 0
    bridge._rx_downmix_channel = "mix"
    bridge.sample_rate = 8000
    bridge._rx_rate_start_monotonic = None
    bridge._rx_rate_bytes = 0
    bridge._rx_rate_next_report_at = None
    bridge._rx_queue = queue.Queue(maxsize=50)
    bridge._status = lambda msg: None
    return bridge


def test_add_and_remove_are_idempotent():
    print("--- add_extra_rx_callback/remove_extra_rx_callback list management ---")
    bridge = make_bare_bridge()
    callback_a = lambda data: None
    callback_b = lambda data: None

    bridge.add_extra_rx_callback(callback_a)
    assert bridge._extra_rx_callbacks == [callback_a]

    bridge.add_extra_rx_callback(callback_a)  # no-op: already registered
    assert bridge._extra_rx_callbacks == [callback_a]

    bridge.add_extra_rx_callback(callback_b)
    assert bridge._extra_rx_callbacks == [callback_a, callback_b]

    bridge.remove_extra_rx_callback(callback_a)
    assert bridge._extra_rx_callbacks == [callback_b]

    bridge.remove_extra_rx_callback(callback_a)  # no-op: not registered
    assert bridge._extra_rx_callbacks == [callback_b]

    bridge.remove_extra_rx_callback(callback_b)
    assert bridge._extra_rx_callbacks == []
    print("  PASSED\n")


def test_two_simultaneous_consumers_both_receive_every_chunk():
    print("--- two simultaneous consumers (e.g. CW decode + RX audio stream) both fire ---")
    bridge = make_bare_bridge()
    received_a = []
    received_b = []
    bridge.add_extra_rx_callback(received_a.append)
    bridge.add_extra_rx_callback(received_b.append)

    bridge._on_rx_audio(b"\x01\x02\x03\x04")

    assert received_a == [b"\x01\x02\x03\x04"], "first consumer must receive the chunk"
    assert received_b == [b"\x01\x02\x03\x04"], "second consumer must ALSO receive it -- the actual bug this fixes"
    print("  PASSED\n")


def test_removing_one_consumer_leaves_the_other_intact():
    print("--- removing one consumer doesn't affect the other ---")
    bridge = make_bare_bridge()
    received_a = []
    received_b = []
    bridge.add_extra_rx_callback(received_a.append)
    bridge.add_extra_rx_callback(received_b.append)

    bridge.remove_extra_rx_callback(received_a.append)
    bridge._on_rx_audio(b"\xaa\xbb")

    assert received_a == [], "removed consumer must not receive further chunks"
    assert received_b == [b"\xaa\xbb"], "remaining consumer must be unaffected by the other's removal"
    print("  PASSED\n")


def main():
    ok = True
    try:
        test_add_and_remove_are_idempotent()
        test_two_simultaneous_consumers_both_receive_every_chunk()
        test_removing_one_consumer_leaves_the_other_intact()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        ok = False
    if ok:
        print("ALL EXTRA-RX-CALLBACK TESTS PASSED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
