"""
Digital-mode tool routes -- CW and APRS only, per the approved Phase 2
plan (RTTY/PSK31/SSTV stay desktop-only: they're not fully tested
against real hardware yet). Each web page attaches to the SAME
CwToolWindow/AprsToolWindow decode session the desktop tool window
would use (see bridge.py's CwRemoteState/AprsRemoteState and
ham_dashboard.py's eager-but-hidden construction of both windows at
radio-connect time) -- never an independent decode session.

Same polling-websocket shape as app.py's dashboard/radio sockets: a
plain `while True: send; sleep()` loop per connection, not an
event-driven broadcast, since everything shown here is already a
GIL-safe-to-read-cross-thread plain-dict cache (bridge.py).
"""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from web_remote.common import find_radio_window, make_token_check

TOOL_POLL_INTERVAL = 0.3


def create_tools_router(dashboard, token=None):
    router = APIRouter()
    token_ok = make_token_check(token)

    @router.websocket("/ws/radio/{radio_id}/tool/cw")
    async def ws_cw(websocket: WebSocket, radio_id: int):
        # accept() BEFORE close(code=...) -- see app.py's ws_dashboard
        # for why (a close before accept can't deliver a real WS close
        # code to the browser, only an opaque HTTP-level rejection).
        await websocket.accept()
        if not token_ok(websocket.query_params.get("token")):
            await websocket.close(code=4401)
            return
        window = find_radio_window(dashboard, radio_id)
        if window is None or not hasattr(window, "cw_remote_state"):
            await websocket.close(code=4404)
            return
        remote_state = window.cw_remote_state
        receive_task = asyncio.create_task(_handle_cw_commands(websocket, remote_state))
        try:
            while True:
                snapshot = dict(remote_state.state)
                snapshot["macros"] = remote_state.macros()
                await websocket.send_json(snapshot)
                await asyncio.sleep(TOOL_POLL_INTERVAL)
        except WebSocketDisconnect:
            pass
        finally:
            receive_task.cancel()

    @router.websocket("/ws/radio/{radio_id}/tool/aprs")
    async def ws_aprs(websocket: WebSocket, radio_id: int):
        await websocket.accept()
        if not token_ok(websocket.query_params.get("token")):
            await websocket.close(code=4401)
            return
        window = find_radio_window(dashboard, radio_id)
        if window is None or not hasattr(window, "aprs_remote_state"):
            await websocket.close(code=4404)
            return
        remote_state = window.aprs_remote_state
        receive_task = asyncio.create_task(_handle_aprs_commands(websocket, remote_state))
        try:
            while True:
                await websocket.send_json(remote_state.state)
                await asyncio.sleep(TOOL_POLL_INTERVAL)
        except WebSocketDisconnect:
            pass
        finally:
            receive_task.cancel()

    return router


async def _handle_cw_commands(websocket, remote_state):
    try:
        while True:
            msg = await websocket.receive_json()
            cmd = msg.get("cmd")
            if cmd == "start_decode":
                remote_state.request_decoding(True)
            elif cmd == "stop_decode":
                remote_state.request_decoding(False)
            elif cmd == "set_wpm" and "wpm" in msg:
                remote_state.request_wpm(msg["wpm"])
            elif cmd == "set_pitch" and "hz" in msg:
                remote_state.request_pitch(msg["hz"])
            elif cmd == "send_text" and "text" in msg:
                remote_state.request_send(msg["text"])
    except WebSocketDisconnect:
        pass


async def _handle_aprs_commands(websocket, remote_state):
    try:
        while True:
            msg = await websocket.receive_json()
            cmd = msg.get("cmd")
            if cmd == "start_decode":
                remote_state.request_decoding(True)
            elif cmd == "stop_decode":
                remote_state.request_decoding(False)
            elif cmd == "set_backend" and "index" in msg:
                remote_state.request_backend(msg["index"])
            elif cmd == "send_position":
                remote_state.request_send_position(msg.get("comment", ""))
    except WebSocketDisconnect:
        pass
