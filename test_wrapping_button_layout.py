"""WrappingButtonLayout: a balanced, equal-width wrapping row of buttons."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

from wrapping_button_layout import WrappingButtonLayout


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _bar(count, width=None):
    widget = QWidget()
    layout = WrappingButtonLayout(widget)
    buttons = [QPushButton(f"{20 + i}m") for i in range(count)]
    for button in buttons:
        layout.addWidget(button)
    if width is not None:
        widget.resize(width, layout.heightForWidth(width))
    widget.show()
    QApplication.processEvents()
    return widget, layout, buttons


def test_one_row_when_everything_fits_and_buttons_share_the_width_equally(app):
    widget, layout, buttons = _bar(5, 800)
    assert layout.grid_for_width(800) == (5, 1)
    widths = {b.width() for b in buttons}
    assert len(widths) == 1
    assert buttons[-1].geometry().right() <= 800 and buttons[-1].geometry().right() > 800 - 8   # fills the row like an HBox


def test_wraps_into_balanced_rows_not_a_lone_orphan(app):
    # 13 buttons that do not all fit: two rows of 7 + 6, never 12 + 1
    widget, layout, buttons = _bar(13)
    cell = max(b.sizeHint().width() for b in buttons)
    width = 12 * (cell + 6)                                  # room for 12 columns, not 13
    assert layout.grid_for_width(width) == (7, 2)
    widget.resize(width, layout.heightForWidth(width))
    app.processEvents()
    rows = {}
    for b in buttons:
        rows.setdefault(b.y(), []).append(b)
    assert sorted(len(r) for r in rows.values()) == [6, 7]
    assert len({b.width() for b in buttons}) == 1             # every button the same width


def test_height_grows_as_the_width_shrinks_and_nothing_overlaps_or_escapes(app):
    widget, layout, buttons = _bar(13)
    cell = max(b.sizeHint().width() for b in buttons)
    heights = [layout.heightForWidth(w) for w in (13 * (cell + 6), 7 * (cell + 6), 4 * (cell + 6), cell)]
    assert heights == sorted(heights) and heights[0] < heights[-1]
    for width in (13 * (cell + 6), 7 * (cell + 6), 4 * (cell + 6), cell + 2):
        widget.resize(width, layout.heightForWidth(width))
        app.processEvents()
        for i, a in enumerate(buttons):
            assert a.geometry().right() < width and a.geometry().bottom() < widget.height(), (width, a.text())
            for b in buttons[i + 1:]:
                assert not a.geometry().intersects(b.geometry()), (width, a.text(), b.text())


def test_minimum_width_is_a_single_button_so_a_window_can_be_narrow(app):
    widget, layout, buttons = _bar(13)
    assert layout.minimumSize().width() <= max(b.minimumSizeHint().width() for b in buttons) + 2
    assert layout.hasHeightForWidth()


def test_empty_and_single_button_layouts_are_fine(app):
    widget, layout, _ = _bar(0, 300)
    assert layout.count() == 0 and layout.grid_for_width(300) == (0, 0)
    widget2, layout2, buttons2 = _bar(1, 300)
    assert layout2.grid_for_width(300) == (1, 1) and buttons2[0].width() >= 290


def test_works_inside_a_vertical_layout_with_other_widgets(app):
    host = QWidget()
    outer = QVBoxLayout(host)
    flow = WrappingButtonLayout()
    for i in range(13):
        flow.addWidget(QPushButton(f"{i}m"))
    outer.addLayout(flow)
    below = QPushButton("below")
    outer.addWidget(below)
    narrow = 300
    host.resize(narrow, outer.totalHeightForWidth(narrow) if hasattr(outer, "totalHeightForWidth") else 400)
    host.show()
    app.processEvents()
    assert below.y() >= flow.geometry().bottom() - 1          # the widget below is pushed down below the wrapped rows
