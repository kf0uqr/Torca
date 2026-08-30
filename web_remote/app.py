"""
create_app(dashboard, token): the Remote Access FastAPI app -- a Ham
Dashboard page (/) and a per-radio page (/radio/{id}), each backed by a
polling websocket (/ws/dashboard, /ws/radio/{id}) rather than an
event-driven broadcast. Polling (a plain `while True: send; sleep()`
loop per connection) was chosen over wiring Qt signals straight into
asyncio queues because every piece of state this needs to show is
already a plain, GIL-safe-to-read-cross-thread attribute (see bridge.py
and HamClockWindow's own _upcoming_passes/_pota_spots_cache/
_pskreporter_spots_cache caches) -- no cross-thread queue plumbing
needed for an MVP dashboard/radio-status view. 5Hz for the radio page
(covers frequency/mode/PTT/meters/scope reasonably smoothly), 0.5Hz for
the dashboard (satellite passes and spot counts don't change fast).

Runs entirely on RemoteWebServer's own asyncio loop/thread (server.py)
-- never touches a QWidget directly. Incoming commands call straight
into RadioRemoteState's request_* methods (bridge.py), which themselves
only call RadioWorker's already-thread-safe methods or emit a queued Qt
signal -- never a direct widget mutation from this thread.
"""

import asyncio
import pathlib

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

import adif
import qso_log
from web_remote.common import find_radio_window, make_token_check
from web_remote.routes_tools import create_tools_router
from web_remote.routes_satellite import create_satellite_router
from web_remote.routes_audio import create_audio_router
from web_remote.routes_map import create_map_router

STATIC_DIR = pathlib.Path(__file__).parent / "static"

DASHBOARD_POLL_INTERVAL = 2.0
RADIO_POLL_INTERVAL = 0.2


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """Forces `Cache-Control: no-store` on every /static/* response.

    Without this, a browser can silently keep serving an OLD cached
    dashboard.js/cw.js/etc. after this app updates on disk (a self-
    update, or -- during development -- an in-place code edit) even
    though the freshly-served HTML references the new markup. Confirmed
    as a real, reproducible bug: new HTML (new buttons/dropdown) plus
    stale cached JS (no click handlers, no population logic for either)
    made a feature look completely non-functional even after a plain
    page reload, until the browser's cache was force-cleared. Given how
    small this app's static assets are, unconditionally disabling
    caching is a simpler, more robust fix than cache-busting query
    params or ETag plumbing."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def create_app(dashboard, token=None):
    app = FastAPI()
    app.add_middleware(NoCacheStaticMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_tools_router(dashboard, token))
    app.include_router(create_satellite_router(dashboard, token))
    app.include_router(create_audio_router(dashboard, token))
    app.include_router(create_map_router(dashboard, token))

    token_ok = make_token_check(token)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (STATIC_DIR / "dashboard.html").read_text()

    @app.get("/radio/{radio_id}", response_class=HTMLResponse)
    async def radio_page(radio_id: int):
        return (STATIC_DIR / "radio.html").read_text()

    @app.get("/radio/{radio_id}/tool/cw", response_class=HTMLResponse)
    async def cw_tool_page(radio_id: int):
        return (STATIC_DIR / "cw.html").read_text()

    @app.get("/radio/{radio_id}/tool/aprs", response_class=HTMLResponse)
    async def aprs_tool_page(radio_id: int):
        return (STATIC_DIR / "aprs.html").read_text()

    @app.websocket("/ws/dashboard")
    async def ws_dashboard(websocket: WebSocket):
        # accept() BEFORE close(code=...) -- closing pre-accept sends an
        # HTTP-level rejection (403) per the ASGI/WebSocket handshake
        # spec, not a real WS close frame, so a browser's onclose never
        # actually sees the custom code at all (confirmed live: it
        # showed up as an opaque 1006, not 4401 -- the exact reason a
        # real user's page silently retried forever with an empty
        # token and never showed why, even though the backend had
        # their connected radio the whole time).
        await websocket.accept()
        if not token_ok(websocket.query_params.get("token")):
            await websocket.close(code=4401)
            return
        try:
            while True:
                await websocket.send_json(dashboard_snapshot(dashboard))
                await asyncio.sleep(DASHBOARD_POLL_INTERVAL)
        except WebSocketDisconnect:
            pass

    @app.websocket("/ws/radio/{radio_id}")
    async def ws_radio(websocket: WebSocket, radio_id: int):
        await websocket.accept()
        if not token_ok(websocket.query_params.get("token")):
            await websocket.close(code=4401)
            return
        window = find_radio_window(dashboard, radio_id)
        if window is None or not hasattr(window, "remote_state"):
            await websocket.close(code=4404)
            return
        receive_task = asyncio.create_task(_handle_radio_commands(websocket, window))
        try:
            while True:
                await websocket.send_json(radio_snapshot(window))
                await asyncio.sleep(RADIO_POLL_INTERVAL)
        except WebSocketDisconnect:
            pass
        finally:
            receive_task.cancel()

    return app


async def _handle_radio_commands(websocket, window):
    try:
        while True:
            msg = await websocket.receive_json()
            cmd = msg.get("cmd")
            if cmd == "set_frequency" and "freq_hz" in msg:
                window.remote_state.request_frequency(msg["freq_hz"])
            elif cmd == "set_control" and "key" in msg:
                window.remote_state.request_control(msg["key"], msg.get("value"))
            elif cmd == "ptt":
                window.remote_state.request_ptt(bool(msg.get("on")))
    except WebSocketDisconnect:
        pass


def radio_snapshot(window):
    state = dict(window.remote_state.state)
    state["id"] = getattr(window, "remote_id", None)
    state["label"] = window._details.get("radio_model", "Radio")
    return state


def dashboard_snapshot(dashboard):
    radios = []
    for window in dashboard._connected_radios:
        remote_state = getattr(window, "remote_state", None)
        state = remote_state.state if remote_state is not None else {}
        radios.append({
            "id": getattr(window, "remote_id", None),
            "label": window._details.get("radio_model", "Radio"),
            "freq_hz": state.get("freq_hz"),
            "mode": state.get("mode"),
        })

    passes = []
    for pass_info in getattr(dashboard, "_upcoming_passes", [])[:10]:
        aos = pass_info.get("aos_time")
        los = pass_info.get("los_time")
        passes.append({
            "name": pass_info.get("name"),
            "aos": aos.isoformat() if aos else None,
            "los": los.isoformat() if los else None,
            "max_elevation_deg": pass_info.get("max_elevation_deg"),
        })

    try:
        recent = qso_log.load_qso_log()[-10:]
        recent.reverse()
    except Exception:
        recent = []
    recent_qsos = [
        {
            "call": qso.get("CALL", ""),
            "freq_mhz": qso.get("FREQ", ""),
            "mode": qso.get("MODE", ""),
            "datetime": f"{qso.get('QSO_DATE', '')} {qso.get('TIME_ON', '')}",
        }
        for qso in recent
    ]

    satellite_remote_state = getattr(dashboard, "satellite_remote_state", None)
    map_layers_remote_state = getattr(dashboard, "map_layers_remote_state", None)

    operator_lat = getattr(dashboard, "_observer_lat", None)
    operator_lon = getattr(dashboard, "_observer_lon", None)

    return {
        "radios": radios,
        "passes": passes,
        "spots": {
            "pota": len(getattr(dashboard, "_pota_spots_cache", [])),
            "pskreporter": len(getattr(dashboard, "_pskreporter_spots_cache", [])),
        },
        "recent_qsos": recent_qsos,
        "satellite": satellite_remote_state.state if satellite_remote_state is not None else None,
        "operator_location": {"lat": operator_lat, "lon": operator_lon} if operator_lat is not None and operator_lon is not None else None,
        "map_layers": map_layers_remote_state.state if map_layers_remote_state is not None else None,
        "map_markers": map_markers(dashboard),
        "satellite_positions": satellite_positions(dashboard, map_layers_remote_state),
    }


def satellite_positions(dashboard, map_layers_remote_state):
    """The "Satellites" map overlay -- every currently-selected
    satellite's live position (plus footprint/ground-track for the
    ones showing one), mirroring what map_widget.set_satellite_
    positions() draws on the desktop. Built from
    _satellite_positions_cache, a plain list _update_satellite_
    positions() (ham_dashboard.py) writes alongside its existing
    map_widget call -- GIL-safe to read cross-thread, same convention
    as every other cache in this module. Gated on the "satellites"
    toggle (same reasoning as map_markers' pota/pskreporter/qsos/aprs
    gates): the cache only stops updating once the toggle turns off
    (the 5s timer that refreshes it is itself gated on that same
    button), so without this gate a still-fresh cache could briefly
    keep showing satellites the desktop just turned off."""
    if map_layers_remote_state is None or not map_layers_remote_state.state.get("satellites"):
        return []
    positions = []
    for sat in getattr(dashboard, "_satellite_positions_cache", []):
        positions.append({
            "name": sat.get("name", "?"),
            "lat": sat.get("lat"),
            "lon": sat.get("lon"),
            "active": sat.get("active", False),
            "footprint": [{"lat": lat, "lon": lon} for lat, lon in sat.get("footprint", [])],
            "path": [{"lat": lat, "lon": lon} for lat, lon in sat.get("path", [])] if "path" in sat else None,
        })
    return positions


def map_markers(dashboard):
    """POTA/PSKReporter/QSO/APRS markers for the dashboard's map --
    built straight from already-cached plain data (_pota_spots_cache/
    _pskreporter_spots_cache/_aprs_stations, all GIL-safe-to-read per
    this module's own docstring) and adif.grid_square_to_latlon (a
    pure function), NOT by calling HamClockWindow's own
    _build_pota_markers/_build_pskreporter_markers/_build_qso_markers
    directly -- those read qso_band_filter_combo (a QWidget), unsafe
    to touch from this thread. Band filtering is reimplemented here
    against the plain band_filter VALUE cached in
    map_layers_remote_state.state instead, same filter logic
    (spot.get("band")/qso.get("BAND") equality) as the desktop
    versions.

    Each category is gated on map_layers_remote_state.state (a plain
    dict mirroring each toggle button's checked state, updated via a
    GUI-thread signal connection -- see bridge.py's
    MapLayersRemoteState) rather than the raw caches alone:
    _pota_spots_cache/_pskreporter_spots_cache keep the last fetch's
    data even after the desktop's own toggle is switched OFF (only the
    desktop map WIDGET's markers get cleared then, not the cache
    itself) -- without this gate, the web map showed stale spots the
    desktop wasn't showing at all, confirmed as a real bug."""
    layers = getattr(dashboard, "map_layers_remote_state", None)
    state = layers.state if layers is not None else {}
    band_filter = state.get("band_filter")

    pota_markers = []
    if state.get("pota"):
        spots = getattr(dashboard, "_pota_spots_cache", [])
        if band_filter is not None:
            spots = [spot for spot in spots if spot.get("band") == band_filter]
        pota_markers = [
            {"lat": spot["lat"], "lon": spot["lon"], "label": spot.get("activator", "?")}
            for spot in spots
            if "lat" in spot and "lon" in spot
        ]

    pskreporter_markers = []
    if state.get("pskreporter"):
        spots = getattr(dashboard, "_pskreporter_spots_cache", [])
        if band_filter is not None:
            spots = [spot for spot in spots if spot.get("band") == band_filter]
        for spot in spots:
            latlon = adif.grid_square_to_latlon(spot.get("locator"))
            if latlon is None:
                continue
            lat, lon = latlon
            pskreporter_markers.append({"lat": lat, "lon": lon, "label": spot.get("callsign", "?")})

    qso_markers = []
    try:
        if state.get("qsos"):
            qsos = qso_log.load_qso_log()
            if band_filter is not None:
                qsos = [qso for qso in qsos if qso.get("BAND") == band_filter]
            qsos.sort(key=lambda qso: (qso.get("QSO_DATE", ""), qso.get("TIME_ON", "")), reverse=True)
            qso_map_count = getattr(dashboard, "_qso_map_count", 25)
            if qso_map_count is not None:
                qsos = qsos[:qso_map_count]
            for qso in qsos:
                latlon = adif.grid_square_to_latlon(qso.get("GRIDSQUARE"))
                if latlon is None:
                    continue
                lat, lon = latlon
                qso_markers.append({"lat": lat, "lon": lon, "label": qso.get("CALL", "?")})
    except Exception:
        pass

    aprs_markers = []
    if state.get("aprs"):
        for station in getattr(dashboard, "_aprs_stations", {}).values():
            aprs_markers.append({"lat": station["lat"], "lon": station["lon"], "label": station.get("tooltip", "?")})

    return {"pota": pota_markers, "pskreporter": pskreporter_markers, "qsos": qso_markers, "aprs": aprs_markers}
