# Icom Radio Control (Radione) - v0.0.1a

A PySide6 GUI for controlling Icom radios (7300, 9700, 705) via [rigplane](https://pypi.org/project/rigplane/), with a dedicated worker thread for all async radio I/O.

Also included:

- **Connection profiles** - save a whole setup (radio, network/USB settings, audio devices, location) under a name in the Connect dialog, and reload it from a dropdown next time instead of re-entering everything. Handy for e.g. a home station vs. a portable/field-day setup.
- **Ham Dashboard** - solar conditions (NOAA/hamqsl), a day/night world map, and SGP4-based satellite tracking with TLE (CelesTrak) and transponder (SatNOGS DB) fetching. Stores every known transponder per satellite, editable by hand too.
- **Satellite Doppler correction** - double-click a tracked satellite on the Ham Dashboard map to start live tracking in the *main* window: pick one of its stored transponders from a dropdown there, and VFO A is continuously re-tuned to track that transponder's downlink through its Doppler shift (real SGP4 orbital velocity against your set location), with elevation/azimuth/Doppler/time-to-AOS-or-LOS overlaid on the spectrum scope, next to the frequency readout. Double-click another satellite any time to switch -- tracking keeps running rather than blocking either window.
- **Upcoming satellite passes** - the Ham Dashboard lists the next 10 AOS windows across every satellite checked to display on the map, soonest first, each with a live countdown, max elevation, and pass duration.
- **Split mode** - a toggle next to the VFO A/B button.
- **WSJT-X bridge** - launch WSJT-X/JTDX/fldigi into an isolated profile and drive it through this app's radio connection via a built-in Hamlib `rigctld`-compatible server (no external Hamlib install required).
- **Audio streaming** - mic/speaker audio to and from the radio via rigplane's `AudioTransport`, plus Linux/PulseAudio virtual-audio-cable helpers.
- Spectrum, waterfall, meter, and tuning-knob widgets, and a dark theme throughout.

<img width="1920" height="1200" alt="image" src="https://github.com/user-attachments/assets/6e8cb9c8-2238-40f2-be9f-ccf2d1c92e72" />
<img width="1920" height="1200" alt="image" src="https://github.com/user-attachments/assets/a65dc651-961a-4ade-9faf-1b504b297bb1" />

## Requirements

- Python 3.9+
- [PySide6](https://pypi.org/project/PySide6/) - GUI framework
- [rigplane](https://pypi.org/project/rigplane/) - radio control backend
- [sounddevice](https://pypi.org/project/sounddevice/) - audio device listing and streaming (PortAudio wrapper)
- [sgp4](https://pypi.org/project/sgp4/) - satellite propagation for the Ham Dashboard's satellite tracking feature

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/kf0uqr/irc.git
cd irc

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate        # On Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

## Running

```bash
python main.py
```

You'll be prompted for connection details (serial or LAN), plus your location (lat/lon/elevation, used for satellite Doppler correction), before the main window opens. Click **Save Profile...** in that dialog to save the whole setup under a name for next time -- the last-used profile (or a fresh one you pick from the dropdown) loads automatically.

Location can be entered by hand, or looked up approximately from your public IP via **Get GPS Coordinates (from IP)** -- that's an IP-geolocation lookup, not a real GPS fix, so it's typically only accurate to city level. Elevation always has to be entered manually and measurably improves Doppler accuracy on low satellite passes.

### Using satellite tracking + Doppler correction

1. In the Ham Dashboard, click **Satellites** to turn tracking on; right-click the button to manage the tracked list (refresh TLEs from CelesTrak, add satellites by hand, fetch transponder data from SatNOGS DB for every tracked satellite, or hand-edit transponders, and choose which satellites show on the map). This also populates **Upcoming Satellite Passes**, next to HF Band Conditions, with the next 10 passes across whichever satellites are checked to show on the map -- any pass already in progress right now sorts to the top, showing time until it sets instead of time until it starts.
2. Double-click a satellite marker on the map, *or* a row in Upcoming Satellite Passes -- both do the same thing. Tracking starts in the *main radio window*, not the dashboard: pick a transponder from the dropdown that appears there, and VFO A gets continuously Doppler-corrected while elevation/azimuth/Doppler shift/time-to-AOS-or-LOS show as an overlay on the spectrum scope. The tuning knob adjusts an offset from the transponder's nominal downlink (applied before Doppler correction) instead of the radio directly while tracking's running -- use it to tune within a linear satellite's passband or nudge to better match where it's actually transmitting.
3. **Start/Stop Tracking** pauses and resumes re-tuning without losing the selection -- the satellite (and any tuning offset) stays put until you double-click a different one on the map.
4. How PTT drives the uplink depends on the radio, detected automatically at connect time: on a single-receiver radio (IC-7300/IC-705), **Split** (next to VFO A/B) turns on automatically while tracking runs, and **PTT** swaps VFO B in with a Doppler-corrected *uplink* for the transmission and back to VFO A's downlink the instant you release. On a dual-receiver radio (IC-9700/IC-7610), Main tracks the downlink while receiving and Sub takes over with the Doppler-corrected uplink for the duration of a transmission -- only whichever one is actually in use gets touched, so the radio stays put on it instead of the two flickering back and forth.

## Project layout

| File | Purpose |
| --- | --- |
| `main.py` | Entry point |
| `constants.py` | Bands, meter/control/level tables |
| `rig_discovery.py` | Shared "find the real method name" helper |
| `audio.py` | `AudioBridge` + Linux virtual-audio-cable helpers |
| `radio_worker.py` | `RadioWorker` (QThread owning the radio connection) |
| `connection_dialog.py` | `ConnectionDialog` (connection details, location, saved profiles), shown before the main window |
| `widgets.py` | `SpectrumWidget`, `WaterfallWidget`, `MeterWidget`, `TuningKnobWidget` |
| `wsjtx_rigctld.py` | WSJT-X launcher + `RigctldServer` |
| `solar_data.py` | NOAA/hamqsl fetching, `SolarDataWorker`, astronomy helpers |
| `world_map.py` | `WorldMapWidget` (Ham Dashboard's day/night map, satellite markers) |
| `satellite_tracking.py` | SGP4 propagation, TLE/SatNOGS fetching, Doppler/look-angle/AOS-LOS math, satellite/transponder dialogs |
| `ham_dashboard.py` | `HamClockWindow`, ties the above three together |
| `main_window.py` | `RadioWindow`, the main application window |
| `theme.py` | Dark theme applied to the whole app |
