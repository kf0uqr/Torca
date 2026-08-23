"""
ConnectionDialog: collects radio connection details (model, network/USB
settings, CI-V address, audio device selection) before the main window
opens. Call ConnectionDialog.get_details() rather than instantiating
directly -- see the class docstring below. All of that can be saved as
a named profile and reloaded from a dropdown on a later run.

The operator's own location (for Doppler correction) used to be
collected here too -- moved out to operator_profile.py's own
OperatorProfileDialog, per explicit instruction, since a radio
connection has nothing to do with where the OPERATOR is standing. Both
dialogs still read/write the exact same QSettings keys (operator_lat/
operator_lon/operator_elevation_m), so nothing downstream needed to
change.
"""

import pathlib
import json

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QComboBox,
    QPushButton,
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
    DEFAULT_REMOTE_PORT,
)
from audio import sd, SOUNDDEVICE_AVAILABLE

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
    never a mix, never a duplicate of either half of the pair, and
    never a second Full Duplex radio once one is already handling
    both directions -- there's no scenario needing more than one
    transmit/receive slot for satellite work at a time. "Non-Sat" is
    always available regardless, since it doesn't participate in
    satellite tracking at all.
    """
    if "full_duplex" in active_roles:
        return {"non_sat"}
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
        self.connection_combo.addItem("Remote Server", "remote")
        self.connection_combo.setToolTip(
            "Remote Server: connect to a radio being shared over the network "
            "by torca-server (or a plain `rigplane web` server) running on "
            "a different machine -- typically a USB-only radio with no LAN "
            "capability of its own. Full feature parity with a direct "
            "connection: frequency/mode/PTT/levels/meters. No CI-V address "
            "needed here -- the server already has its own connection to "
            "the actual radio."
        )
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

        # --- Remote Server-specific fields ---
        self.remote_host_input = QLineEdit()
        self.remote_host_input.setPlaceholderText("192.168.1.50")

        self.remote_port_input = QSpinBox()
        self.remote_port_input.setRange(1, 65535)
        self.remote_port_input.setValue(DEFAULT_REMOTE_PORT)

        self.remote_token_input = QLineEdit()
        self.remote_token_input.setEchoMode(QLineEdit.Password)
        self.remote_token_input.setPlaceholderText("leave blank if the server has no auth token set")

        # --- Audio device selection (independent of CI-V connection type) ---
        self.audio_input_combo = QComboBox()
        self.audio_output_combo = QComboBox()
        self._populate_audio_devices()

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
        self.remote_rows = [
            ("Server Host:", self.remote_host_input),
            ("Server Port:", self.remote_port_input),
            ("Auth Token:", self.remote_token_input),
        ]
        for label, widget in self.network_rows + self.usb_rows + self.remote_rows:
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
        the streams. Stores each device's numeric PortAudio index as
        the combo's item data (not a name string), since numeric
        indices don't have the cross-library name-mismatch problem a
        name string would.

        Lists every device sounddevice reports, with no filtering --
        an earlier version of this excluded ALSA "virtual"/plugin
        devices (default, pulse, sysdefault, ...) in favor of raw
        "hw:X,Y" hardware devices, to dodge a real PortAudio-ALSA-via-
        PulseAudio capture bug confirmed on one specific setup. That
        traded a narrow, input-specific crash for a much worse
        regression: on a PulseAudio-managed system, PulseAudio
        typically holds the raw hw: device exclusively, so opening it
        directly from here (bypassing Pulse entirely) often just
        doesn't produce audio at all, confirmed live to break EVERY
        radio's audio, not just the one hitting the original bug. The
        virtual/plugin devices are usually the ones that actually work
        correctly on a Pulse-managed system precisely because they
        route through Pulse instead of fighting it for the hardware --
        reverted rather than guessed at a narrower filter blind. If
        the original "default"-as-capture-device crash resurfaces, the
        confirmed workaround is picking a specific, real hardware
        device for Audio Input only (leave Audio Output alone)."""
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
            "remote_host": self.remote_host_input.text().strip(),
            "remote_port": self.remote_port_input.value(),
            "remote_token": self.remote_token_input.text(),
            "audio_input_name": self.audio_input_combo.currentText(),
            "audio_output_name": self.audio_output_combo.currentText(),
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
        if profile.get("remote_host"):
            self.remote_host_input.setText(profile["remote_host"])
        if profile.get("remote_port"):
            self.remote_port_input.setValue(profile["remote_port"])
        if "remote_token" in profile:
            self.remote_token_input.setText(profile["remote_token"])

        audio_input_index = self.audio_input_combo.findText(profile.get("audio_input_name", ""))
        if audio_input_index != -1:
            self.audio_input_combo.setCurrentIndex(audio_input_index)
        audio_output_index = self.audio_output_combo.findText(profile.get("audio_output_name", ""))
        if audio_output_index != -1:
            self.audio_output_combo.setCurrentIndex(audio_output_index)

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

    def _on_radio_changed(self, radio_model):
        profile = RADIO_PROFILES[radio_model]
        self.addr_input.setText(profile["addr_hex"])
        index = self.connection_combo.findData(profile["default_connection"])
        if index != -1 and index != self.connection_combo.currentIndex():
            self.connection_combo.setCurrentIndex(index)  # currentIndexChanged -> _on_connection_type_changed
        else:
            # Either this radio has no preferred connection type, or its
            # default is the SAME index the combo is already on (e.g.
            # switching between two "usb"-default radios) -- setCurrentIndex
            # wouldn't fire currentIndexChanged in that case, so refresh
            # the addr field's visibility explicitly (it depends on the
            # newly-selected radio's protocol, not just connection type,
            # see _current_radio_protocol).
            self._on_connection_type_changed(self.connection_combo.currentIndex())

    def _current_radio_protocol(self):
        """"civ" (the default -- every radio this app supported before
        IC-7610/X6200/FTX-1 were added, plus IC-7610/X6200 themselves)
        or "yaesu_cat" (FTX-1, no CI-V address concept at all -- see
        RADIO_PROFILES' own comment)."""
        radio_model = self.radio_combo.currentText()
        profile = RADIO_PROFILES.get(radio_model, {})
        return profile.get("protocol", "civ")

    def _on_connection_type_changed(self, _index):
        connection_type = self.connection_combo.currentData()
        groups = {
            "network": self.network_rows,
            "usb": self.usb_rows,
            "remote": self.remote_rows,
        }
        # CI-V Address isn't its own tracked group (added directly to
        # the form, not via network_rows/usb_rows/remote_rows) -- a
        # Remote Server connection doesn't need it at all, since the
        # server already has its own connection (and its own CI-V
        # address) to the actual radio. Neither does a non-CI-V radio
        # (FTX-1's Yaesu CAT protocol has no such concept) regardless of
        # connection type -- see _current_radio_protocol.
        form = self.layout().itemAt(0).layout()
        addr_label = form.labelForField(self.addr_input)
        needs_addr = connection_type != "remote" and self._current_radio_protocol() == "civ"
        self.addr_input.setVisible(needs_addr)
        self.addr_input.setEnabled(needs_addr)
        if addr_label is not None:
            addr_label.setVisible(needs_addr)

        for group_type, rows in groups.items():
            visible = connection_type == group_type
            for _label, widget in rows:
                widget.setEnabled(visible)
                widget.setVisible(visible)
                row_label = form.labelForField(widget)
                if row_label is not None:
                    row_label.setVisible(visible)

    def _on_accept(self):
        connection_type = self.connection_combo.currentData()

        # No CI-V address for a Remote Server connection -- the server
        # already has its own connection (and its own CI-V address) to
        # the actual radio; this client never speaks CI-V directly. Same
        # for a non-CI-V radio (FTX-1's Yaesu CAT protocol), regardless
        # of connection type -- see _current_radio_protocol.
        addr = None
        if connection_type != "remote" and self._current_radio_protocol() == "civ":
            addr_text = self.addr_input.text().strip()
            try:
                addr = int(addr_text, 16)
            except ValueError:
                QMessageBox.warning(
                    self, "Invalid address", "CI-V address must be hex, e.g. A2."
                )
                return

        details = {
            "radio_model": self.radio_combo.currentText(),
            "role": self.role_combo.currentData(),
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
        elif connection_type == "remote":
            remote_host = self.remote_host_input.text().strip()
            if not remote_host:
                QMessageBox.warning(self, "Missing server host", "Enter the torca-server / rigplane web server's address.")
                return
            details.update({
                "remote_host": remote_host,
                "remote_port": self.remote_port_input.value(),
                "remote_token": self.remote_token_input.text(),
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

