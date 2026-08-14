# Icom Radio Control (Radione) - v0.0.1a

A PySide6 GUI for controlling Icom radios (7300, 9700, 705) via [rigplane](https://pypi.org/project/rigplane/), with a dedicated worker thread for all async radio I/O.

Also included:

- **Ham Dashboard** - solar conditions (NOAA/hamqsl), a day/night world map, and SGP4-based satellite tracking with TLE/SatNOGS fetching.
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

You'll be prompted for connection details (serial or LAN) before the main window opens.

## Project layout

| File | Purpose |
| --- | --- |
| `main.py` | Entry point |
| `constants.py` | Bands, meter/control/level tables |
| `rig_discovery.py` | Shared "find the real method name" helper |
| `audio.py` | `AudioBridge` + Linux virtual-audio-cable helpers |
| `radio_worker.py` | `RadioWorker` (QThread owning the radio connection) |
| `connection_dialog.py` | `ConnectionDialog`, shown before the main window |
| `widgets.py` | `SpectrumWidget`, `WaterfallWidget`, `MeterWidget`, `TuningKnobWidget` |
| `wsjtx_rigctld.py` | WSJT-X launcher + `RigctldServer` |
| `solar_data.py` | NOAA/hamqsl fetching, `SolarDataWorker`, astronomy helpers |
| `world_map.py` | `WorldMapWidget` (Ham Dashboard's day/night map) |
| `satellite_tracking.py` | SGP4 propagation, TLE/SatNOGS fetching, satellite dialogs |
| `ham_dashboard.py` | `HamClockWindow`, ties the above three together |
| `main_window.py` | `RadioWindow`, the main application window |
| `theme.py` | Dark theme applied to the whole app |
