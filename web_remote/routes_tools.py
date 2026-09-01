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

from web_remote.common import ROLE_VIEWER, find_radio_window

TOOL_POLL_INTERVAL = 0.3


def create_tools_router(dashboard, role_for, audit=None):
    router = APIRouter()

    @router.websocket("/ws/radio/{radio_id}/tool/cw")
    async def ws_cw(websocket: WebSocket, radio_id: int):
        # accept() BEFORE close(code=...) -- see app.py's ws_dashboard
        # for why (a close before accept can't deliver a real WS close
        # code to the browser, only an opaque HTTP-level rejection).
        await websocket.accept()
        role = role_for(websocket.query_params.get("token"))
        if role is None:
            await websocket.close(code=4401)
            return
        window = find_radio_window(dashboard, radio_id)
        if window is None or not hasattr(window, "cw_remote_state"):
            await websocket.close(code=4404)
            return
        remote_state = window.cw_remote_state
        # CW send_text keys the transmitter -- gated by the SAME
        # can_transmit() the radio's own PTT uses (window.remote_state,
        # not window.cw_remote_state -- the CW tool has no supervision
        # state of its own, it shares the radio's).
        receive_task = asyncio.create_task(
            _handle_cw_commands(websocket, remote_state, window, role, radio_id, audit)
        )
        try:
            while True:
                snapshot = dict(remote_state.state)
                snapshot["macros"] = remote_state.macros()
                snapshot["role"] = role
                snapshot["operator_present"] = window.remote_state.state.get("operator_present", False)
                snapshot["tx_locked"] = window.remote_state.state.get("tx_locked", False)
                try:
                    await websocket.send_json(snapshot)
                except TypeError:
                    # A non-JSON-serializable value slipped into the
                    # cached state -- skip this tick rather than
                    # killing the whole connection over one bad
                    # snapshot (see ws_aprs's own comment for a real
                    # example of this happening).
                    pass
                await asyncio.sleep(TOOL_POLL_INTERVAL)
        except WebSocketDisconnect:
            pass
        finally:
            receive_task.cancel()

    @router.websocket("/ws/radio/{radio_id}/tool/aprs")
    async def ws_aprs(websocket: WebSocket, radio_id: int):
        await websocket.accept()
        role = role_for(websocket.query_params.get("token"))
        if role is None:
            await websocket.close(code=4401)
            return
        window = find_radio_window(dashboard, radio_id)
        if window is None or not hasattr(window, "aprs_remote_state"):
            await websocket.close(code=4404)
            return
        remote_state = window.aprs_remote_state
        # send_position transmits an APRS beacon -- same shared
        # can_transmit() gate as CW/PTT, see ws_cw's own comment.
        receive_task = asyncio.create_task(
            _handle_aprs_commands(websocket, remote_state, window, role, radio_id, audit)
        )
        try:
            while True:
                try:
                    # A copy, not remote_state.state itself -- this
                    # connection's own role/operator_present/tx_locked
                    # fields must never leak into the shared cache
                    # object another connection (a different role) also
                    # reads on its own poll tick.
                    snapshot = dict(remote_state.state)
                    snapshot["role"] = role
                    snapshot["operator_present"] = window.remote_state.state.get("operator_present", False)
                    snapshot["tx_locked"] = window.remote_state.state.get("tx_locked", False)
                    await websocket.send_json(snapshot)
                except TypeError:
                    # bytes (or anything else non-JSON-serializable)
                    # slipping into remote_state.state shouldn't kill
                    # the whole connection -- confirmed as a real bug
                    # (AprsRemoteState._on_packet used to cache a raw
                    # `info_raw` bytes field, bridge.py) that produced
                    # exactly this failure mode: every snapshot after
                    # the first bad one raised here, uncaught, closing
                    # the socket and sending the browser into an
                    # endless "disconnected -- retrying" loop. That
                    # root cause is fixed at the source now too, but
                    # skipping a bad tick here is cheap insurance
                    # against the next thing like it.
                    pass
                await asyncio.sleep(TOOL_POLL_INTERVAL)
        except WebSocketDisconnect:
            pass
        finally:
            receive_task.cancel()

    return router


async def _handle_cw_commands(websocket, remote_state, window, role, radio_id, audit):
    try:
        while True:
            msg = await websocket.receive_json()
            cmd = msg.get("cmd")
            if role == ROLE_VIEWER:
                if audit is not None:
                    audit.log(radio_id, role, cmd or "unknown", False, detail="viewer role -- read-only session")
                await websocket.send_json({"error": "read-only session -- command ignored"})
                continue
            if cmd == "start_decode":
                remote_state.request_decoding(True)
            elif cmd == "stop_decode":
                remote_state.request_decoding(False)
            elif cmd == "set_wpm" and "wpm" in msg:
                remote_state.request_wpm(msg["wpm"])
            elif cmd == "set_pitch" and "hz" in msg:
                remote_state.request_pitch(msg["hz"])
            elif cmd == "send_text" and "text" in msg:
                if not window.remote_state.can_transmit(role):
                    if audit is not None:
                        audit.log(radio_id, role, "send_text", False, detail="unsupervised or TX-locked")
                    await websocket.send_json({"error": "transmit not permitted -- no control operator supervising, or TX is locked"})
                    continue
                remote_state.request_send(msg["text"])
                if audit is not None:
                    audit.log(radio_id, role, "send_text", True)
    except WebSocketDisconnect:
        pass


async def _handle_aprs_commands(websocket, remote_state, window, role, radio_id, audit):
    try:
        while True:
            msg = await websocket.receive_json()
            cmd = msg.get("cmd")
            if role == ROLE_VIEWER:
                if audit is not None:
                    audit.log(radio_id, role, cmd or "unknown", False, detail="viewer role -- read-only session")
                await websocket.send_json({"error": "read-only session -- command ignored"})
                continue
            if cmd == "start_decode":
                remote_state.request_decoding(True)
            elif cmd == "stop_decode":
                remote_state.request_decoding(False)
            elif cmd == "set_backend" and "index" in msg:
                remote_state.request_backend(msg["index"])
            elif cmd == "send_position":
                if not window.remote_state.can_transmit(role):
                    if audit is not None:
                        audit.log(radio_id, role, "send_position", False, detail="unsupervised or TX-locked")
                    await websocket.send_json({"error": "transmit not permitted -- no control operator supervising, or TX is locked"})
                    continue
                remote_state.request_send_position(msg.get("comment", ""))
                if audit is not None:
                    audit.log(radio_id, role, "send_position", True)
    except WebSocketDisconnect:
        pass
