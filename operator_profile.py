"""
OperatorProfileDialog: collects the operator's own callsign and
station location (latitude/longitude/elevation, for satellite Doppler
correction and the dashboard's map marker) as a named, saveable/
loadable "profile" -- the same save/load/delete-by-name pattern
connection_dialog.py already uses for radio connections, applied here
to the operator's own identity/location instead of a specific radio.

Shown once automatically at app startup (main.py, before HamClockWindow
is constructed) and reachable afterward from Ham Dashboard's own
"Profile..." button (_on_profile_button_clicked) -- e.g. a shared club
station switching between operators, or one operator switching between
a home and portable/POTA location, without retyping either by hand.

Persists to the SAME QSettings keys this app has always used for these
values (operator_callsign/operator_lat/operator_lon/operator_elevation_m
-- ham_dashboard.py's _ensure_operator_callsign/_load_observer_location,
satellite_session.py, every meter/window that reads the operator's own
callsign or location already reads these exact keys) -- so nothing
downstream needed to change at all; this dialog is just a friendlier,
named-profile front end for values that were previously entered ad hoc
(a plain QInputDialog for callsign, buried fields in ConnectionDialog
for location) and are now collected together, once, up front.

GPS-from-IP lookup (fetch_ip_location) was moved here from
connection_dialog.py wholesale, per explicit instruction to move
location out of the radio-connection dialog entirely -- a radio
connection has nothing to do with where the OPERATOR is standing.
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
    QDoubleSpinBox,
    QComboBox,
    QPushButton,
    QLabel,
    QInputDialog,
    QMessageBox,
)

_SETTINGS_ORG = "IcomRadioApp"
_SETTINGS_APP = "RadioControl"

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


# Saved operator profiles -- {name: {"callsign", "lat", "lon",
# "elevation_m"}}. Plain JSON, same pattern/location as
# connection_profiles.json (connection_dialog.py) and memories.json
# (memories_window.py).
OPERATOR_PROFILES_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "operator_profiles.json"


def load_operator_profiles():
    """Loads saved operator profiles as {name: profile_dict}. Returns
    {} if none are saved yet."""
    if not OPERATOR_PROFILES_PATH.exists():
        return {}
    try:
        return json.loads(OPERATOR_PROFILES_PATH.read_text())
    except Exception as exc:
        print(f"[ERROR] Operator profiles: couldn't read saved profiles ({exc}); starting with none.")
        return {}


def save_operator_profiles(profiles):
    try:
        OPERATOR_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
        OPERATOR_PROFILES_PATH.write_text(json.dumps(profiles, indent=2))
    except Exception as exc:
        print(f"[ERROR] Operator profiles: couldn't save profiles ({exc}).")


class OperatorProfileDialog(QDialog):
    """Collects the operator's callsign and location, as a named,
    save/load/delete-able profile -- see this module's own docstring.

    Call OperatorProfileDialog.run(parent) rather than instantiating
    directly -- unlike ConnectionDialog.get_details() (which hands back
    a details dict for the caller to act on), everything this dialog
    collects is persisted straight to QSettings on accept, so run()
    just needs to report whether the operator actually confirmed
    (True) or cancelled/closed it (False) -- callers that want to react
    to a possible change (re-reading location, refreshing a map marker)
    do so unconditionally either way, the same "cheap enough to just
    always re-read" approach ham_dashboard.py's own _load_observer_
    location already takes.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Operator Profile")
        self._profiles = load_operator_profiles()

        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        self.save_profile_button = QPushButton("Save Profile...")
        self.save_profile_button.clicked.connect(self._on_save_profile_clicked)
        self.delete_profile_button = QPushButton("Delete Profile")
        self.delete_profile_button.clicked.connect(self._on_delete_profile_clicked)

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)

        self.callsign_input = QLineEdit(settings.value("operator_callsign", "") or "")
        self.callsign_input.setPlaceholderText("W1AW")

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

        form.addRow("Callsign:", self.callsign_input)

        location_header = QLabel("<b>Your Location</b> (for satellite Doppler correction and the map):")
        form.addRow(location_header)
        form.addRow(self.gps_button)
        form.addRow("Latitude:", self.lat_input)
        form.addRow("Longitude:", self.lon_input)
        form.addRow("Elevation:", self.elevation_input)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        intro = QLabel(
            "Set up who's operating and from where. Save this as a named "
            "profile to switch between operators/locations later (Ham "
            "Dashboard's own \"Profile...\" button)."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

        self._refresh_profile_combo()
        last_profile = settings.value("last_operator_profile", "")
        if last_profile and last_profile in self._profiles:
            # Triggers _on_profile_selected, which applies it -- takes
            # priority over the plain QSettings values already loaded
            # above, same "saved profile wins" precedent as
            # ConnectionDialog's own startup sequence.
            self.profile_combo.setCurrentIndex(self.profile_combo.findData(last_profile))

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
        return {
            "callsign": self.callsign_input.text().strip().upper(),
            "lat": self.lat_input.value(),
            "lon": self.lon_input.value(),
            "elevation_m": self.elevation_input.value(),
        }

    def _apply_profile(self, profile):
        if "callsign" in profile:
            self.callsign_input.setText(profile["callsign"])
        if "lat" in profile:
            self.lat_input.setValue(profile["lat"])
        if "lon" in profile:
            self.lon_input.setValue(profile["lon"])
        if "elevation_m" in profile:
            self.elevation_input.setValue(profile["elevation_m"])

    def _on_profile_selected(self, _index):
        name = self.profile_combo.currentData()
        if not name:
            return
        profile = self._profiles.get(name)
        if profile:
            self._apply_profile(profile)

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
        save_operator_profiles(self._profiles)
        self._refresh_profile_combo(select_name=name)

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
        save_operator_profiles(self._profiles)
        self._refresh_profile_combo()
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        if settings.value("last_operator_profile") == name:
            settings.remove("last_operator_profile")

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

    def _on_accept(self):
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        callsign = self.callsign_input.text().strip().upper()
        settings.setValue("operator_callsign", callsign)
        settings.setValue("operator_lat", self.lat_input.value())
        settings.setValue("operator_lon", self.lon_input.value())
        settings.setValue("operator_elevation_m", self.elevation_input.value())

        selected_name = self.profile_combo.currentData()
        if selected_name:
            settings.setValue("last_operator_profile", selected_name)

        self.accept()

    @staticmethod
    def run(parent=None):
        """Shows the dialog modally. Returns True if the operator
        confirmed (OK), False if they cancelled/closed it -- see the
        class docstring for why callers generally don't need to
        branch on this (everything's already in QSettings on True;
        nothing changed on False)."""
        dialog = OperatorProfileDialog(parent)
        return dialog.exec() == QDialog.Accepted
