"""
RadioRemoteState: a small QObject created on the GUI thread (right
alongside each RadioWindow, see ham_dashboard.py's
_on_connect_radio_clicked) that keeps a plain-dict cache of a
RadioWorker's last-known state, updated by direct Qt signal
connections. Those connections run their slots on the GUI thread
(same thread this object is constructed on), so the writes into
self.state are GUI-thread-only -- but reads of that same dict from
another thread (the Remote Access web server's own QThread, see
server.py) are safe without a lock: CPython's GIL makes a single
dict-key get/set atomic, the same "plain attribute is safe enough to
read cross-thread" reasoning this app already relies on elsewhere
(e.g. RadioWindow._current_freq_hz, read directly by RigctldServer's
callbacks). This is a best-effort snapshot, not a transactionally
consistent one -- fine for a status display, same as every other
polling readout in this app.

PTT is the one piece of state that has NO RadioWorker signal at all
(main_window.py's ptt_button is purely UI-driven -- see its own
_on_ptt_toggled). request_ptt() below both calls the actual thread-safe
RadioWorker.start_ptt()/stop_ptt() (safe to call from any thread, not
just the GUI thread -- see radio_worker.py's own use of
asyncio.run_coroutine_threadsafe internally) AND re-checks the desktop
ptt_button via a queued Qt signal connected directly to its
setChecked slot -- connecting a Signal to a QWidget's own slot and
emitting it from a different thread than the widget's is the standard,
supported way to safely mutate a widget cross-thread (Qt resolves the
connection to Qt.QueuedConnection automatically based on thread
affinity), avoiding a direct cross-thread .setChecked() call.
"""

from PySide6.QtCore import QObject, Signal


class RadioRemoteState(QObject):
    _set_ptt_widget = Signal(bool)

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.state = {
            "freq_hz": window._current_freq_hz,
            "mode": None,
            "ptt": False,
            "meters": {},
            "meter_defs": {},
            "scope": None,
        }
        window.worker.frequency_updated.connect(self._on_freq)
        window.worker.control_updated.connect(self._on_control)
        window.worker.meter_updated.connect(self._on_meter)
        window.worker.meters_ready.connect(self._on_meter_defs)
        window.worker.scope_frame_received.connect(self._on_scope_frame)
        self._set_ptt_widget.connect(window.ptt_button.setChecked)

    def _on_freq(self, hz):
        self.state["freq_hz"] = hz

    def _on_control(self, key, value):
        self.state[key] = value

    def _on_meter(self, meter_type, value):
        self.state["meters"][meter_type] = value

    def _on_meter_defs(self, definitions):
        # Static metadata (label/unit/kind/raw_max/display_max) -- sent
        # once so the browser knows how to scale/label each bar, same
        # information MeterWidget itself is configured from.
        self.state["meter_defs"] = {
            key: {k: v for k, v in defn.items() if k != "getter" and k != "getter_candidates"}
            for key, defn in definitions.items()
        }

    def _on_scope_frame(self, frame):
        import base64
        self.state["scope"] = {
            "receiver": frame.receiver,
            "start_freq_hz": frame.start_freq_hz,
            "end_freq_hz": frame.end_freq_hz,
            "pixels_b64": base64.b64encode(frame.pixels).decode("ascii"),
        }

    def request_frequency(self, freq_hz: int):
        self.window.worker.set_frequency(int(freq_hz))

    def request_control(self, key: str, value):
        self.window.worker.set_control_value(key, value)

    def request_ptt(self, on: bool):
        self.state["ptt"] = bool(on)
        if on:
            self.window.worker.start_ptt()
        else:
            self.window.worker.stop_ptt()
        self._set_ptt_widget.emit(bool(on))


class CwRemoteState(QObject):
    """GUI-thread cache + queued-signal command bridge for one radio's
    CwToolWindow (cw_window.py) -- lets Remote Access's CW web page
    show/drive the SAME decode session the desktop tool window uses
    (see ham_dashboard.py's _on_connect_radio_clicked, which eagerly
    but invisibly constructs the tool window via
    RadioWindow.ensure_cw_tool_window() specifically so this can attach
    to it), rather than a second, independent one -- avoids two decode
    sessions fighting over AudioBridge's RX tap (even though that's
    now a list, not a single slot, two CW decoders on the same radio
    still wouldn't make sense).

    Same discipline as RadioRemoteState: constructed on the GUI thread,
    plain-dict cache updated by direct signal connections (GIL-safe to
    read cross-thread), and any WRITE to a QWidget goes through a
    queued Signal->slot connection rather than touching the widget
    directly from the web server's own thread. Toggling
    decode_toggle_button this way re-runs the desktop window's own
    _on_decode_toggled (it's connected to that button's toggled signal
    already) -- so starting/stopping decode from the web calls the
    exact same RadioWorker.start_cw_decode/stop_cw_decode the desktop
    button would, no duplicated logic. Sending text is a direct
    RadioWorker.send_cw_text() call instead (safe from any thread, see
    RadioRemoteState's own docstring) -- no need to route it through
    the window's send button at all.

    Macro editing (add/rename/delete) stays desktop-only for this
    pass -- the web page can list and SEND existing macros (a GIL-safe
    read of the window's already-loaded self._macros list), not create
    new ones."""

    _set_decoding = Signal(bool)
    _set_wpm = Signal(int)
    _set_pitch = Signal(int)

    def __init__(self, cw_window, parent=None):
        super().__init__(parent)
        self.window = cw_window
        self.state = {
            "text": "",
            "decoding": cw_window.decode_toggle_button.isChecked(),
            "wpm": cw_window.known_speed_spin.value(),
            "pitch": cw_window.pitch_spin.value(),
        }
        cw_window._decoded_text_ready.connect(self._on_text)
        cw_window.decode_toggle_button.toggled.connect(self._on_decoding_toggled)
        cw_window.known_speed_spin.valueChanged.connect(self._on_wpm_changed)
        cw_window.pitch_spin.valueChanged.connect(self._on_pitch_changed)

        self._set_decoding.connect(cw_window.decode_toggle_button.setChecked)
        self._set_wpm.connect(cw_window.known_speed_spin.setValue)
        self._set_pitch.connect(cw_window.pitch_spin.setValue)

    # Caps how much decoded text a browser client can pull in one
    # snapshot -- unbounded growth over a long decode session would
    # otherwise mean an ever-larger JSON payload every poll tick.
    _MAX_BUFFERED_TEXT = 20000

    def _on_text(self, text):
        self.state["text"] = (self.state["text"] + text)[-self._MAX_BUFFERED_TEXT:]

    def _on_decoding_toggled(self, checked):
        self.state["decoding"] = checked

    def _on_wpm_changed(self, value):
        self.state["wpm"] = value

    def _on_pitch_changed(self, value):
        self.state["pitch"] = value

    def macros(self):
        """GIL-safe plain-list read of the window's already-loaded
        macro bank -- see class docstring for why this is read-only
        from the web side."""
        return list(self.window._macros)

    def request_decoding(self, on: bool):
        self._set_decoding.emit(bool(on))

    def request_wpm(self, wpm: int):
        self._set_wpm.emit(int(wpm))

    def request_pitch(self, hz: int):
        self._set_pitch.emit(int(hz))

    def request_send(self, text: str):
        self.window._radio_window.worker.send_cw_text(text)


class AprsRemoteState(QObject):
    """GUI-thread cache + queued-signal command bridge for one radio's
    AprsToolWindow (aprs_window.py) -- same reuse-the-desktop-session
    reasoning as CwRemoteState above.

    Sending a position report does NOT go through
    SendAprsPacketDialog's widgets at all -- aprs.build_position_
    packet_pcm() is a pure function (confirmed via its own docstring:
    "this function only produces audio bytes, it never touches a
    radio"), so the web bridge calls it directly with the operator's
    saved location (QSettings, same source the dialog itself defaults
    from) and pushes the result through RadioWorker.send_tx_audio_pcm()
    -- a direct RadioWorker call, safe from any thread, no widget
    touch needed for sending at all. Manual lat/lon override from the
    web isn't exposed this pass (see the approved plan) -- it always
    sends from the operator's configured location."""

    _set_decoding = Signal(bool)
    _set_backend_index = Signal(int)

    def __init__(self, aprs_window, parent=None):
        super().__init__(parent)
        self.window = aprs_window
        self.state = {
            "packets": [],
            "decoding": aprs_window.decode_toggle_button.isChecked(),
            "backend_index": aprs_window.backend_combo.currentIndex(),
        }
        aprs_window.packet_decoded.connect(self._on_packet)
        aprs_window.decode_toggle_button.toggled.connect(self._on_decoding_toggled)
        aprs_window.backend_combo.currentIndexChanged.connect(self._on_backend_changed)

        self._set_decoding.connect(aprs_window.decode_toggle_button.setChecked)
        self._set_backend_index.connect(aprs_window.backend_combo.setCurrentIndex)

    # Caps how many decoded packets a browser client sees in one
    # snapshot -- same "don't let a long session's JSON payload grow
    # unbounded" reasoning as CwRemoteState._MAX_BUFFERED_TEXT.
    _MAX_BUFFERED_PACKETS = 200

    def _on_packet(self, packet):
        self.state["packets"] = (self.state["packets"] + [packet])[-self._MAX_BUFFERED_PACKETS:]

    def _on_decoding_toggled(self, checked):
        self.state["decoding"] = checked

    def _on_backend_changed(self, index):
        self.state["backend_index"] = index

    def request_decoding(self, on: bool):
        self._set_decoding.emit(bool(on))

    def request_backend(self, index: int):
        self._set_backend_index.emit(int(index))

    def request_send_position(self, comment: str = ""):
        import aprs
        from constants import AUDIO_TX_PCM_SAMPLE_RATE
        from PySide6.QtCore import QSettings

        settings = QSettings("IcomRadioApp", "RadioControl")
        source = (settings.value("operator_callsign", "") or "N0CALL").strip().upper()
        lat = float(settings.value("operator_lat", 0.0) or 0.0)
        lon = float(settings.value("operator_lon", 0.0) or 0.0)
        pcm = aprs.build_position_packet_pcm(
            source, "APRS", lat, lon, "/", "-", comment,
            sample_rate=AUDIO_TX_PCM_SAMPLE_RATE,
        )
        self.window._radio_window.worker.send_tx_audio_pcm(pcm)


class SatelliteRemoteState(QObject):
    """GUI-thread cache + queued-signal command bridge for satellite
    Doppler tracking. Unlike RadioWorker, SatelliteSession was never
    built for cross-thread calls -- no asyncio.run_coroutine_
    threadsafe internally, no thread-safety contract in its own
    docstring, and its _tick() (GUI-thread QTimer) mutates the same
    plain instance attributes start()/stop()/set_transponder()/
    adjust_offset() do. So every mutating call from the web server's
    own thread goes through a queued Qt signal -- same discipline as
    RadioRemoteState.request_ptt's widget touches.

    request_start/request_transponder deliberately do NOT connect
    straight to SatelliteSession.start()/set_transponder() (an earlier
    version of this class did) -- those are only HALF of what
    selecting a satellite/transponder needs to do. The desktop's own
    _on_satellite_double_clicked/_on_transponder_changed
    (ham_dashboard.py) also update self._active_satellite, the
    satellite name label, and -- critically -- (re)populate
    transponder_combo itself. Calling SatelliteSession directly skips
    all of that, leaving transponder_combo completely empty --
    confirmed as a real bug (a web-selected transponder had nothing on
    the desktop to sync against, since the combo was never populated
    for a satellite selected via the web at all). request_start/
    request_transponder instead go through HamClockWindow's own
    dashboard._select_satellite_for_tracking/select_transponder_
    remote, which do the full job (including calling into
    SatelliteSession themselves) -- the exact same path a real
    double-click or combo selection takes."""

    _do_start = Signal(dict)
    _do_stop = Signal()
    _do_set_transponder = Signal(int)
    _do_adjust_offset = Signal(int)

    def __init__(self, dashboard, parent=None):
        super().__init__(parent)
        self.dashboard = dashboard
        satellite_session = dashboard.satellite_session
        self.session = satellite_session
        self.state = {
            "satellite_name": None,
            "tracking": satellite_session.is_tracking(),
            "elevation_deg": None,
            "azimuth_deg": None,
            "crossing_text": "",
            "downlink_doppler_hz": None,
            "uplink_doppler_hz": None,
            "warning_text": "",
        }
        satellite_session.state_updated.connect(self._on_state_updated)
        satellite_session.tracking_changed.connect(self._on_tracking_changed)

        self._do_start.connect(dashboard._select_satellite_for_tracking)
        self._do_stop.connect(satellite_session.stop)
        self._do_set_transponder.connect(dashboard.select_transponder_remote)
        self._do_adjust_offset.connect(satellite_session.adjust_offset)

    def _on_state_updated(self, satellite, look, crossing_text, downlink_doppler_hz, uplink_doppler_hz, warning_text):
        self.state["satellite_name"] = satellite.get("name") if satellite else None
        self.state["elevation_deg"] = look.get("elevation_deg") if look else None
        self.state["azimuth_deg"] = look.get("azimuth_deg") if look else None
        self.state["crossing_text"] = crossing_text
        self.state["downlink_doppler_hz"] = downlink_doppler_hz
        self.state["uplink_doppler_hz"] = uplink_doppler_hz
        self.state["warning_text"] = warning_text

    def _on_tracking_changed(self, tracking):
        self.state["tracking"] = tracking

    def request_start(self, satellite: dict):
        self._do_start.emit(satellite)

    def request_stop(self):
        self._do_stop.emit()

    def request_transponder(self, index: int):
        self._do_set_transponder.emit(int(index))

    def request_offset(self, delta_hz: int):
        self._do_adjust_offset.emit(int(delta_hz))


class MapLayersRemoteState(QObject):
    """GUI-thread cache + queued-signal command bridge for
    HamClockWindow's map-overlay button row -- Satellites/QSO Map/
    PSKReporter/POTA/APRS toggles plus the band-filter dropdown
    (ham_dashboard.py, constructed once all those widgets exist).

    Same discipline as RadioRemoteState/SatelliteRemoteState: reads
    (`.state`) are a plain dict kept current by direct signal
    connections running on the GUI thread, safe to read cross-thread
    thanks to the GIL; writes are queued Qt signals connected straight
    to each button's own `setChecked` (or, for the band filter, to a
    small HamClockWindow helper that resolves a band string to a combo
    index) -- never a direct `.setChecked()`/`.setCurrentIndex()` call
    from the web server's own thread. Toggling a button this way reruns
    its EXISTING toggled handler (_on_pota_toggled etc.), so a web
    toggle does exactly what a real click does -- starts/stops the
    same fetch workers, applies the same band filter, no duplicated
    logic."""

    _set_satellites = Signal(bool)
    _set_qsos = Signal(bool)
    _set_pskreporter = Signal(bool)
    _set_pota = Signal(bool)
    _set_aprs = Signal(bool)
    _set_band_filter = Signal(object)  # str or None ("All Bands")

    def __init__(self, dashboard, parent=None):
        super().__init__(parent)
        self.dashboard = dashboard
        self.state = {
            "satellites": dashboard.satellite_button.isChecked(),
            "qsos": dashboard.qso_map_button.isChecked(),
            "pskreporter": dashboard.pskreporter_button.isChecked(),
            "pota": dashboard.pota_button.isChecked(),
            "aprs": dashboard.aprs_button.isChecked(),
            "band_filter": dashboard.qso_band_filter_combo.currentData(),
            "band_options": [
                {"label": dashboard.qso_band_filter_combo.itemText(i),
                 "value": dashboard.qso_band_filter_combo.itemData(i)}
                for i in range(dashboard.qso_band_filter_combo.count())
            ],
        }
        dashboard.satellite_button.toggled.connect(self._on_satellites_toggled)
        dashboard.qso_map_button.toggled.connect(self._on_qsos_toggled)
        dashboard.pskreporter_button.toggled.connect(self._on_pskreporter_toggled)
        dashboard.pota_button.toggled.connect(self._on_pota_toggled)
        dashboard.aprs_button.toggled.connect(self._on_aprs_toggled)
        dashboard.qso_band_filter_combo.currentIndexChanged.connect(self._on_band_filter_changed)

        self._set_satellites.connect(dashboard.satellite_button.setChecked)
        self._set_qsos.connect(dashboard.qso_map_button.setChecked)
        self._set_pskreporter.connect(dashboard.pskreporter_button.setChecked)
        self._set_pota.connect(dashboard.pota_button.setChecked)
        self._set_aprs.connect(dashboard.aprs_button.setChecked)
        self._set_band_filter.connect(dashboard.set_map_band_filter)

    def _on_satellites_toggled(self, checked):
        self.state["satellites"] = checked

    def _on_qsos_toggled(self, checked):
        self.state["qsos"] = checked

    def _on_pskreporter_toggled(self, checked):
        self.state["pskreporter"] = checked

    def _on_pota_toggled(self, checked):
        self.state["pota"] = checked

    def _on_aprs_toggled(self, checked):
        self.state["aprs"] = checked

    def _on_band_filter_changed(self, index):
        self.state["band_filter"] = self.dashboard.qso_band_filter_combo.itemData(index)

    def request_satellites(self, on: bool):
        self._set_satellites.emit(bool(on))

    def request_qsos(self, on: bool):
        self._set_qsos.emit(bool(on))

    def request_pskreporter(self, on: bool):
        self._set_pskreporter.emit(bool(on))

    def request_pota(self, on: bool):
        self._set_pota.emit(bool(on))

    def request_aprs(self, on: bool):
        self._set_aprs.emit(bool(on))

    def request_band_filter(self, band):
        self._set_band_filter.emit(band)
