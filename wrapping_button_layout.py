"""A layout that lays buttons out in as few, evenly filled rows as fit.

Used for the radio window's band buttons: an IC-705 has 13 of them, which in
a single row make the whole window wider than a half-screen workspace and
force a horizontal scroll bar. Here they wrap instead.

Every item gets the same width, so it reads as a tidy grid rather than a
ragged flow. With room for them all it is one full-width row, exactly like
a QHBoxLayout; when it must wrap, rows are balanced (13 buttons in two rows
become 7 + 6, not 12 + 1).

A layout with height-for-width: its height depends on the width it is given.
Qt propagates that through nested layouts, and QScrollArea (which hosts every
radio window) honours it, so the content simply gets taller as it gets
narrower."""

import math

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import QLayout


class WrappingButtonLayout(QLayout):
    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    # ---- QLayout interface ----

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def minimumSize(self):
        # The narrowest it can get: one button wide (it just gets taller).
        size = QSize()
        for item in self._visible_items():
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def sizeHint(self):
        return self.minimumSize()

    # ---- layout maths ----

    def _visible_items(self):
        return [item for item in self._items if not item.isEmpty()]

    def grid_for_width(self, width):
        """(columns, rows) used at this width -- exposed for tests."""
        items = self._visible_items()
        if not items:
            return 0, 0
        margins = self.contentsMargins()
        available = width - margins.left() - margins.right()
        spacing = max(self.spacing(), 0)
        cell_min = max(item.sizeHint().width() for item in items)
        max_columns = max(1, (available + spacing) // (cell_min + spacing))
        rows = math.ceil(len(items) / max_columns)
        return math.ceil(len(items) / rows), rows

    def _arrange(self, rect, apply):
        items = self._visible_items()
        margins = self.contentsMargins()
        if not items:
            return margins.top() + margins.bottom()
        spacing = max(self.spacing(), 0)
        columns, rows = self.grid_for_width(rect.width())
        available = rect.width() - margins.left() - margins.right()
        cell_width = max((available - (columns - 1) * spacing) // columns, 1)
        cell_height = max(item.sizeHint().height() for item in items)
        if apply:
            for position, item in enumerate(items):
                row, column = divmod(position, columns)
                item.setGeometry(QRect(
                    rect.x() + margins.left() + column * (cell_width + spacing),
                    rect.y() + margins.top() + row * (cell_height + spacing),
                    cell_width, cell_height,
                ))
        return rows * cell_height + (rows - 1) * spacing + margins.top() + margins.bottom()
