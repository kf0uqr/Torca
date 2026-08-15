"""
Custom-painted Qt widgets used in the main radio window: the spectrum
scope, waterfall display, segmented S-meter/multi-meter, and the tuning
knob. All pure QPainter widgets with no rigplane/asyncio dependency --
they only ever receive already-computed values via method calls from
RadioWindow, never talk to the radio themselves.
"""

import math

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPainterPath, QImage, QRadialGradient
from PySide6.QtWidgets import QWidget, QMenu

from constants import (
    METER_DEFINITIONS, S_METER_RAW_S9, S_METER_RAW_MAX, S_METER_S9_FRACTION, DEGREES_PER_KNOB_STEP,
    WATERFALL_ROWS,
)

# Amplitude (0-160, per ScopeFrame.pixels) -> color, modeled on rigplane's
# own "classic" scope theme: dark blue (noise floor) through cyan and
# yellow to red (strong signal).
_COLOR_ANCHORS = [
    (0.0, (0, 0, 40)),
    (0.25, (0, 0, 180)),
    (0.5, (0, 180, 180)),
    (0.75, (255, 255, 0)),
    (1.0, (255, 0, 0)),
]


def amplitude_to_color(amp, max_amp=160):
    frac = max(0.0, min(1.0, amp / max_amp))
    for (f0, c0), (f1, c1) in zip(_COLOR_ANCHORS, _COLOR_ANCHORS[1:]):
        if f0 <= frac <= f1:
            t = (frac - f0) / (f1 - f0)
            r = int(c0[0] + (c1[0] - c0[0]) * t)
            g = int(c0[1] + (c1[1] - c0[1]) * t)
            b = int(c0[2] + (c1[2] - c0[2]) * t)
            return QColor(r, g, b)
    return QColor(*_COLOR_ANCHORS[-1][1])

class SpectrumWidget(QWidget):
    """Amplitude-vs-frequency plot of the most recent ScopeFrame."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self._frame = None
        self._overlays = {}  # corner -> (widget, margin)

    def set_overlay_widget(self, widget, margin=8, corner="top-right"):
        """Reparents `widget` onto this scope as a fixed overlay (e.g.
        the main frequency readout at "top-right", satellite tracking
        info at "top-left") -- Qt composites child widgets on top of a
        parent's own paintEvent automatically, so no z-order trick is
        needed beyond normal parenting. Corners are independent --
        multiple widgets can coexist as long as each has its own corner.

        A widget outside a real layout (which every overlay is, by
        definition -- it's just moved to a fixed position) doesn't
        auto-resize when its content changes; _position_overlays() calls
        adjustSize() on every reposition to compensate, so a widget with
        setFixedWidth()/setFixedHeight() stays fixed (adjustSize()
        respects those) while one without shrinks/grows to fit its
        current content. Call reposition_overlays() after changing an
        overlay's content (e.g. QLabel.setText()) if you're not also
        relying on the next resizeEvent to catch it."""
        self._overlays[corner] = (widget, margin)
        widget.setParent(self)
        self._position_overlays()

    def reposition_overlays(self):
        self._position_overlays()

    def _position_overlays(self):
        for corner, (widget, margin) in self._overlays.items():
            widget.adjustSize()
            x = self.width() - widget.width() - margin if "right" in corner else margin
            y = self.height() - widget.height() - margin if "bottom" in corner else margin
            widget.move(max(0, x), max(0, y))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_overlays()

    def set_frame(self, frame):
        self._frame = frame
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(10, 10, 20))

        if self._frame is None or not self._frame.pixels:
            painter.setPen(QColor(120, 120, 120))
            painter.drawText(self.rect(), Qt.AlignCenter, "Waiting for scope data...")
            return

        pixels = self._frame.pixels
        n = len(pixels)
        w, h = self.width(), self.height()

        painter.setPen(QPen(QColor(0, 220, 120), 1.5))
        path = QPainterPath()
        for i, amp in enumerate(pixels):
            x = (i / (n - 1)) * w if n > 1 else 0
            y = h - (amp / 160.0) * h
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.drawPath(path)

        painter.setPen(QColor(150, 150, 150))
        start_text = f"{self._frame.start_freq_hz / 1e6:.4f} MHz"
        end_text = f"{self._frame.end_freq_hz / 1e6:.4f} MHz"
        painter.drawText(5, h - 5, start_text)
        painter.drawText(w - painter.fontMetrics().horizontalAdvance(end_text) - 5, h - 5, end_text)


class WaterfallWidget(QWidget):
    """Scrolling history of ScopeFrames, newest at the top."""

    def __init__(self, max_rows=WATERFALL_ROWS, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self._max_rows = max_rows
        self._image = None  # QImage buffer: width = pixels/frame, height = max_rows

    def set_frame(self, frame):
        pixels = frame.pixels
        n = len(pixels)
        if n == 0:
            return

        if self._image is None or self._image.width() != n:
            self._image = QImage(n, self._max_rows, QImage.Format_RGB32)
            self._image.fill(QColor(10, 10, 20))

        # Shift the existing image down by one row, dropping the oldest.
        scrolled = QImage(n, self._max_rows, QImage.Format_RGB32)
        painter = QPainter(scrolled)
        painter.drawImage(0, 1, self._image, 0, 0, n, self._max_rows - 1)
        painter.end()
        self._image = scrolled

        for x, amp in enumerate(pixels):
            self._image.setPixelColor(x, 0, amplitude_to_color(amp))

        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._image is None:
            painter.fillRect(self.rect(), QColor(10, 10, 20))
            painter.setPen(QColor(120, 120, 120))
            painter.drawText(self.rect(), Qt.AlignCenter, "Waiting for scope data...")
            return
        painter.drawImage(self.rect(), self._image, self._image.rect())


class MeterWidget(QWidget):
    """Segmented bargraph meter, styled after the LCD meter dock on the
    IC-7300 / IC-9700 / IC-705 touchscreen. Double-click to switch which
    reading it displays (see METER_DEFINITIONS)."""

    NUM_SEGMENTS = 32
    S_TICKS = [("S1", 0.072), ("S3", 0.217), ("S5", 0.361), ("S7", 0.506), ("S9", S_METER_S9_FRACTION)]
    DB_TICKS = [("+20", 0.767), ("+40", 0.884), ("+60", 1.0)]

    meter_type_changed = Signal(str)

    def __init__(self, meter_type="s_meter", parent=None):
        super().__init__(parent)
        self.setMinimumHeight(70)
        self.setMinimumWidth(300)
        self.setToolTip("Double-click to change meter")
        self._raw_value = 0
        self._meter_type = meter_type

    @property
    def meter_type(self):
        return self._meter_type

    def set_meter_type(self, meter_type):
        if meter_type not in METER_DEFINITIONS:
            return
        self._meter_type = meter_type
        self._raw_value = 0
        self.update()

    def set_value(self, raw_value):
        self._raw_value = raw_value
        self.update()

    def mouseDoubleClickEvent(self, event):
        menu = QMenu(self)
        for key, definition in METER_DEFINITIONS.items():
            action = menu.addAction(definition["label"])
            action.setCheckable(True)
            action.setChecked(key == self._meter_type)
            action.triggered.connect(lambda checked=False, k=key: self._select_meter_type(k))
        menu.exec(event.globalPosition().toPoint())

    def _select_meter_type(self, key):
        if key == self._meter_type:
            return
        self.set_meter_type(key)
        self.meter_type_changed.emit(key)

    # ---- S-meter calibration (raw 0-255 -> S0..S9+60dB) ----

    def _s_meter_fraction(self):
        raw = self._raw_value
        if raw <= S_METER_RAW_S9:
            return max(0.0, raw / S_METER_RAW_S9) * S_METER_S9_FRACTION
        span = S_METER_RAW_MAX - S_METER_RAW_S9
        over = (raw - S_METER_RAW_S9) / span if span else 0
        return S_METER_S9_FRACTION + min(1.0, over) * (1.0 - S_METER_S9_FRACTION)

    def _s_meter_label(self):
        raw = self._raw_value
        if raw <= S_METER_RAW_S9:
            s_units = (raw / S_METER_RAW_S9) * 9 if S_METER_RAW_S9 else 0
            return f"S{round(s_units)}"
        span = S_METER_RAW_MAX - S_METER_RAW_S9
        db_over = ((raw - S_METER_RAW_S9) / span) * 60 if span else 0
        return f"S9+{round(db_over)}dB"

    # ---- generic linear meter (raw 0-raw_max -> display_min-display_max) ----

    def _linear_fraction(self, definition):
        raw_max = definition["raw_max"]
        return max(0.0, min(1.0, self._raw_value / raw_max)) if raw_max else 0.0

    def _linear_label(self, definition):
        frac = self._linear_fraction(definition)
        display_min = definition.get("display_min", 0.0)
        display_max = definition["display_max"]
        value = display_min + frac * (display_max - display_min)
        return f"{value:.1f}{definition['unit']}"

    # ---- direct-value meter (reading is already in real units, e.g.
    # watts -- no raw/255 normalization, unlike "linear" above) ----

    def _direct_fraction(self, definition):
        display_min = definition.get("display_min", 0.0)
        display_max = definition["display_max"]
        span = display_max - display_min
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (self._raw_value - display_min) / span))

    def _direct_label(self, definition):
        return f"{self._raw_value:.1f}{definition['unit']}"

    def paintEvent(self, event):
        definition = METER_DEFINITIONS[self._meter_type]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        painter.fillRect(self.rect(), QColor(15, 15, 20))

        margin = 10
        bar_top = 20
        bar_height = h - 40
        bar_left = margin
        bar_width = w - 2 * margin

        if definition["kind"] == "s_meter":
            fraction = self._s_meter_fraction()
            redline_segment = int(S_METER_S9_FRACTION * self.NUM_SEGMENTS)
            label_text = self._s_meter_label()
        elif definition["kind"] == "direct":
            fraction = self._direct_fraction(definition)
            redline_segment = self.NUM_SEGMENTS + 1  # never red for a plain direct-value meter
            label_text = self._direct_label(definition)
        else:
            fraction = self._linear_fraction(definition)
            redline_segment = self.NUM_SEGMENTS + 1  # never red for a plain linear meter
            label_text = self._linear_label(definition)

        lit_segments = int(fraction * self.NUM_SEGMENTS)

        seg_gap = 2
        seg_width = (bar_width - seg_gap * (self.NUM_SEGMENTS - 1)) / self.NUM_SEGMENTS

        for i in range(self.NUM_SEGMENTS):
            x = bar_left + i * (seg_width + seg_gap)
            if i < lit_segments:
                color = QColor(255, 90, 60) if i >= redline_segment else QColor(80, 220, 140)
            else:
                color = QColor(40, 40, 48)
            painter.fillRect(QRectF(x, bar_top, seg_width, bar_height), color)

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        label_y = bar_top + bar_height + 2

        if definition["kind"] == "s_meter":
            painter.setPen(QColor(190, 190, 190))
            for label, frac in self.S_TICKS:
                x = bar_left + frac * bar_width
                painter.drawText(QRectF(x - 15, label_y, 30, 14), Qt.AlignCenter, label)
            painter.setPen(QColor(255, 130, 110))
            for label, frac in self.DB_TICKS:
                x = bar_left + frac * bar_width
                painter.drawText(QRectF(x - 15, label_y, 30, 14), Qt.AlignCenter, label)
        else:
            painter.setPen(QColor(190, 190, 190))
            display_min = definition.get("display_min", 0.0)
            display_max = definition["display_max"]
            for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
                value = display_min + frac * (display_max - display_min)
                x = bar_left + frac * bar_width
                painter.drawText(QRectF(x - 20, label_y, 40, 14), Qt.AlignCenter, f"{value:.0f}")

        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(230, 230, 230))
        painter.drawText(QRectF(bar_left, 0, bar_width * 0.6, bar_top - 2), Qt.AlignLeft | Qt.AlignVCenter, label_text)
        painter.setPen(QColor(150, 150, 150))
        painter.drawText(QRectF(bar_left + bar_width * 0.6, 0, bar_width * 0.4, bar_top - 2), Qt.AlignRight | Qt.AlignVCenter, definition["label"])


class TuningKnobWidget(QWidget):
    """A rotary tuning knob styled after an Icom main-dial knob: a
    knurled metal ring around a raised center cap with a white
    position mark.

    Unlike a slider, a real tuning knob has no fixed endpoints -- it's
    a rotary encoder that spins freely and reports relative clicks
    (detents), not an absolute position. This widget works the same
    way: dragging in a circle emits steps_changed(+1) or (-1) each time
    the drag crosses DEGREES_PER_KNOB_STEP of rotation, and the knob's
    own visual rotation is purely cosmetic (it just keeps spinning).
    Mouse-wheel scrolling over the knob also emits one step per notch.
    """

    steps_changed = Signal(int)  # +1 = clockwise/increase, -1 = counter-clockwise/decrease

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(130, 130)
        self.setCursor(Qt.OpenHandCursor)
        self._rotation = 0.0       # cumulative visual rotation, degrees, wraps mod 360
        self._dragging = False
        self._last_angle = None
        self._angle_accum = 0.0    # rotation since the last emitted step

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self._dragging = True
            self._last_angle = self._angle_at(event.position())
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        angle = self._angle_at(event.position())
        delta = self._shortest_delta(self._last_angle, angle)
        self._last_angle = angle
        self._rotation = (self._rotation + delta) % 360
        self._angle_accum += delta
        self.update()
        while abs(self._angle_accum) >= DEGREES_PER_KNOB_STEP:
            step = 1 if self._angle_accum > 0 else -1
            self._angle_accum -= step * DEGREES_PER_KNOB_STEP
            self.steps_changed.emit(step)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = False
            self._angle_accum = 0.0
            self.setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, event):
        if not self.isEnabled():
            return
        step = 1 if event.angleDelta().y() > 0 else -1
        self._rotation = (self._rotation + step * DEGREES_PER_KNOB_STEP) % 360
        self.update()
        self.steps_changed.emit(step)

    def _angle_at(self, pos):
        cx, cy = self.width() / 2, self.height() / 2
        return math.degrees(math.atan2(pos.y() - cy, pos.x() - cx))

    @staticmethod
    def _shortest_delta(a0, a1):
        """Signed angular difference a1-a0, wrapped to [-180, 180] so a
        drag crossing the -180/180 seam doesn't register as a huge jump."""
        return (a1 - a0 + 180) % 360 - 180

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        radius = min(w, h) / 2 - 4

        # Outer knurled ring, brushed-metal look via a radial gradient.
        outer_gradient = QRadialGradient(cx - radius * 0.3, cy - radius * 0.3, radius * 1.4)
        outer_gradient.setColorAt(0.0, QColor(150, 150, 155))
        outer_gradient.setColorAt(0.55, QColor(90, 90, 95))
        outer_gradient.setColorAt(1.0, QColor(35, 35, 38))
        painter.setBrush(QBrush(outer_gradient))
        painter.setPen(QPen(QColor(15, 15, 15), 2))
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # Knurled grooves around the rim, rotating with the knob.
        painter.save()
        painter.translate(cx, cy)
        painter.rotate(self._rotation)
        for i in range(40):
            painter.save()
            painter.rotate(i * (360 / 40))
            painter.setPen(QPen(QColor(15, 15, 15), 1.4))
            painter.drawLine(QPointF(radius * 0.86, 0), QPointF(radius * 0.98, 0))
            painter.restore()
        painter.restore()

        # Raised center cap.
        inner_radius = radius * 0.70
        inner_gradient = QRadialGradient(cx - inner_radius * 0.3, cy - inner_radius * 0.3, inner_radius * 1.3)
        inner_gradient.setColorAt(0.0, QColor(75, 75, 80))
        inner_gradient.setColorAt(1.0, QColor(25, 25, 28))
        painter.setBrush(QBrush(inner_gradient))
        painter.setPen(QPen(QColor(10, 10, 10), 1))
        painter.drawEllipse(QPointF(cx, cy), inner_radius, inner_radius)

        # White position mark, like the paint dab on an Icom VFO knob.
        painter.save()
        painter.translate(cx, cy)
        painter.rotate(self._rotation)
        pen = QPen(QColor(235, 235, 235), 3)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(0, -inner_radius * 0.28), QPointF(0, -inner_radius * 0.82))
        painter.restore()

        if not self.isEnabled():
            painter.setBrush(QColor(0, 0, 0, 120))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(cx, cy), radius, radius)
