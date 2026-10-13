"""The Satellite tool: how a radio takes part in a satellite pass.

A radio has no satellite role when it connects. Opening "Satellite Tool" from a radio's tools menu
makes THAT radio the pass's transmit radio, and by default it does everything itself (full duplex:
a dual-receiver radio such as the IC-9700 receives the downlink on its second receiver while it
transmits the uplink). The tool's "Receive radio" box can instead name a second connected radio
for the downlink -- the "poor man's full duplex" pair -- and then this radio is uplink-only and the
other is downlink-only. Closing the tool (or closing either radio) puts the radios back to being
ordinary ones.

The tool also holds the pass planning and tracking controls: the passes list, the satellite and
transponder pickers, Start/Stop Tracking, and the live Doppler readout. Those are the dashboard's
single set of widgets (there is one shared tracking session -- see satellite_session.py), which the
tool takes into itself while it is the active one. Only one Satellite tool can be active at a time:
starting it on another radio moves everything there, and the earlier tool says so.
"""

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

_SETTINGS_ORG = "IcomRadioApp"
_SETTINGS_APP = "RadioControl"
NO_RECEIVE_RADIO = "this radio receives too"


class _LiveRadioCombo(QComboBox):
    """A combo of the other connected radios that re-reads who is connected each time it is opened."""

    def __init__(self, refresh, parent=None):
        super().__init__(parent)
        self._refresh = refresh

    def showPopup(self):
        self._refresh()
        super().showPopup()


class SatelliteToolWindow(QWidget):
    def __init__(self, radio_window, dashboard):
        super().__init__()
        self._radio = radio_window
        self._dashboard = dashboard
        self._active = False
        self._receive = None              # the RadioWindow chosen for the downlink, if any
        self._assigned = set()            # radios currently given a satellite role by this tool
        self._watched = None              # the receive radio whose closing we are listening for
        self._closing_for_real = False
        self.setWindowTitle(radio_window.tool_title("Satellite Tool"))
        self.resize(900, 700)

        self.transmit_label = QLabel()
        self.receive_combo = _LiveRadioCombo(self._refresh_receive_choices)
        self.receive_combo.setToolTip(
            "Leave on 'This radio receives too' for one dual-receiver radio (such as an IC-9700) doing the whole "
            "pass. Pick another connected radio to use it for the downlink while this one only transmits.")
        self.receive_combo.activated.connect(self._on_receive_chosen)
        self.role_label = QLabel()
        self.role_label.setWordWrap(True)
        self.role_label.setStyleSheet("color: #aaa;")

        row = QHBoxLayout()
        row.addWidget(self.transmit_label)
        row.addStretch()
        row.addWidget(QLabel("Receive radio:"))
        row.addWidget(self.receive_combo)
        self.page_holder = QVBoxLayout()
        self.moved_label = QLabel("")
        self.moved_label.setAlignment(Qt.AlignCenter)
        self.moved_label.setWordWrap(True)
        self.moved_label.setStyleSheet("color: #aaa;")
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.role_label)
        layout.addLayout(self.page_holder, 1)
        layout.addWidget(self.moved_label)

        self._refresh_receive_choices()
        saved = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(self._setting_key(), "")
        self._receive = self._find_radio(saved)
        self._select_in_combo()
        self._refresh_texts()

    # ---- which radios ----

    def _setting_key(self):
        return f"satellite_tool/receive_radio/{self._radio.workspace_key}"

    def _other_radios(self):
        return self._radio.other_radios()

    def _find_radio(self, key):
        return next((w for w in self._other_radios() if key and w.workspace_key == key), None)

    def _refresh_receive_choices(self):
        chosen = self._receive
        self.receive_combo.blockSignals(True)
        self.receive_combo.clear()
        self.receive_combo.addItem(NO_RECEIVE_RADIO.capitalize(), None)
        for window in self._other_radios():
            self.receive_combo.addItem(f"{window.display_name()}  ({window._connection_label})", window)
        index = self.receive_combo.findData(chosen) if chosen is not None else 0
        self.receive_combo.setCurrentIndex(max(index, 0))
        self.receive_combo.blockSignals(False)

    def _select_in_combo(self):
        index = self.receive_combo.findData(self._receive) if self._receive is not None else 0
        self.receive_combo.setCurrentIndex(max(index, 0))

    def _on_receive_chosen(self, _index):
        self._receive = self.receive_combo.currentData()
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(self._setting_key(), self._receive.workspace_key if self._receive else "")
        if self._active:
            self._apply_roles()
        self._refresh_texts()

    # ---- the roles ----

    def _live_receive_radio(self):
        radio = self._receive
        return radio if radio is not None and radio in self._other_radios() else None

    def _apply_roles(self):
        receive = self._live_receive_radio()
        wanted = {self._radio: "full_duplex" if receive is None else "uplink"}
        if receive is not None:
            wanted[receive] = "downlink"
        for window in list(self._assigned - set(wanted)):
            window.set_satellite_role("non_sat")
        for window, role in wanted.items():
            window.set_satellite_role(role)
        self._assigned = set(wanted)
        if receive is not None and receive is not self._watched:
            receive.closed.connect(self._on_receive_radio_closed)
            self._watched = receive
        self._refresh_texts()

    def _on_receive_radio_closed(self, window):
        if window is self._receive:
            self._receive = None              # it is gone: this radio is on its own again
            self._watched = None
            self._select_in_combo()
        if self._active:
            self._apply_roles()
        self._refresh_receive_choices()

    def _refresh_texts(self):
        self.transmit_label.setText(f"<b>Transmit radio:</b> {self._radio.display_name()}")
        receive = self._live_receive_radio()
        if receive is None:
            self.role_label.setText(
                f"{self._radio.display_name()} does the whole pass (transmitting the uplink, receiving the downlink)."
                " Dual-receiver radios receive on their second receiver while transmitting.")
        else:
            self.role_label.setText(
                f"{self._radio.display_name()} transmits the uplink; {receive.display_name()} receives the downlink "
                "and never transmits.")

    # ---- taking over / giving back the shared controls ----

    def activate(self):
        """Makes this the active Satellite tool: the shared pass controls come here, and the radios take their roles."""
        if not self._active:
            self._dashboard.claim_satellite_tool(self)
            page = self._dashboard.satellite_passes_page
            self.page_holder.addWidget(page)
            page.show()
            self.moved_label.setText("")
            self._active = True
        self._refresh_receive_choices()
        self._apply_roles()

    def release(self, moved_to=None):
        """Gives the controls and the radios back (the tool is closing, or another radio's tool is taking over)."""
        if not self._active:
            return
        self._active = False
        for window in list(self._assigned):
            window.set_satellite_role("non_sat")
        self._assigned = set()
        page = self._dashboard.satellite_passes_page
        self.page_holder.removeWidget(page)
        page.hide()
        page.setParent(None)
        self._dashboard.release_satellite_tool(self)
        self.moved_label.setText(
            f"The satellite controls are now in the Satellite tool on {moved_to}." if moved_to
            else "This Satellite tool is not active: its radios are ordinary radios again. Reopen it from the radio's tools menu.")

    # ---- lifecycle ----

    def closing_for_real(self):
        """The owning radio is going away."""
        self._closing_for_real = True
        self.close()

    def closeEvent(self, event):
        self.release()
        if self._closing_for_real:
            event.accept()
            return
        event.ignore()
        self.hide()
