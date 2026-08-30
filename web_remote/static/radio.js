// Per-radio control page: connects to /ws/radio/{id} (id parsed from
// the URL path), a poll-driven websocket (~5Hz, see web_remote/app.py)
// that resends this radio's full state each tick, and sends command
// messages back for tuning/mode/PTT.

const RADIO_ID = parseInt(location.pathname.split("/").pop(), 10);

document.getElementById("cw-tool-link").href = `/radio/${RADIO_ID}/tool/cw`;
document.getElementById("aprs-tool-link").href = `/radio/${RADIO_ID}/tool/aprs`;

function storedToken() {
    return localStorage.getItem("torca_token") || "";
}

function wsUrl(path) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${path}?token=${encodeURIComponent(storedToken())}`;
}

let ws = null;
let pttActive = false;
let modeSelectFocused = false;

// Same inline-token-banner flow as dashboard.js -- see its own
// comment for why this deliberately avoids window.prompt().
let retryTimer = null;

function showTokenBanner() {
    document.getElementById("token-banner").style.display = "flex";
}

function hideTokenBanner() {
    document.getElementById("token-banner").style.display = "none";
}

function connect() {
    const status = document.getElementById("conn-status");
    ws = new WebSocket(wsUrl(`/ws/radio/${RADIO_ID}`));

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
            return;
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

function send(cmd) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(cmd));
}

function render(state) {
    document.getElementById("radio-label").textContent = state.label || "Radio";
    document.getElementById("freq-display").textContent =
        state.freq_hz != null ? (state.freq_hz / 1e6).toFixed(6) + " MHz" : "-- MHz";

    if (!modeSelectFocused && state.mode) {
        const select = document.getElementById("mode-select");
        if (select.value !== state.mode) select.value = state.mode;
    }

    pttActive = !!state.ptt;
    const pttButton = document.getElementById("ptt-button");
    pttButton.classList.toggle("active", pttActive);
    pttButton.textContent = pttActive ? "TRANSMITTING" : "PTT";

    renderMeters(state.meters || {}, state.meter_defs || {});
    if (state.scope) renderScope(state.scope);
}

function renderMeters(meters, defs) {
    const container = document.getElementById("meters");
    const keys = Object.keys(defs).length ? Object.keys(defs) : Object.keys(meters);
    if (keys.length === 0) {
        container.innerHTML = '<p class="empty">No meters yet.</p>';
        return;
    }
    container.innerHTML = keys.map((key) => {
        const def = defs[key] || {};
        const raw = meters[key];
        const rawMax = def.raw_max || 255;
        const pct = raw != null ? Math.max(0, Math.min(100, (raw / rawMax) * 100)) : 0;
        const display = raw != null ? raw.toFixed ? raw.toFixed(1) : raw : "--";
        return `<div class="meter">
            <div class="label"><span>${def.label || key}</span><span>${display}${def.unit || ""}</span></div>
            <div class="bar-bg"><div class="bar-fill" style="width:${pct}%"></div></div>
        </div>`;
    }).join("");
}

function renderScope(scope) {
    const raw = atob(scope.pixels_b64);
    drawSpectrumBars(raw);
    pushWaterfallRow(raw);
}

function drawSpectrumBars(raw) {
    const canvas = document.getElementById("scope-canvas");
    const ctx = canvas.getContext("2d");
    const n = raw.length;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#3c78c8";
    const barWidth = canvas.width / n;
    for (let i = 0; i < n; i++) {
        const amplitude = raw.charCodeAt(i); // 0x00-0xA0 per rigplane.scope.ScopeFrame
        const h = (amplitude / 160) * canvas.height;
        ctx.fillRect(i * barWidth, canvas.height - h, Math.max(1, barWidth), h);
    }
}

// ---- Waterfall ----
// Scrolling history of scope frames, newest at the top -- ported from
// widgets.py's WaterfallWidget/amplitude_to_color line-for-line (same
// color anchors, same 160 max amplitude, same "shift down one row,
// draw the new row at the top" approach against a small fixed-size
// offscreen buffer that's then scaled up into the visible canvas,
// mirroring WaterfallWidget.paintEvent's painter.drawImage(rect,
// image, image.rect()) scaling) so the web page looks like the same
// app, not a different rendering of the same data.

const WATERFALL_MAX_ROWS = 200; // matches constants.WATERFALL_ROWS
const WATERFALL_COLOR_ANCHORS = [
    [0.0, [0, 0, 40]],
    [0.25, [0, 0, 180]],
    [0.5, [0, 180, 180]],
    [0.75, [255, 255, 0]],
    [1.0, [255, 0, 0]],
];

function amplitudeToColor(amp, maxAmp = 160) {
    const frac = Math.max(0, Math.min(1, amp / maxAmp));
    for (let i = 0; i < WATERFALL_COLOR_ANCHORS.length - 1; i++) {
        const [f0, c0] = WATERFALL_COLOR_ANCHORS[i];
        const [f1, c1] = WATERFALL_COLOR_ANCHORS[i + 1];
        if (frac >= f0 && frac <= f1) {
            const t = (frac - f0) / (f1 - f0);
            return [
                Math.round(c0[0] + (c1[0] - c0[0]) * t),
                Math.round(c0[1] + (c1[1] - c0[1]) * t),
                Math.round(c0[2] + (c1[2] - c0[2]) * t),
            ];
        }
    }
    return WATERFALL_COLOR_ANCHORS[WATERFALL_COLOR_ANCHORS.length - 1][1];
}

let waterfallBuffer = null; // offscreen canvas, width = pixels/frame, height = WATERFALL_MAX_ROWS

function pushWaterfallRow(raw) {
    const n = raw.length;
    if (n === 0) return;

    if (!waterfallBuffer || waterfallBuffer.width !== n) {
        waterfallBuffer = document.createElement("canvas");
        waterfallBuffer.width = n;
        waterfallBuffer.height = WATERFALL_MAX_ROWS;
        const bctx = waterfallBuffer.getContext("2d");
        bctx.fillStyle = "rgb(10,10,20)";
        bctx.fillRect(0, 0, n, WATERFALL_MAX_ROWS);
    }
    const bctx = waterfallBuffer.getContext("2d");

    // Shift the existing buffer down by one row, dropping the oldest.
    bctx.drawImage(waterfallBuffer, 0, 0, n, WATERFALL_MAX_ROWS - 1, 0, 1, n, WATERFALL_MAX_ROWS - 1);

    const row = bctx.createImageData(n, 1);
    for (let x = 0; x < n; x++) {
        const [r, g, b] = amplitudeToColor(raw.charCodeAt(x));
        const i = x * 4;
        row.data[i] = r;
        row.data[i + 1] = g;
        row.data[i + 2] = b;
        row.data[i + 3] = 255;
    }
    bctx.putImageData(row, 0, 0);

    const canvas = document.getElementById("waterfall-canvas");
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(waterfallBuffer, 0, 0, n, WATERFALL_MAX_ROWS, 0, 0, canvas.width, canvas.height);
}

document.getElementById("tune-button").addEventListener("click", () => {
    const mhz = parseFloat(document.getElementById("freq-input").value);
    if (!isNaN(mhz)) send({ cmd: "set_frequency", freq_hz: Math.round(mhz * 1e6) });
});

document.getElementById("mode-select").addEventListener("focus", () => { modeSelectFocused = true; });
document.getElementById("mode-select").addEventListener("blur", () => { modeSelectFocused = false; });
document.getElementById("mode-select").addEventListener("change", (e) => {
    send({ cmd: "set_control", key: "mode", value: e.target.value });
});

document.getElementById("ptt-button").addEventListener("click", () => {
    send({ cmd: "ptt", on: !pttActive });
});

connect();

// ---- RX audio streaming ----
// Opus-over-WebSocket, not WebRTC (see web_remote/routes_audio.py's
// own docstring for why) -- the audio WS connection's lifetime IS the
// enable/disable toggle. Decoding uses a vendored WASM Opus decoder
// (web_remote/static/vendor/opus-decoder/) rather than the simpler
// WebCodecs API so this works in browsers WebCodecs doesn't cover
// (Firefox, older Safari), per explicit requirement. Playback
// schedules each decoded chunk as its own AudioBufferSourceNode,
// back-to-back against a running "next start time" -- simple and
// sufficient at this stream's low (8kHz mono) bitrate, no
// AudioWorklet module needed.

let audioWs = null;
let audioContext = null;
let opusDecoder = null;
let nextPlaybackTime = 0;

async function enableAudio() {
    const status = document.getElementById("audio-status");
    const button = document.getElementById("audio-toggle");
    button.disabled = true;
    status.textContent = "starting...";

    if (!opusDecoder) {
        opusDecoder = new window["opus-decoder"].OpusDecoder();
        await opusDecoder.ready;
    }
    if (!audioContext) {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
    }
    await audioContext.resume();
    nextPlaybackTime = audioContext.currentTime;

    audioWs = new WebSocket(wsUrl(`/ws/radio/${RADIO_ID}/audio`));
    audioWs.binaryType = "arraybuffer";

    audioWs.onopen = () => {
        button.disabled = false;
        button.textContent = "Disable Audio";
        button.classList.add("active");
        status.textContent = "streaming";
    };
    audioWs.onclose = (ev) => {
        button.disabled = false;
        button.textContent = "Enable Audio";
        button.classList.remove("active");
        if (ev.code === 4405) status.textContent = "no RX audio device configured for this radio";
        else if (ev.code === 4406) status.textContent = "this radio's audio sample rate isn't Opus-compatible";
        else if (ev.code === 4401) status.textContent = "access token required";
        else status.textContent = "";
        audioWs = null;
    };
    audioWs.onerror = () => { if (audioWs) audioWs.close(); };
    audioWs.onmessage = (event) => playOpusFrame(new Uint8Array(event.data));
}

function disableAudio() {
    if (audioWs) audioWs.close();
}

function playOpusFrame(opusBytes) {
    let decoded;
    try {
        decoded = opusDecoder.decodeFrame(opusBytes);
    } catch (e) {
        return; // a single bad frame shouldn't kill the stream
    }
    const channelData = decoded.channelData[0];
    if (!channelData || channelData.length === 0) return;

    const buffer = audioContext.createBuffer(1, channelData.length, decoded.sampleRate);
    buffer.copyToChannel(channelData, 0);

    const source = audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(audioContext.destination);

    // If playback has fallen behind (tab was backgrounded, a burst of
    // frames arrived at once), resync to "now" rather than queuing an
    // ever-growing backlog of stale audio.
    const startAt = Math.max(nextPlaybackTime, audioContext.currentTime);
    source.start(startAt);
    nextPlaybackTime = startAt + buffer.duration;
}

document.getElementById("audio-toggle").addEventListener("click", () => {
    if (audioWs) disableAudio();
    else enableAudio();
});
