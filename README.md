# TORCA -- That One Radio Control App - v0.0.1a

A PySide6 GUI for controlling amateur radios via [rigplane](https://pypi.org/project/rigplane/) -- Icom CI-V (IC-7300, IC-9700, IC-7610, IC-705, Xiegu X6200) and Yaesu CAT (FTX-1) -- built around a Ham Dashboard (satellite tracking, world map, QSO log, spot networks) and one window per connected radio (spectrum/waterfall, meters, memories, and a full set of digital-mode tools), with a dedicated worker thread handling all async radio I/O. **See [Supported radios](#supported-radios) below -- three of these have never been tested against real hardware by this project.**

**See [USAGE.md](USAGE.md) for step-by-step instructions on every feature below.**

<img width="1920" height="1200" alt="image" src="https://github.com/user-attachments/assets/6e8cb9c8-2238-40f2-be9f-ccf2d1c92e72" />
<img width="1920" height="1200" alt="image" src="https://github.com/user-attachments/assets/a65dc651-961a-4ade-9faf-1b504b297bb1" />

## Features

**Multi-radio & satellite**
- Connect any number of radios at once, each its own window, each with a saveable/loadable named connection profile (radio model, network/USB settings, audio devices).
- Satellite tracking (SGP4, TLEs from CelesTrak, transponder data from SatNOGS DB) with live Doppler correction, an elevation/azimuth/Doppler/AOS-LOS overlay on the scope, and upcoming-pass predictions on the dashboard. Works across a single dual-receiver radio (Main/Sub) or a "poor man's full duplex" pair of separate uplink/downlink radios, coordinated by role.
- A band-plan overlay under the waterfall showing CW/data/phone segments and the minimum license class for each, sourced directly from FCC rules -- plus a toggleable satellite-imagery map layer.

**Digital mode tools** (each a per-radio window: send + live decode, sharing RX audio with normal listening)
- CW: send via the radio's own keyer, adaptive tone decode, macro bank, adjustable WPM lock.
- RTTY: AFSK send/decode, macro bank.
- PSK31: BPSK31 send/decode with an adjustable audio tone, macro bank.
- SSTV: continuous image decode (multiple modes auto-detected).
- APRS: full packet decode (position/Mic-E/compressed/status/object/item/messages/telemetry/weather/third-party-relayed), symbol translation, packet send, and an optional `direwolf`-backed decoder alongside the built-in one.

**Logging & spotting**
- A local QSO log book (plain ADIF file) with optional two-way QRZ.com Logbook sync, configurable columns, and auto-fill from a connected radio's live frequency/mode (and active satellite/transponder, if tracking).
- WSJT-X auto-logging via its own UDP broadcast -- no manual entry needed.
- POTA activator spots, PSKReporter reception reports, and a traditional DX cluster feed, all plotted on the world map.
- A read-only contest calendar (WA7BNM).

**Memories**
- App-side memory channels (VFO A/B frequency/mode + repeater tone), captured from a live radio or imported wholesale from a RepeaterBook CSV export -- no API key needed. Recall any entry back to the radio with one click.

**Radio control**
- Spectrum scope, waterfall, segmented meters, and a rotary tuning knob widget.
- Split mode, RIT, band buttons (with band-stacking-register recall), memory recall.
- Virtual Cables (Linux/PulseAudio or PipeWire) to route RX/TX audio to/from external apps.
- A built-in Hamlib `rigctld`-compatible server per radio, and one-click WSJT-X/JS8Call launch buttons that can point at it.

**Networking**
- `torca-server`: share a USB-only radio over the network so any other TORCA instance can connect to it like a LAN radio (frequency/mode/PTT/meters/levels/scope/audio all work the same).
- Remote Access: a browser-based Ham Dashboard (with live map) and per-radio control pages -- including RX audio streaming, CW/APRS tools, and satellite Doppler control -- reachable locally or over the internet at your own domain via a Cloudflare Tunnel. See [Remote Access over the Internet](#remote-access-over-the-internet-cloudflare-tunnel) below.

**General**
- An Operator Profile (callsign + location, saveable/loadable by name) shown at launch and reachable anytime, feeding satellite Doppler correction, the map marker, and PSKReporter/DX cluster/QSO logging.
- Solar-terrestrial conditions (NOAA/hamqsl) and HF band-condition estimates.
- Self-update in place from GitHub (dev checkout or `install.sh` install).
- Dark theme throughout.

## Supported radios

| Radio | Protocol | Connection | Status |
|---|---|---|---|
| IC-7300 | Icom CI-V | USB | Tested against real hardware |
| IC-9700 | Icom CI-V | Network (LAN) | Tested against real hardware |
| IC-705 | Icom CI-V | Network (LAN) | Tested against real hardware |
| IC-7610 | Icom CI-V | Network (LAN) or USB | **Not tested against real hardware** |
| Xiegu X6200 | Icom CI-V (subset) | USB | **Not tested against real hardware** |
| Yaesu FTX-1 | Yaesu CAT | USB | **Not tested against real hardware** |

The first three were added and verified against real radios over the course of this project (including real bugs found and fixed on real hardware -- see the commit history). **IC-7610, X6200, and FTX-1 were added purely because [rigplane](https://pypi.org/project/rigplane/) (the library this app is built on) already ships a working profile/backend for each of them -- none of the three has been connected to real hardware by this project.** Concretely, that means:

- Frequency/mode control, meters, memories, and the digital-mode tools *should* work per rigplane's own protocol implementation, but haven't been confirmed to actually work correctly end-to-end the way the tested radios have.
- Model-specific quirks that only show up on real hardware (wrong meter scaling, a control that silently no-ops, an audio format mismatch) won't have been caught yet -- this project already hit and fixed exactly this class of bug on the IC-705 (see the commit history for the power-meter scaling fix).
- The Xiegu X6200's own rigplane profile explicitly notes several capabilities (tone squelch, passband tuning) are disabled pending hardware confirmation Xiegu's own maintainers don't have either -- so some controls may simply not appear for it even once wired up correctly.
- The Yaesu FTX-1 uses a completely different wire protocol (Yaesu CAT, not CI-V) from every other supported radio, so it exercises an almost entirely separate code path in rigplane.

If you own one of these three radios, connecting it and reporting back (what works, what doesn't) is genuinely useful -- that's exactly how the first three radios went from "should work" to "confirmed working."

**Not currently supported at all: Xiegu X6100 and Lab599 TX-500.** Both have a declarative rigplane profile (`rigs/x6100.toml`, `rigs/tx500.toml`) describing their CI-V/CAT command sets, but neither has an actual backend implementation wired up in the installed rigplane version -- attempting to connect either one raises an immediate `Unsupported serial model` error (TX-500's own profile file even says so directly: "the TX-500 has no backend yet"). Revisit if/when rigplane ships real backends for them.

## Requirements

- Python 3.9+
- [PySide6](https://pypi.org/project/PySide6/) -- GUI framework
- [rigplane](https://pypi.org/project/rigplane/) -- radio control backend
- [sounddevice](https://pypi.org/project/sounddevice/) -- audio device listing and streaming (PortAudio wrapper)
- [sgp4](https://pypi.org/project/sgp4/) -- satellite propagation for satellite tracking
- [numpy](https://pypi.org/project/numpy/) -- DSP for the CW/RTTY/PSK31/SSTV/APRS decoders
- [fastapi](https://pypi.org/project/fastapi/) + [uvicorn](https://pypi.org/project/uvicorn/) -- Remote Access's embedded web server
- [opuslib](https://pypi.org/project/opuslib/) -- encodes RX audio for Remote Access's audio streaming

All required packages install via `pip install -r requirements.txt` (or automatically with `install.sh`). No external Hamlib install is needed -- the built-in `rigctld`-compatible server and WSJT-X bridge are self-contained.

### Optional

- **`pactl`** (PulseAudio, or PipeWire's `pipewire-pulse` compatibility layer) -- Linux only, needed for Virtual Cables. Nothing else requires it; the rest of the app runs the same without it.
- **[direwolf](https://github.com/wb2osz/direwolf)** -- an alternative APRS decode backend, selectable alongside the built-in decoder in the APRS Tool. Purely optional; the built-in decoder needs nothing extra.
- **`git`** -- only needed for self-update to work when running from a dev checkout (`install.sh` installs are updated the same way either way).
- A **QRZ.com XML/Logbook Data subscription** -- only needed for QSO Log Book sync; the local log itself works fully without one.

## Installation

### System-wide (Linux)

```bash
git clone https://github.com/kf0uqr/Torca.git
cd Torca
sudo ./install.sh
```

Installs the app and its own Python virtual environment to `/opt/torca`, and adds `torca`/`torca-server` commands to `/usr/local/bin` plus a desktop launcher entry. Re-run `sudo ./install.sh` any time to reinstall/upgrade (or use the in-app **Check for Updates...** button), or `sudo ./install.sh --uninstall` to remove everything it installed.

### Manual (any platform)

```bash
# 1. Clone the repository
git clone https://github.com/kf0uqr/Torca.git
cd Torca

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate        # On Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

## Running

```bash
torca            # if installed via install.sh
python main.py   # manual install
```

The first thing you'll see is the **Operator Profile** dialog (callsign + location) -- see [USAGE.md](USAGE.md#operator-profile) for details, including the GPS-from-address/IP lookup. After that, the Ham Dashboard opens; connect a radio from its **Radios** button whenever you're ready. Full walkthroughs for every feature -- satellite tracking, the digital-mode tools, QSO logging, memories, and more -- are in **[USAGE.md](USAGE.md)**.

## Connecting to a radio over the network

USB-only radios (e.g. IC-7300) normally require TORCA to run on the same machine they're plugged into. `torca-server` lifts that limit: run it on the machine physically connected to the radio, and connect to it from any other TORCA instance on the network as if it were a LAN radio -- frequency, mode, PTT, meters, levels, spectrum scope, and RX/TX audio all work the same as a direct connection.

Under the hood this is just [rigplane](https://pypi.org/project/rigplane/)'s own `web` server (`torca-server` is a thin wrapper around its `rigplane` CLI, already in TORCA's venv) -- no separate protocol or server of TORCA's own.

### 1. Share the radio

On the machine with the radio physically connected:

```bash
torca-server --backend serial --serial-port /dev/ttyUSB0 --model IC-7300 web    # installed via install.sh
./torca-server --backend serial --serial-port /dev/ttyUSB0 --model IC-7300 web  # manual/dev checkout
```

- **`--model` is required.** Without it, rigplane's serial backend silently defaults to `IC-7610` and talks to your radio with the wrong CI-V address -- it'll connect, but frequency reads back as 0 Hz and tuning commands are silently ignored. Match it to your actual radio (`IC-7300`, `IC-9700`, `IC-7610`, `IC-705`, `X6200`, `FTX-1`). See [Supported radios](#supported-radios) above -- `IC-7610`/`X6200`/`FTX-1` haven't been tested against real hardware by this project.
- Connection flags (`--backend`, `--serial-port`, `--model`, etc.) go *before* `web`, not after -- `web`'s own arguments only cover LAN radios (`--radio-host` etc.), not serial ones.
- Defaults to port 8080 with no auth token (LAN-trusted). Add `--auth-token YOURTOKEN` to require one, and `--port` to use a different port.
- Run `torca-server --help` (or `rigplane --help` / `rigplane web --help`) for the full flag list -- audio bridge options, TLS, etc.

### 2. Connect to it

In TORCA's connect dialog (on any machine on the network, including the same one), choose **Remote Server** as the connection type and enter:

- **Server Host** -- the sharing machine's IP or hostname (`127.0.0.1` if testing on the same machine)
- **Server Port** -- `8080` unless you changed it with `--port`
- **Auth Token** -- leave blank unless you set `--auth-token`

### VFO A/B flicker + audio glitch, and how torca-server disables it

rigplane's own "unselected VFO slot" refresh opportunistically swaps to VFO B, reads it, and swaps back via real CI-V commands roughly every 5 seconds per receiver, to keep VFO B's frequency/mode known for split display. Two side effects on a real radio: the display briefly flashes to VFO B and back, and -- on radios whose USB audio output follows the active VFO (confirmed on an IC-7300) -- RX audio streamed over the web audio channel briefly glitches on the same cadence.

rigplane has no public flag or config option to disable this (as of v2.11.1). `torca-server` disables it anyway: it runs through `torca_server.py`, a thin wrapper that patches the relevant internal (`RadioPoller._UNSELECTED_SLOT_INTERVAL`) before handing off to rigplane's own CLI unchanged. This relies on a private, underscore-prefixed rigplane internal, not a stable API -- if a future rigplane release restructures it, the patch just becomes a no-op (caught and reported, not fatal) and the flicker/glitch would return. The direct tradeoff: VFO B's frequency/mode goes stale in the server's own state, since the refresh that keeps it current is exactly what's disabled -- irrelevant unless something (e.g. a split-mode display) depends on it.

If you connect directly (serial/USB, not through `torca-server`) rather than via Remote Server, this patch doesn't apply and both side effects are still present.

## Remote Access over the Internet (Cloudflare Tunnel)

Remote Access serves a browser-based version of the Ham Dashboard and each connected radio -- reachable on your local network always, and over the internet at a hostname on your own domain (e.g. `torca.kf0uqr.com`) via a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/). It covers:

- The dashboard: connected radios, upcoming satellite passes, spot counts, recent QSOs (read-only), and a live map (day/night terminator, operator location, and POTA/PSKReporter/QSO/APRS/satellite markers -- each layer toggled the same as the desktop's own Satellites/QSO Map/PSKReporter/POTA/APRS buttons and band-filter dropdown, right there on the dashboard).
- A per-radio control page: frequency/mode/PTT, meters, a spectrum scope + waterfall (same color scale as the desktop), and an **Enable Audio** toggle streaming that radio's live RX audio to the browser (Opus over the same websocket -- works in any modern browser, including ones without WebCodecs support). The frequency/controls and meters sit side by side so the whole page fits a normal desktop browser window without scrolling (narrow/mobile screens stack them and scroll as usual).
- **CW and APRS tool pages** per radio (the two fully-hardware-tested digital-mode decoders) -- live decode text/packets, sending, and (CW) the macro bank, all sharing the exact same decode session the desktop tool window would use.
- **Satellite Doppler control** from the dashboard: pick a satellite/transponder, start/stop tracking, and nudge the manual offset -- goes through the exact same selection code path a real double-click on the desktop map does, so the desktop's own transponder dropdown/labels/tracking button stay in sync with whatever's selected from the web.

**Not yet available from the web**: RTTY/PSK31/SSTV tool pages (desktop-only for now -- those three decoders aren't confirmed against real hardware yet, see [Supported radios](#supported-radios)), Memories, full QSO log editing, and Virtual Cable management. Memories/logging are being held back until per-operator profiles exist.

### One-time setup

1. **Install `cloudflared`** (not a pip package -- see [Cloudflare's own install docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)) and log in once:
   ```bash
   cloudflared tunnel login
   ```
   This opens a browser to authorize `cloudflared` against your Cloudflare account (the one `kf0uqr.com`, or whichever domain you're using, is added to as a zone). One-time, outside this app -- TORCA has no way to drive an OAuth browser flow itself.
2. In TORCA, open the Ham Dashboard's **Remote Access...** button, fill in the local port (default `8765`), a tunnel name (default `torca`), and your hostname (e.g. `torca.kf0uqr.com`), then click **Run Cloudflare Setup...**. This creates the named tunnel if it doesn't already exist, routes that hostname's DNS to it, and writes TORCA's own tunnel config under `~/.torca/cloudflared/` -- entirely separate from `~/.cloudflared/config.yml` or any other tunnel already on the machine. Safe to re-run any time the port/hostname changes.
3. **Secure it with Cloudflare Access** (recommended, and assumed by this app's own security posture -- the local web server binds to `127.0.0.1` only, so it's reachable exclusively through the tunnel): in the Cloudflare dashboard, add an Access application for your hostname requiring a login (email OTP or your identity provider) before any request reaches TORCA at all. Done once, outside this app, the same way you'd configure Access for anything else on your account.

### Using it

Click **Start** in the same dialog -- this brings up the local web server and the tunnel together, and shows the public URL. The dialog also shows a randomly-generated **access token** (stored locally, regenerated only if cleared from QSettings) -- the first time a browser connects, it'll prompt for this token (via an inline banner, not a native popup) if it isn't already remembered for that browser. This token is defense-in-depth on top of Cloudflare Access, not a replacement for it -- don't rely on it alone if you skip the Access step above.

### Troubleshooting

- **`cloudflared tunnel login` says a certificate already exists.** It only ever authorizes ONE zone/domain at a time -- if you need to point it at a different domain than before, back up the existing `~/.cloudflared/cert.pem` (rename it, don't delete it) and re-run the login, picking the right zone in the browser step this time.
- **DNS resolves but the page 404s / loads the wrong thing through the tunnel.** `cloudflared tunnel route dns <name> <hostname>` silently no-ops if that hostname is already routed to a DIFFERENT tunnel (including one you don't otherwise use) -- it won't overwrite an existing route even with `--overwrite-dns`. Delete the stale DNS record for that hostname in the Cloudflare dashboard (find the right zone -- it may not be the one you expect) and re-run the route command.
- **Local (`127.0.0.1`) works but the public hostname doesn't, even though the DNS record and tunnel both look correct.** Cloudflare's own edge cache can be serving a stale copy of a static file from before you last updated Torca, ignoring the origin's `Cache-Control: no-store` header for common static extensions -- confirm with `curl -sI https://<hostname>/static/dashboard.js` and check `cf-cache-status`/`age`. Fix: Cloudflare dashboard -> **Caching -> Configuration -> Purge Cache**, and to stop it recurring on every future update, **Rules -> Cache Rules -> Create rule** matching your hostname with **Cache eligibility: Bypass cache**.
- **A freshly (re)started tunnel is unreachable for the first minute or so.** Normal -- a brand-new tunnel connector takes a short moment to propagate across Cloudflare's edge network. It should settle on its own within a minute or two; no action needed.

## Project layout

**Entry points / core**

| File | Purpose |
| --- | --- |
| `main.py` | Entry point -- shows the Operator Profile dialog, then opens the Ham Dashboard |
| `constants.py` | Bands, meter/control/level tables, radio profiles, satellite roles |
| `rig_discovery.py` | Shared "find the real method name" helper |
| `audio.py` | `AudioBridge` + Linux virtual-audio-cable (PulseAudio/PipeWire) helpers |
| `radio_worker.py` | `RadioWorker` (QThread owning one radio connection) |
| `theme.py` | Dark theme applied to the whole app |

**Connection & identity**

| File | Purpose |
| --- | --- |
| `connection_dialog.py` | `ConnectionDialog` -- radio connection details + saved profiles |
| `operator_profile.py` | `OperatorProfileDialog` -- callsign/location, saved profiles, shown at launch |
| `remote_radio.py` | `RemoteWebRadio` -- client for a radio shared over the network via `torca-server` |
| `torca_server.py` | Wrapper `torca-server` runs -- patches rigplane's periodic VFO-slot refresh, then dispatches to its own CLI |

**Ham Dashboard & main radio window**

| File | Purpose |
| --- | --- |
| `ham_dashboard.py` | `HamClockWindow` -- the central window: map, satellites, logging, spot networks, radio/Virtual Cable/rigctld management |
| `main_window.py` | `RadioWindow` -- the per-radio window: scope/waterfall, meters, band buttons, tool launchers |
| `widgets.py` | `SpectrumWidget`, `WaterfallWidget`, `BandPlanOverlayWidget`, `MeterWidget`, `TuningKnobWidget` |
| `world_map.py` | `WorldMapWidget` -- day/night map, satellite markers, QSO/POTA/PSKReporter/APRS overlays |
| `map_tiles.py` | OSM + satellite-imagery tile fetching/caching for the world map |
| `band_plan.py` | US band-plan data (CW/data/phone segments, license class) sourced from FCC rules |
| `split_dialog.py` | `SplitSettingsDialog` -- split/repeater-tone configuration |
| `memories_window.py` | `MemoriesWindow` -- app-side memory channels, Local Repeaters CSV import, Recall |
| `repeater_import.py` | Parses a RepeaterBook CSV export for the Local Repeaters tab |

**Satellite tracking**

| File | Purpose |
| --- | --- |
| `satellite_tracking.py` | SGP4 propagation, TLE/SatNOGS fetching, Doppler/look-angle/AOS-LOS math, satellite/transponder dialogs |
| `satellite_session.py` | `SatelliteSession` -- shared Doppler-tracking coordinator across however many radios are connected |

**Digital mode tools**

| File | Purpose |
| --- | --- |
| `cw.py` / `cw_window.py` | Morse decode/send engine + `CwToolWindow` |
| `rtty.py` / `rtty_window.py` | RTTY AFSK decode/send engine + `RttyToolWindow` |
| `psk31.py` / `psk31_window.py` | BPSK31 decode/send engine + `Psk31ToolWindow` |
| `sstv.py` / `sstv_window.py` | SSTV image decoder + `SstvToolWindow` |
| `aprs.py` / `aprs_window.py` | APRS/AX.25 packet decode/send engine + `AprsToolWindow` |
| `direwolf_backend.py` | Alternative APRS decode backend using the external `direwolf` TNC |

**Logging & spot networks**

| File | Purpose |
| --- | --- |
| `adif.py` | ADIF read/write, mode mapping |
| `qso_log.py` | Local QSO log (source of truth) + optional QRZ sync |
| `qrz_logbook.py` | QRZ.com Logbook API client |
| `log_book_window.py` | `LogBookWindow` -- the QSO log book UI |
| `new_qso_dialog.py` | `NewQsoDialog` -- add/edit a QSO, auto-filled from a live radio |
| `pota.py` | Parks on the Air activator-spot API client |
| `pskreporter.py` | PSKReporter reception-report query client |
| `dxcluster.py` | Traditional AK1A-protocol DX cluster telnet client |
| `contests.py` | WA7BNM contest calendar feed |
| `solar_data.py` | NOAA/hamqsl solar-terrestrial data fetching + astronomy helpers |

**WSJT-X / JS8Call integration & updates**

| File | Purpose |
| --- | --- |
| `wsjtx_rigctld.py` | WSJT-X + JS8Call launchers + `RigctldServer` (Hamlib-compatible) |
| `wsjtx_udp.py` | WSJT-X UDP protocol listener for auto-logging |
| `updater.py` | Self-update in place from GitHub |

**Remote Access**

| File | Purpose |
| --- | --- |
| `web_remote/server.py` | `RemoteWebServer` -- runs the FastAPI app on its own QThread/asyncio loop |
| `web_remote/app.py` | FastAPI app: dashboard/radio pages, polling websockets, token auth |
| `web_remote/bridge.py` | Cross-thread GUI-thread state caches/command bridges: `RadioRemoteState`, `CwRemoteState`, `AprsRemoteState`, `SatelliteRemoteState` |
| `web_remote/routes_tools.py` | CW/APRS tool page websockets |
| `web_remote/routes_satellite.py` | Satellite picker + Doppler start/stop/transponder/offset control |
| `web_remote/routes_audio.py` | RX audio streaming (Opus-encodes `AudioBridge`'s RX PCM, one radio at a time) |
| `web_remote/static/` | Page HTML/CSS/JS (no build step); `static/vendor/` has vendored Leaflet + opus-decoder (no CDN dependency) |
| `cloudflare_tunnel.py` | `CloudflareTunnel` + `CloudflareSetupWorker` -- Cloudflare Tunnel setup/start/stop |

## License

[MIT](LICENSE)
