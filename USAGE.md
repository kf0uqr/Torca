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
  - [FT8 / FT4](#ft8--ft4-tool)
  - [WSPR](#wspr-tool)
- [QSO Log Book](#qso-log-book)
- [World map & overlays](#world-map--overlays)
- [Band Conditions](#band-conditions)
- [Spot networks](#spot-networks)
  - [POTA](#pota)
  - [PSKReporter](#pskreporter)
  - [DX Cluster](#dx-cluster)
  - [Contest Calendar](#contest-calendar)
- [WSJT-X and JS8Call integration](#wsjt-x-and-js8call-integration)
- [Audio](#audio)
- [Virtual Cables](#virtual-cables)
- [rigctld](#rigctld)
- [Updating](#updating)
- [Connecting a radio over the network](#connecting-a-radio-over-the-network)
- [Remote Access](#remote-access)

---

## Operator Profile

The first thing you see on every launch. Enter:

- **Callsign** -- used for PSKReporter lookups, DX cluster login, and CW/RTTY macros (`{MYCALL}`).
- **Name** / **Address** -- both optional. Address, if entered, makes **Get GPS Coordinates** look up your position by street address (via OpenStreetMap's Nominatim geocoder, typically street-level accurate) instead of the coarser IP-based lookup.
- **Latitude / Longitude / Elevation** -- your station location, used for satellite Doppler correction and the map marker. Elevation must be entered by hand (not available from either GPS lookup) and measurably improves Doppler accuracy on low passes.

Click **Save Profile...** to save the whole thing under a name, so a shared station or a home-vs-portable setup can switch with one click from the dropdown. The last-used profile loads automatically next time. You can reopen this dialog anytime from the Ham Dashboard's **Profile...** button.

## Connecting a radio

From the Ham Dashboard, click **Radios**, then **Connect New Radio...**. Pick:

- **Radio** -- IC-7300, IC-9700, IC-705, IC-7610, Xiegu X6200, Icom ID-5100, or Yaesu FTX-1. **IC-7610, X6200, ID-5100, and FTX-1 have not been tested against real hardware by this project** -- see [README.md's "Supported radios"](README.md#supported-radios) for what that means in practice before relying on one. Selecting FTX-1 hides the CI-V Address field (it uses Yaesu's own CAT protocol instead) and only offers a USB connection. ID-5100 is USB-only and only supports FM/FM-N/AM/AM-N/DV modes with no scope/DSP controls -- it's a 2m/70cm mobile, not an SDR-class rig.
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
- Tool launchers: **CW Tool...**, **RTTY Tool...**, **SSTV Tool...**, **APRS Tool...**, **FT8 Tool...**, **Memories**.

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

### FT8 / FT4 Tool

Decode **and make contacts** in FT8 (15 s slots) and FT4 (7.5 s slots) straight from the radio's audio -- no WSJT-X needed. Set the radio to **USB** (or USB-D) on an FT8/FT4 frequency (FT8 is commonly 14.074 MHz on 20 m), pick the mode, and press **Start Decoding**. After each time slot the decoded messages are added to the table: dB, the message, its audio frequency and DT (seconds from the slot's start), under a time line (such as 14:32:15) that marks the start of that slot. Messages calling **CQ** are green, and anything involving your own callsign (from your operator profile) is amber. Right-click a row to copy the message.

**Two lists, side by side** (drag the divider to resize): **Band activity** on the left holds everything decoded; **Your messages** on the right (as in WSJT-X, with its own UTC column rather than time lines) holds the messages addressed to you (amber), everything decoded on your receive frequency (the green marker on the waterfall), and the ones you transmit (red, marked Tx), so a contact in progress reads like a conversation. Double-click a message in either to answer it.

**The waterfall** at the bottom of the tool shows the radio's audio from 0 to 3000 Hz, newest at the top, with a green **Rx** and a red **Tx** marker bracketing the width of a signal (about 50 Hz for FT8). **Left-click** sets the receive frequency, **right-click** sets the transmit frequency, and **double-click** sets both (the Tx box follows, and typing in it moves the red marker). Decodes on the receive frequency are shown in bold. Double-clicking a decoded message to answer it also listens on that station's frequency and, unless **Hold Tx freq** is ticked, calls on it too, as WSJT-X does.

**Making a contact.** Double-click a decoded message to answer it (or press **Call CQ**) and TORCA runs the conversation for you: each message from the other station picks what to send next (grid, report, R-report, RR73, 73), it is sent in the next slot of the right parity, and the finished contact is logged to the Log Book (and uploaded to QRZ if you have that set up). The line under the buttons shows the message that will be sent next -- you can edit it before the slot. Set the **Tx** audio frequency to a quiet spot in the passband and the **Level** so the radio's ALC barely moves (FT8 needs a clean signal: use USB-D/data audio input and reduced power). **Halt TX** stops at once, closing the tool halts it too, and TX switches itself off after 6 minutes with nothing heard for the contact. Transmitting needs your callsign set in **Profile**; the grid sent is worked out from your profile's location. You are responsible for transmitting only where and when your licence allows.

- **Your computer's clock matters.** FT8/FT4 are timed to the UTC clock, so it must be accurate to about a second (use NTP). The first slot after pressing Start is partial and is skipped, so wait for the second one.
- The **dB** column is the decoder's estimate and is not directly comparable to WSJT-X's SNR.
- Decoding runs the bundled [ft8_lib](https://github.com/kgoba/ft8_lib) decoder, compiled when TORCA is installed (or updated). If it couldn't be built -- no `make`/C compiler -- the tool says so and offers **Build Decoder** (Debian/Ubuntu: `sudo apt install build-essential`).
- It does not transmit, and does not show a waterfall; use the main radio window's scope for that, or WSJT-X if you want to work stations.

### WSPR Tool

Listens for WSPR beacons (two-minute slots starting on even minutes) and lists each station heard: UTC, signal (dB), DT, frequency, drift, callsign, grid, power (dBm and watts) and its distance from you. Choose a **Band** and press **Tune Radio** to put the radio on that band's standard WSPR dial frequency in USB (14.0956 MHz on 20 m, and so on), then **Start Decoding**. Results arrive about two minutes after each slot starts, so the first take a few minutes; your computer's clock must be accurate to about a second. Stations that sent a grid are plotted on the map (PSKReporter overlay, **Stations you are hearing**) -- they are not sent to PSKReporter; WSPR has its own network, WSPRnet.

**Uploading to WSPRnet.** Tick **Upload spots to WSPRnet** to send each station the tool decodes to wsprnet.org, so your station shows up as a receiver there. It is off until you turn it on, and TORCA first tells you what becomes public: your callsign, your 6-character grid square (from your profile) and each station heard with its frequency, signal and power. Beacons that didn't carry a grid or power (hashed callsigns) can't be uploaded and are skipped. The status line shows how many spots have gone up this session, or why one didn't. The tool is receive-only: no transmitting.

Decoding uses `wsprd`, the decoder that comes with WSJT-X, run as a separate program (nothing of WSJT-X is copied into TORCA). Install WSJT-X (Debian/Ubuntu: `sudo apt install wsjtx`); without it the tool says so and Start Decoding stays disabled.

## QSO Log Book

Opened from the Ham Dashboard's **Log Book...** button. Lists every QSO from a local ADIF file (works fully with no QRZ account). Click a column header to sort; **Manage Columns...** picks which fields show.

- **New QSO** (in a workspace's **+** menu) -- opens a form, as a tab, auto-filled from a connected radio's live frequency/mode (and the active satellite/transponder, if tracking) -- everything stays editable.
- **Sync with QRZ** -- optional, two-way reconciliation with your QRZ.com Logbook (needs a QRZ Logbook API key, entered via **QRZ Settings...**).
- **Edit Selected** / **Delete Selected** -- edit or remove one or more selected rows (deleting also removes them from QRZ if synced).
- **WSJT-X Auto-Log** (button at the top of the Log Book): once toggled on, every QSO WSJT-X logs (its own "Log QSO" dialog, OK) is logged here automatically over WSJT-X's UDP protocol -- no manual entry, works with any running WSJT-X instance whose UDP Server port matches (use **UDP Port...** next to it to change TORCA's own listening port).

## World map & overlays

The Ham Dashboard's day/night map shows the terminator, sub-solar point, your own location, and (while satellite tracking is on) satellite markers with footprint circles and ground tracks. A **Sat** button under the zoom controls toggles the background between OpenStreetMap and satellite imagery. Toggle buttons add: **QSO Map** (your logged contacts), **POTA**, **PSKReporter**, and **APRS** station markers -- see below for each.

### VOACAP propagation overlay

The **VOACAP** button (in the map's filters sidebar) paints on the map how likely each place on Earth is to be reachable from **your station** on the band chosen in the box below it, for the current UTC hour, month and sunspot number: transparent where there is no chance, then red, yellow and green as the predicted **reliability** rises (the share of days in the month, at this hour, that the signal is strong enough). A key at the bottom left of the map says which band and conditions it shows. Pick another band from the box and the picture changes (about a second the first time; pictures already worked out are kept). It refreshes on its own when the UTC hour changes.

Right-click the button to say what you are doing and with what: **voice, CW, or FT8** (the weakest signal each needs -- FT8 works far below voice), your **power** and **antenna gain** (the prediction treats it as the same at every angle; default 100 W and 3 dBi, a typical wire), and the **noise** where the signal is received. Change them and the picture follows. It is a model of the ionosphere, not a guarantee, and it assumes your antenna and noise are as you tell it.

It needs VOACAP itself, the US government (NTIA/ITS) HF propagation model, which TORCA runs as a separate program: on Debian/Ubuntu `sudo apt install voacapl` (it brings its data files too). Without it, turning the overlay on says so. It needs your location set in **Profile**. (The map stays on the HF bands, 80 m to 10 m, which is what VOACAP covers.)

## Band Conditions

Open **Band Conditions** from a workspace's **+** menu. It shows, top to bottom: the **HF conditions** (Good / Fair / Poor by band group, day and night -- N0NBH's calculation); the **VHF conditions** from the same feed -- aurora and 2 m / 4 m / 6 m sporadic-E -- with the space-weather readings behind them (aurora index and how far south it reaches, solar-wind speed, Bz, K and A index, X-ray level, protons); and **The Sun now**: one picture from NASA's Solar Dynamics Observatory at a time, with **◀ ▶** buttons to step through nine views -- the 193 Å corona (dark coronal holes), 304 Å chromosphere (prominences, flare regions), visible light (sunspots), a magnetogram, and the 171, 211, 335, 94 and 131 Å extreme-ultraviolet views (loops, active regions, flares). Each has a line explaining what it shows. The pictures come from [Helioviewer](https://helioviewer.org), which serves SDO's newest images within minutes of them being taken (the extreme-ultraviolet ones are typically 20-30 minutes old; visible light and the magnetogram run a few hours behind); if Helioviewer can't be reached TORCA falls back to NASA's own "latest" pictures, which can go stale. Under each picture it says how old it is and where it came from, and warns if it is hours old rather than letting an old picture pass for the current Sun. All nine are fetched when the tab is shown, so stepping through is instant, and again every 15 minutes while it stays open (or press **Refresh**).

## Spot networks

### POTA

Toggle **POTA** to plot currently active Parks on the Air activator spots on the map -- right-click it for **Refresh Now** (spots go stale within POTA's own short expiry window, so this isn't on a timer). Separately, the **Parks** tab is a full browsable park directory (not just active ones): pick a program from its dropdown, search by name/reference, and use **Filter...** to narrow to parks within a radius of your saved location. Double-click a park for its details.

### PSKReporter

Toggle **PSKReporter** to plot recent reception reports involving your callsign (who heard you, or who you were heard by, depending on direction) -- configurable lookback window in its settings dialog.

The stations your own decoders hear are also remembered for 24 hours, even across restarts, so the map still shows them when PSKReporter's query service has less than its website does (it can hold older reports back).

If PSKReporter can't be reached, or says it is being asked too often, the layer stays on: the button reads **PSKReporter: ON (server unavailable)** (hover for the reason), a one-time message says the server's spots are missing, and the map keeps showing the stations your own decoders have heard. TORCA then leaves the server alone for five minutes if it said "too many requests"; turn the layer off and on to try again.

With the direction set to **Stations you are hearing** (or **Both**), the stations your FT8/FT4 tool decodes are plotted straight away, as you hear them (a station appears once a message from it has told TORCA its grid square; the tooltip shows band, mode, signal and time).

**Filtering the map by mode.** In the same settings dialog, **Modes shown** picks which modes are plotted: FT8, FT4, WSPR, and Other (everything else the server reports, such as PSK31). The band filter beside the map buttons still applies too.

**Reporting what you hear.** Right-click the PSKReporter button and tick **Report the stations I hear to PSKReporter** to also send those decodes to pskreporter.info, so you show up as a receiver there and your reports appear on PSKReporter's own map. It is off until you turn it on, and TORCA first tells you what becomes public: your callsign, your location as a 6-character grid square from your profile, and each station heard with its frequency, signal and time. Reports go out every 30 seconds or so while there is something to send (PSKReporter ignores a spot whose time is more than about a minute old, so they can't wait), a station is reported at most once per ten minutes per band, and anything still queued is sent when you quit. Spots can take a minute or two to appear on pskreporter.info. Only the FT8/FT4 tool reports for now (WSPR spots go to WSPRnet, see the WSPR tool), and it needs the radio's frequency (a connected radio), and your callsign and location set in Profile.

### DX Cluster

A traditional telnet DX cluster feed (default: a public AK1A-format cluster). Enter a host/port if you want a different one, then **Connect** for a continuous, live stream of spots.

### Contest Calendar

A read-only table of upcoming contests (WA7BNM's own published calendar) -- double-click a row for details and a link.

## WSJT-X and JS8Call integration

**Launch WSJT-X** starts it in its own isolated profile (`--rig-name`), separate from your regular WSJT-X settings, so it won't collide with an existing setup. **Launch JS8Call** starts JS8Call directly (no isolated-profile flag -- it uses its own regular settings). Use **Rigctld...** (below) to let either one control a specific connected radio (point its own Radio settings at `127.0.0.1:<port>`, rig "Hamlib NET rigctl"). Both remember whichever executable path you locate the first time, and re-prompt (a file browser) if that path ever stops existing.

## Audio

Every connected radio's audio goes to one place first -- its own audio hub -- which feeds every decoder tool (CW, RTTY, PSK31, SSTV, APRS, FT8), the recorder and the web remote at full level, all at once, whether or not you hear anything and whichever audio device you chose when connecting. (Before, a radio connected with no audio device chosen could feed only one decoder at a time.)

What you HEAR is separate. Open the **Audio** tab from a workspace's **+** menu: each radio has a level meter (the audio its decoders are being fed, so you can see at a glance whether a radio is delivering audio, and "no audio for N s" if it stops), the number of tools using it, and its own **Play on** device (the system default, any sound card or virtual cable, or **Silent** -- nothing played, decoders unaffected), **Volume** and **Mute**. Choices are remembered per radio. A radio whose audio comes from a sound card (cable-connected, e.g. truSDX) is metered but not played by TORCA.

## Virtual Cables

Linux/PulseAudio or PipeWire only (needs `pactl`). In a radio's tab, click **Virtual Cables...** (below SWR Protect) to independently choose which radio's RX (Main/Sub/mixed) feeds a virtual audio input, and which radio's TX pulls from a virtual audio output -- letting an external app (WSJT-X, fldigi, etc.) send/receive audio through the radio without touching real hardware devices. RX and TX can be different radios, e.g. decoding one radio's downlink while transmitting through a separate uplink radio.

## rigctld

In a radio's tab, click **Rigctld...** (below SWR Protect) to start a minimal Hamlib `rigctld`-compatible TCP server for it -- point any CAT-aware app at `127.0.0.1:<port>` with rig model "Hamlib NET rigctl" (model 2). Each radio needs its own port if you're running more than one.

## Updating

**Check for Updates...** on the Ham Dashboard checks GitHub for a newer commit than what's currently running, and can update in place -- works the same whether you're running a dev checkout (`git pull` + dependency reinstall) or an `install.sh` install.

## Connecting a radio over the network

See [README.md](README.md#connecting-to-a-radio-over-the-network) for the full `torca-server` walkthrough (sharing a USB-only radio so any other TORCA instance can connect to it like a LAN radio).

## Remote Access

TORCA serves a browser-based Ham Dashboard (with its own live map and Satellites/QSO Map/PSKReporter/POTA/APRS overlay toggles) and, per connected radio, a control page (frequency/mode/PTT, meters, spectrum + waterfall, RX audio streaming) plus CW and APRS tool pages, as a **local web app**. Open **Remote Access...** from a workspace's **+** menu, choose the port and whether to listen on **this computer only** or the **local network**, and click **Start**; the dialog shows the address to open and the operator / guest / viewer tokens. Getting to it from outside your network is done by software of your own in front of that port (a reverse proxy, VPN or tunnel) -- TORCA no longer sets up or manages a Cloudflare Tunnel. See [README.md](README.md#remote-access-a-local-web-app) for details and cautions.
