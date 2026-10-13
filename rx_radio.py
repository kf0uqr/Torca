"""Which radio a tool listens to.

A tool belongs to the radio it was started on: that is the radio it transmits through. For decoding, the
tool can take its audio from a different connected radio instead -- e.g. transmit FT8 on the IC-9700 while
decoding what the IC-7300 hears. ReceiveRadioSelector is the small "Receive radio" box each tool carries; by
default it means "this radio". The choice is remembered per tool and radio, is fixed while the tool is
decoding (stop to change it), and falls back to "this radio" if the chosen radio is closed.
"""

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget

_SETTINGS_ORG = "IcomRadioApp"
_SETTINGS_APP = "RadioControl"


class _LiveCombo(QComboBox):
    def __init__(self, refresh, parent=None):
        super().__init__(parent)
        self._refresh = refresh

    def showPopup(self):
        self._refresh()
        super().showPopup()


class ReceiveRadioSelector(QWidget):
    changed = Signal()     # the choice changed
    lost = Signal()        # the chosen radio was closed: the selection is back to "this radio"

    def __init__(self, owner_radio, tool_name, parent=None):
        super().__init__(parent)
        self._owner = owner_radio
        self._key = f"rx_radio/{owner_radio.workspace_key}/{tool_name}"
        self._chosen = None
        self._watching = None
        self.combo = _LiveCombo(self._refresh_choices)
        self.combo.setToolTip(
            "Where this tool gets the audio it decodes. 'This radio' (the default), or another connected radio -- "
            "transmitting still goes through this tool's own radio. Fixed while decoding.")
        self.combo.activated.connect(self._on_activated)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Receive radio:"))
        layout.addWidget(self.combo)
        saved = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(self._key, "")
        self._chosen = next((w for w in owner_radio.other_radios() if saved and w.workspace_key == saved), None)
        self._watch(self._chosen)
        self._refresh_choices()

    # ---- the choice ----

    def radio(self):
        """The radio whose audio is decoded: the chosen one, else the owner."""
        chosen = self._chosen
        return chosen if chosen is not None and chosen in self._owner.other_radios() else self._owner

    def worker(self):
        return self.radio().worker

    def is_other(self):
        return self.radio() is not self._owner

    def set_locked(self, locked):
        self.combo.setEnabled(not locked)

    def select(self, radio_window):
        """Choose a radio (None = this radio) -- for restoring and for tests."""
        self._chosen = radio_window if radio_window is not self._owner else None
        self._watch(self._chosen)
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(self._key, self._chosen.workspace_key if self._chosen else "")
        self._refresh_choices()
        self.changed.emit()

    def _refresh_choices(self):
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem(f"This radio ({self._owner.display_name()})", None)
        for window in self._owner.other_radios():
            self.combo.addItem(f"{window.display_name()}  ({window._connection_label})", window)
        index = self.combo.findData(self._chosen) if self._chosen is not None else 0
        self.combo.setCurrentIndex(max(index, 0))
        self.combo.blockSignals(False)

    def _on_activated(self, _index):
        self.select(self.combo.currentData())

    # ---- the chosen radio going away ----

    def _watch(self, window):
        if window is self._watching:
            return
        self._watching = window
        if window is not None:
            window.closed.connect(self._on_radio_closed)

    def _on_radio_closed(self, window):
        if window is self._chosen:
            self._chosen = None
            self._watching = None
            self._refresh_choices()
            self.lost.emit()
