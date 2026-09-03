// Per-radio control page: connects to /ws/radio/{id} (id parsed from
// the URL path), a poll-driven websocket (~5Hz, see web_remote/app.py)
// that resends this radio's full state each tick, and sends command
// messages back for tuning/mode/PTT.

const RADIO_ID = parseInt(location.pathname.split("/").pop(), 10);

document.getElementById("cw-tool-link").href = `/radio/${RADIO_ID}/tool/cw`;
document.getElementById("aprs-tool-link").href = `/radio/${RADIO_ID}/tool/aprs`;

// Hides "<- Dashboard" when loaded inside one of the dashboard's own
// radio tabs (dashboard.js's openRadioTab) -- that tab already has
// its own × close button, and navigating this link would otherwise
// load a second, redundant dashboard inside the iframe itself.
if (window.self !== window.top) {
    const dashboardLink = document.querySelector('header a[href="/"]');
    if (dashboardLink) dashboardLink.style.display = "none";
}

// ---- Tool pane (CW/APRS) ----
// Opening a tool used to navigate this whole page away, losing the
// radio controls entirely. Now it opens alongside instead, on the
// left, so the tool and the radio stay visible together -- exactly
// where depends on how THIS page is itself currently being shown:
//   - Embedded in one of the dashboard's own radio tabs (window.top
//     is the dashboard, this page is that tab's iframe): can't reach
//     across into a different document's DOM directly, so it posts a
//     message up asking the dashboard to open its own #tool-pane
//     instead (see dashboard.js's "message" listener) -- that hides
//     only the dashboard's left-hand tab column, leaving this radio's
//     own tab (already active, since only the active tab is
//     clickable) untouched on the right.
//   - Standalone (this page IS window.top, e.g. loaded directly or in
//     its own tab): no parent to ask, so it manages an equivalent
//     split of its own (#tool-pane/#radio-content in radio.html).
function openTool(url, label) {
    if (window.self !== window.top) {
        window.parent.postMessage({ type: "torca-open-tool", url, label }, window.location.origin);
        return;
    }
    document.getElementById("tool-pane-title").textContent = label;
    document.getElementById("tool-frame").src = url;
    document.getElementById("tool-pane").classList.add("open");
}

document.getElementById("cw-tool-link").addEventListener("click", (e) => {
    e.preventDefault();
    openTool(e.target.href, "CW Tool");
});
document.getElementById("aprs-tool-link").addEventListener("click", (e) => {
    e.preventDefault();
    openTool(e.target.href, "APRS Tool");
});
document.getElementById("tool-pane-close").addEventListener("click", () => {
    document.getElementById("tool-pane").classList.remove("open");
    document.getElementById("tool-frame").src = "about:blank";
});

function storedToken() {
    return localStorage.getItem("torca_token") || "";
}

function wsUrl(path) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${path}?token=${encodeURIComponent(storedToken())}`;
}

let ws = null;
let pttActive = false;
let dataModeActive = false;
let currentVfo = "A";
let currentActiveReceiver = 0; // 0=MAIN, 1=SUB
let modeSelectFocused = false;
let spanSelectFocused = false;
let spanLabelsRendered = 0; // guards against rebuilding <option>s (and losing focus/mid-pick state) every ~200ms poll tick
let currentMode = null;

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
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        // Command-rejection frames (server-side gating: viewer tried a
        // command, or a guest tried to transmit unsupervised/locked --
        // see web_remote/app.py's _handle_radio_commands) look like
        // {"error": "..."} with none of the full snapshot's own keys.
        if (data.error !== undefined && data.label === undefined) {
            showCommandError(data.error);
            return;
        }
        render(data);
    };
}

let commandErrorTimer = null;
function showCommandError(message) {
    const el = document.getElementById("command-error");
    el.textContent = message;
    el.className = "";
    clearTimeout(commandErrorTimer);
    commandErrorTimer = setTimeout(() => { el.textContent = ""; el.className = "empty"; }, 5000);
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

// ---- Role-based UI (operator/guest/viewer -- see web_remote/common.py) ----
// TX-capable controls (PTT, mic, freq/mode/level/band changes) are
// disabled client-side for a viewer and for an unsupervised/locked
// guest -- purely a UX nicety, since the server enforces the real gate
// (app.py's can_transmit()) regardless of what this does.
let currentRole = null;
let txAllowed = true; // set by renderRoleBanner -- read by the scope/waterfall canvas click-to-tune handlers below, which aren't plain form controls TX_CONTROL_IDS' el.disabled loop can gate

const TX_CONTROL_IDS = [
    "ptt-button", "atu-button", "mic-toggle", "mode-select", "data-mode-button",
    "vfo-button", "band-select", "tune-button", "freq-input", "span-select",
    "squelch-slider", "tx_level-slider", "rf_level-slider",
];

function renderRoleBanner(state) {
    currentRole = state.role || null;
    const banner = document.getElementById("role-banner");
    const text = document.getElementById("role-banner-text");
    const killButton = document.getElementById("kill-tx-button");
    const clearButton = document.getElementById("clear-lock-button");

    txAllowed = true;
    if (currentRole === "viewer") {
        banner.style.display = "flex";
        banner.classList.remove("role-banner-ok");
        text.textContent = "Read-only viewer session -- cannot transmit or change any setting.";
        killButton.style.display = "none";
        clearButton.style.display = "none";
        txAllowed = false;
    } else if (currentRole === "guest") {
        const supervised = !!state.operator_present;
        const locked = !!state.tx_locked;
        banner.style.display = "flex";
        banner.classList.toggle("role-banner-ok", supervised && !locked);
        if (locked) text.textContent = "TX locked by the control operator -- transmit disabled.";
        else if (supervised) text.textContent = "Supervised by a connected control operator -- OK to transmit.";
        else text.textContent = "No control operator connected -- transmit disabled (47 CFR 97.115 direct-supervision requirement).";
        killButton.style.display = "none";
        clearButton.style.display = "none";
        txAllowed = supervised && !locked;
    } else if (currentRole === "operator") {
        const locked = !!state.tx_locked;
        banner.style.display = locked ? "flex" : "none";
        banner.classList.toggle("role-banner-ok", false);
        text.textContent = locked ? "TX locked -- clear the lock to resume transmitting." : "";
        killButton.style.display = "";
        clearButton.style.display = locked ? "" : "none";
        txAllowed = !locked;
    } else {
        banner.style.display = "none";
    }

    for (const id of TX_CONTROL_IDS) {
        const el = document.getElementById(id);
        if (el) el.disabled = !txAllowed;
    }
}

document.getElementById("kill-tx-button").addEventListener("click", () => send({ cmd: "kill_tx" }));
document.getElementById("clear-lock-button").addEventListener("click", () => send({ cmd: "clear_tx_lock" }));

let currentFreqHz = null;

// After a digit-arrow click, the radio (real hardware, over serial/CI-V)
// takes a moment to actually retune -- until it does, the poll-driven
// state below still reports the old frequency. Blocking incoming
// freq_hz updates for a short window after each click lets the digit
// display keep responding instantly to further clicks (composing a
// multi-digit change) instead of being yanked back to the stale polled
// value every ~200ms; the radio tunes in the background regardless,
// and the spectrum/waterfall (not blocked) is how the user sees it
// land. Every click extends the window, so a burst of clicks stays
// smooth; once clicks stop, the next poll's real value takes over.
const FREQ_DISPLAY_SYNC_HOLDOFF_MS = 2000;
let freqSyncBlockedUntil = 0;

function render(state) {
    document.getElementById("radio-label").textContent = state.label || "Radio";
    renderRoleBanner(state);
    if (Date.now() >= freqSyncBlockedUntil) {
        currentFreqHz = state.freq_hz;
        renderFreqDisplay(state.freq_hz);
    }

    if (state.mode) currentMode = state.mode; // read by drawSpectrumBars for the passband overlay, independent of mode-select's own focus guard
    if (!modeSelectFocused && state.mode) {
        const select = document.getElementById("mode-select");
        if (select.value !== state.mode) select.value = state.mode;
    }

    renderSpanOptions(state.scope_span_labels || []);
    if (!spanSelectFocused && state.scope_span != null) {
        const spanSelect = document.getElementById("span-select");
        if (spanSelect.value !== String(state.scope_span)) spanSelect.value = String(state.scope_span);
    }

    pttActive = !!state.ptt;
    const pttButton = document.getElementById("ptt-button");
    pttButton.classList.toggle("active", pttActive);
    pttButton.textContent = pttActive ? "TRANSMITTING" : "PTT";

    // Tuner status: 0=off, 1=on, 2=tuning. null on radios with no
    // tuner (get_tuner_status() never succeeded) -- leaves the button
    // showing "ATU" but role-gated same as every other TX control,
    // same "just never does anything useful" fallback as the desktop.
    const tuning = state.tuner_status === 2;
    const atuButton = document.getElementById("atu-button");
    atuButton.classList.toggle("active", tuning);
    atuButton.textContent = tuning ? "Tuning..." : (state.tuner_status === 1 ? "ATU (ON)" : "ATU");
    if (tuning) atuButton.disabled = true; // on top of renderRoleBanner's role-based disable above

    dataModeActive = !!state.data_mode;
    document.getElementById("data-mode-button").classList.toggle("active", dataModeActive);

    if (state.vfo === "A" || state.vfo === "B") {
        currentVfo = state.vfo;
        document.getElementById("vfo-button").textContent = `VFO ${currentVfo}`;
    }

    const receiverButton = document.getElementById("active-receiver-button");
    receiverButton.style.display = state.is_dual_receiver ? "" : "none";
    if (state.is_dual_receiver && (state.active_receiver === 0 || state.active_receiver === 1)) {
        currentActiveReceiver = state.active_receiver;
        receiverButton.textContent = currentActiveReceiver === 1 ? "Active: SUB" : "Active: MAIN";
    }

    renderMeters(state.meters || {}, state.meter_defs || {});
    if (state.scope) renderScope(state.scope);

    renderLevels(state.levels || {});
    renderBandOptions(state.bands || []);
}

// ---- Band select ----
// Same RADIO_BANDS table (keyed by radio model) the desktop's own
// band buttons are built from, served fresh in every snapshot
// (app.py's radio_snapshot). This is an ACTION select, not a
// persistent-selection one -- there's no server-reported "current
// band" (the desktop itself only ever highlights a band button
// client-side, from the live frequency, never round-trips it), so
// picking an option sends select_band immediately and then resets
// back to the placeholder, the same one-shot semantics as a button
// click rather than a dropdown that stays "on" a value.
let radioBands = [];

function renderBandOptions(bands) {
    // Rebuilt only when the band list itself changes (effectively
    // once, right after the first snapshot arrives) -- not on every
    // ~200ms poll tick, so an open dropdown never gets yanked out from
    // under the user mid-pick (same discipline as the frequency digit
    // spinner and the level sliders).
    if (bands.length === radioBands.length && bands.every((b, i) => b.label === radioBands[i].label)) return;
    radioBands = bands;
    const select = document.getElementById("band-select");
    select.innerHTML = '<option value="">Band...</option>' +
        bands.map((b, i) => `<option value="${i}">${b.label}</option>`).join("");
}

// ---- Scope span select ----
// Unlike band-select, this DOES have a real server-reported current
// value (state.scope_span, from get_scope_span() polling), so it's
// reflected persistently rather than one-shot -- same
// focused-while-picking guard as mode-select above.
function renderSpanOptions(labels) {
    if (labels.length === spanLabelsRendered) return; // rebuilt only once, right after the first snapshot arrives
    spanLabelsRendered = labels.length;
    document.getElementById("span-select").innerHTML =
        labels.map((label, i) => `<option value="${i}">${label}</option>`).join("");
}

document.getElementById("span-select").addEventListener("focus", () => { spanSelectFocused = true; });
document.getElementById("span-select").addEventListener("blur", () => { spanSelectFocused = false; });
document.getElementById("span-select").addEventListener("change", (e) => {
    send({ cmd: "set_scope_span", index: Number(e.target.value) });
});

document.getElementById("band-select").addEventListener("change", (e) => {
    const index = e.target.value;
    if (index === "") return;
    const band = radioBands[Number(index)];
    e.target.value = "";
    if (!band) return;
    // Optimistic, same as the digit spinner/Tune button -- the radio's
    // band-stacking recall might land somewhere slightly different
    // than the bare low edge, which the next poll (once
    // FREQ_DISPLAY_SYNC_HOLDOFF_MS elapses) corrects for.
    currentFreqHz = band.low_hz;
    renderFreqDisplay(band.low_hz);
    freqSyncBlockedUntil = Date.now() + FREQ_DISPLAY_SYNC_HOLDOFF_MS;
    send({ cmd: "select_band", band_label: band.label, low_edge_hz: band.low_hz });
});

// ---- Level sliders (Squelch/TX Level/RF Level) ----
// Static range inputs (radio.html) -- render() only ever updates
// .value/text on them, and skips whichever one the user is currently
// dragging (levelDragging) so a poll tick can't yank the thumb out
// from under an in-progress drag, same "don't touch what the user's
// mid-interaction with" discipline as the frequency digit spinner and
// modeSelectFocused. Sends on every "input" event (continuous, like
// the desktop slider's valueChanged) -- RadioWorker already debounces
// writes to the radio server-side (LEVEL_DEBOUNCE_SECONDS).
const LEVEL_KEYS = ["squelch", "tx_level", "rf_level"];
const levelDragging = {};

function renderLevels(levels) {
    for (const key of LEVEL_KEYS) {
        if (levelDragging[key]) continue;
        const value = levels[key];
        if (value == null) continue;
        const percent = Math.round(value * 100);
        const slider = document.getElementById(`${key}-slider`);
        if (document.activeElement !== slider) slider.value = percent;
        document.getElementById(`${key}-value`).textContent = `${percent}%`;
    }
}

for (const key of LEVEL_KEYS) {
    const slider = document.getElementById(`${key}-slider`);
    slider.addEventListener("pointerdown", () => { levelDragging[key] = true; });
    slider.addEventListener("pointerup", () => { levelDragging[key] = false; });
    slider.addEventListener("input", () => {
        document.getElementById(`${key}-value`).textContent = `${slider.value}%`;
        send({ cmd: "set_level", key, value: Number(slider.value) / 100 });
    });
}

// ---- Per-digit frequency spinner ----
// An up arrow above and a down arrow below each digit of the
// displayed Hz value -- clicking one nudges the frequency by exactly
// that digit's place value (e.g. the "kHz" digit's arrows move 1000
// Hz at a time), like a classic rig's digit-by-digit tuning display.
// At least 9 digits (covers up to 999.999999 MHz) are always shown,
// growing automatically for anything higher (e.g. the IC-9700's
// optional 1.2GHz module) rather than truncating.
//
// Free tuning, same as the desktop's own frequency controls -- not
// clamped to any band. Worth reconsidering once band buttons land on
// this page (a likely next step): those will probably want the
// stepper to stay within the selected band's edges.

const FREQ_MIN_DIGITS = 9;

// The button elements are built exactly ONCE per digit-count (see
// buildFreqDigitButtons) and never touched again except for each
// digit <div>'s own textContent -- NOT rebuilt on every poll tick.
// An earlier version tore down and recreated the whole button set on
// every single state update (5/sec) -- functionally fine for a
// synthetic/instant click, but a real mouse click's mousedown-to-
// mouseup gap is long enough that a poll tick could swap the button
// out from under the click mid-interaction, so the browser's "click"
// event never fired at all -- confirmed as the actual cause of a real
// "arrows are in the right place but clicking does nothing" report
// (and matches a "ref is stale (element removed)" error hit during
// this feature's own testing, which should have been the tell at the
// time).
let freqDigitCount = 0;
let freqDigitTextEls = []; // index i => the <div class="freq-digit"> for that position

function renderFreqDisplay(freqHz) {
    const container = document.getElementById("freq-display");
    if (freqHz == null) {
        if (freqDigitCount !== 0) {
            container.innerHTML = '<span class="freq-empty">-- Hz</span>';
            freqDigitCount = 0;
            freqDigitTextEls = [];
        }
        return;
    }

    const rounded = Math.max(0, Math.round(freqHz));
    const digitCount = Math.max(FREQ_MIN_DIGITS, String(rounded).length);
    const digitChars = String(rounded).padStart(digitCount, "0").split("");

    if (digitCount !== freqDigitCount) {
        buildFreqDigitButtons(container, digitCount);
        freqDigitCount = digitCount;
    }
    digitChars.forEach((digitChar, i) => {
        freqDigitTextEls[i].textContent = digitChar;
    });
}

function buildFreqDigitButtons(container, digitCount) {
    container.innerHTML = "";
    freqDigitTextEls = [];
    for (let i = 0; i < digitCount; i++) {
        const placeValue = Math.pow(10, digitCount - 1 - i);

        const col = document.createElement("div");
        col.className = "freq-digit-col";

        const up = document.createElement("button");
        up.type = "button";
        up.className = "freq-digit-arrow freq-digit-up";
        up.textContent = "▲";
        up.setAttribute("aria-label", `Increase by ${placeValue} Hz`);
        up.addEventListener("click", () => bumpFrequencyDigit(placeValue));

        const digitEl = document.createElement("div");
        digitEl.className = "freq-digit";
        digitEl.textContent = "0";
        freqDigitTextEls.push(digitEl);

        const down = document.createElement("button");
        down.type = "button";
        down.className = "freq-digit-arrow freq-digit-down";
        down.textContent = "▼";
        down.setAttribute("aria-label", `Decrease by ${placeValue} Hz`);
        down.addEventListener("click", () => bumpFrequencyDigit(-placeValue));

        col.appendChild(up);
        col.appendChild(digitEl);
        col.appendChild(down);
        container.appendChild(col);

        // A thin gap every 3 digits from the right (thousands
        // grouping -- MHz/kHz/Hz), purely visual.
        const remaining = digitCount - 1 - i;
        if (remaining > 0 && remaining % 3 === 0) {
            const sep = document.createElement("div");
            sep.className = "freq-digit-sep";
            container.appendChild(sep);
        }
    }

    const unitLabel = document.createElement("div");
    unitLabel.className = "freq-unit-label";
    unitLabel.textContent = "Hz";
    container.appendChild(unitLabel);
}

function bumpFrequencyDigit(deltaHz) {
    if (currentFreqHz == null) return;
    const newFreq = Math.max(0, Math.round(currentFreqHz + deltaHz));
    currentFreqHz = newFreq;
    renderFreqDisplay(newFreq);
    freqSyncBlockedUntil = Date.now() + FREQ_DISPLAY_SYNC_HOLDOFF_MS;
    send({ cmd: "set_frequency", freq_hz: newFreq });
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

// Approximate occupied-bandwidth passband per mode (hz_below_tuned_
// freq, hz_above_tuned_freq) -- ported line-for-line from constants.
// MODE_BANDWIDTH_HZ (widgets.py's SpectrumWidget uses the same table
// for the desktop's own passband shading), so the web page matches
// the desktop's overlay exactly rather than reimplementing a rough
// guess at the same numbers.
const MODE_BANDWIDTH_HZ = {
    LSB: [2400, 0], USB: [0, 2400], AM: [3000, 3000], CW: [250, 250],
    CW_R: [250, 250], RTTY: [350, 350], RTTY_R: [350, 350],
    FM: [6000, 6000], WFM: [90000, 90000], DV: [3000, 3000],
};

let lastScopeFrame = null; // {start_freq_hz, end_freq_hz} -- read by the click-to-tune handlers on both canvases

function renderScope(scope) {
    lastScopeFrame = scope;
    const raw = atob(scope.pixels_b64);
    drawSpectrumBars(raw, scope);
    pushWaterfallRow(raw);
}

// Same x<->frequency mapping as widgets.py's SpectrumWidget._freq_to_x/
// _x_to_freq -- against the CSS-displayed width (canvas.clientWidth),
// not the canvas's internal pixel-buffer width (canvas.width, used
// only for bar-drawing resolution below), so click coordinates (which
// arrive in CSS pixels) line up correctly regardless of any zoom/
// scaling between the two.
function freqToX(freqHz, scope, width) {
    const { start_freq_hz: start, end_freq_hz: end } = scope;
    if (start === end) return null;
    return ((freqHz - start) / (end - start)) * width;
}

function xToFreq(x, scope, width) {
    if (!scope || width <= 0) return null;
    const { start_freq_hz: start, end_freq_hz: end } = scope;
    return start + (x / width) * (end - start);
}

function drawSpectrumBars(raw, scope) {
    const canvas = document.getElementById("scope-canvas");
    const ctx = canvas.getContext("2d");
    const n = raw.length;
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // Passband/bandwidth shading -- drawn BEFORE the amplitude bars so
    // the bars stay fully visible on top of it, same layering as the
    // desktop's SpectrumWidget.paintEvent.
    const bandwidth = currentMode && MODE_BANDWIDTH_HZ[currentMode];
    if (currentFreqHz != null && bandwidth) {
        const [belowHz, aboveHz] = bandwidth;
        let xLow = freqToX(currentFreqHz - belowHz, scope, canvas.width);
        let xHigh = freqToX(currentFreqHz + aboveHz, scope, canvas.width);
        if (xLow != null && xHigh != null) {
            if (xLow > xHigh) [xLow, xHigh] = [xHigh, xLow];
            xLow = Math.max(0, xLow);
            xHigh = Math.min(canvas.width, xHigh);
            if (xHigh > xLow) {
                ctx.fillStyle = "rgba(0, 150, 255, 0.18)";
                ctx.fillRect(xLow, 0, xHigh - xLow, canvas.height);
            }
        }
    }

    ctx.fillStyle = "#3c78c8";
    const barWidth = canvas.width / n;
    for (let i = 0; i < n; i++) {
        const amplitude = raw.charCodeAt(i); // 0x00-0xA0 per rigplane.scope.ScopeFrame
        const h = (amplitude / 160) * canvas.height;
        ctx.fillRect(i * barWidth, canvas.height - h, Math.max(1, barWidth), h);
    }

    // Tuning line -- drawn on top of both the shading and the bars,
    // same as the desktop.
    if (currentFreqHz != null) {
        const xTuned = freqToX(currentFreqHz, scope, canvas.width);
        if (xTuned != null && xTuned >= 0 && xTuned <= canvas.width) {
            ctx.strokeStyle = "rgba(255, 255, 255, 0.8)";
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(xTuned, 0);
            ctx.lineTo(xTuned, canvas.height);
            ctx.stroke();
        }
    }
}

function scopeCanvasClickToFreq(canvas, event) {
    if (!lastScopeFrame) return null;
    const rect = canvas.getBoundingClientRect();
    return xToFreq(event.clientX - rect.left, lastScopeFrame, rect.width);
}

// Click-to-tune on either canvas -- same contract as the desktop's
// SpectrumWidget/WaterfallWidget.frequency_clicked (mousePressEvent):
// a click anywhere in the plot retunes there, optimistically updated
// like the digit spinner/band-select/Tune button above.
function onScopeCanvasClick(event) {
    if (!txAllowed) return;
    const freqHz = scopeCanvasClickToFreq(event.currentTarget, event);
    if (freqHz == null) return;
    const rounded = Math.round(freqHz);
    currentFreqHz = rounded;
    renderFreqDisplay(rounded);
    freqSyncBlockedUntil = Date.now() + FREQ_DISPLAY_SYNC_HOLDOFF_MS;
    send({ cmd: "set_frequency", freq_hz: rounded });
}
document.getElementById("scope-canvas").addEventListener("click", onScopeCanvasClick);
document.getElementById("waterfall-canvas").addEventListener("click", onScopeCanvasClick);

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
    if (isNaN(mhz)) return;
    const newFreq = Math.round(mhz * 1e6);
    currentFreqHz = newFreq;
    renderFreqDisplay(newFreq);
    freqSyncBlockedUntil = Date.now() + FREQ_DISPLAY_SYNC_HOLDOFF_MS;
    send({ cmd: "set_frequency", freq_hz: newFreq });
});

document.getElementById("mode-select").addEventListener("focus", () => { modeSelectFocused = true; });
document.getElementById("mode-select").addEventListener("blur", () => { modeSelectFocused = false; });
document.getElementById("mode-select").addEventListener("change", (e) => {
    send({ cmd: "set_control", key: "mode", value: e.target.value });
});

document.getElementById("ptt-button").addEventListener("click", () => {
    send({ cmd: "ptt", on: !pttActive });
});

document.getElementById("atu-button").addEventListener("click", () => {
    send({ cmd: "tuner_start" });
});

document.getElementById("data-mode-button").addEventListener("click", () => {
    send({ cmd: "set_control", key: "data_mode", value: !dataModeActive });
});

document.getElementById("vfo-button").addEventListener("click", () => {
    send({ cmd: "set_control", key: "vfo", value: currentVfo === "A" ? "B" : "A" });
});

document.getElementById("active-receiver-button").addEventListener("click", () => {
    send({ cmd: "select_receiver", receiver: currentActiveReceiver === 0 ? 1 : 0 });
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

// ---- TX audio streaming (browser mic -> radio) ----
// Raw PCM16 mono 48kHz over the websocket, NOT Opus -- see
// routes_audio.py's ws_tx_audio for why (short push-to-talk bursts,
// not a continuous stream, don't justify vendoring a second WASM
// codec just for this). Captured via a ScriptProcessorNode --
// deprecated in favor of AudioWorklet, but still universally
// supported and far simpler here (no separate worklet module file to
// load), same "keep it simple and robust" call as RX's own choice not
// to bother with AudioWorklet either. Runs at whatever sample rate
// the browser's AudioContext actually uses (a hint, not something
// getUserMedia is required to honor) and is linearly resampled to
// exactly 48000 Hz client-side, since RadioWorker.push_tx_audio_pcm
// requires EXACTLY that rate.
//
// Captured continuously once armed, but only actually SENT over the
// wire while PTT is held (pttActive) -- both to avoid wasting upload
// bandwidth while idle, and because the server independently drops
// anything that arrives while PTT is off anyway (routes_audio.py
// checks live PTT state itself, not just trusting the client).

const TX_AUDIO_SAMPLE_RATE = 48000;
let micStream = null;
let micAudioContext = null;
let micProcessor = null;
let micWs = null;

function floatTo48kInt16(input, sourceRate) {
    if (sourceRate === TX_AUDIO_SAMPLE_RATE) {
        const out = new Int16Array(input.length);
        for (let i = 0; i < input.length; i++) out[i] = Math.max(-32768, Math.min(32767, Math.round(input[i] * 32768)));
        return out;
    }
    const ratio = sourceRate / TX_AUDIO_SAMPLE_RATE;
    const outLength = Math.round(input.length / ratio);
    const out = new Int16Array(outLength);
    for (let i = 0; i < outLength; i++) {
        const srcPos = i * ratio;
        const i0 = Math.floor(srcPos);
        const i1 = Math.min(i0 + 1, input.length - 1);
        const frac = srcPos - i0;
        const sample = input[i0] + (input[i1] - input[i0]) * frac;
        out[i] = Math.max(-32768, Math.min(32767, Math.round(sample * 32768)));
    }
    return out;
}

function teardownMic() {
    if (micProcessor) { micProcessor.disconnect(); micProcessor.onaudioprocess = null; micProcessor = null; }
    if (micAudioContext) { micAudioContext.close(); micAudioContext = null; }
    if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
}

async function enableMic() {
    const status = document.getElementById("mic-status");
    const button = document.getElementById("mic-toggle");
    button.disabled = true;

    try {
        micStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
    } catch (e) {
        status.textContent = `Microphone access denied or unavailable: ${e.message}`;
        button.disabled = false;
        return;
    }

    micWs = new WebSocket(wsUrl(`/ws/radio/${RADIO_ID}/tx_audio`));
    micWs.binaryType = "arraybuffer";

    micWs.onopen = () => {
        micAudioContext = new (window.AudioContext || window.webkitAudioContext)();
        const source = micAudioContext.createMediaStreamSource(micStream);
        // 4096 samples: a reasonable latency/CPU tradeoff for voice.
        micProcessor = micAudioContext.createScriptProcessor(4096, 1, 1);
        micProcessor.onaudioprocess = (event) => {
            if (!pttActive || !micWs || micWs.readyState !== WebSocket.OPEN) return;
            const pcm16 = floatTo48kInt16(event.inputBuffer.getChannelData(0), micAudioContext.sampleRate);
            micWs.send(pcm16.buffer);
        };
        source.connect(micProcessor);
        // ScriptProcessorNode only fires onaudioprocess once connected
        // all the way to a destination -- routed through a muted gain
        // node so the mic doesn't also get played back out the
        // speakers (a direct connect(destination) would do that).
        const silentGain = micAudioContext.createGain();
        silentGain.gain.value = 0;
        micProcessor.connect(silentGain);
        silentGain.connect(micAudioContext.destination);

        button.disabled = false;
        button.textContent = "Disable Mic";
        button.classList.add("active");
        status.textContent = "Mic armed -- streams while PTT is held.";
    };
    micWs.onclose = (ev) => {
        teardownMic();
        button.disabled = false;
        button.textContent = "Enable Mic";
        button.classList.remove("active");
        if (ev.code === 4401) status.textContent = "access token required";
        else if (ev.code === 4405) status.textContent = "not connected to the radio yet";
        else status.textContent = "TX audio streams to the radio only while PTT is held.";
        micWs = null;
    };
    micWs.onerror = () => { if (micWs) micWs.close(); };
}

function disableMic() {
    if (micWs) micWs.close();
}

document.getElementById("mic-toggle").addEventListener("click", () => {
    if (micWs) disableMic();
    else enableMic();
});
