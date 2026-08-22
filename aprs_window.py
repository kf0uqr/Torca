"""
AprsToolWindow: the per-radio APRS (Automatic Packet Reporting System)
decode/send tool. Opened as a singleton from its owning RadioWindow
(main_window.py), same show-or-raise / hide-not-destroy pattern as
CwToolWindow/SstvToolWindow/RttyToolWindow -- closeEvent stops any
active decode tap before hiding, so the same instance can be reopened
freely without losing its packet log.

Decode: a continuously-running Bell 202/AX.25 decoder
(aprs.AprsDecoder) fed live PCM via RadioWorker.start_aprs_decode().
Works alongside normal listening audio or an active Virtual Cable on
the same radio, exactly like CW/SSTV/RTTY decode -- RadioWorker shares
whichever RX stream is already running rather than requiring exclusive
access.

Unlike CW/RTTY (a continuous text transcript), APRS is inherently
packet-oriented -- each decode is a discrete, self-contained event
(a station's position/status/message), not a stream of characters to
append to a running line. So this shows a table, one row per decoded
packet, newest at the bottom (same append-and-scroll convention as the
CW/RTTY transcripts, just structured into columns instead of raw text).

Send: position reports only (matching the decode side's own scope),
via Send Packet..., which opens SendAprsPacketDialog. Builds AFSK PCM
with aprs.py's build_position_packet_pcm and hands it to RadioWorker.
send_tx_audio_pcm -- a generic PTT-key/push-PCM/unkey primitive (see
its own docstring in radio_worker.py), not itself APRS-specific.
"""

import datetime

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import (
    QWidget,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QVBoxLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QComboBox,
    QDoubleSpinBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from constants import AUDIO_DEFAULT_SAMPLE_RATE, AUDIO_TX_PCM_SAMPLE_RATE
from aprs import AprsDecoder, build_position_packet_pcm, symbol_description
from direwolf_backend import DirewolfBackend, direwolf_available

_COLUMNS = ["Time", "Source", "Destination", "Type", "Details"]

_SETTINGS_ORG = "IcomRadioApp"
_SETTINGS_APP = "RadioControl"


def _format_comment_extension(ext):
    """Human-readable translation of aprs.parse_comment_extensions()'s
    output -- the structured Data Extension sub-fields (APRS101.PDF
    Chapter 7) a position report's comment MAY start with (course/
    speed, PHG, RNG, or DFS -- mutually exclusive, at most one present)
    plus altitude (independent of those four -- can appear anywhere in
    the comment). Returns "" if ext is empty/None (an ordinary comment
    with none of these)."""
    if not ext:
        return ""
    parts = []
    if "course_deg" in ext:
        parts.append(f"course {ext['course_deg']}° @ {ext['speed_knots']}kt")
    if "phg" in ext:
        phg = ext["phg"]
        direction = f"{phg['directivity_deg']}°" if phg["directivity_deg"] is not None else "omni"
        parts.append(
            f"{phg['power_w']}W @ {phg['height_ft']}ft AAT, {phg['gain_db']}dB gain, {direction}"
        )
    if "df" in ext:
        df = ext["df"]
        direction = f"{df['directivity_deg']}°" if df["directivity_deg"] is not None else "omni"
        parts.append(
            f"DF strength S{df['strength_s']} @ {df['height_ft']}ft AAT, {df['gain_db']}dB gain, {direction}"
        )
    if "range_miles" in ext:
        parts.append(f"range {ext['range_miles']}mi")
    if "altitude_ft" in ext:
        parts.append(f"altitude {ext['altitude_ft']}ft")
    return ", ".join(parts)


def _format_weather(weather):
    if not weather:
        return ""
    parts = []
    if "wind_deg" in weather:
        parts.append(f"wind {weather['wind_deg']}°@{weather['wind_speed_mph']}mph")
    if "gust_mph" in weather:
        parts.append(f"gust {weather['gust_mph']}mph")
    if "temp_f" in weather:
        parts.append(f"{weather['temp_f']}°F")
    if "humidity_pct" in weather:
        parts.append(f"humidity {weather['humidity_pct']}%")
    if "pressure_mbar" in weather:
        parts.append(f"{weather['pressure_mbar']:.1f}mbar")
    if "rain_last_hr_in" in weather:
        parts.append(f"rain(1h) {weather['rain_last_hr_in']:.2f}in")
    if "rain_24hr_in" in weather:
        parts.append(f"rain(24h) {weather['rain_24hr_in']:.2f}in")
    if "rain_since_midnight_in" in weather:
        parts.append(f"rain(today) {weather['rain_since_midnight_in']:.2f}in")
    if "snow_24hr_in" in weather:
        parts.append(f"snow(24h) {weather['snow_24hr_in']}in")
    return ", ".join(parts)


# Human-readable tag per non-plain position source -- shown as a prefix
# on the Details text so an operator can tell which wire format
# actually produced a given translated row (all of them decode to the
# exact same "type": "position" shape internally, so nothing else in
# this table would otherwise distinguish them).
_SOURCE_FORMAT_LABELS = {"mic_e": "Mic-E", "compressed": "Compressed"}


def _format_packet_details(info):
    if info is None:
        return "(no info field)"

    if info["type"] == "position":
        # Translates the raw two-character symbol table/code (e.g.
        # "/-") into its official human-readable name (e.g. "House QTH
        # (VHF)") via aprs.py's SYMBOL_TABLE_PRIMARY/ALTERNATE -- the
        # raw code is still shown alongside it in brackets, both for
        # operators who already know the codes by heart and as a
        # fallback for symbols the table doesn't have a name for.
        symbol_name = symbol_description(info["symbol_table"], info["symbol_code"])
        symbol_code = f"{info['symbol_table']}{info['symbol_code']}"
        symbol_part = f"{symbol_name} [{symbol_code}]" if symbol_name else f"[{symbol_code}]"

        prefix = ""
        source_format = info.get("source_format")
        if source_format in _SOURCE_FORMAT_LABELS:
            prefix = f"[{_SOURCE_FORMAT_LABELS[source_format]}] "
        if info.get("object_name"):
            kind = "Item" if source_format == "item" else "Object"
            live = "" if info.get("object_live", True) else " KILLED"
            prefix = f"[{kind}: {info['object_name']}{live}] "

        details = f"{prefix}{info['lat']:.5f}, {info['lon']:.5f}  {symbol_part}"
        if info.get("mic_e_message_type"):
            details += f"  ({info['mic_e_message_type']})"
        if info.get("mic_e_device"):
            details += f"  [{info['mic_e_device']}]"
        if info.get("mic_e_grid_square"):
            details += f"  Grid: {info['mic_e_grid_square']}"
        if info["comment"]:
            details += f"  {info['comment']}"
        # Structured Data Extensions parsed out of that same comment
        # (course/speed, PHG, RNG, DFS, altitude) -- shown as an
        # additional translated summary, not in place of the raw
        # comment text above, same "translate alongside, don't replace"
        # approach the symbol name takes with its own raw code. For a
        # weather report, the SAME leading 7 comment bytes were already
        # reinterpreted as wind direction/speed (mph, not knots) by
        # _attach_weather_if_present -- the Weather line below already
        # shows that correctly labeled, so skip the (course .../...kt)
        # extension line here to avoid double-showing the same number
        # under a wrong unit.
        weather = info.get("weather")
        extension = info.get("comment_extension")
        if weather and "wind_deg" in weather and extension and "course_deg" in extension:
            extension = {k: v for k, v in extension.items() if k not in ("course_deg", "speed_knots")}
        extension_text = _format_comment_extension(extension)
        if extension_text:
            details += f"  ({extension_text})"
        weather_text = _format_weather(weather)
        if weather_text:
            details += f"  Weather: {weather_text}"
        if info.get("mic_e_telemetry"):
            channels = info["mic_e_telemetry"]["channels"]
            details += "  Telemetry: " + ", ".join(f"ch{k}={v}" for k, v in sorted(channels.items()))
        return details

    if info["type"] == "status":
        parts = []
        if info.get("timestamp"):
            parts.append(f"[{info['timestamp']}]")
        parts.append(info["text"])
        if info.get("beam_heading_deg") is not None:
            parts.append(f"(beam {info['beam_heading_deg']}°, {info['erp_watts']}W ERP)")
        return " ".join(parts)

    if info["type"] == "message":
        details = f"To {info['addressee']}: {info['text']}"
        if info.get("message_id"):
            details += f"  (msg #{info['message_id']}, ack requested)"
        return details

    if info["type"] == "message_ack":
        return f"To {info['addressee']}: ACK for message #{info['message_id']}"

    if info["type"] == "message_reject":
        return f"To {info['addressee']}: REJECTED message #{info['message_id']}"

    if info["type"] == "telemetry":
        analog = ", ".join(f"A{i + 1}={v}" for i, v in enumerate(info["analog"]))
        digital = "".join("1" if b else "0" for b in info["digital"])
        return f"Seq {info['sequence']}: {analog}, digital={digital}"

    if info["type"] == "telemetry_definition":
        kind_labels = {
            "parameter_names": "Parameter Names", "units_labels": "Units/Labels",
            "equation_coefficients": "Equation Coefficients", "bit_sense": "Bit Sense/Project",
        }
        label = kind_labels.get(info["kind"], info["kind"])
        return f"To {info['addressee']}: Telemetry {label}: {', '.join(info['raw_fields'])}"

    return info["raw"]


class SendAprsPacketDialog(QDialog):
    """One-shot "build and send" form for a single APRS position
    report. Live-apply on Send (no separate OK/commit step) -- Send
    itself builds the AFSK PCM and hands it straight to RadioWorker.
    send_tx_audio_pcm, then the dialog stays open so the operator can
    send another packet (e.g. a periodic beacon, sent by hand each
    time) without reopening it; Close just dismisses.

    Source callsign and lat/lon default from the same QSettings this
    app already uses elsewhere (operator_callsign -- cw_window.py's
    {MYCALL}; operator_lat/operator_lon -- ConnectionDialog/ham_
    dashboard.py's satellite map location) rather than asking the
    operator to retype values already on file."""

    def __init__(self, radio_window, parent=None):
        super().__init__(parent)
        self._radio_window = radio_window
        self.setWindowTitle("Send APRS Packet")

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        default_callsign = settings.value("operator_callsign", "") or ""
        default_lat = float(settings.value("operator_lat", 0.0) or 0.0)
        default_lon = float(settings.value("operator_lon", 0.0) or 0.0)

        self.source_edit = QLineEdit(default_callsign)
        self.source_edit.setPlaceholderText("e.g. N0CALL-9")
        self.destination_edit = QLineEdit("APRS")
        self.destination_edit.setToolTip(
            "The APRS destination/TOCALL field -- \"APRS\" is a generic, widely-accepted "
            "value; specific client software often uses its own registered TOCALL instead."
        )
        self.digipeaters_edit = QLineEdit()
        self.digipeaters_edit.setPlaceholderText("WIDE1-1,WIDE2-1 (optional)")

        self.lat_spin = QDoubleSpinBox()
        self.lat_spin.setRange(-90.0, 90.0)
        self.lat_spin.setDecimals(6)
        self.lat_spin.setValue(default_lat)
        self.lon_spin = QDoubleSpinBox()
        self.lon_spin.setRange(-180.0, 180.0)
        self.lon_spin.setDecimals(6)
        self.lon_spin.setValue(default_lon)

        self.symbol_table_edit = QLineEdit("/")
        self.symbol_table_edit.setMaxLength(1)
        self.symbol_table_edit.setFixedWidth(30)
        self.symbol_code_edit = QLineEdit("-")
        self.symbol_code_edit.setMaxLength(1)
        self.symbol_code_edit.setFixedWidth(30)
        self.symbol_code_edit.setToolTip(
            "Common symbol codes (primary table \"/\"): \"-\" house, \">\" car, \"k\" truck, "
            "\"_\" weather station. Full symbol tables are listed in the APRS spec, Chapter 20."
        )
        symbol_row = QHBoxLayout()
        symbol_row.addWidget(self.symbol_table_edit)
        symbol_row.addWidget(self.symbol_code_edit)
        symbol_row.addStretch()

        self.comment_edit = QLineEdit()
        self.comment_edit.setMaxLength(43)  # APRS101.PDF's own stated max for this field

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #aaa;")

        send_button = QPushButton("Send")
        send_button.clicked.connect(self._on_send_clicked)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        button_row = QHBoxLayout()
        button_row.addWidget(send_button)
        button_row.addWidget(close_button)

        form = QFormLayout()
        form.addRow("Source:", self.source_edit)
        form.addRow("Destination:", self.destination_edit)
        form.addRow("Digipeaters:", self.digipeaters_edit)
        form.addRow("Latitude:", self.lat_spin)
        form.addRow("Longitude:", self.lon_spin)
        form.addRow("Symbol (table/code):", symbol_row)
        form.addRow("Comment:", self.comment_edit)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addLayout(button_row)
        self.setLayout(layout)

    def _on_send_clicked(self):
        source = self.source_edit.text().strip().upper()
        if not source:
            self.status_label.setText("Enter a source callsign first.")
            return
        destination = self.destination_edit.text().strip().upper() or "APRS"
        digipeaters = [d.strip().upper() for d in self.digipeaters_edit.text().split(",") if d.strip()]
        symbol_table = self.symbol_table_edit.text() or "/"
        symbol_code = self.symbol_code_edit.text() or "-"
        try:
            pcm = build_position_packet_pcm(
                source, destination, self.lat_spin.value(), self.lon_spin.value(),
                symbol_table, symbol_code, self.comment_edit.text(),
                sample_rate=AUDIO_TX_PCM_SAMPLE_RATE, digipeaters=digipeaters,
            )
        except ValueError as exc:
            self.status_label.setText(f"Couldn't build packet: {exc}")
            return
        self._radio_window.worker.send_tx_audio_pcm(pcm)
        # send_tx_audio_pcm is fire-and-forget (its own PTT-key/push/
        # unkey sequence runs on the worker's asyncio thread) -- this
        # is just an immediate, optimistic confirmation that the
        # packet was queued, not a "transmission complete" signal.
        # Real progress/failures show up via the owning RadioWindow's
        # existing audio_status/error handling (same as PTT/CW send).
        duration_s = len(pcm) / (AUDIO_TX_PCM_SAMPLE_RATE * 2)
        self.status_label.setText(f"Sending packet (~{duration_s:.1f}s)...")


class AprsToolWindow(QWidget):
    # Internal cross-thread hop only: _on_decode_audio_frame runs on
    # RadioWorker's asyncio-loop thread (same requirement as every
    # other RadioWorker callback in this app) and must not touch any
    # widget directly.
    _packets_ready = Signal(list)

    # Public: one decoded position-report packet, emitted from
    # _on_packets_ready (already on the GUI thread by the time that
    # runs, regardless of which decode backend produced it -- see its
    # own comment). ham_dashboard.py's APRS map overlay is the
    # consumer -- forwarded through RadioWindow.aprs_packet_decoded
    # (main_window.py) since this window is only ever constructed
    # lazily, on demand, not necessarily by the time ham_dashboard.py
    # wants to connect to it.
    packet_decoded = Signal(dict)

    def __init__(self, radio_window):
        super().__init__()
        self._radio_window = radio_window
        self.setWindowTitle(f"APRS Tool -- {radio_window._connection_label}")
        self.resize(640, 420)

        self._decoder = None  # aprs.AprsDecoder, constructed fresh each time decoding starts
        self._direwolf_backend = None  # direwolf_backend.DirewolfBackend, alternative to self._decoder

        # "Built-in" is this app's own from-scratch decoder (aprs.py) --
        # always available, the default. "Direwolf" delegates decoding
        # to an external direwolf subprocess (direwolf_backend.py) --
        # offered as a selectable alternative per the user's own choice
        # to add it alongside, not replace, the built-in decoder.
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("Built-in")
        self.backend_combo.addItem("Direwolf (external)")
        if not direwolf_available():
            self.backend_combo.setItemData(1, "direwolf executable not found on PATH", 3)  # Qt.ToolTipRole=3
        backend_label = QLabel("Decoder:")

        self.decode_toggle_button = QPushButton("Start Decoding")
        self.decode_toggle_button.setCheckable(True)
        self.decode_toggle_button.toggled.connect(self._on_decode_toggled)

        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._on_clear_clicked)

        send_packet_button = QPushButton("Send Packet...")
        send_packet_button.clicked.connect(self._on_send_packet_clicked)
        self._send_dialog = None  # lazily constructed, same singleton-per-window pattern as the tool windows themselves

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(backend_label)
        buttons_row.addWidget(self.backend_combo)
        buttons_row.addWidget(self.decode_toggle_button)
        buttons_row.addWidget(clear_button)
        buttons_row.addWidget(send_packet_button)
        buttons_row.addStretch()

        self.status_label = QLabel("Not decoding.")
        self.status_label.setStyleSheet("color: #aaa;")

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)

        layout = QVBoxLayout()
        layout.addLayout(buttons_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.table, stretch=1)
        self.setLayout(layout)

        self._packets_ready.connect(self._on_packets_ready)

    # ---- Decode ----

    def _on_decode_toggled(self, checked):
        worker = self._radio_window.worker
        use_direwolf = self.backend_combo.currentIndex() == 1
        if checked:
            if use_direwolf and not direwolf_available():
                self.status_label.setText("Direwolf backend: 'direwolf' executable not found on PATH.")
                self.decode_toggle_button.blockSignals(True)
                self.decode_toggle_button.setChecked(False)
                self.decode_toggle_button.blockSignals(False)
                return
            sample_rate = getattr(worker.radio, "audio_sample_rate", None) or AUDIO_DEFAULT_SAMPLE_RATE
            if use_direwolf:
                settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
                mycall = settings.value("operator_callsign", "") or "N0CALL"
                self._direwolf_backend = DirewolfBackend(sample_rate, mycall=mycall, parent=self)
                self._direwolf_backend.packet_received.connect(self._on_direwolf_packet)
                self._direwolf_backend.status.connect(self.status_label.setText)
                self._direwolf_backend.error.connect(self.status_label.setText)
                self._direwolf_backend.start()
                feed_callback = self._on_direwolf_audio_frame
            else:
                self._decoder = AprsDecoder(sample_rate)
                feed_callback = self._on_decode_audio_frame
            try:
                worker.start_aprs_decode(feed_callback)
            except RuntimeError as exc:
                # Defensive backstop only -- with aprs_tool_button only
                # ever enabled once the radio is connected (main_
                # window.py's _on_connected), start_aprs_decode()
                # raising synchronously (not-connected-yet) shouldn't
                # actually be reachable from here. A failure to
                # actually attach (e.g. the radio doesn't support RX
                # audio at all) happens asynchronously instead and is
                # reported via worker.error, not this exception.
                self._decoder = None
                if self._direwolf_backend is not None:
                    self._direwolf_backend.stop()
                    self._direwolf_backend = None
                self.status_label.setText(str(exc))
                self.decode_toggle_button.blockSignals(True)
                self.decode_toggle_button.setChecked(False)
                self.decode_toggle_button.blockSignals(False)
                return
            self.backend_combo.setEnabled(False)
            self.decode_toggle_button.setText("Stop Decoding")
            if not use_direwolf:
                self.status_label.setText("Decoding -- listening for AX.25 packets...")
        else:
            worker.stop_aprs_decode()
            self._decoder = None
            if self._direwolf_backend is not None:
                self._direwolf_backend.stop()
                self._direwolf_backend = None
            self.backend_combo.setEnabled(True)
            self.decode_toggle_button.setText("Start Decoding")
            self.status_label.setText("Not decoding.")

    def _on_decode_audio_frame(self, pcm_bytes):
        # Runs on RadioWorker's asyncio-loop thread. aprs.AprsDecoder
        # has no thread-safety of its own, but it's only ever touched
        # here (RadioWorker delivers its own audio callbacks serially)
        # and from _on_decode_toggled's start/stop on the GUI thread,
        # which only replace/clear self._decoder while decoding is NOT
        # active -- so there's never genuine concurrent access to the
        # same AprsDecoder instance.
        if self._decoder is None:
            return
        packets = self._decoder.feed(pcm_bytes)
        if packets:
            self._packets_ready.emit(packets)

    def _on_direwolf_audio_frame(self, pcm_bytes):
        # Runs on RadioWorker's asyncio-loop thread. DirewolfBackend.
        # feed_audio() is a plain synchronous UDP send with no Qt
        # event-loop or widget touch, so this crosses no thread
        # boundary that needs marshaling (see its own docstring).
        if self._direwolf_backend is None:
            return
        self._direwolf_backend.feed_audio(pcm_bytes)

    def _on_direwolf_packet(self, packet):
        # DirewolfBackend.packet_received is emitted from its own
        # QTcpSocket readyRead handler, which runs on the GUI thread
        # (the socket lives there) -- no cross-thread hop needed here,
        # unlike _on_decode_audio_frame's worker-thread callback.
        self._on_packets_ready([packet])

    def _on_packets_ready(self, packets):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        for packet in packets:
            row = self.table.rowCount()
            self.table.insertRow(row)
            digipeater_suffix = f" via {','.join(packet['digipeaters'])}" if packet["digipeaters"] else ""
            info = packet["info"]
            values = [
                now,
                packet["source"],
                packet["destination"] + digipeater_suffix,
                (info["type"] if info else "unknown"),
                _format_packet_details(info),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
            if info is not None and info["type"] == "position":
                self.packet_decoded.emit(packet)
        self.table.scrollToBottom()

    def _on_clear_clicked(self):
        self.table.setRowCount(0)

    # ---- Send ----

    def _on_send_packet_clicked(self):
        if self._send_dialog is None:
            self._send_dialog = SendAprsPacketDialog(self._radio_window, self)
        self._send_dialog.show()
        self._send_dialog.raise_()
        self._send_dialog.activateWindow()

    # ---- Lifecycle ----

    def closing_for_real(self):
        """Called by RadioWindow.closeEvent when the OWNING radio
        window is actually going away for good -- see CwToolWindow.
        closing_for_real for the full rationale."""
        self._closing_for_real = True
        self.close()

    def closeEvent(self, event):
        # Singleton -- hidden, not destroyed -- but an active decode
        # tap is a real hardware side effect (an abandoned
        # radio.start_rx() tap would keep running with nothing
        # consuming it, and an abandoned direwolf subprocess would
        # keep running too) that must be stopped first, same as CW/
        # SSTV/RTTY Tool.
        if self.decode_toggle_button.isChecked():
            self.decode_toggle_button.setChecked(False)  # synchronously runs _on_decode_toggled(False)
        if getattr(self, "_closing_for_real", False):
            event.accept()
            return
        event.ignore()
        self.hide()
