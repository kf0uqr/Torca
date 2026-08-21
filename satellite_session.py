"""
SatelliteSession: the single shared source of truth for "which satellite/
transponder is selected, what's the manual tuning offset, is tracking
running" -- owned by HamClockWindow (ham_dashboard.py), which is the one
place that exists independently of any radio connection.

Before this, that state lived as instance attributes directly on
RadioWindow (main_window.py) -- fine when there was only ever one radio,
but with multiple radios now able to cooperate on one satellite pass
(e.g. a separate Satellite Uplink radio and a separate Satellite Downlink
radio), each RadioWindow computing its own copy independently would let
them disagree about what's even being tracked. This class computes it
ONCE per tick (via satellite_tracking.compute_satellite_state, a pure
function with no radio I/O) and dispatches the result to every currently-
registered radio, which then does whatever its own role calls for.

Role dispatch itself lives on RadioWindow (see main_window.py's
apply_satellite_tick/apply_satellite_mode) -- this class only decides
WHEN to compute and WHO to tell, not HOW a given role acts on it. Roles:

- "full_duplex": one dual-receiver radio (e.g. IC-9700) handling both
  uplink and downlink itself -- today's original single-radio behavior,
  unchanged mechanics, just now driven from here instead of owning its
  own timer/transponder combo.
- "downlink": continuously retuned to the Doppler-corrected downlink on
  every tick; never transmits.
- "uplink": left alone on every periodic tick (mirrors how a dual-
  receiver radio's Main is left alone between transmissions) -- PTT
  press on that radio computes a fresh uplink frequency via
  current_state() and retunes right then, bundled atomically with the
  PTT command itself, exactly like the existing full_duplex/single-radio
  PTT pattern already does.
- "non_sat": never registered with this class at all -- a plain,
  standalone connection, completely unaffected by satellite tracking.
"""

from PySide6.QtCore import QObject, Signal, QTimer

from satellite_tracking import compute_satellite_state

SATELLITE_TRACKING_INTERVAL_MS = 2000


class SatelliteSession(QObject):
    """See module docstring. Construct one per app run (HamClockWindow
    owns it); RadioWindow instances register/unregister as they connect/
    close."""

    # (satellite, look, crossing_text, downlink_doppler_hz,
    #  uplink_doppler_hz, warning_text) -- for whoever renders the
    # tracking overlay (HamClockWindow).
    state_updated = Signal(object, object, str, object, object, str)
    tracking_changed = Signal(bool)  # emitted on start()/stop()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._observer_lat = None
        self._observer_lon = None
        self._observer_elevation_km = 0.0
        self._active_satellite = None
        self._selected_transponder = None
        self._freq_offset_hz = 0
        self._cached_crossing = None
        self._radios = {}  # RadioWindow -> role string ("full_duplex"/"downlink"/"uplink")

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def set_observer_location(self, lat, lon, elevation_m):
        """Called by HamClockWindow at startup (from QSettings) and again
        after each successful new-radio connection (ConnectionDialog may
        have just updated the saved location)."""
        self._observer_lat = lat
        self._observer_lon = lon
        self._observer_elevation_km = (elevation_m or 0.0) / 1000.0

    # ---- registry ----

    def register(self, window, role):
        """role must not be "non_sat" -- callers should simply not
        register those radios at all (see module docstring)."""
        self._radios[window] = role
        if self._active_satellite is not None:
            window.apply_satellite_mode(self._selected_transponder)

    def unregister(self, window):
        self._radios.pop(window, None)

    # ---- satellite/transponder selection ----

    def start(self, satellite):
        """(Re)starts tracking a satellite -- replaces whatever was
        active before. Transponder selection is separate (set_
        transponder) since the transponder combo is populated from the
        satellite's own stored list after this is called."""
        self._active_satellite = satellite
        self._selected_transponder = None
        self._freq_offset_hz = 0
        self._cached_crossing = None
        self._timer.start(SATELLITE_TRACKING_INTERVAL_MS)
        self.tracking_changed.emit(True)

    def stop(self):
        """Pauses re-tuning -- the satellite/transponder selection stays
        as-is until either resumed (start() again) or replaced, matching
        the pre-refactor behavior of the RadioWindow method this was
        extracted from."""
        self._timer.stop()
        self.tracking_changed.emit(False)

    def is_tracking(self):
        return self._timer.isActive()

    def set_transponder(self, transponder):
        self._selected_transponder = transponder
        self._freq_offset_hz = 0  # only meaningful relative to the transponder it was dialed in against
        for window in list(self._radios):
            window.apply_satellite_mode(transponder)
        self._tick()  # reflect the new choice immediately, don't wait for the timer

    def adjust_offset(self, delta_hz):
        self._freq_offset_hz += delta_hz
        self._tick()  # apply immediately, don't wait for the timer

    # ---- on-demand fresh computation (PTT) ----

    def current_state(self):
        """Fresh computation for PTT press/release, which needs the
        retune bundled atomically with the PTT command itself rather
        than reused from the last periodic tick (see main_window.py's
        _on_ptt_toggled for why -- an independently-scheduled retune
        arriving after PTT already started keying was a confirmed real
        bug earlier this session). Returns (look, crossing_text,
        downlink_hz, downlink_doppler_hz, uplink_hz, uplink_doppler_hz,
        base_downlink_hz, base_uplink_hz), or None if there's no active
        satellite or propagation fails."""
        if self._active_satellite is None:
            return None
        result = compute_satellite_state(
            self._active_satellite, self._selected_transponder,
            self._observer_lat, self._observer_lon, self._observer_elevation_km,
            self._freq_offset_hz, self._cached_crossing,
        )
        if result is None:
            return None
        *state, crossing = result
        self._cached_crossing = crossing
        return tuple(state)

    # ---- tick ----

    def _tick(self):
        satellite = self._active_satellite
        if satellite is None:
            return
        state = self.current_state()
        if state is None:
            self.state_updated.emit(satellite, None, "Orbit propagation failed (invalid TLE?)", None, None, "")
            return
        (look, crossing_text, downlink_hz, downlink_doppler_hz, uplink_hz, uplink_doppler_hz,
         base_downlink_hz, base_uplink_hz) = state

        warnings = []
        for window in list(self._radios):
            warning = window.apply_satellite_tick(
                downlink_hz, downlink_doppler_hz, uplink_hz, uplink_doppler_hz,
                base_downlink_hz, base_uplink_hz,
            )
            if warning:
                warnings.append(warning)
        warning_text = "  " + "; ".join(warnings) if warnings else ""

        self.state_updated.emit(satellite, look, crossing_text, downlink_doppler_hz, uplink_doppler_hz, warning_text)
