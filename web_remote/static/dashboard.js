// Ham Dashboard page: connects to /ws/dashboard, a poll-driven
// websocket (see web_remote/app.py) that resends a full JSON snapshot
// every couple seconds -- simplest thing that works for a status page
// like this (no client-side diffing needed).

function storedToken() {
    return localStorage.getItem("torca_token") || "";
}

function wsUrl(path) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${path}?token=${encodeURIComponent(storedToken())}`;
}

function apiFetch(path, options = {}) {
    const headers = Object.assign({}, options.headers, { Authorization: `Bearer ${storedToken()}` });
    return fetch(path, Object.assign({}, options, { headers }));
}

// Doesn't ask for a token up front -- most setups either need none at
// all (Cloudflare Access is the real gate) or already have one saved
// from a previous visit. Only asks if the server actually rejects the
// connection (close code 4401, see web_remote/app.py) -- via an inline
// on-page form, not window.prompt(): a native prompt is easy to
// dismiss/cancel with no visible trace, silently leaving the page
// retrying forever with an empty token and never showing why (this
// bit a real user -- the backend had the connected radio the whole
// time, the browser just never got past an unnoticed failed prompt).
// Some embedded/automated browser contexts block window.prompt()
// outright, which this also avoids depending on.
let retryTimer = null;

function showTokenBanner() {
    document.getElementById("token-banner").style.display = "flex";
}

function hideTokenBanner() {
    document.getElementById("token-banner").style.display = "none";
}

function connect() {
    const status = document.getElementById("conn-status");
    const ws = new WebSocket(wsUrl("/ws/dashboard"));

    ws.onopen = () => {
        status.textContent = "connected";
        status.className = "";
        hideTokenBanner();
    };
    ws.onclose = (ev) => {
        if (ev.code === 4401) {
            status.textContent = "access token required";
            status.className = "empty";
            showTokenBanner();
            return; // wait for the token form instead of auto-retrying
        }
        status.textContent = "disconnected -- retrying...";
        status.className = "empty";
        clearTimeout(retryTimer);
        retryTimer = setTimeout(connect, 2000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (event) => render(JSON.parse(event.data));
}

document.getElementById("token-submit").addEventListener("click", () => {
    localStorage.setItem("torca_token", document.getElementById("token-input").value.trim());
    connect();
});
document.getElementById("token-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("token-submit").click();
});

const DASHBOARD_CONTROL_IDS = [
    "satellite-select", "satellite-toggle", "transponder-select", "offset-up", "offset-down",
    "layer-satellites", "layer-qsos", "layer-pskreporter", "layer-pota", "layer-aprs", "layer-band-filter",
];

function renderRoleBanner(role) {
    const isViewer = role === "viewer";
    document.getElementById("role-banner").style.display = isViewer ? "flex" : "none";
    for (const id of DASHBOARD_CONTROL_IDS) {
        const el = document.getElementById(id);
        if (el) el.disabled = isViewer;
    }
}

// ---- Dashboard tabs (Connected Radios/Satellite Passes/Tracking/Spot
// Networks/Recent QSOs) -- plain show/hide, no routing: nothing here
// needs a URL of its own, and the websocket snapshot keeps updating
// every panel's content regardless of which tab is visible.
document.getElementById("dashboard-tab-bar").addEventListener("click", (e) => {
    const button = e.target.closest(".tab-btn");
    if (!button) return;
    const tab = button.dataset.tab;
    document.querySelectorAll("#dashboard-tab-bar .tab-btn").forEach((b) => b.classList.toggle("active", b === button));
    document.querySelectorAll(".dashboard-tabs-col .tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.tab === tab));
});

function render(snapshot) {
    renderRoleBanner(snapshot.role);
    const radiosDiv = document.getElementById("radios-list");
    if (snapshot.radios.length === 0) {
        radiosDiv.innerHTML = '<p class="empty">No radios connected.</p>';
    } else {
        radiosDiv.innerHTML = snapshot.radios.map((r) => {
            const freq = r.freq_hz != null ? (r.freq_hz / 1e6).toFixed(6) + " MHz" : "--";
            const mode = r.mode ? `<span class="mode">${r.mode}</span>` : "";
            return `<a class="radio-row" href="/radio/${r.id}" data-radio-id="${r.id}" data-radio-label="${r.label}">
                <span>${r.label}</span>
                <span><span class="freq">${freq}</span>${mode}</span>
            </a>`;
        }).join("");
    }

    const passesBody = document.getElementById("passes-body");
    if (snapshot.passes.length === 0) {
        passesBody.innerHTML = '<tr><td colspan="3" class="empty">No passes.</td></tr>';
    } else {
        passesBody.innerHTML = snapshot.passes.map((p) => `
            <tr>
                <td>${p.name || "?"}</td>
                <td>${p.aos ? new Date(p.aos).toLocaleString() : "--"}</td>
                <td>${p.max_elevation_deg != null ? p.max_elevation_deg.toFixed(0) + "°" : "--"}</td>
            </tr>`).join("");
    }

    document.getElementById("pota-count").firstChild.textContent = snapshot.spots.pota;
    document.getElementById("pskreporter-count").firstChild.textContent = snapshot.spots.pskreporter;

    const qsosBody = document.getElementById("qsos-body");
    if (snapshot.recent_qsos.length === 0) {
        qsosBody.innerHTML = '<tr><td colspan="4" class="empty">No QSOs logged.</td></tr>';
    } else {
        qsosBody.innerHTML = snapshot.recent_qsos.map((q) => `
            <tr>
                <td>${q.call || "?"}</td>
                <td>${q.freq_mhz || "--"}</td>
                <td>${q.mode || "--"}</td>
                <td>${q.datetime.trim() || "--"}</td>
            </tr>`).join("");
    }

    renderSatellite(snapshot.satellite);
    renderMapLayers(snapshot.map_layers);
    renderMapMarkers(snapshot);
    updateSatelliteOnMap(snapshot.satellite);
    renderSatelliteOverlay(snapshot.satellite_positions);
}

// ---- Satellite Tracking ----
// Picker data (GET /api/satellites) is fetched once at load; live
// tracking status comes from the dashboard websocket snapshot's own
// "satellite" field (SatelliteRemoteState's cache, see bridge.py) --
// no separate socket needed for dashboard-scoped state.

let satelliteCatalog = [];
let transponderSelectFocused = false;

async function loadSatelliteCatalog() {
    try {
        const response = await apiFetch("/api/satellites");
        if (!response.ok) return;
        satelliteCatalog = await response.json();
        const select = document.getElementById("satellite-select");
        select.innerHTML = '<option value="">Select satellite...</option>' +
            satelliteCatalog.map((s) => `<option value="${s.name}">${s.name}</option>`).join("");
    } catch (e) { /* satellite catalog is best-effort -- tracking status still works without it */ }
}

function populateTransponders(satelliteName) {
    const select = document.getElementById("transponder-select");
    const satellite = satelliteCatalog.find((s) => s.name === satelliteName);
    if (!satellite || satellite.transponders.length === 0) {
        select.innerHTML = '<option value="">(no transponders)</option>';
        select.disabled = true;
        return;
    }
    select.disabled = false;
    select.innerHTML = satellite.transponders.map((t, i) =>
        `<option value="${i}">${t.description || t.mode || "Transponder " + i}</option>`
    ).join("");
}

document.getElementById("satellite-select").addEventListener("change", (e) => {
    populateTransponders(e.target.value);
    // If tracking is already running, picking a different satellite
    // from the dropdown should switch straight to it -- same as the
    // desktop's double-click behavior ("(re)starts tracking, replaces
    // whatever was active before"), and ham_dashboard.py's
    // _select_satellite_for_tracking() already handles being called
    // while a different satellite is active by just reassigning
    // everything (no need to stop first). If nothing is being tracked
    // yet, only repopulate the transponder list -- the user still has
    // to press "Start Tracking" explicitly, unchanged from before.
    if (document.getElementById("satellite-toggle").classList.contains("active")) {
        apiFetch("/api/satellite/start", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: e.target.value }),
        });
    }
});

document.getElementById("satellite-toggle").addEventListener("click", async () => {
    const button = document.getElementById("satellite-toggle");
    if (button.classList.contains("active")) {
        await apiFetch("/api/satellite/stop", { method: "POST" });
        return;
    }
    const name = document.getElementById("satellite-select").value;
    if (!name) return;
    await apiFetch("/api/satellite/start", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
    });
});

document.getElementById("transponder-select").addEventListener("focus", () => { transponderSelectFocused = true; });
document.getElementById("transponder-select").addEventListener("blur", () => { transponderSelectFocused = false; });
document.getElementById("transponder-select").addEventListener("change", (e) => {
    if (e.target.value === "") return;
    apiFetch("/api/satellite/transponder", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ index: parseInt(e.target.value, 10) }),
    });
});

function adjustOffset(deltaHz) {
    apiFetch("/api/satellite/offset", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ delta_hz: deltaHz }),
    });
}
document.getElementById("offset-up").addEventListener("click", () => adjustOffset(1000));
document.getElementById("offset-down").addEventListener("click", () => adjustOffset(-1000));

function renderSatellite(satellite) {
    const toggleButton = document.getElementById("satellite-toggle");
    const statusDiv = document.getElementById("satellite-status");
    if (!satellite || !satellite.satellite_name) {
        toggleButton.textContent = "Start Tracking";
        toggleButton.classList.remove("active");
        statusDiv.textContent = "No satellite selected.";
        return;
    }

    const select = document.getElementById("satellite-select");
    if (select.value !== satellite.satellite_name) {
        select.value = satellite.satellite_name;
        populateTransponders(satellite.satellite_name);
    }

    toggleButton.textContent = satellite.tracking ? "Stop Tracking" : "Start Tracking";
    toggleButton.classList.toggle("active", satellite.tracking);

    const parts = [satellite.satellite_name];
    if (satellite.elevation_deg != null) {
        parts.push(`El ${satellite.elevation_deg.toFixed(1)}° Az ${satellite.azimuth_deg.toFixed(1)}°`);
    }
    if (satellite.downlink_doppler_hz != null) {
        parts.push(`Downlink Doppler ${satellite.downlink_doppler_hz > 0 ? "+" : ""}${satellite.downlink_doppler_hz.toFixed(0)} Hz`);
    }
    if (satellite.crossing_text) parts.push(satellite.crossing_text);
    if (satellite.warning_text) parts.push(satellite.warning_text);
    statusDiv.textContent = parts.join(" -- ");
}

// ---- Split view (radio pane) ----
// Clicking a radio in the list opens its control page in an iframe
// alongside the dashboard instead of navigating away -- listener is
// on #radios-list itself (delegation), not on the individual <a>
// rows, since render() replaces those rows wholesale on every ~2s
// poll tick (same "don't attach to something that gets rebuilt out
// from under you" lesson as the radio page's frequency digit
// spinner). The iframe is same-origin, so it shares localStorage's
// saved token automatically -- no need to thread it through the URL.
document.getElementById("radios-list").addEventListener("click", (e) => {
    const row = e.target.closest(".radio-row");
    if (!row) return;
    e.preventDefault();
    openRadioPane(row.dataset.radioId, row.dataset.radioLabel);
});

function openRadioPane(radioId, label) {
    document.getElementById("radio-pane-title").textContent = label || "Radio";
    document.getElementById("radio-frame").src = `/radio/${radioId}`;
    document.getElementById("radio-pane").classList.add("open");
}

function closeRadioPane() {
    document.getElementById("radio-pane").classList.remove("open");
    // Navigating the iframe away tears down its websocket connections
    // (audio, tool pages if the user drilled further in) rather than
    // leaving them running invisibly in a hidden pane.
    document.getElementById("radio-frame").src = "about:blank";
    closeToolPane();  // a tool without its radio showing doesn't make sense
}

document.getElementById("radio-pane-close").addEventListener("click", closeRadioPane);

// ---- Split view (tool pane) ----
// The radio pane's own iframe (radio.js) can't reach across into ITS
// parent's DOM directly (cross-document, even though same-origin) --
// it posts a message up asking this page to open the tool pane over
// #dashboard-pane instead, keeping the radio pane (already open, to
// the right) untouched. This is what makes "opening a tool covers the
// dashboard but the radio stays visible" work when the radio is being
// viewed through the dashboard's own split view -- see radio.js's own
// comment for the parallel case where radio.html is loaded standalone
// (no parent dashboard to cover, so it manages an equivalent split of
// its own instead).
window.addEventListener("message", (e) => {
    if (e.origin !== window.location.origin) return;
    if (!e.data || e.data.type !== "torca-open-tool") return;
    openToolPane(e.data.url, e.data.label);
});

function openToolPane(url, label) {
    document.getElementById("tool-pane-title").textContent = label || "Tool";
    document.getElementById("tool-frame").src = url;
    document.getElementById("tool-pane").classList.add("open");
    document.getElementById("dashboard-pane").style.display = "none";
}

function closeToolPane() {
    document.getElementById("tool-pane").classList.remove("open");
    document.getElementById("tool-frame").src = "about:blank";
    document.getElementById("dashboard-pane").style.display = "";
}

document.getElementById("tool-pane-close").addEventListener("click", closeToolPane);

loadSatelliteCatalog();
connect();

// ---- Map ----
// Leaflet + OSM tiles (same tile source map_tiles.py's desktop map
// uses) rather than reimplementing WorldMapWidget's custom Qt
// painting -- see the approved Phase 2 plan's "World map visual
// parity" section. The terminator is recomputed client-side (ported
// line-for-line from solar_data.py's _solar_subpoint and
// world_map.py's _draw_terminator -- Cooper's declination
// approximation plus one closed-form trig solve per longitude step,
// simple enough not to need a server round trip). The satellite
// ground track is NOT re-derived in JS -- it's fetched from
// GET /api/satellites/{name}/ground_track, which calls the exact same
// satellite_tracking.ground_track_points() the desktop uses, for
// numerical parity without porting SGP4 to JS.

// Satellite ground tracks/footprints are computed in plain -180..180
// longitude, so a pass crossing the antimeridian has consecutive
// points jumping straight from e.g. 179 to -179 -- drawn literally,
// Leaflet connects those with a chord straight across the whole map.
// Un-wrapping (letting longitude run continuously past ±180 instead
// of snapping back) keeps each point's true position relative to the
// last one, which the Mercator projection renders correctly with no
// special-casing needed on top.
function unwrapLongitudes(points) {
    if (!points || points.length === 0) return points;
    let prevLon = points[0].lon;
    const result = [{ lat: points[0].lat, lon: prevLon }];
    for (let i = 1; i < points.length; i++) {
        let lon = points[i].lon;
        while (lon - prevLon > 180) lon -= 360;
        while (lon - prevLon < -180) lon += 360;
        result.push({ lat: points[i].lat, lon });
        prevLon = lon;
    }
    return result;
}

const map = L.map("map", { worldCopyJump: true }).setView([20, 0], 2);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
}).addTo(map);

let terminatorLine = null;
let terminatorPolygon = null;
let subSolarMarker = null;
let operatorMarker = null;
let satelliteMarker = null;
let groundTrackLine = null;
let potaMarkers = [];
let pskreporterMarkers = [];
let qsoMarkers = [];
let lastGroundTrackSatellite = null;

function toRad(deg) { return deg * Math.PI / 180; }
function toDeg(rad) { return rad * 180 / Math.PI; }

function computeTerminator(nowUtc) {
    const startOfYear = Date.UTC(nowUtc.getUTCFullYear(), 0, 0);
    const dayOfYear = Math.floor((nowUtc.getTime() - startOfYear) / 86400000);
    const declDeg = 23.44 * Math.sin(toRad(360 / 365.0 * (dayOfYear - 81)));
    const hoursUtc = nowUtc.getUTCHours() + nowUtc.getUTCMinutes() / 60 + nowUtc.getUTCSeconds() / 3600;
    let subLon = -(hoursUtc - 12.0) * 15.0;
    subLon = ((subLon + 180) % 360 + 360) % 360 - 180;

    const decl = toRad(declDeg);
    const sinDecl = Math.sin(decl);
    const cosDecl = Math.cos(decl);

    const points = [];
    for (let lon = -180; lon <= 180 + 1e-9; lon += 2.0) {
        const hourAngle = toRad(lon - subLon);
        let termLat;
        if (Math.abs(sinDecl) < 1e-6) {
            termLat = Math.cos(hourAngle) < 0 ? 90 : -90;
        } else {
            termLat = toDeg(Math.atan(-cosDecl * Math.cos(hourAngle) / sinDecl));
        }
        points.push([termLat, lon]);
    }
    return { points, declDeg, subLat: declDeg, subLon };
}

function redrawTerminator() {
    const { points, declDeg, subLat, subLon } = computeTerminator(new Date());

    if (terminatorLine) map.removeLayer(terminatorLine);
    terminatorLine = L.polyline(points, { color: "#a0a0a0", weight: 1.5, opacity: 0.8, interactive: false }).addTo(map);

    if (terminatorPolygon) map.removeLayer(terminatorPolygon);
    const fillPoints = points.slice();
    // Closes the fill polygon off along whichever pole is fully night
    // (north when declination < 0, south otherwise -- see
    // world_map.py's _draw_terminator for why that's always correct).
    if (declDeg < 0) fillPoints.push([90, 180], [90, -180]);
    else fillPoints.push([-90, 180], [-90, -180]);
    terminatorPolygon = L.polygon(fillPoints, {
        stroke: false, fillColor: "#000", fillOpacity: 0.35, interactive: false,
    }).addTo(map);

    if (subSolarMarker) map.removeLayer(subSolarMarker);
    subSolarMarker = L.circleMarker([subLat, subLon], {
        radius: 5, color: "#1c1c1e", weight: 1, fillColor: "#ffd23c", fillOpacity: 1,
    }).bindTooltip("Sub-solar point").addTo(map);
}

setInterval(redrawTerminator, 30000);
redrawTerminator();

function renderMapMarkers(snapshot) {
    if (operatorMarker) { map.removeLayer(operatorMarker); operatorMarker = null; }
    if (snapshot.operator_location) {
        operatorMarker = L.circleMarker([snapshot.operator_location.lat, snapshot.operator_location.lon], {
            radius: 6, color: "#fff", weight: 2, fillColor: "#ff3c3c", fillOpacity: 1,
        }).bindTooltip("Operator location").addTo(map);
    }

    const markers = snapshot.map_markers || { pota: [], pskreporter: [], qsos: [] };

    potaMarkers.forEach((m) => map.removeLayer(m));
    potaMarkers = (markers.pota || []).map((s) =>
        L.circleMarker([s.lat, s.lon], { radius: 4, color: "#1c1c1e", weight: 1, fillColor: "#3ce0c8", fillOpacity: 1 })
            .bindTooltip(s.label).addTo(map));

    pskreporterMarkers.forEach((m) => map.removeLayer(m));
    pskreporterMarkers = (markers.pskreporter || []).map((s) =>
        L.circleMarker([s.lat, s.lon], { radius: 3.5, color: "#1c1c1e", weight: 1, fillColor: "#c060ff", fillOpacity: 1 })
            .bindTooltip(s.label).addTo(map));

    qsoMarkers.forEach((m) => map.removeLayer(m));
    qsoMarkers = (markers.qsos || []).map((s) =>
        L.circleMarker([s.lat, s.lon], { radius: 3.5, color: "#1c1c1e", weight: 1, fillColor: "#50e682", fillOpacity: 1 })
            .bindTooltip(s.label).addTo(map));
}

async function updateSatelliteOnMap(satellite) {
    const name = satellite && satellite.satellite_name;
    if (!name) {
        if (satelliteMarker) { map.removeLayer(satelliteMarker); satelliteMarker = null; }
        if (groundTrackLine) { map.removeLayer(groundTrackLine); groundTrackLine = null; }
        lastGroundTrackSatellite = null;
        return;
    }
    if (name === lastGroundTrackSatellite) return; // avoid refetching every poll tick
    lastGroundTrackSatellite = name;

    try {
        const response = await apiFetch(`/api/satellites/${encodeURIComponent(name)}/ground_track`);
        if (!response.ok) return;
        const data = await response.json();

        if (groundTrackLine) map.removeLayer(groundTrackLine);
        groundTrackLine = L.polyline(unwrapLongitudes(data.points || []).map((p) => [p.lat, p.lon]), {
            color: "#ffa53c", weight: 2, dashArray: "4,4", interactive: false,
        }).addTo(map);

        if (satelliteMarker) map.removeLayer(satelliteMarker);
        if (data.current) {
            satelliteMarker = L.circleMarker([data.current.lat, data.current.lon], {
                radius: 6, color: "#1c1c1e", weight: 1, fillColor: "#ffa53c", fillOpacity: 1,
            }).bindTooltip(name).addTo(map);
        }
    } catch (e) { /* map overlay is best-effort */ }
}

// ---- Map layer toggle row ----
// Mirrors ham_dashboard.py's map-overlay button row (Satellites/QSO
// Map/PSKReporter/POTA/APRS + band filter) -- each button posts to
// web_remote/routes_map.py, which goes through MapLayersRemoteState's
// queued-signal marshaling into the REAL button on the desktop, so a
// web toggle does exactly what clicking the desktop button would
// (starts/stops the same fetch workers, applies the same band filter).

let bandFilterOptionsPopulated = false;
let bandFilterSelectFocused = false;

function layerButton(id) { return document.getElementById(id); }

function postLayerToggle(path, on) {
    return apiFetch(path, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on }),
    });
}

layerButton("layer-satellites").addEventListener("click", () =>
    postLayerToggle("/api/map/satellites", !layerButton("layer-satellites").classList.contains("active")));
layerButton("layer-qsos").addEventListener("click", () =>
    postLayerToggle("/api/map/qsos", !layerButton("layer-qsos").classList.contains("active")));
layerButton("layer-pskreporter").addEventListener("click", () =>
    postLayerToggle("/api/map/pskreporter", !layerButton("layer-pskreporter").classList.contains("active")));
layerButton("layer-pota").addEventListener("click", () =>
    postLayerToggle("/api/map/pota", !layerButton("layer-pota").classList.contains("active")));
layerButton("layer-aprs").addEventListener("click", () =>
    postLayerToggle("/api/map/aprs", !layerButton("layer-aprs").classList.contains("active")));

document.getElementById("layer-band-filter").addEventListener("focus", () => { bandFilterSelectFocused = true; });
document.getElementById("layer-band-filter").addEventListener("blur", () => { bandFilterSelectFocused = false; });
document.getElementById("layer-band-filter").addEventListener("change", (e) => {
    apiFetch("/api/map/band_filter", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ band: e.target.value || null }),
    });
});

function renderMapLayers(layers) {
    if (!layers) return;

    const buttons = {
        "layer-satellites": ["satellites", "Satellites"],
        "layer-qsos": ["qsos", "QSO Map"],
        "layer-pskreporter": ["pskreporter", "PSKReporter"],
        "layer-pota": ["pota", "POTA"],
        "layer-aprs": ["aprs", "APRS"],
    };
    for (const [id, [key, label]] of Object.entries(buttons)) {
        const button = layerButton(id);
        const on = !!layers[key];
        button.textContent = `${label}: ${on ? "ON" : "OFF"}`;
        button.classList.toggle("active", on);
    }

    const select = document.getElementById("layer-band-filter");
    if (!bandFilterOptionsPopulated && (layers.band_options || []).length) {
        select.innerHTML = layers.band_options.map((opt) =>
            `<option value="${opt.value || ""}">${opt.label}</option>`).join("");
        bandFilterOptionsPopulated = true;
    }
    if (!bandFilterSelectFocused) {
        select.value = layers.band_filter || "";
    }
}

// ---- "Satellites" map overlay ----
// Distinct from updateSatelliteOnMap's single actively-Doppler-tracked
// satellite marker/ground-track above -- this is the desktop's
// separate "Satellites: ON" overlay (satellite_button), showing EVERY
// currently-selected satellite's live position at once, from
// snapshot.satellite_positions (web_remote/app.py's satellite_
// positions()). Empty when the toggle is off, so this group just
// clears itself -- no separate on/off branch needed here.

const satelliteOverlayGroup = L.layerGroup().addTo(map);

function renderSatelliteOverlay(positions) {
    satelliteOverlayGroup.clearLayers();
    for (const sat of positions || []) {
        if (sat.lat == null || sat.lon == null) continue;
        L.circleMarker([sat.lat, sat.lon], {
            radius: sat.active ? 6 : 4,
            color: "#1c1c1e", weight: 1,
            fillColor: sat.active ? "#ffa53c" : "#ffe28a",
            fillOpacity: 1,
        }).bindTooltip(sat.name).addTo(satelliteOverlayGroup);

        if (sat.footprint && sat.footprint.length > 1) {
            L.polygon(unwrapLongitudes(sat.footprint).map((p) => [p.lat, p.lon]), {
                color: "#ffe28a", weight: 1, fillOpacity: 0.05, interactive: false,
            }).addTo(satelliteOverlayGroup);
        }
        if (sat.path && sat.path.length > 1) {
            L.polyline(unwrapLongitudes(sat.path).map((p) => [p.lat, p.lon]), {
                color: "#ffe28a", weight: 1.5, dashArray: "2,4", opacity: 0.7, interactive: false,
            }).addTo(satelliteOverlayGroup);
        }
    }
}
