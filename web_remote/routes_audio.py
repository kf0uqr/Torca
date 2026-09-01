"""
RX and TX audio streaming routes.

RX: WS /ws/radio/{id}/audio streams a radio's live RX audio to the
browser as Opus frames, one per binary WS message. The connection's
own lifetime IS the enable/disable toggle:
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

TX: WS /ws/radio/{id}/tx_audio is the reverse direction, browser
microphone to radio -- raw PCM16 mono 48kHz frames, not Opus (see
ws_tx_audio's own comment for why), pushed through RadioWorker's
push_tx_audio_pcm(). See its own docstring for details.
"""

import asyncio
import queue

import opuslib
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from web_remote.common import find_radio_window

POLL_INTERVAL = 0.02
FRAME_MS = 20
BYTES_PER_SAMPLE = 2  # 16-bit mono PCM, per constants.AUDIO_SAMPLE_WIDTH


def create_audio_router(dashboard, role_for, audit=None):
    router = APIRouter()

    @router.websocket("/ws/radio/{radio_id}/audio")
    async def ws_audio(websocket: WebSocket, radio_id: int):
        # RX audio is listen-only -- any authenticated role (including
        # viewer) may enable it, same as watching meters/scope.
        await websocket.accept()
        role = role_for(websocket.query_params.get("token"))
        if role is None:
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

    @router.websocket("/ws/radio/{radio_id}/tx_audio")
    async def ws_tx_audio(websocket: WebSocket, radio_id: int):
        # Mirrors ws_audio above but reversed: raw PCM16 mono 48kHz
        # frames (no Opus) FROM the browser's microphone, pushed
        # straight into RadioWorker.push_tx_audio_pcm(). Raw, not Opus
        # -- unlike RX (a continuous monitoring stream where bandwidth
        # matters), TX audio only ever flows while the operator is
        # actively holding the browser's PTT button, and going raw
        # avoids needing to vendor a second WASM codec (an encoder,
        # this time) just for short push-to-talk bursts; encoding
        # RX only needed a decoder since RX already had one vendored.
        #
        # This connection's lifetime brackets ONE start_tx_audio_stream
        # ... stop_tx_audio_stream() session (radio_worker.py) -- NOT
        # PTT itself, which the browser's existing PTT button already
        # keys/unkeys independently (request_ptt -> start_ptt/stop_ptt).
        # Frames are only actually forwarded to the radio while PTT is
        # currently on (checked server-side, not just trusted from the
        # client) -- sending audio while unkeyed would just be wasted
        # upload, never actually transmitted.
        await websocket.accept()
        role = role_for(websocket.query_params.get("token"))
        if role is None:
            await websocket.close(code=4401)
            return
        window = find_radio_window(dashboard, radio_id)
        if window is None:
            await websocket.close(code=4404)
            return
        if window.worker.loop is None or window.worker.radio is None:
            # 4405, same code RX audio uses for "not usable right now"
            # -- here it means "not connected yet" rather than "no
            # audio device configured" (there's no TX-specific device
            # check the way RX has has_rx_stream(); TX audio streaming
            # doesn't go through AudioBridge/a local device at all, see
            # RadioWorker.start_tx_audio_stream's own docstring).
            await websocket.close(code=4405)
            return

        window.remote_state.request_start_tx_audio_stream()
        try:
            while True:
                data = await websocket.receive_bytes()
                # Both conditions checked server-side, not just trusted
                # from the client: PTT must actually be on, AND (for a
                # guest) an operator must still be supervising / TX must
                # not be locked -- a mid-stream loss of supervision or a
                # kill_tx should stop audio immediately, not just block
                # the next PTT toggle.
                if window.remote_state.state.get("ptt") and window.remote_state.can_transmit(role):
                    window.remote_state.request_push_tx_audio(data)
        except WebSocketDisconnect:
            pass
        finally:
            window.remote_state.request_stop_tx_audio_stream()

    return router
