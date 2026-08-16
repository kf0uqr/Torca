"""
ConnectionDialog: collects radio connection details (model, network/USB
settings, CI-V address, audio device selection) and the operator's own
location (for Doppler correction) before the main window opens. Call
ConnectionDialog.get_details() rather than instantiating directly --
see the class docstring below. All of that can be saved as a named
profile and reloaded from a dropdown on a later run.
"""

import json
import pathlib
import urllib.request

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QDoubleSpinBox,
    QComboBox,
    QPushButton,
    QLabel,
    QInputDialog,
    QMessageBox,
)

from constants import (
    RADIO_PROFILES,
    RADIO_ROLES,
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


# Saved connection profiles -- everything ConnectionDialog collects,
# under a name the user picks, so a whole setup (radio, network/USB
# settings, audio devices, location) can be swapped in from one dropdown
# instead of re-entered by hand. Plain JSON, same pattern as the
# satellite list. Passwords land in here in plaintext -- consistent with
# how this app already handles them (DEFAULT_PASSWORD, the password
# field itself) since this is a radio's own local-network CI-V/LAN
# credential, not a personal account, and this file never leaves disk.
CONNECTION_PROFILES_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "connection_profiles.json"


def load_connection_profiles():
    """Loads saved connection profiles as {name: details_dict}. Returns
    {} if none are saved yet."""
    if not CONNECTION_PROFILES_PATH.exists():
        return {}
    try:
        return json.loads(CONNECTION_PROFILES_PATH.read_text())
    except Exception as exc:
        print(f"[ERROR] Connection profiles: couldn't read saved profiles ({exc}); starting with none.")
        return {}


def save_connection_profiles(profiles):
    try:
        CONNECTION_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONNECTION_PROFILES_PATH.write_text(json.dumps(profiles, indent=2))
    except Exception as exc:
        print(f"[ERROR] Connection profiles: couldn't save profiles ({exc}).")


def allowed_satellite_roles(active_roles):
    """Given the set of roles already in use by connected radios,
    returns the set of role VALUES (constants.RADIO_ROLES' second
    tuple element) that make sense to offer for a NEW radio.

    A satellite pass only makes sense driven one of two ways at a
    time: one Full Duplex radio, or one Downlink + one Uplink pair --
    never a mix, and never a duplicate of either half of the pair.
    "Non-Sat" is always available regardless, since it doesn't
    participate in satellite tracking at all.
    """
    if "full_duplex" in active_roles:
        # A second Full Duplex radio is still fine (e.g. two
        # independent full-duplex-capable radios each running their
        # own pass) -- but Downlink/Uplink no longer make sense once
        # a Full Duplex radio is already handling both directions.
        return {"full_duplex", "non_sat"}
    if "downlink" in active_roles and "uplink" in active_roles:
        # The pair is already complete.
        return {"non_sat"}
    if "downlink" in active_roles:
        return {"uplink", "non_sat"}
    if "uplink" in active_roles:
        return {"downlink", "non_sat"}
    return {"full_duplex", "downlink", "uplink", "non_sat"}


class ConnectionDialog(QDialog):
    """Collects radio connection details before the main window opens.

    Call get_details() (a static helper below) rather than
    instantiating this directly -- it handles exec() and returns
    either a details dict or None if the user cancelled.
    """

    def __init__(self, parent=None, active_roles=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to Radio")
        self._allowed_roles = allowed_satellite_roles(active_roles or set())

        self._profiles = load_connection_profiles()

        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        self.save_profile_button = QPushButton("Save Profile...")
        self.save_profile_button.clicked.connect(self._on_save_profile_clicked)
        self.delete_profile_button = QPushButton("Delete Profile")
        self.delete_profile_button.clicked.connect(self._on_delete_profile_clicked)

        self.radio_combo = QComboBox()
        self.radio_combo.addItems(RADIO_PROFILES.keys())
        self.radio_combo.currentTextChanged.connect(self._on_radio_changed)

        # Which role this radio plays in satellite Doppler control --
        # see RADIO_ROLES's own comment in constants.py. Chosen here,
        # per-connection, rather than as a separate post-connect prompt,
        # since the dashboard needs to know it before registering the
        # new radio with the shared satellite session.
        self.role_combo = QComboBox()
        for label, value in RADIO_ROLES:
            if value in self._allowed_roles:
                self.role_combo.addItem(label, value)
        self.role_combo.setToolTip(
            "Satellite Full Duplex: one dual-receiver radio (e.g. IC-9700) "
            "handles both uplink and downlink itself.\n"
            "Satellite Downlink / Satellite Uplink: a \"poor man's full "
            "duplex\" pair -- pick these for two separate radios working "
            "the same pass together.\n"
            "Non-Sat: this radio isn't part of satellite tracking at all.\n"
            "Options here are limited to what still makes sense given "
            "already-connected radios -- e.g. once one radio is Satellite "
            "Downlink, only Satellite Uplink (or Non-Sat) is offered for "
            "the next one."
        )

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

        profile_row = QHBoxLayout()
        profile_row.addWidget(self.profile_combo, 1)
        profile_row.addWidget(self.save_profile_button)
        profile_row.addWidget(self.delete_profile_button)
        form.addRow("Profile:", profile_row)

        form.addRow("Radio:", self.radio_combo)
        form.addRow("Satellite Role:", self.role_combo)
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

        # Apply initial defaults for whichever radio is selected first,
        # then the last-used profile (if any) on top of that -- a saved
        # profile should win over the plain per-radio defaults.
        self._on_radio_changed(self.radio_combo.currentText())
        self._refresh_profile_combo()
        last_profile = QSettings("IcomRadioApp", "RadioControl").value("last_connection_profile", "")
        if last_profile and last_profile in self._profiles:
            # Triggers _on_profile_selected, which applies it.
            self.profile_combo.setCurrentIndex(self.profile_combo.findData(last_profile))

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

    def _refresh_profile_combo(self, select_name=None):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("-- Select Profile --", None)
        for name in sorted(self._profiles.keys()):
            self.profile_combo.addItem(name, name)
        if select_name:
            index = self.profile_combo.findData(select_name)
            if index != -1:
                self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)

    def _current_profile_dict(self):
        """Everything needed to fully restore this dialog's state later
        -- both connection-type field sets regardless of which is active
        (so switching types afterward doesn't lose the other one's
        values), and audio devices by NAME rather than PortAudio index
        (indices aren't stable across runs/machines; names are re-
        resolved against whatever's actually plugged in when loaded)."""
        return {
            "radio_model": self.radio_combo.currentText(),
            "role": self.role_combo.currentData(),
            "connection_type": self.connection_combo.currentData(),
            "addr_hex": self.addr_input.text().strip(),
            "host": self.host_input.text().strip(),
            "port": self.port_input.value(),
            "username": self.username_input.text().strip(),
            "password": self.password_input.text(),
            "serial_port": self.serial_port_input.text().strip(),
            "baud_rate": self.baud_rate_input.value(),
            "audio_input_name": self.audio_input_combo.currentText(),
            "audio_output_name": self.audio_output_combo.currentText(),
            "observer_lat": self.lat_input.value(),
            "observer_lon": self.lon_input.value(),
            "observer_elevation_m": self.elevation_input.value(),
        }

    def _apply_profile(self, profile):
        radio_model = profile.get("radio_model")
        if radio_model in RADIO_PROFILES:
            self.radio_combo.setCurrentText(radio_model)  # applies that radio's own addr/connection-type defaults first

        role_index = self.role_combo.findData(profile.get("role"))
        if role_index != -1:
            self.role_combo.setCurrentIndex(role_index)

        connection_index = self.connection_combo.findData(profile.get("connection_type"))
        if connection_index != -1:
            self.connection_combo.setCurrentIndex(connection_index)

        if profile.get("addr_hex"):
            self.addr_input.setText(profile["addr_hex"])
        if profile.get("host"):
            self.host_input.setText(profile["host"])
        if profile.get("port"):
            self.port_input.setValue(profile["port"])
        if "username" in profile:
            self.username_input.setText(profile["username"])
        if "password" in profile:
            self.password_input.setText(profile["password"])
        if profile.get("serial_port"):
            self.serial_port_input.setText(profile["serial_port"])
        if profile.get("baud_rate"):
            self.baud_rate_input.setValue(profile["baud_rate"])

        audio_input_index = self.audio_input_combo.findText(profile.get("audio_input_name", ""))
        if audio_input_index != -1:
            self.audio_input_combo.setCurrentIndex(audio_input_index)
        audio_output_index = self.audio_output_combo.findText(profile.get("audio_output_name", ""))
        if audio_output_index != -1:
            self.audio_output_combo.setCurrentIndex(audio_output_index)

        if "observer_lat" in profile:
            self.lat_input.setValue(profile["observer_lat"])
        if "observer_lon" in profile:
            self.lon_input.setValue(profile["observer_lon"])
        if "observer_elevation_m" in profile:
            self.elevation_input.setValue(profile["observer_elevation_m"])

    def _on_profile_selected(self, _index):
        name = self.profile_combo.currentData()
        if not name:
            return
        profile = self._profiles.get(name)
        if profile:
            self._apply_profile(profile)
        QSettings("IcomRadioApp", "RadioControl").setValue("last_connection_profile", name)

    def _on_save_profile_clicked(self):
        current_name = self.profile_combo.currentData() or ""
        name, ok = QInputDialog.getText(self, "Save Profile", "Profile name:", text=current_name)
        name = name.strip()
        if not ok or not name:
            return
        if name in self._profiles:
            confirm = QMessageBox.question(
                self, "Save Profile", f'A profile named "{name}" already exists. Overwrite it?',
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        self._profiles[name] = self._current_profile_dict()
        save_connection_profiles(self._profiles)
        self._refresh_profile_combo(select_name=name)
        QSettings("IcomRadioApp", "RadioControl").setValue("last_connection_profile", name)

    def _on_delete_profile_clicked(self):
        name = self.profile_combo.currentData()
        if not name:
            QMessageBox.information(self, "Delete Profile", "Select a saved profile first.")
            return
        confirm = QMessageBox.question(
            self, "Delete Profile", f'Delete the profile "{name}"?',
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self._profiles.pop(name, None)
        save_connection_profiles(self._profiles)
        self._refresh_profile_combo()
        settings = QSettings("IcomRadioApp", "RadioControl")
        if settings.value("last_connection_profile") == name:
            settings.remove("last_connection_profile")

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
            "role": self.role_combo.currentData(),
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

