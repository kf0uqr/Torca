"""
PySide6 GUI for controlling Icom radios via rigplane, using a dedicated
worker thread per radio connection for all async radio I/O.

Install:
    pip install rigplane PySide6 sounddevice
Optional (Ham Dashboard satellite tracking):
    pip install sgp4

Run:
    python3 main.py

Why a worker thread instead of qasync:
    All rigplane calls are asyncio coroutines. RadioWorker (a QThread)
    owns its own asyncio event loop and never touches a single Qt
    widget directly -- it only ever talks to the GUI thread by
    emitting signals. Commands go the other way via
    asyncio.run_coroutine_threadsafe(), so calling worker.set_frequency(...)
    from a button click is safe and non-blocking.

App structure: Ham Dashboard is the central/main window -- it opens
directly (no radio connection required, no connection dialog at
startup) and is where radios get added ("Connect New Radio..."), each
assigned a satellite role (RADIO_ROLES in constants.py) in the
connection dialog. Multiple radios can be connected at once and
cooperate on one satellite pass via SatelliteSession, which
HamClockWindow owns.

Package layout (see each module's own docstring for details):
    constants.py         -- pure data: bands, roles, meter/control/level tables
    rig_discovery.py      -- shared "find the real method name" helper
    audio.py               -- AudioBridge + Linux virtual-audio-cable helpers
    radio_worker.py        -- RadioWorker (QThread owning one radio connection)
    connection_dialog.py   -- ConnectionDialog (shown per radio, from the dashboard)
    widgets.py              -- SpectrumWidget/WaterfallWidget/MeterWidget/TuningKnobWidget
    wsjtx_rigctld.py        -- WSJT-X launcher + RigctldServer
    solar_data.py            -- NOAA/hamqsl fetching, SolarDataWorker, astronomy helpers
    world_map.py             -- WorldMapWidget (Ham Dashboard's day/night map)
    satellite_tracking.py    -- SGP4 propagation, TLE/SatNOGS fetching, satellite dialogs
    satellite_session.py     -- SatelliteSession (shared Doppler-tracking coordinator)
    main_window.py            -- RadioWindow (one per connected radio)
    ham_dashboard.py           -- HamClockWindow (the central/main window)
    theme.py                    -- dark theme + window placement helper
    main.py                      -- this file: the entry point
"""

import logging
import os
import sys

from PySide6.QtWidgets import QApplication

from ham_dashboard import HamClockWindow
from operator_profile import OperatorProfileDialog
from theme import apply_dark_theme

# Diagnostic only -- TORCA_DEBUG_RIGPLANE=1 python3 main.py. Surfaces
# rigplane's own internal DEBUG logging (logging.getLogger("rigplane")),
# which includes lines like "civ-rx: active receiver -> SUB" whenever it
# detects an active-receiver change -- useful for seeing, in real time,
# whether something is flipping the active receiver back and forth
# during dual-receiver (9700/7610) operation, independent of anything
# this app's own code does. Off by default since it's fairly verbose.
if os.environ.get("TORCA_DEBUG_RIGPLANE"):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("rigplane").setLevel(logging.DEBUG)


def main():
    app = QApplication(sys.argv)
    apply_dark_theme(app)

    # First thing on every launch: who's operating, and from where.
    # Purely a QSettings side effect (operator_callsign/operator_lat/
    # operator_lon/operator_elevation_m) -- see operator_profile.py's
    # own docstring. Cancelling just proceeds with whatever was already
    # saved (or blank, on a genuinely first-ever run) rather than
    # blocking startup -- same non-blocking precedent as this app's
    # older single-field callsign prompt.
    OperatorProfileDialog.run()

    window = HamClockWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
