"""
RX audio streaming route -- WS /ws/radio/{id}/audio streams a radio's
live RX audio to the browser as Opus frames, one per binary WS
message. The connection's own lifetime IS the enable/disable toggle:
opening it registers an extra RX callback (AudioBridge.
add_extra_rx_callback, audio.py -- now a list, not a single
overwriting slot, specifically so this can run alongside a digital-
mode decoder on the same radio, see audio.py's own docstring),
closing it (browser navigates away, clicks "Disable", or disconnects)
unregisters it -- no separate start/stop protocol message needed.

Opus, not WebRTC: WebRTC's ICE/UDP model doesn't fit cleanly through a
Cloudflare Tunnel (plain HTTP/WS ingress only), whereas Opus frames
over the same WS transport everything else here already uses do.
`opuslib` is already an installed dependency (a transitive rigplane
one, per Phase 2's own research) -- this is simply the first place
this app's own code calls it.

Re-framing: AudioBridge hands over whatever chunk size rigplane's
AudioPacket happens to deliver, but Opus requires a FIXED frame
duration (2.5/5/10/20/40/60ms) -- arbitrary chunk sizes would make
encoder.encode() raise. A small persistent byte buffer re-slices the
incoming stream into fixed 20ms frames (a good latency/overhead
tradeoff for a monitoring stream like this) before each encode call.

Cross-thread handoff: AudioBridge's callback fires on RadioWorker's
own asyncio-loop thread (a THIRD thread, neither the GUI thread nor
this websocket's own RemoteWebServer thread) -- a plain thread-safe
queue.Queue carries raw PCM from that thread to this route's own
async consumer loop, polled with a short sleep rather than blocking,
since asyncio.Queue.put isn't safe to call from another thread without
extra marshaling this doesn't need.
"""

import asyncio
import queue

import opuslib
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from web_remote.common import find_radio_window, make_token_check

POLL_INTERVAL = 0.02
FRAME_MS = 20
BYTES_PER_SAMPLE = 2  # 16-bit mono PCM, per constants.AUDIO_SAMPLE_WIDTH


def create_audio_router(dashboard, token=None):
    router = APIRouter()
    token_ok = make_token_check(token)

    @router.websocket("/ws/radio/{radio_id}/audio")
    async def ws_audio(websocket: WebSocket, radio_id: int):
        await websocket.accept()
        if not token_ok(websocket.query_params.get("token")):
            await websocket.close(code=4401)
            return
        window = find_radio_window(dashboard, radio_id)
        if window is None:
            await websocket.close(code=4404)
            return
        audio_bridge = getattr(window.worker, "audio_bridge", None)
        if audio_bridge is None or not audio_bridge.has_rx_stream():
            # 4405: no RX audio device was configured for this radio's
            # connection -- same requirement CW/APRS decode already has
            # (see AudioBridge.has_rx_stream's own docstring).
            await websocket.close(code=4405)
            return

        sample_rate = audio_bridge.sample_rate
        try:
            encoder = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_AUDIO)
        except Exception:
            # e.g. a radio negotiated a sample rate Opus doesn't support
            # (valid rates: 8000/12000/16000/24000/48000).
            await websocket.close(code=4406)
            return

        frame_bytes = int(sample_rate * FRAME_MS / 1000) * BYTES_PER_SAMPLE
        frame_samples = frame_bytes // BYTES_PER_SAMPLE

        raw_queue = queue.Queue(maxsize=200)

        def on_pcm(data: bytes):
            try:
                raw_queue.put_nowait(data)
            except queue.Full:
                pass  # drop rather than block AudioBridge's own callback thread

        audio_bridge.add_extra_rx_callback(on_pcm)
        buffer = bytearray()
        try:
            while True:
                try:
                    pcm = raw_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                buffer.extend(pcm)
                while len(buffer) >= frame_bytes:
                    frame = bytes(buffer[:frame_bytes])
                    del buffer[:frame_bytes]
                    try:
                        encoded = encoder.encode(frame, frame_samples)
                    except Exception:
                        continue
                    await websocket.send_bytes(encoded)
        except WebSocketDisconnect:
            pass
        finally:
            audio_bridge.remove_extra_rx_callback(on_pcm)

    return router
