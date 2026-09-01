"""
Satellite Doppler control routes for Remote Access -- a satellite/
transponder picker (GET /api/satellites) plus start/stop/transponder/
offset control (POST). Every mutating call goes through
HamClockWindow.satellite_remote_state's queued-signal marshaling
(bridge.py's SatelliteRemoteState), never a direct call into
SatelliteSession -- see that class's own docstring for why (unlike
RadioWorker, SatelliteSession was never built for cross-thread calls).

Plain REST, not a websocket -- these are one-shot commands, and
satellite/tracking status is already folded into the dashboard's own
polling snapshot (app.py's dashboard_snapshot) rather than a second
socket for what's dashboard-scoped state anyway.
"""

import datetime

from fastapi import APIRouter, Request, HTTPException

import satellite_tracking
from web_remote.common import ROLE_VIEWER


def create_satellite_router(dashboard, role_for):
    router = APIRouter()

    def check_auth(request: Request, allow_viewer=True):
        header = request.headers.get("authorization", "")
        candidate = header[7:] if header.lower().startswith("bearer ") else None
        role = role_for(candidate)
        if role is None:
            raise HTTPException(status_code=401, detail="invalid or missing token")
        if not allow_viewer and role == ROLE_VIEWER:
            raise HTTPException(status_code=403, detail="read-only session -- viewer role cannot control the station")
        return role

    def find_satellite(name):
        # dashboard.satellites (the desktop's own in-memory catalog,
        # ham_dashboard.py) rather than a separate
        # load_satellite_data() disk read -- avoids finding/starting a
        # satellite that's stale relative to whatever the desktop UI
        # (and SatelliteConfigDialog's unsaved edits) currently has.
        return next((sat for sat in dashboard.satellites if sat.get("name") == name), None)

    @router.get("/api/satellites")
    async def list_satellites(request: Request):
        check_auth(request)
        # Only satellites checked "visible" in the desktop's
        # SatelliteConfigDialog (satellite_tracking.py) -- same
        # sat.get("selected") filter ham_dashboard.py already applies
        # to the map overlay and upcoming-passes table, so the web
        # picker only ever offers satellites the desktop itself is
        # actually tracking/showing.
        satellites = [sat for sat in dashboard.satellites if sat.get("selected")]
        return [
            {
                "name": sat.get("name"),
                "transponders": [
                    {
                        "description": t.get("description"),
                        "uplink_mhz": t.get("uplink_mhz"),
                        "downlink_mhz": t.get("downlink_mhz"),
                        "mode": t.get("mode"),
                    }
                    for t in sat.get("transponders", [])
                ],
            }
            for sat in satellites
        ]

    @router.post("/api/satellite/start")
    async def start_satellite(request: Request):
        check_auth(request, allow_viewer=False)
        body = await request.json()
        name = body.get("name")
        satellite = find_satellite(name)
        if satellite is None:
            raise HTTPException(status_code=404, detail=f"Unknown satellite: {name!r}")
        dashboard.satellite_remote_state.request_start(satellite)
        return {"ok": True}

    @router.post("/api/satellite/stop")
    async def stop_satellite(request: Request):
        check_auth(request, allow_viewer=False)
        dashboard.satellite_remote_state.request_stop()
        return {"ok": True}

    @router.post("/api/satellite/transponder")
    async def set_transponder(request: Request):
        # Passes the index straight through to transponder_combo.
        # setCurrentIndex (via SatelliteRemoteState.request_transponder
        # -> dashboard.select_transponder_remote) rather than resolving
        # it to a transponder dict here -- the combo is the actual
        # source of truth for "which transponder is selected" on the
        # desktop, and only setting IT (not calling SatelliteSession
        # directly) keeps the desktop UI in sync. Still validated
        # against the satellite's own transponder count so a bogus
        # index 400s instead of silently no-op'ing against an
        # out-of-range combo index.
        check_auth(request, allow_viewer=False)
        body = await request.json()
        index = body.get("index")
        satellite_name = dashboard.satellite_remote_state.state.get("satellite_name")
        satellite = find_satellite(satellite_name) if satellite_name else None
        if satellite is None:
            raise HTTPException(status_code=400, detail="No satellite is currently selected")
        transponders = satellite.get("transponders", [])
        if not isinstance(index, int) or not (0 <= index < len(transponders)):
            raise HTTPException(status_code=400, detail="Invalid transponder index")
        dashboard.satellite_remote_state.request_transponder(index)
        return {"ok": True}

    @router.get("/api/satellites/{name}/ground_track")
    async def ground_track(name: str, request: Request):
        # Computed server-side (satellite_tracking.ground_track_points,
        # the exact same function the desktop map uses) rather than
        # porting SGP4 to JS -- guarantees numerical parity with the
        # desktop's own display for far less code. See the approved
        # Phase 2 plan's "World map visual parity" section.
        check_auth(request)
        satellite = find_satellite(name)
        if satellite is None:
            raise HTTPException(status_code=404, detail=f"Unknown satellite: {name!r}")
        line1, line2 = satellite.get("line1", ""), satellite.get("line2", "")
        now = datetime.datetime.now(datetime.timezone.utc)
        points = satellite_tracking.ground_track_points(line1, line2, now)
        return {
            "points": [{"lat": lat, "lon": lon} for lat, lon in points],
            "current": {"lat": points[0][0], "lon": points[0][1]} if points else None,
        }

    @router.post("/api/satellite/offset")
    async def adjust_offset(request: Request):
        check_auth(request, allow_viewer=False)
        body = await request.json()
        delta_hz = body.get("delta_hz", 0)
        dashboard.satellite_remote_state.request_offset(delta_hz)
        return {"ok": True}

    return router
