"""A RadioWorker whose thread has finished leaves self.loop pointing at
the closed loop. GUI-side calls into it (e.g. SatelliteSession.stop()
reaching every radio window during app shutdown) must be harmless
no-ops, not "RuntimeError: Event loop is closed" tracebacks."""

import asyncio
import warnings

from radio_worker import RadioWorker, RECEIVER_MAIN, _run_threadsafe


def _stopped_worker():
    worker = RadioWorker({"radio_model": "IC-9700"})
    worker.radio = object()
    loop = asyncio.new_event_loop()
    loop.close()
    worker.loop = loop
    return worker


def test_gui_calls_into_a_stopped_worker_do_not_raise():
    worker = _stopped_worker()
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a never-awaited coroutine would surface as RuntimeWarning
        worker.select_receiver(RECEIVER_MAIN)
        worker.set_scope_receiver(RECEIVER_MAIN)
        worker.set_dual_receiver_linking(False)
        worker.send_tx_audio_pcm(b"\x00" * 1920)
        worker.stop_tx_audio_send()


def test_helper_returns_none_for_missing_or_closed_loop_and_future_otherwise():
    async def noop():
        return 1

    assert _run_threadsafe(noop(), None) is None
    closed = asyncio.new_event_loop()
    closed.close()
    assert _run_threadsafe(noop(), closed) is None

    loop = asyncio.new_event_loop()
    import threading
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        assert _run_threadsafe(noop(), loop).result(timeout=5) == 1
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_stop_on_a_worker_whose_connection_already_failed_does_not_raise():
    # Closing the tab of a radio that never connected must still close it.
    worker = _stopped_worker()
    worker._main_task = type("T", (), {"cancel": lambda self: None})()
    worker.stop()
    assert worker._stop_requested


def test_generated_tx_audio_is_paced_and_the_ptt_stays_on_until_it_has_played():
    """The radio queues what it is given in a small buffer, so a whole FT8 transmission
    handed over at once would key the radio for an instant. It must go out in real time."""
    import time
    import radio_worker
    from constants import AUDIO_TX_PCM_FRAME_BYTES, AUDIO_TX_PCM_FRAME_MS

    events = []
    start = time.monotonic()

    class Radio:
        async def set_ptt(self, on): events.append(("ptt", on, time.monotonic() - start))
        async def start_audio_tx_pcm(self): events.append(("start", None, time.monotonic() - start))
        async def stop_audio_tx_pcm(self): events.append(("stop", None, time.monotonic() - start))

    async def push(frame): events.append(("push", None, time.monotonic() - start))

    worker = RadioWorker({"radio_model": "IC-9700"})
    worker.radio = Radio()
    worker._tx_push_target = lambda: push
    frames = 40                                                     # 0.8 s of audio
    asyncio.run(worker._send_tx_audio_pcm(b"\x00" * AUDIO_TX_PCM_FRAME_BYTES * frames))
    seconds = frames * AUDIO_TX_PCM_FRAME_MS / 1000.0
    pushes = [t for kind, _, t in events if kind == "push"]
    assert len(pushes) == frames
    assert pushes[-1] >= seconds - radio_worker._TX_PUSH_LEAD_SECONDS - 0.05        # not dumped all at once
    unkey = next(t for kind, on, t in events if kind == "ptt" and on is False)
    assert unkey >= seconds - 0.05                                                    # keyed for the whole transmission
