"""
ConnectionDialog: collects radio connection details (model, network/USB
settings, CI-V address, audio device selection) before the main window
opens. Call ConnectionDialog.get_details() rather than instantiating
directly -- see the class docstring below.
"""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QComboBox,
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

