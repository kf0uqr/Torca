"""
Map-overlay toggle routes for Remote Access -- the Satellites/QSO Map/
PSKReporter/POTA/APRS row plus the band-filter dropdown. Every
mutating call goes through HamClockWindow.map_layers_remote_state's
queued-signal marshaling (bridge.py's MapLayersRemoteState), never a
direct button/combo call -- see that class's own docstring for why.

Plain REST, same reasoning as routes_satellite.py: these are one-shot
toggle commands, and the resulting state is already folded into the
dashboard's own polling snapshot (app.py's dashboard_snapshot) rather
than needing a second socket.
"""

from fastapi import APIRouter, Request, HTTPException

from web_remote.common import ROLE_VIEWER


def create_map_router(dashboard, role_for):
    router = APIRouter()

    def check_auth(request: Request):
        # Every endpoint here is a mutating toggle -- viewer (read-only)
        # is never allowed, no allow_viewer param needed like
        # routes_satellite.py's GET endpoints have.
        header = request.headers.get("authorization", "")
        candidate = header[7:] if header.lower().startswith("bearer ") else None
        role = role_for(candidate)
        if role is None:
            raise HTTPException(status_code=401, detail="invalid or missing token")
        if role == ROLE_VIEWER:
            raise HTTPException(status_code=403, detail="read-only session -- viewer role cannot control the station")

    async def _toggle(request: Request, request_method):
        check_auth(request)
        body = await request.json()
        request_method(bool(body.get("on")))
        return {"ok": True}

    @router.post("/api/map/satellites")
    async def toggle_satellites(request: Request):
        return await _toggle(request, dashboard.map_layers_remote_state.request_satellites)

    @router.post("/api/map/qsos")
    async def toggle_qsos(request: Request):
        return await _toggle(request, dashboard.map_layers_remote_state.request_qsos)

    @router.post("/api/map/pskreporter")
    async def toggle_pskreporter(request: Request):
        return await _toggle(request, dashboard.map_layers_remote_state.request_pskreporter)

    @router.post("/api/map/pota")
    async def toggle_pota(request: Request):
        return await _toggle(request, dashboard.map_layers_remote_state.request_pota)

    @router.post("/api/map/aprs")
    async def toggle_aprs(request: Request):
        return await _toggle(request, dashboard.map_layers_remote_state.request_aprs)

    @router.post("/api/map/band_filter")
    async def set_band_filter(request: Request):
        check_auth(request)
        body = await request.json()
        dashboard.map_layers_remote_state.request_band_filter(body.get("band"))
        return {"ok": True}

    return router
