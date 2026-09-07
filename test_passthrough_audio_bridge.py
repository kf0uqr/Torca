"""
Tests audio.PassthroughAudioBridge's core plumbing directly -- the
bridge used for radios with no software audio channel at all (audio
travels purely as an analog signal over cables, e.g. the truSDX; see
that class's own docstring). No real sound hardware is exercised here
(construction bypasses start()/_open_rx_stream/_open_tx_stream, same
"bare instance" approach test_audio_extra_rx_callbacks.py already uses
for AudioBridge) -- this only confirms the RX fan-out and TX byte-buffer
logic (_on_rx_captured, _on_tx_needed, play_tx_pcm) actually work, since
those are exactly what a live PortAudio device would call/receive.

A live loopback test (PassthroughAudioBridge driving two real ALSA
"snd-aloop" devices end to end -- play_tx_pcm() on one side, the tone
arriving via add_extra_rx_callback() on the other) confirmed byte-level
plumbing (chunk counts, real-time capture rate) works against genuine
sounddevice streams too, but is not reproducible here without assuming
a specific loopback device is present on the machine running tests --
that's why this file sticks to the hardware-independent unit-level
checks instead.

Run directly: ./bin/python3 test_passthrough_audio_bridge.py
"""

import asyncio
import struct
import sys

from audio import PassthroughAudioBridge


def make_bare_bridge():
    bridge = object.__new__(PassthroughAudioBridge)
    bridge.sample_rate = 48000
    bridge._in_stream = None
    bridge._out_stream = None
    bridge._rx_active = False
    bridge._rx_chunk_count = 0
    bridge._tx_active = False
    bridge._extra_rx_callbacks = []
    bridge._extra_rx_raw_callbacks = []
    bridge._extra_tx_callbacks = []
    import threading
    bridge._tx_lock = threading.Lock()
    bridge._tx_playback_buf = bytearray()
    bridge._status = lambda msg: None
    return bridge


def test_rx_capture_fans_out_to_extra_callbacks():
    print("--- RX capture feeds every registered extra_rx_callback/extra_rx_raw_callback ---")
    bridge = make_bare_bridge()
    received = []
    received_raw = []
    bridge.add_extra_rx_callback(received.append)
    bridge.add_extra_rx_raw_callback(received_raw.append)

    chunk = struct.pack("<4h", 1, 2, 3, 4)
    bridge._on_rx_captured(chunk, 4, None, None)

    assert received == [chunk]
    assert received_raw == [chunk]
    assert bridge._rx_chunk_count == 1
    print("  PASSED\n")


def test_has_rx_stream_and_has_mic_and_has_tx_output():
    print("--- has_rx_stream/has_mic/has_tx_output report real stream state ---")
    bridge = make_bare_bridge()
    assert not bridge.has_rx_stream()
    assert not bridge.has_tx_output()
    # has_mic() is ALWAYS False -- there is no operator-mic capture path
    # through this bridge at all (see its own docstring): live voice PTT
    # on a cable radio bypasses the PC entirely.
    assert not bridge.has_mic()

    bridge._rx_active = True
    bridge._out_stream = object()
    assert bridge.has_rx_stream()
    assert bridge.has_tx_output()
    assert not bridge.has_mic()
    print("  PASSED\n")


def test_play_tx_pcm_writes_and_on_tx_needed_drains_exactly():
    print("--- play_tx_pcm() writes bytes that _on_tx_needed drains in order ---")
    bridge = make_bare_bridge()
    bridge._out_stream = object()  # play_tx_pcm requires a stream to be "open"

    async def go():
        await bridge.play_tx_pcm(struct.pack("<4h", 100, 200, 300, 400))
        outdata = bytearray(8)
        bridge._on_tx_needed(outdata, 4, None, None)
        assert struct.unpack("<4h", bytes(outdata)) == (100, 200, 300, 400)

        # Buffer now empty -- next callback must zero-fill (starvation),
        # not repeat stale data or raise.
        outdata2 = bytearray(8)
        bridge._on_tx_needed(outdata2, 4, None, None)
        assert struct.unpack("<4h", bytes(outdata2)) == (0, 0, 0, 0)

        # Partial fill: less data available than one callback needs --
        # must play what's there and zero-pad the rest, not block/raise.
        await bridge.play_tx_pcm(struct.pack("<2h", 42, 43))
        outdata3 = bytearray(8)
        bridge._on_tx_needed(outdata3, 4, None, None)
        assert struct.unpack("<4h", bytes(outdata3)) == (42, 43, 0, 0)

    asyncio.run(go())
    print("  PASSED\n")


def test_play_tx_pcm_is_a_noop_without_an_open_output_stream():
    print("--- play_tx_pcm() is a no-op if no output device is open ---")
    bridge = make_bare_bridge()  # _out_stream stays None

    async def go():
        await bridge.play_tx_pcm(struct.pack("<2h", 1, 2))
        assert bridge._tx_playback_buf == b""

    asyncio.run(go())
    print("  PASSED\n")


def test_extra_tx_callback_sees_every_pushed_chunk():
    print("--- add_extra_tx_callback() sees every play_tx_pcm() chunk ---")
    bridge = make_bare_bridge()
    bridge._out_stream = object()
    seen = []
    bridge.add_extra_tx_callback(seen.append)

    async def go():
        chunk = struct.pack("<2h", 7, 8)
        await bridge.play_tx_pcm(chunk)
        assert seen == [chunk]

    asyncio.run(go())
    print("  PASSED\n")


def main():
    ok = True
    try:
        test_rx_capture_fans_out_to_extra_callbacks()
        test_has_rx_stream_and_has_mic_and_has_tx_output()
        test_play_tx_pcm_writes_and_on_tx_needed_drains_exactly()
        test_play_tx_pcm_is_a_noop_without_an_open_output_stream()
        test_extra_tx_callback_sees_every_pushed_chunk()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        ok = False
    if ok:
        print("ALL PASSTHROUGH AUDIO BRIDGE TESTS PASSED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
