# TORCA Usage Guide

Step-by-step instructions for every feature in TORCA. See [README.md](README.md) for installation and requirements.

## Contents

- [First launch: Operator Profile](#operator-profile)
- [Connecting a radio](#connecting-a-radio)
- [The main radio window](#the-main-radio-window)
- [Split, RIT, and repeater tone](#split-rit-and-repeater-tone)
- [Memories](#memories)
- [Satellite tracking + Doppler correction](#satellite-tracking--doppler-correction)
- [Band-plan overlay](#band-plan-overlay)
- [Digital mode tools](#digital-mode-tools)
  - [CW](#cw-tool)
  - [RTTY](#rtty-tool)
  - [PSK31](#psk31-tool)
  - [SSTV](#sstv-tool)
  - [APRS](#aprs-tool)
- [QSO Log Book](#qso-log-book)
- [World map & overlays](#world-map--overlays)
- [Spot networks](#spot-networks)
  - [POTA](#pota)
  - [PSKReporter](#pskreporter)
  - [DX Cluster](#dx-cluster)
  - [Contest Calendar](#contest-calendar)
- [WSJT-X and JS8Call integration](#wsjt-x-and-js8call-integration)
- [Virtual Cables](#virtual-cables)
- [rigctld](#rigctld)
- [Updating](#updating)
- [Connecting a radio over the network](#connecting-a-radio-over-the-network)
- [Remote Access over the Internet](#remote-access-over-the-internet)

---

## Operator Profile

The first thing you see on every launch. Enter:

- **Callsign** -- used for PSKReporter lookups, DX cluster login, and CW/RTTY macros (`{MYCALL}`).
- **Name** / **Address** -- both optional. Address, if entered, makes **Get GPS Coordinates** look up your position by street address (via OpenStreetMap's Nominatim geocoder, typically street-level accurate) instead of the coarser IP-based lookup.
- **Latitude / Longitude / Elevation** -- your station location, used for satellite Doppler correction and the map marker. Elevation must be entered by hand (not available from either GPS lookup) and measurably improves Doppler accuracy on low passes.

Click **Save Profile...** to save the whole thing under a name, so a shared station or a home-vs-portable setup can switch with one click from the dropdown. The last-used profile loads automatically next time. You can reopen this dialog anytime from the Ham Dashboard's **Profile...** button.

## Connecting a radio

From the Ham Dashboard, click **Radios**, then **Connect New Radio...**. Pick:

- **Radio** -- IC-7300, IC-9700, IC-705, IC-7610, Xiegu X6200, or Yaesu FTX-1. **The last three (IC-7610, X6200, FTX-1) have not been tested against real hardware by this project** -- see [README.md's "Supported radios"](README.md#supported-radios) for what that means in practice before relying on one. Selecting FTX-1 hides the CI-V Address field (it uses Yaesu's own CAT protocol instead) and only offers a USB connection.
- **Satellite Role** -- Non-Sat (default), Satellite Full Duplex (one dual-receiver radio), or Satellite Downlink/Uplink (a pair of separate radios). Only roles that still make sense given already-connected radios are offered.
- **Connection** -- Network (LAN), USB (Serial), or Remote Server (see [Connecting a radio over the network](#connecting-a-radio-over-the-network)).
- **Audio Input/Output** -- which PortAudio devices to use for RX/TX, or "None" to skip audio entirely.

Click **Save Profile...** to save the whole setup under a name for next time. Each connected radio gets its own window, listed (with a double-click-to-focus shortcut) in the **Radios** dialog.

## The main radio window

Each connected radio's window has:

- **Spectrum scope + waterfall** -- click either to tune. Span/Ref/Speed controls above the scope (dual-receiver radios only) control the currently-scoped receiver.
- **Meters** -- double-click any meter to change what it displays (S-meter, SWR, ALC, power output, voltage, current, etc.). Power output is scaled to your specific radio's real rated max, not a generic guess.
- **Tuning knob** -- click-drag to spin; the step size combo next to it sets Hz-per-click.
- **Band buttons** -- one click recalls the band's last-used frequency (via the radio's own band-stacking register where supported) or tunes to its low edge.
- **PTT** -- push-to-talk. On a dual-receiver radio, an **Active: MAIN/SUB** toggle picks which receiver the knob/band buttons/PTT currently target.
- Tool launchers: **CW Tool...**, **RTTY Tool...**, **SSTV Tool...**, **APRS Tool...**, **Memories**.

## Split, RIT, and repeater tone

Right-click the **Split** control for a dedicated panel covering split operation and repeater tone (CTCSS tone/tone squelch) -- modeled on wfview's own Split panel. Every control there live-applies immediately, no OK/Cancel. The **RIT** knob (next to the main tuning knob) adjusts a receive-only offset independently of the transmit frequency.

## Memories

Opened via the **Memories** button. Each tab holds a list of named entries (VFO A/B frequency+mode, repeater tone):

- **Add Memory** -- captures the radio's *current* live VFO A/B and tone settings into a new named entry.
- **Recall** -- select exactly one entry and click Recall to tune the radio to it (VFO A, VFO B, and repeater tone, in one action).
- **Delete Memory** -- select one or more entries to remove.
- **+ CSV** -- adds a new tab populated by importing an entire repeater-list CSV file, no filtering, no API key needed. Download a search-results export from [RepeaterBook.com](https://www.repeaterbook.com)'s own website first, then pick that file here. Populates **Location** and **Modes** columns from the CSV alongside the usual frequency/mode/tone fields. **Refresh** on that tab re-imports a (possibly different) CSV file, replacing every entry.
- Every cell in the table is directly editable by double-clicking it.

Memories persist across radios and app restarts (one shared bank, not per-radio-model).

## Satellite tracking + Doppler correction

1. On the Ham Dashboard, click **Satellites** to turn tracking on. Right-click it to manage the tracked list: refresh TLEs from CelesTrak, add satellites by hand, fetch transponder data from SatNOGS DB, hand-edit transponders, and choose which satellites show on the map. This also populates **Upcoming Satellite Passes** with the next 10 AOS windows across whichever satellites are checked to show on the map -- a pass already in progress sorts to the top, showing time-to-LOS instead of time-to-AOS.
2. Double-click a satellite marker on the map, *or* a row in Upcoming Satellite Passes. Both start live tracking: pick a transponder from the dropdown that appears on the dashboard, and every satellite-role radio's downlink gets continuously Doppler-corrected, with elevation/azimuth/Doppler/time-to-AOS-or-LOS overlaid on its scope. The tuning knob on a tracking radio adjusts an offset from the transponder's nominal downlink (applied before Doppler correction) instead of the radio directly -- use it to tune within a linear satellite's passband or nudge onto where it's actually transmitting.
3. **Start/Stop Tracking** pauses/resumes re-tuning without losing the selection -- the satellite (and any tuning offset) stays put until you double-click a different one.
4. How PTT drives the uplink depends on each radio's role, resolved automatically:
   - **Satellite Full Duplex** (single dual-receiver radio, e.g. IC-9700): Main tracks the downlink while receiving; Sub takes over with the Doppler-corrected uplink for the duration of a transmission.
   - **Satellite Downlink** + **Satellite Uplink** (a pair of single-receiver radios): the downlink radio tracks continuously; the uplink radio's **Split** turns on automatically and **PTT** swaps in the Doppler-corrected uplink for the transmission, back to the downlink frequency the instant you release.

## Band-plan overlay

A thin colored strip directly under the waterfall, sharing its exact frequency axis: orange = CW-only, purple = CW/data, blue = phone, green = unrestricted, each segment labeled with the minimum license class needed there (sourced from FCC 47 CFR 97.301/97.305). Yellow tick marks show any saved Memory entries that fall within the visible span. Hover any segment or tick for details.

## Digital mode tools

Every tool below shares RX audio with normal listening (or an active Virtual Cable) -- you don't have to choose between them.

### CW Tool

Type text and click **Send** to key the radio's own built-in Morse keyer over CI-V (no local audio synthesis). Decode runs continuously once toggled on, using an adaptive tone detector -- lock the WPM manually if auto-detection is fighting noisy conditions. A macro bank gives one-click common CW phrases (`{MYCALL}` expands to your Operator Profile callsign).

### RTTY Tool

Same send/decode/macro shape as CW, but RTTY has no radio-side keyer -- send synthesizes AFSK mark/space tones locally and streams them out via PTT.

### PSK31 Tool

Same send/decode/macro shape as CW/RTTY, plus a **Tone (Hz)** field -- set it to match wherever you're actually tuned in the passband (e.g. the tone you see on the waterfall), since PSK31 has no fixed on-air audio frequency the way RTTY's mark/space pair does. Decode locks onto a signal automatically once one appears near that tone; it re-locks on its own if the signal drops out for a while (e.g. you retune).

### SSTV Tool

Decode-only: continuously updates as an image is received, auto-detecting the SSTV mode in use. No encode/send side.

### APRS Tool

Decode-only in one continuously-scrolling table, one row per packet, with full protocol coverage: position reports (plain and compressed), Mic-E, status, object/item, messages/acks, telemetry, weather, and third-party/network-tunneled (IGate-relayed) packets, each translated into plain English alongside the raw bytes. Right-click any row to copy its raw bytes for debugging a packet that isn't decoding correctly. **Send Packet...** builds and transmits a position report. A **Decoder** dropdown lets you switch to an external [direwolf](https://github.com/wb2osz/direwolf) TNC instead of the built-in decoder, if installed.

## QSO Log Book

Opened from the Ham Dashboard's **Log Book...** button. Lists every QSO from a local ADIF file (works fully with no QRZ account). Click a column header to sort; **Manage Columns...** picks which fields show.

- **New QSO...** -- opens a form auto-filled from a connected radio's live frequency/mode (and the active satellite/transponder, if tracking) -- everything stays editable.
- **Sync with QRZ** -- optional, two-way reconciliation with your QRZ.com Logbook (needs a QRZ Logbook API key, entered via **QRZ Settings...**).
- **Edit Selected** / **Delete Selected** -- edit or remove one or more selected rows (deleting also removes them from QRZ if synced).
- **WSJT-X Auto-Log** (Ham Dashboard button): once toggled on, every QSO WSJT-X logs (its own "Log QSO" dialog, OK) is logged here automatically over WSJT-X's UDP protocol -- no manual entry, works with any running WSJT-X instance whose UDP Server port matches (right-click the button to change TORCA's own listening port).

## World map & overlays

The Ham Dashboard's day/night map shows the terminator, sub-solar point, your own location, and (while satellite tracking is on) satellite markers with footprint circles and ground tracks. A **Sat** button under the zoom controls toggles the background between OpenStreetMap and satellite imagery. Toggle buttons add: **QSO Map** (your logged contacts), **POTA**, **PSKReporter**, and **APRS** station markers -- see below for each.

## Spot networks

### POTA

Toggle **POTA** to plot currently active Parks on the Air activator spots on the map -- right-click it for **Refresh Now** (spots go stale within POTA's own short expiry window, so this isn't on a timer). Separately, the **Parks** tab is a full browsable park directory (not just active ones): pick a program from its dropdown, search by name/reference, and use **Filter...** to narrow to parks within a radius of your saved location. Double-click a park for its details.

### PSKReporter

Toggle **PSKReporter** to plot recent reception reports involving your callsign (who heard you, or who you were heard by, depending on direction) -- configurable lookback window in its settings dialog.

### DX Cluster

A traditional telnet DX cluster feed (default: a public AK1A-format cluster). Enter a host/port if you want a different one, then **Connect** for a continuous, live stream of spots.

### Contest Calendar

A read-only table of upcoming contests (WA7BNM's own published calendar) -- double-click a row for details and a link.

## WSJT-X and JS8Call integration

**Launch WSJT-X** starts it in its own isolated profile (`--rig-name`), separate from your regular WSJT-X settings, so it won't collide with an existing setup. **Launch JS8Call** starts JS8Call directly (no isolated-profile flag -- it uses its own regular settings). Use **Rigctld...** (below) to let either one control a specific connected radio (point its own Radio settings at `127.0.0.1:<port>`, rig "Hamlib NET rigctl"). Both remember whichever executable path you locate the first time, and re-prompt (a file browser) if that path ever stops existing.

## Virtual Cables

Linux/PulseAudio or PipeWire only (needs `pactl`). Select a radio in the Radios list, then **Virtual Cables...** to independently choose which radio's RX (Main/Sub/mixed) feeds a virtual audio input, and which radio's TX pulls from a virtual audio output -- letting an external app (WSJT-X, fldigi, etc.) send/receive audio through the radio without touching real hardware devices. RX and TX can be different radios, e.g. decoding one radio's downlink while transmitting through a separate uplink radio.

## rigctld

Select a radio in the Radios list, then **Rigctld...** to start a minimal Hamlib `rigctld`-compatible TCP server for it -- point any CAT-aware app at `127.0.0.1:<port>` with rig model "Hamlib NET rigctl" (model 2). Each radio needs its own port if you're running more than one.

## Updating

**Check for Updates...** on the Ham Dashboard checks GitHub for a newer commit than what's currently running, and can update in place -- works the same whether you're running a dev checkout (`git pull` + dependency reinstall) or an `install.sh` install.

## Connecting a radio over the network

See [README.md](README.md#connecting-to-a-radio-over-the-network) for the full `torca-server` walkthrough (sharing a USB-only radio so any other TORCA instance can connect to it like a LAN radio).

## Remote Access over the Internet

See [README.md](README.md#remote-access-over-the-internet-cloudflare-tunnel) for the full Cloudflare Tunnel setup walkthrough. Once it's running, the **Remote Access...** button on the Ham Dashboard gives you a browser-based Ham Dashboard (with its own live map, and Satellites/QSO Map/PSKReporter/POTA/APRS overlay toggles) and, per connected radio, a control page (frequency/mode/PTT, meters, spectrum + waterfall, RX audio streaming) plus CW and APRS tool pages -- reachable both on your local network and, once set up, at your own domain over the internet.
