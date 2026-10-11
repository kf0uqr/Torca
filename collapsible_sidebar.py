"""A narrow sidebar that expands to show its controls and collapses to a
thin strip with just an arrow, so the controls can be shown or hidden.

Qt-only and generic: the owner adds widgets to content_layout() and connects
to toggled(expanded) (e.g. to remember the state). Collapsed, the sidebar is
only as wide as its arrow button -- the controls are hidden, not destroyed,
so their state (checked buttons, selected combo items) is unaffected and
everything wired to them keeps working."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QSizePolicy, QToolButton, QVBoxLayout, QWidget


class CollapsibleSidebar(QWidget):
    toggled = Signal(bool)  # True = expanded

    def __init__(self, title, expanded=True, parent=None):
        super().__init__(parent)
        self._title = title
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)

        self.header = QToolButton()
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setAutoRaise(True)
        self.header.clicked.connect(self._on_header_clicked)

        self._content = QFrame()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 4, 0, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.addWidget(self.header)
        layout.addWidget(self._content)
        layout.addStretch()

        self._expanded = None
        self.set_expanded(expanded, emit=False)

    def content_layout(self):
        return self._content_layout

    def is_expanded(self):
        return self._expanded

    def set_expanded(self, expanded, emit=True):
        expanded = bool(expanded)
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._content.setVisible(expanded)
        self.header.setArrowType(Qt.LeftArrow if expanded else Qt.RightArrow)
        self.header.setText(self._title if expanded else "")
        self.header.setToolTip(f"Hide {self._title.lower()}" if expanded else f"Show {self._title.lower()}")
        if emit:
            self.toggled.emit(expanded)

    def _on_header_clicked(self):
        self.set_expanded(not self._expanded)
