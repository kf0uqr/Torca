"""
ConnectionDialog: collects radio connection details (model, network/USB
settings, CI-V address, audio device selection) and the operator's own
location (for Doppler correction) before the main window opens. Call
ConnectionDialog.get_details() rather than instantiating directly --
see the class docstring below.
"""

import json
import urllib.request

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QDoubleSpinBox,
    QComboBox,
    QPushButton,
    QLabel,
    QMessageBox,
)

from constants import (
    RADIO_PROFILES,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DEFAULT_PASSWORD,
    DEFAULT_SERIAL_PORT,
    DEFAULT_BAUD_RATE,
)
from audio import sd, SOUNDDEVICE_AVAILABLE

# Free, keyless IP geolocation -- approximate (city-level accuracy at
# best, since it's inferred from the network's IP allocation, not an
# actual GPS fix), but that's plenty for Doppler correction where
# satellite ranges are hundreds of km+. HTTPS confirmed working without
# an API key or account. Elevation isn't available from this or any
# other IP-geolocation service, so that's always a manual entry.
IP_GEOLOCATION_URL = "https://ipapi.co/json/"


def fetch_ip_location():
    """Looks up (lat, lon) from the current public IP. Raises on
    failure or if the service didn't return usable coordinates --
    callers should catch and report."""
    request = urllib.request.Request(
        IP_GEOLOCATION_URL,
        headers={"User-Agent": "IcomRadioControlApp/1.0 (desktop ham radio control application)"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("error"):
        raise RuntimeError(data.get("reason") or "geolocation service returned an error")
    lat, lon = data.get("latitude"), data.get("longitude")
    if lat is None or lon is None:
        raise RuntimeError("response didn't include coordinates")
    return float(lat), float(lon)

class ConnectionDialog(QDialog):
    """Collects radio connection details before the main window opens.

    Call get_details() (a static helper below) rather than
    instantiating this directly -- it handles exec() and returns
    either a details dict or None if the user cancelled.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to Radio")

        self.radio_combo = QComboBox()
        self.radio_combo.addItems(RADIO_PROFILES.keys())
        self.radio_combo.currentTextChanged.connect(self._on_radio_changed)

        self.connection_combo = QComboBox()
        self.connection_combo.addItem("Network (LAN)", "network")
        self.connection_combo.addItem("USB (Serial)", "usb")
        self.connection_combo.currentIndexChanged.connect(self._on_connection_type_changed)

        self.addr_input = QLineEdit()
        self.addr_input.setPlaceholderText("A2")

        # --- Network-specific fields ---
        self.host_input = QLineEdit(DEFAULT_HOST)
        self.host_input.setPlaceholderText("192.168.1.100")

        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(DEFAULT_PORT)

        self.username_input = QLineEdit(DEFAULT_USERNAME)

        self.password_input = QLineEdit(DEFAULT_PASSWORD)
        self.password_input.setEchoMode(QLineEdit.Password)

        # --- USB-specific fields ---
        self.serial_port_input = QLineEdit(DEFAULT_SERIAL_PORT)
        self.serial_port_input.setPlaceholderText("/dev/ttyUSB0 or COM3")

        self.baud_rate_input = QSpinBox()
        self.baud_rate_input.setRange(300, 921_600)
        self.baud_rate_input.setValue(DEFAULT_BAUD_RATE)

        # --- Audio device selection (independent of CI-V connection type) ---
        self.audio_input_combo = QComboBox()
        self.audio_output_combo = QComboBox()
        self._populate_audio_devices()

        # --- Operator location (for Doppler correction) ---
        settings = QSettings("IcomRadioApp", "RadioControl")
        self.lat_input = QDoubleSpinBox()
        self.lat_input.setRange(-90.0, 90.0)
        self.lat_input.setDecimals(5)
        self.lat_input.setSuffix("°")
        self.lat_input.setValue(float(settings.value("operator_lat", 0.0)))

        self.lon_input = QDoubleSpinBox()
        self.lon_input.setRange(-180.0, 180.0)
        self.lon_input.setDecimals(5)
        self.lon_input.setSuffix("°")
        self.lon_input.setValue(float(settings.value("operator_lon", 0.0)))

        self.elevation_input = QDoubleSpinBox()
        self.elevation_input.setRange(-500.0, 9000.0)
        self.elevation_input.setDecimals(0)
        self.elevation_input.setSuffix(" m")
        self.elevation_input.setValue(float(settings.value("operator_elevation_m", 0.0)))

        self.gps_button = QPushButton("Get GPS Coordinates (from IP)")
        self.gps_button.setToolTip(
            "Looks up an approximate latitude/longitude from your public IP "
            "address (city-level accuracy, not a real GPS fix). Adjust "
            "manually afterward if you know your exact coordinates."
        )
        self.gps_button.clicked.connect(self._on_get_gps_clicked)

        form = QFormLayout()
        form.addRow("Radio:", self.radio_combo)
        form.addRow("Connection:", self.connection_combo)
        form.addRow("CI-V Address (hex):", self.addr_input)

        self.network_rows = [
            ("Host / IP:", self.host_input),
            ("Port:", self.port_input),
            ("Username:", self.username_input),
            ("Password:", self.password_input),
        ]
        self.usb_rows = [
            ("Serial Port:", self.serial_port_input),
            ("Baud Rate:", self.baud_rate_input),
        ]
        for label, widget in self.network_rows + self.usb_rows:
            form.addRow(label, widget)

        form.addRow("Audio Input:", self.audio_input_combo)
        form.addRow("Audio Output:", self.audio_output_combo)

        location_header = QLabel("<b>Your Location</b> (for satellite Doppler correction):")
        form.addRow(location_header)
        form.addRow(self.gps_button)
        form.addRow("Latitude:", self.lat_input)
        form.addRow("Longitude:", self.lon_input)
        form.addRow("Elevation:", self.elevation_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

        self.details = None  # populated on successful accept

        # Apply initial defaults for whichever radio is selected first.
        self._on_radio_changed(self.radio_combo.currentText())

    def _populate_audio_devices(self):
        """Fill the audio combos by querying sounddevice/PortAudio
        directly -- the same library AudioBridge uses to actually open
        the streams, so whatever's picked here is guaranteed openable.
        Stores each device's numeric PortAudio index as the combo's
        item data (not a name string), since numeric indices don't have
        the cross-library name-mismatch problem a name string would."""
        self.audio_input_combo.addItem("None (no RX audio)", None)
        self.audio_output_combo.addItem("None (no TX audio)", None)

        if not SOUNDDEVICE_AVAILABLE:
            self.audio_input_combo.addItem("sounddevice not installed", None)
            self.audio_output_combo.addItem("sounddevice not installed", None)
            self.audio_input_combo.setEnabled(False)
            self.audio_output_combo.setEnabled(False)
            return

        try:
            devices = sd.query_devices()
        except Exception as exc:
            self.audio_input_combo.addItem(f"Couldn't query devices: {exc}", None)
            self.audio_output_combo.addItem(f"Couldn't query devices: {exc}", None)
            self.audio_input_combo.setEnabled(False)
            self.audio_output_combo.setEnabled(False)
            return

        for index, device in enumerate(devices):
            if device.get("max_input_channels", 0) > 0:
                self.audio_input_combo.addItem(device["name"], index)
            if device.get("max_output_channels", 0) > 0:
                self.audio_output_combo.addItem(device["name"], index)

    def _on_get_gps_clicked(self):
        try:
            lat, lon = fetch_ip_location()
        except Exception as exc:
            QMessageBox.warning(self, "Get GPS Coordinates", f"Couldn't determine location: {exc}")
            return
        self.lat_input.setValue(lat)
        self.lon_input.setValue(lon)
        QMessageBox.information(
            self, "Get GPS Coordinates",
            "Latitude/longitude set from your IP address. This is approximate "
            "(city-level) -- adjust manually if you know your exact "
            "coordinates, and enter your elevation separately (not available "
            "from IP lookup)."
        )

    def _on_radio_changed(self, radio_model):
        profile = RADIO_PROFILES[radio_model]
        self.addr_input.setText(profile["addr_hex"])
        index = self.connection_combo.findData(profile["default_connection"])
        if index != -1:
            self.connection_combo.setCurrentIndex(index)
        else:
            self._on_connection_type_changed(self.connection_combo.currentIndex())

    def _on_connection_type_changed(self, _index):
        is_network = self.connection_combo.currentData() == "network"
        for _label, widget in self.network_rows:
            widget.setEnabled(is_network)
            widget.setVisible(is_network)
        for _label, widget in self.usb_rows:
            widget.setEnabled(not is_network)
            widget.setVisible(not is_network)
        # Also hide/show the row labels themselves.
        form = self.layout().itemAt(0).layout()
        for label, widget in self.network_rows:
            row_label = form.labelForField(widget)
            if row_label is not None:
                row_label.setVisible(is_network)
        for label, widget in self.usb_rows:
            row_label = form.labelForField(widget)
            if row_label is not None:
                row_label.setVisible(not is_network)

    def _on_accept(self):
        addr_text = self.addr_input.text().strip()
        try:
            addr = int(addr_text, 16)
        except ValueError:
            QMessageBox.warning(
                self, "Invalid address", "CI-V address must be hex, e.g. A2."
            )
            return

        connection_type = self.connection_combo.currentData()
        details = {
            "radio_model": self.radio_combo.currentText(),
            "connection_type": connection_type,
            "addr": addr,
            "audio_input_device": self.audio_input_combo.currentData(),
            "audio_output_device": self.audio_output_combo.currentData(),
            "observer_lat": self.lat_input.value(),
            "observer_lon": self.lon_input.value(),
            "observer_elevation_m": self.elevation_input.value(),
        }

        if connection_type == "network":
            host = self.host_input.text().strip()
            if not host:
                QMessageBox.warning(self, "Missing host", "Enter the radio's IP address.")
                return
            details.update({
                "host": host,
                "port": self.port_input.value(),
                "username": self.username_input.text().strip(),
                "password": self.password_input.text(),
            })
        else:
            serial_port = self.serial_port_input.text().strip()
            if not serial_port:
                QMessageBox.warning(self, "Missing serial port", "Enter the serial device path.")
                return
            details.update({
                "serial_port": serial_port,
                "baud_rate": self.baud_rate_input.value(),
            })

        settings = QSettings("IcomRadioApp", "RadioControl")
        settings.setValue("operator_lat", details["observer_lat"])
        settings.setValue("operator_lon", details["observer_lon"])
        settings.setValue("operator_elevation_m", details["observer_elevation_m"])

        self.details = details
        self.accept()

    @staticmethod
    def get_details(parent=None):
        """Show the dialog modally. Returns a details dict, or None
        if the user cancelled."""
        dialog = ConnectionDialog(parent)
        if dialog.exec() == QDialog.Accepted:
            return dialog.details
        return None

