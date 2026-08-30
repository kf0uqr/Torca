// CW Tool page: connects to /ws/radio/{id}/tool/cw (id parsed from the
// URL path -- /radio/{id}/tool/cw), attaching to the SAME decode
// session the desktop CW Tool window would use for this radio (see
// web_remote/bridge.py's CwRemoteState).

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
let wpmFocused = false;
let pitchFocused = false;

function showTokenBanner() {
    document.getElementById("token-banner").style.display = "flex";
}
function hideTokenBanner() {
    document.getElementById("token-banner").style.display = "none";
}

function connect() {
    const status = document.getElementById("conn-status");
    ws = new WebSocket(wsUrl(`/ws/radio/${RADIO_ID}/tool/cw`));

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

function render(state) {
    const decodeButton = document.getElementById("decode-toggle");
    decodeButton.textContent = state.decoding ? "Stop Decoding" : "Start Decoding";
    decodeButton.classList.toggle("active", state.decoding);

    if (!wpmFocused) document.getElementById("wpm-input").value = state.wpm;
    if (!pitchFocused) document.getElementById("pitch-input").value = state.pitch;

    const textEl = document.getElementById("decoded-text");
    const wasScrolledToBottom = textEl.scrollHeight - textEl.clientHeight <= textEl.scrollTop + 4;
    textEl.textContent = state.text;
    if (wasScrolledToBottom) textEl.scrollTop = textEl.scrollHeight;

    const macrosDiv = document.getElementById("macros");
    macrosDiv.innerHTML = (state.macros || []).map((m, i) =>
        `<button class="macro-button" data-index="${i}">${m.label || "(empty)"}</button>`
    ).join("");
    macrosDiv.querySelectorAll(".macro-button").forEach((button) => {
        button.addEventListener("click", () => {
            const macro = state.macros[parseInt(button.dataset.index, 10)];
            if (macro) send({ cmd: "send_text", text: macro.text });
        });
    });
}

document.getElementById("decode-toggle").addEventListener("click", () => {
    const decoding = document.getElementById("decode-toggle").classList.contains("active");
    send({ cmd: decoding ? "stop_decode" : "start_decode" });
});

document.getElementById("wpm-input").addEventListener("focus", () => { wpmFocused = true; });
document.getElementById("wpm-input").addEventListener("blur", () => { wpmFocused = false; });
document.getElementById("wpm-input").addEventListener("change", (e) => {
    send({ cmd: "set_wpm", wpm: parseInt(e.target.value, 10) });
});

document.getElementById("pitch-input").addEventListener("focus", () => { pitchFocused = true; });
document.getElementById("pitch-input").addEventListener("blur", () => { pitchFocused = false; });
document.getElementById("pitch-input").addEventListener("change", (e) => {
    send({ cmd: "set_pitch", hz: parseInt(e.target.value, 10) });
});

function sendTypedText() {
    const input = document.getElementById("send-input");
    if (input.value.trim()) {
        send({ cmd: "send_text", text: input.value });
        input.value = "";
    }
}
document.getElementById("send-button").addEventListener("click", sendTypedText);
document.getElementById("send-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendTypedText();
});

document.getElementById("token-submit").addEventListener("click", () => {
    localStorage.setItem("torca_token", document.getElementById("token-input").value.trim());
    connect();
});
document.getElementById("token-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("token-submit").click();
});

connect();
