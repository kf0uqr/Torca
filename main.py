"""
PySide6 GUI for controlling an Icom radio via rigplane, using a
dedicated worker thread for all async radio I/O.

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

Package layout (see each module's own docstring for details):
    constants.py         -- pure data: bands, meter/control/level tables
    rig_discovery.py      -- shared "find the real method name" helper
    audio.py               -- AudioBridge + Linux virtual-audio-cable helpers
    radio_worker.py        -- RadioWorker (QThread owning the radio connection)
    connection_dialog.py   -- ConnectionDialog (shown before the main window)
    widgets.py              -- SpectrumWidget/WaterfallWidget/MeterWidget/TuningKnobWidget
    wsjtx_rigctld.py        -- WSJT-X launcher + RigctldServer
    solar_data.py            -- NOAA/hamqsl fetching, SolarDataWorker, astronomy helpers
    world_map.py             -- WorldMapWidget (Ham Dashboard's day/night map)
    satellite_tracking.py    -- SGP4 propagation, TLE/SatNOGS fetching, satellite dialogs
    ham_dashboard.py          -- HamClockWindow (ties the above three together)
    main_window.py            -- RadioWindow (the main application window)
    theme.py                   -- dark theme applied to the whole app
    main.py                     -- this file: the entry point
"""

import sys

from PySide6.QtWidgets import QApplication

from connection_dialog import ConnectionDialog
from main_window import RadioWindow
from theme import apply_dark_theme


def main():
    app = QApplication(sys.argv)
    apply_dark_theme(app)

    details = ConnectionDialog.get_details()
    if details is None:
        sys.exit(0)  # user cancelled -- exit quietly, nothing to clean up yet

    window = RadioWindow(details)
    window.resize(700, 620)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
