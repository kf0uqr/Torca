// APRS Tool page: connects to /ws/radio/{id}/tool/aprs (id parsed from
// the URL path -- /radio/{id}/tool/aprs), attaching to the SAME decode
// session the desktop APRS Tool window would use for this radio (see
// web_remote/bridge.py's AprsRemoteState).

const RADIO_ID = parseInt(location.pathname.split("/")[2], 10);
document.getElementById("back-link").href = `/radio/${RADIO_ID}`;

function storedToken() {
    return localStorage.getItem("torca_token") || "";
}

function wsUrl(path) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${path}?token=${encodeURIComponent(storedToken())}`;
}

let ws = null;
let backendFocused = false;

function showTokenBanner() {
    document.getElementById("token-banner").style.display = "flex";
}
function hideTokenBanner() {
    document.getElementById("token-banner").style.display = "none";
}

function connect() {
    const status = document.getElementById("conn-status");
    ws = new WebSocket(wsUrl(`/ws/radio/${RADIO_ID}/tool/aprs`));

    ws.onopen = () => { status.textContent = "connected"; status.className = ""; hideTokenBanner(); };
    ws.onclose = (ev) => {
        if (ev.code === 4401) {
            status.textContent = "access token required";
            status.className = "empty";
            showTokenBanner();
            return;
        }
        status.textContent = "disconnected -- retrying...";
        status.className = "empty";
        setTimeout(connect, 2000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (event) => render(JSON.parse(event.data));
}

function send(cmd) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(cmd));
}

function summarizeInfo(info) {
    if (!info) return "(no info field)";
    if (info.comment) return info.comment;
    if (info.text) return info.text;
    if (info.raw) return info.raw;
    if (info.lat != null && info.lon != null) return `${info.lat.toFixed(4)}, ${info.lon.toFixed(4)}`;
    return info.type || "?";
}

function render(state) {
    const decodeButton = document.getElementById("decode-toggle");
    decodeButton.textContent = state.decoding ? "Stop Decoding" : "Start Decoding";
    decodeButton.classList.toggle("active", state.decoding);

    const backendSelect = document.getElementById("backend-select");
    if (!backendFocused && String(backendSelect.value) !== String(state.backend_index)) {
        backendSelect.value = state.backend_index;
    }
    backendSelect.disabled = state.decoding;

    const listDiv = document.getElementById("packet-list");
    const packets = state.packets || [];
    if (packets.length === 0) {
        listDiv.innerHTML = '<p class="empty">No packets yet.</p>';
    } else {
        listDiv.innerHTML = packets.slice().reverse().map((p) => `
            <div class="packet-row">
                <span class="packet-source">${p.source || "?"}</span> &rarr; ${p.destination || "?"}
                <span class="packet-info"> -- ${summarizeInfo(p.info)}</span>
            </div>`).join("");
    }
}

document.getElementById("decode-toggle").addEventListener("click", () => {
    const decoding = document.getElementById("decode-toggle").classList.contains("active");
    send({ cmd: decoding ? "stop_decode" : "start_decode" });
});

document.getElementById("backend-select").addEventListener("focus", () => { backendFocused = true; });
document.getElementById("backend-select").addEventListener("blur", () => { backendFocused = false; });
document.getElementById("backend-select").addEventListener("change", (e) => {
    send({ cmd: "set_backend", index: parseInt(e.target.value, 10) });
});

document.getElementById("send-button").addEventListener("click", () => {
    const comment = document.getElementById("comment-input").value;
    send({ cmd: "send_position", comment });
    document.getElementById("send-status").textContent = "Position report sent.";
});

document.getElementById("token-submit").addEventListener("click", () => {
    localStorage.setItem("torca_token", document.getElementById("token-input").value.trim());
    connect();
});
document.getElementById("token-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("token-submit").click();
});

connect();
