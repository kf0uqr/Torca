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
