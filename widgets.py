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
from PySide6.QtWidgets import QWidget, QMenu, QToolTip, QSizePolicy

from constants import (
    METER_DEFINITIONS, S_METER_RAW_S9, S_METER_RAW_MAX, S_METER_S9_FRACTION, DEGREES_PER_KNOB_STEP,
    WATERFALL_ROWS, MODE_BANDWIDTH_HZ, PBT_CAPABLE_MODES,
)
import band_plan

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


def _mode_edges_hz(mode, filter_width_hz=None):
    """(low_hz, high_hz) offsets from the tuned frequency for `mode`'s
    passband -- MODE_BANDWIDTH_HZ's own default shape, scaled (preserving
    its low:high ratio -- e.g. USB's all-one-side asymmetry, CW's even
    split) to match filter_width_hz when given. filter_width_hz is the
    radio's own LIVE DSP filter width, from RadioWorker's
    get_filter_width() polling (see is_filter_width_capable) -- using it
    in place of MODE_BANDWIDTH_HZ's fixed approximate default is what
    lets the passband shading/trapezoids track FIL1/2/3 selection and
    any custom bandwidth the operator has actually set, instead of
    always assuming one fixed per-mode number. Returns None if `mode`
    has no MODE_BANDWIDTH_HZ entry at all."""
    if mode not in MODE_BANDWIDTH_HZ:
        return None
    low_hz, high_hz = MODE_BANDWIDTH_HZ[mode]
    if filter_width_hz is None:
        return low_hz, high_hz
    default_sum = low_hz + high_hz
    if default_sum <= 0:
        return low_hz, high_hz
    scale = filter_width_hz / default_sum
    return low_hz * scale, high_hz * scale


def _pbt_trapezoid_hz(level, hz_per_level, width_hz, center_hz):
    """One Twin PBT knob's own filter shape: a copy of the mode's full
    default passband (same width_hz, centered at center_hz when the raw
    0-255 level is at 128) that SLIDES along frequency as the knob turns
    away from center -- confirmed against a real Icom radio (IC-705
    Basic Manual, "Using the Digital Twin PBT"): each PBT knob moves a
    whole filter shape left/right, it does NOT narrow one edge in place
    (this module's earlier, unconfirmed assumption). hz_per_level is
    width_hz / 255.0 (see _pbt_overlap_hz) -- NOT a fixed per-mode
    constant, so a knob can still shift its trapezoid past the point the
    two trapezoids stop overlapping (matches the real control: turning
    past that point does nothing further, it doesn't "wrap" or rescale).
    Returns (low_hz, high_hz) offsets from the tuned frequency."""
    shifted_center = center_hz + (level - 128) * hz_per_level
    return shifted_center - width_hz / 2.0, shifted_center + width_hz / 2.0


def _pbt_overlap_hz(mode, inner_level, outer_level, filter_width_hz=None):
    """The actual resulting passband: the OVERLAP of PBT1 (inner) and
    PBT2 (outer)'s own independently-shifted trapezoids (see
    _pbt_trapezoid_hz) -- matches real Twin PBT behavior, where audio
    only passes where BOTH filters pass simultaneously. Both knobs
    centered (128) means both trapezoids sit exactly on the mode's
    default passband, so the overlap IS that default passband, unchanged
    -- same as this app's very first (pre-Twin-PBT) scope shading.
    Modes Twin PBT doesn't apply to (outside PBT_CAPABLE_MODES, e.g.
    FM/WFM/DV) get hz_per_level=0, i.e. both knobs have no effect and the
    overlap is always just the unmodified default passband, rather than
    treating it as an error.

    hz_per_level = width_hz / 255.0 -- NOT the IC-705 Basic Manual's
    stated "50 Hz steps in SSB/CW/RTTY, 200 Hz in AM" (Misc/
    IC-705_ENG_Basic_9.pdf p.4-4), which turned out, confirmed by live
    calibration against a real IC-705, to overstate the actual shift by
    roughly 10x. Calibration data (CW, FIL1=1200 Hz, PBT1 and PBT2 moved
    TOGETHER so the overlap is a pure shift with no narrowing -- see
    _pbt_trapezoid_hz): passband center read at +100 Hz on the radio's
    own screen with both knobs at their raw minimum, +1300 Hz with both
    at their raw maximum, and the width stayed exactly 1200 Hz (the
    mode's own filter width) at both extremes -- a 1200 Hz swing across
    the 255-level raw range, i.e. exactly one filter-width per full
    sweep, independent of mode. Shared by SpectrumWidget's scope overlay
    and FilterShapeWidget's dedicated filter-shape display; the
    trapezoid WIDTH is still only an approximation against
    MODE_BANDWIDTH_HZ (see that dict's own disclaimer), and center_hz
    below (fixed at the mode's nominal midpoint) is known to be off by
    whatever the radio's actual CW pitch/RTTY mark offset happens to be
    for CW/RTTY specifically -- confirmed by that same calibration run,
    whose midpoint (100+1300)/2=700 Hz didn't land on 0 Hz.

    Returns (overlap_low_hz, overlap_high_hz, default_low_hz,
    default_high_hz, width_hz, center_hz), all as offsets from the tuned
    frequency except width_hz -- or None if `mode` has no
    MODE_BANDWIDTH_HZ entry. overlap_high_hz is never less than
    overlap_low_hz (clamped to equal when the trapezoids no longer
    overlap at all); check overlap_high_hz > overlap_low_hz before
    treating the result as an audible (nonzero-width) passband.

    filter_width_hz, when given, overrides MODE_BANDWIDTH_HZ's fixed
    default total width (scaled via _mode_edges_hz, preserving the
    mode's own low:high shape) with the radio's own live DSP filter
    width -- see RadioWorker.is_filter_width_capable."""
    edges = _mode_edges_hz(mode, filter_width_hz)
    if edges is None:
        return None
    default_low_hz, default_high_hz = edges
    width_hz = default_low_hz + default_high_hz
    center_hz = (default_high_hz - default_low_hz) / 2.0
    hz_per_level = (width_hz / 255.0) if mode in PBT_CAPABLE_MODES else 0.0
    low1, high1 = _pbt_trapezoid_hz(inner_level, hz_per_level, width_hz, center_hz)
    low2, high2 = _pbt_trapezoid_hz(outer_level, hz_per_level, width_hz, center_hz)
    overlap_low_hz = max(low1, low2)
    overlap_high_hz = max(overlap_low_hz, min(high1, high2))
    return overlap_low_hz, overlap_high_hz, default_low_hz, default_high_hz, width_hz, center_hz


class SpectrumWidget(QWidget):
    """Amplitude-vs-frequency plot of the most recent ScopeFrame."""

    # Emitted on a left-click anywhere in the plot, with the clicked
    # x-pixel's frequency (Hz) per _x_to_freq -- RadioWindow connects
    # this to click-to-tune (see main_window.py's _on_scope_clicked).
    # Not emitted if there's no frame yet (nothing to click against).
    frequency_clicked = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setCursor(Qt.CrossCursor)
        self._frame = None
        self._overlays = {}  # corner -> (widget, margin)
        self._tuned_freq_hz = None  # set via set_tuned_frequency() -- whichever receiver the scope is currently following
        self._mode = None           # set via set_mode() -- CONTROL_DEFINITIONS["mode"]'s plain string values (e.g. "USB", "FM")
        self._pbt_inner = None      # set via set_pbt() -- raw 0-255 PBT level, 128=centered; None = no PBT overlay drawn
        self._pbt_outer = None
        self._filter_width_hz = None  # set via set_filter_width_hz() -- live DSP filter width; None = use MODE_BANDWIDTH_HZ's default

    def set_tuned_frequency(self, freq_hz):
        """The actual VFO/receiver frequency the scope is centered on --
        NOT necessarily the same as the scope frame's own start/end span
        (which is what the amplitude trace is drawn against), though in
        the radio's normal center-sweep mode they'll line up. Computing
        the tuning-line position from this against the frame's real
        start/end (rather than just always drawing at the literal middle
        pixel) keeps it correct even if that ever isn't exactly true."""
        self._tuned_freq_hz = freq_hz
        self.update()

    def set_mode(self, mode):
        """`mode` is one of CONTROL_DEFINITIONS["mode"]["options"]'s
        plain string values (e.g. "USB", "FM") -- looked up in
        MODE_BANDWIDTH_HZ for the passband overlay; an unrecognized mode
        just means no bandwidth shading gets drawn (the tuning line
        itself doesn't depend on mode)."""
        self._mode = mode
        self.update()

    def set_pbt(self, inner_level, outer_level):
        """Twin PBT (passband tuning) levels, raw 0-255 (128=centered) --
        see PBT_DEFINITIONS in constants.py. Narrows the passband
        shading drawn in paintEvent from the near/far edge respectively;
        pass None/None (the default) to draw only the plain per-mode
        shading, unchanged, e.g. on a radio without PBT."""
        self._pbt_inner = inner_level
        self._pbt_outer = outer_level
        self.update()

    def set_filter_width_hz(self, width_hz):
        """The radio's own live DSP filter width in Hz, from
        RadioWorker.filter_width_updated -- see is_filter_width_capable.
        Scales the passband shading (both the plain per-mode rectangle
        and the Twin PBT overlap) to match the actual selected FIL1/2/3
        bandwidth instead of MODE_BANDWIDTH_HZ's fixed approximate
        default. Pass None (the default) to fall back to that default,
        e.g. on a radio/connection where this isn't readable."""
        self._filter_width_hz = width_hz
        self.update()

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

    def _freq_to_x(self, freq_hz, w):
        """Maps a frequency to an x-pixel against the current frame's own
        start/end span (NOT assumed to always be exactly `w` wide across
        the whole displayed range -- e.g. a fixed-edge scope configuration
        wouldn't necessarily keep the tuned frequency dead-center, even
        though the radio's normal center-sweep mode does). Returns None
        if there's no frame yet or the span is degenerate (start==end)."""
        if self._frame is None:
            return None
        start, end = self._frame.start_freq_hz, self._frame.end_freq_hz
        if start == end:
            return None
        return (freq_hz - start) / (end - start) * w

    def _x_to_freq(self, x, w):
        """Inverse of _freq_to_x -- maps a clicked x-pixel back to a
        frequency against the current frame's start/end span. Returns
        None if there's no frame yet or the widget has zero width."""
        if self._frame is None or w <= 0:
            return None
        start, end = self._frame.start_freq_hz, self._frame.end_freq_hz
        return start + (x / w) * (end - start)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            freq_hz = self._x_to_freq(event.position().x(), self.width())
            if freq_hz is not None:
                self.frequency_clicked.emit(freq_hz)
        super().mousePressEvent(event)

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

        # Passband/bandwidth shading -- drawn BEFORE the amplitude trace
        # so the trace itself stays fully crisp/visible on top of it,
        # rather than the shading obscuring it. Semi-transparent fill,
        # no border. Uses the radio's own live filter width when known
        # (_filter_width_hz, from RadioWorker.filter_width_updated) --
        # otherwise MODE_BANDWIDTH_HZ's fixed approximate default (see
        # that dict's own comment).
        if self._tuned_freq_hz is not None and self._mode in MODE_BANDWIDTH_HZ:
            low_hz, high_hz = _mode_edges_hz(self._mode, self._filter_width_hz)
            x_low = self._freq_to_x(self._tuned_freq_hz - low_hz, w)
            x_high = self._freq_to_x(self._tuned_freq_hz + high_hz, w)
            if x_low is not None and x_high is not None:
                x_low_edge, x_high_edge = sorted((x_low, x_high))
                # Clip to the visible area -- a wide passband (WFM) can
                # easily extend past either edge of a narrow scope span.
                x_low = max(0.0, x_low_edge)
                x_high = min(float(w), x_high_edge)
                if x_high > x_low:
                    painter.fillRect(QRectF(x_low, 0, x_high - x_low, h), QColor(0, 150, 255, 45))

                # Twin PBT overlay -- the actual resulting passband
                # (the overlap of PBT1/PBT2's own independently-shifted
                # trapezoids, see _pbt_overlap_hz), drawn on top of the
                # plain mode-bandwidth shading above in a distinct color
                # so both the full default filter and the real PBT
                # passband stay simultaneously visible.
                if self._pbt_inner is not None and self._pbt_outer is not None:
                    overlap = _pbt_overlap_hz(
                        self._mode, self._pbt_inner, self._pbt_outer, self._filter_width_hz
                    )
                    if overlap is not None:
                        overlap_low_hz, overlap_high_hz, *_rest = overlap
                        pbt_x_low = self._freq_to_x(self._tuned_freq_hz + overlap_low_hz, w)
                        pbt_x_high = self._freq_to_x(self._tuned_freq_hz + overlap_high_hz, w)
                        if pbt_x_low is not None and pbt_x_high is not None:
                            pbt_x_low = max(0.0, pbt_x_low)
                            pbt_x_high = min(float(w), pbt_x_high)
                            if pbt_x_high > pbt_x_low:
                                painter.fillRect(
                                    QRectF(pbt_x_low, 0, pbt_x_high - pbt_x_low, h), QColor(255, 170, 0, 70)
                                )

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

        # Tuning line -- drawn on top of both the shading and the trace
        # so it always reads clearly regardless of what's under it.
        if self._tuned_freq_hz is not None:
            x_tuned = self._freq_to_x(self._tuned_freq_hz, w)
            if x_tuned is not None and 0 <= x_tuned <= w:
                painter.setPen(QPen(QColor(255, 255, 255, 200), 1))
                painter.drawLine(QPointF(x_tuned, 0), QPointF(x_tuned, h))

        painter.setPen(QColor(150, 150, 150))
        start_text = f"{self._frame.start_freq_hz / 1e6:.4f} MHz"
        end_text = f"{self._frame.end_freq_hz / 1e6:.4f} MHz"
        painter.drawText(5, h - 5, start_text)
        painter.drawText(w - painter.fontMetrics().horizontalAdvance(end_text) - 5, h - 5, end_text)


class FilterShapeWidget(QWidget):
    """Dedicated Twin PBT filter-shape readout, styled after a real
    Icom radio's own "FILTER" screen: BW/SFT numeric readouts, PBT1
    (inner, blue) and PBT2 (outer, green) each drawn as their own
    trapezoid -- same width as the mode's default passband, sliding
    along frequency as its knob turns away from center -- with the
    actual resulting passband (their overlap) filled in orange on top,
    plus the two knobs' own raw-level bars below. See _pbt_overlap_hz's
    docstring for the underlying model (confirmed against a real Icom
    radio) and MODE_BANDWIDTH_HZ's disclaimer for why BW/SFT are an
    approximation rather than a calibrated reading -- rigplane exposes
    no Hz value for the raw 0-255 PBT level at all."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # Expanding (not setFixedWidth/a bare minimum) -- lets this
        # widget actually grow to fill the empty space around it in
        # main_window.py's filter_shape_column, rather than staying
        # pinned at a small fixed size while its container is much
        # bigger. paintEvent already computes everything from
        # self.width()/self.height() (proportional, not pixel-fixed), so
        # it scales up cleanly.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumWidth(200)
        self.setMinimumHeight(200)
        self._mode = None            # set via set_mode()
        self._pbt_inner = None       # set via set_pbt() -- None means "draw both trapezoids centered"
        self._pbt_outer = None
        self._filter_width_hz = None  # set via set_filter_width_hz() -- live DSP filter width; None = use MODE_BANDWIDTH_HZ's default

    def set_mode(self, mode):
        self._mode = mode
        self.update()

    def set_pbt(self, inner_level, outer_level):
        self._pbt_inner = inner_level
        self._pbt_outer = outer_level
        self.update()

    def set_filter_width_hz(self, width_hz):
        """See SpectrumWidget.set_filter_width_hz's docstring -- same
        live-filter-width override, shared math via _pbt_overlap_hz."""
        self._filter_width_hz = width_hz
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(10, 10, 20))
        w, h = self.width(), self.height()

        if self._mode not in MODE_BANDWIDTH_HZ:
            painter.setPen(QColor(120, 120, 120))
            painter.drawText(self.rect(), Qt.AlignCenter, "FILTER")
            return

        inner_level = self._pbt_inner if self._pbt_inner is not None else 128
        outer_level = self._pbt_outer if self._pbt_outer is not None else 128
        overlap_low_hz, overlap_high_hz, default_low_hz, default_high_hz, width_hz, center_hz = _pbt_overlap_hz(
            self._mode, inner_level, outer_level, self._filter_width_hz
        )
        hz_per_level = (width_hz / 255.0) if self._mode in PBT_CAPABLE_MODES else 0.0
        pbt1_low_hz, pbt1_high_hz = _pbt_trapezoid_hz(inner_level, hz_per_level, width_hz, center_hz)
        pbt2_low_hz, pbt2_high_hz = _pbt_trapezoid_hz(outer_level, hz_per_level, width_hz, center_hz)

        # Fixed reference scale, one width_hz of headroom on either side
        # of the default passband -- enough to show the two trapezoids
        # all the way out to the point they stop overlapping at all
        # (which happens once each has shifted width_hz/2 apart from
        # center). A knob can shift much farther than that in raw-level
        # terms (see _pbt_trapezoid_hz), but past this point the overlap
        # is already zero and staying zero, so there's nothing more
        # useful to show -- the trapezoid outline just slides off the
        # edge of the plot, same as the real radio's own FILTER screen
        # keeping a fixed axis rather than rescaling to the current knobs.
        axis_low_hz = center_hz - width_hz
        axis_high_hz = center_hz + width_hz
        axis_span_hz = max(axis_high_hz - axis_low_hz, 1)
        margin_left, margin_right = 8, 8
        top_y = 24
        axis_y = h - 42
        plot_w = w - margin_left - margin_right

        def x_for_offset_hz(offset_hz):
            frac = (offset_hz - axis_low_hz) / axis_span_hz
            return margin_left + frac * plot_w

        x_tuned = x_for_offset_hz(0)
        x_axis_low = x_for_offset_hz(axis_low_hz)
        x_axis_high = x_for_offset_hz(axis_high_hz)

        # BW/SFT readouts, matching the real screen's own labels --
        # rounded to the nearest 10 Hz, same approximate status as the
        # trapezoids themselves (see class docstring). SFT is how far
        # the overlap's own center has moved from center_hz -- the
        # passband's nominal, both-knobs-centered position -- NOT from
        # the tuned frequency itself (which for an asymmetric SSB
        # passband isn't the same thing at all).
        bw_hz = round((overlap_high_hz - overlap_low_hz) / 10) * 10
        sft_hz = round(((overlap_high_hz + overlap_low_hz) / 2 - center_hz) / 10) * 10
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(220, 220, 220))
        bw_text = f"BW {bw_hz / 1000:.2f}k" if bw_hz >= 1000 else f"BW {bw_hz}"
        sft_text = f"SFT {'+' if sft_hz > 0 else ''}{sft_hz}"
        painter.drawText(QRectF(0, 4, w / 2, 16), Qt.AlignCenter, bw_text)
        painter.drawText(QRectF(w / 2, 4, w / 2, 16), Qt.AlignCenter, sft_text)

        # Axis line + tick labels at the mode's own nominal default
        # edges/tuned point (NOT the current axis extremes, which exist
        # only to give the trapezoids room to slide) -- same 3-label
        # style as the real screen.
        painter.setPen(QPen(QColor(90, 90, 90), 1))
        painter.drawLine(QPointF(x_axis_low, axis_y), QPointF(x_axis_high, axis_y))
        font.setPointSize(7)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(150, 150, 150))
        for hz, x in (
            (-default_low_hz, x_for_offset_hz(-default_low_hz)),
            (0, x_tuned),
            (default_high_hz, x_for_offset_hz(default_high_hz)),
        ):
            painter.drawText(QRectF(x - 20, axis_y + 4, 40, 12), Qt.AlignCenter, f"{abs(hz):.0f}" if hz else "0")

        def _trapezoid_path(low_hz, high_hz):
            x_low = x_for_offset_hz(low_hz)
            x_high = x_for_offset_hz(high_hz)
            inset = min(10.0, max(2.0, (x_high - x_low) * 0.15))
            path = QPainterPath()
            path.moveTo(QPointF(x_low, axis_y))
            path.lineTo(QPointF(x_low + inset, top_y))
            path.lineTo(QPointF(x_high - inset, top_y))
            path.lineTo(QPointF(x_high, axis_y))
            path.closeSubpath()
            return path

        # PBT1/PBT2's own trapezoids, drawn as outlines only (their
        # combined effect -- the overlap -- is what gets filled below)
        # so it's clear each knob really does move its own whole shape.
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(70, 140, 255), 1.5))
        painter.drawPath(_trapezoid_path(pbt1_low_hz, pbt1_high_hz))
        painter.setPen(QPen(QColor(90, 210, 90), 1.5))
        painter.drawPath(_trapezoid_path(pbt2_low_hz, pbt2_high_hz))

        # The actual resulting passband -- solid orange fill, on top of
        # both outlines, only where they overlap.
        if overlap_high_hz > overlap_low_hz:
            painter.setPen(QPen(QColor(255, 170, 0), 1.5))
            painter.setBrush(QBrush(QColor(255, 140, 0, 150)))
            painter.drawPath(_trapezoid_path(overlap_low_hz, overlap_high_hz))

        painter.setPen(QPen(QColor(255, 255, 255, 200), 1))
        painter.drawLine(QPointF(x_tuned, top_y - 4), QPointF(x_tuned, axis_y))

        # PBT1 (inner)/PBT2 (outer) level bars -- same blue/green color
        # coding as the trapezoids above, raw 0-255 range with a
        # center-detent tick at 128.
        bar_h = 8
        bar_y1 = h - 2 * bar_h - 8
        bar_y2 = h - bar_h - 2
        font.setPointSize(7)
        painter.setFont(font)
        for label, level, color, bar_y in (
            ("PBT1", inner_level, QColor(70, 140, 255), bar_y1),
            ("PBT2", outer_level, QColor(90, 210, 90), bar_y2),
        ):
            bar_x = margin_left + 30
            bar_w = w - bar_x - margin_right
            painter.setPen(QColor(150, 150, 150))
            painter.drawText(QRectF(margin_left, bar_y - 1, 28, bar_h + 2), Qt.AlignVCenter | Qt.AlignLeft, label)
            painter.setPen(QPen(QColor(60, 60, 60), 1))
            painter.setBrush(QBrush(QColor(25, 25, 35)))
            painter.drawRect(QRectF(bar_x, bar_y, bar_w, bar_h))
            marker_x = bar_x + (level / 255.0) * bar_w
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawRect(QRectF(bar_x, bar_y, marker_x - bar_x, bar_h))
            center_x = bar_x + 0.5 * bar_w
            painter.setPen(QPen(QColor(200, 200, 200), 1))
            painter.drawLine(QPointF(center_x, bar_y), QPointF(center_x, bar_y + bar_h))


class WaterfallWidget(QWidget):
    """Scrolling history of ScopeFrames, newest at the top."""

    # Same click-to-tune contract as SpectrumWidget.frequency_clicked --
    # see that class's docstring. Every row shares the most recently
    # received frame's start/end span for the x-to-frequency mapping
    # (rows are historical amplitude data, but the frequency axis
    # itself doesn't change row-to-row under normal use), so a click
    # anywhere in a column, regardless of which row/how far back in
    # time, maps to the same frequency.
    frequency_clicked = Signal(float)

    def __init__(self, max_rows=WATERFALL_ROWS, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setCursor(Qt.CrossCursor)
        self._max_rows = max_rows
        self._image = None  # QImage buffer: width = pixels/frame, height = max_rows
        self._frame = None  # most recent ScopeFrame -- only used for its start/end span, see _x_to_freq

    def set_frame(self, frame):
        pixels = frame.pixels
        n = len(pixels)
        if n == 0:
            return
        self._frame = frame

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

    def _x_to_freq(self, x, w):
        """Same idea as SpectrumWidget._x_to_freq, against the most
        recently received frame's span rather than a per-row one --
        see this class's frequency_clicked docstring for why that's
        fine. Returns None if no frame has arrived yet or the widget
        has zero width."""
        if self._frame is None or w <= 0:
            return None
        start, end = self._frame.start_freq_hz, self._frame.end_freq_hz
        return start + (x / w) * (end - start)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            freq_hz = self._x_to_freq(event.position().x(), self.width())
            if freq_hz is not None:
                self.frequency_clicked.emit(freq_hz)
        super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._image is None:
            painter.fillRect(self.rect(), QColor(10, 10, 20))
            painter.setPen(QColor(120, 120, 120))
            painter.drawText(self.rect(), Qt.AlignCenter, "Waiting for scope data...")
            return
        painter.drawImage(self.rect(), self._image, self._image.rect())


class BandPlanOverlayWidget(QWidget):
    """A thin strip drawn directly under WaterfallWidget, sharing its
    exact same x-axis (frequency-to-pixel mapping) so a CW/data/phone
    segment boundary lines up pixel-for-pixel with whatever's actually
    on the waterfall above it. Three layers, back to front:

    1. Colored fill per band_plan.py segment (CW-only/CW+data/phone/
       all-modes -- see band_plan.MODE_COLORS), with the minimum
       license class as a short abbreviation ("T"/"G"/"A"/"E") drawn
       centered in each segment wide enough to hold it.
    2. A thin darker band-name label strip along the very top edge
       ("40m", "20m", ...) wherever a band boundary makes room for one.
    3. Small triangular tick marks for every saved memory-channel
       frequency (memories_window.py's all_memory_markers()) that
       falls within the visible span, main_window.py's own job to keep
       fed via set_memories() -- this widget has no memories.json
       access of its own, matching this whole module's "no rigplane/
       asyncio dependency, only ever receives already-computed values"
       contract (see the module's own docstring).

    Frequency-to-pixel mapping is deliberately duplicated from
    SpectrumWidget/WaterfallWidget rather than shared via inheritance
    or a mixin -- three near-identical 4-line methods is simpler to
    follow than a shared base class for a mapping this small, and
    matches how those two widgets already relate to each other (no
    shared base between them either)."""

    _HEIGHT_PX = 26
    _MEMORY_MARKER_HALF_WIDTH_PX = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self._HEIGHT_PX)
        self.setMouseTracking(True)
        self._frame = None
        self._memories = []  # [{"name", "freq_hz"}, ...] -- see set_memories

    def set_frame(self, frame):
        self._frame = frame
        self.update()

    def set_memories(self, memories):
        self._memories = memories
        self.update()

    def _freq_to_x(self, freq_hz, w):
        if self._frame is None:
            return None
        start, end = self._frame.start_freq_hz, self._frame.end_freq_hz
        if start == end:
            return None
        return (freq_hz - start) / (end - start) * w

    def _x_to_freq(self, x, w):
        if self._frame is None or w <= 0:
            return None
        start, end = self._frame.start_freq_hz, self._frame.end_freq_hz
        return start + (x / w) * (end - start)

    def _segment_at(self, x, w):
        """The band_plan segment tuple under widget-x `x`, or None --
        used by mouseMoveEvent for the hover tooltip. A plain linear
        rescan of segments_in_range's own (small, span-bounded) result
        is plenty cheap for a mouse-move handler; no spatial index
        needed at this data size."""
        freq_hz = self._x_to_freq(x, w)
        if freq_hz is None:
            return None
        for band_name, seg_start, seg_end, mode, min_license in band_plan.segments_in_range(freq_hz, freq_hz):
            if seg_start <= freq_hz <= seg_end:
                return band_name, seg_start, seg_end, mode, min_license
        return None

    def _memory_at(self, x, w):
        """The nearest memory marker within _MEMORY_MARKER_HALF_WIDTH_PX
        of widget-x `x`, or None -- checked first in mouseMoveEvent
        since the markers are drawn on top of the segment fill and
        should take hover priority over whatever's underneath them."""
        for memory in self._memories:
            mx = self._freq_to_x(memory["freq_hz"], w)
            if mx is not None and abs(mx - x) <= self._MEMORY_MARKER_HALF_WIDTH_PX:
                return memory
        return None

    def mouseMoveEvent(self, event):
        w = self.width()
        x = event.position().x()
        memory = self._memory_at(x, w)
        if memory is not None:
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"{memory['name']}\n{memory['freq_hz'] / 1e6:.4f} MHz",
                self,
            )
            super().mouseMoveEvent(event)
            return

        hit = self._segment_at(x, w)
        if hit is None:
            QToolTip.hideText()
        else:
            band_name, seg_start, seg_end, mode, min_license = hit
            text = (
                f"{band_name}: {seg_start / 1e6:.4f}-{seg_end / 1e6:.4f} MHz\n"
                f"{band_plan.MODE_LABELS[mode]} -- {min_license} class and above"
            )
            QToolTip.showText(event.globalPosition().toPoint(), text, self)
        super().mouseMoveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        w, h = self.width(), self.height()
        painter.fillRect(self.rect(), QColor(20, 20, 24))

        if self._frame is None:
            return

        start_hz, end_hz = self._frame.start_freq_hz, self._frame.end_freq_hz
        if start_hz == end_hz:
            return

        for band_name, seg_start, seg_end, mode, min_license in band_plan.segments_in_range(start_hz, end_hz):
            x0 = self._freq_to_x(max(seg_start, min(start_hz, end_hz)), w)
            x1 = self._freq_to_x(min(seg_end, max(start_hz, end_hz)), w)
            if x0 is None or x1 is None:
                continue
            x0, x1 = sorted((x0, x1))
            x0 = max(0.0, x0)
            x1 = min(float(w), x1)
            if x1 <= x0:
                continue
            r, g, b = band_plan.MODE_COLORS[mode]
            painter.fillRect(QRectF(x0, 0, x1 - x0, h), QColor(r, g, b, 200))
            painter.setPen(QPen(QColor(10, 10, 12), 1))
            painter.drawLine(QPointF(x0, 0), QPointF(x0, h))

            # License-class abbreviation, only if there's realistically
            # room for it -- an unreadably squashed single letter in a
            # 3px-wide sliver is worse than omitting it.
            if x1 - x0 >= 12:
                painter.setPen(QColor(15, 15, 15))
                font = painter.font()
                font.setPointSize(8)
                font.setBold(True)
                painter.setFont(font)
                painter.drawText(QRectF(x0, 0, x1 - x0, h), Qt.AlignCenter, min_license[0])

        # Memory-channel tick marks, drawn last so they always read
        # clearly on top of whatever segment color is underneath.
        painter.setPen(QPen(QColor(15, 15, 20), 1))
        painter.setBrush(QColor(255, 215, 0))
        for memory in self._memories:
            x = self._freq_to_x(memory["freq_hz"], w)
            if x is None or not (0 <= x <= w):
                continue
            half = self._MEMORY_MARKER_HALF_WIDTH_PX
            triangle = QPainterPath()
            triangle.moveTo(x, 0)
            triangle.lineTo(x - half, half * 2)
            triangle.lineTo(x + half, half * 2)
            triangle.closeSubpath()
            painter.drawPath(triangle)


class MeterWidget(QWidget):
    """Segmented bargraph meter, styled after the LCD meter dock on the
    IC-7300 / IC-9700 / IC-705 touchscreen. Double-click to switch which
    reading it displays (see METER_DEFINITIONS).

    Reads scaling (kind/raw_max/display_max/...) from self._definitions,
    NOT the module-level METER_DEFINITIONS constant directly -- that
    constant is only ever the pre-connection fallback (set here as the
    initial value, before any real radio has connected). Once connected,
    RadioWorker._setup_meters() builds its own per-radio-corrected copy
    (e.g. the power meter's display_max scaled to THIS radio's actual
    rated output, not a generic guess -- see that method's own comment)
    and main_window.py pushes it into every MeterWidget via
    set_definitions(). Root-caused a real "fixed _setup_meters() but the
    displayed power reading didn't change at all" report to this widget
    quietly reading the untouched global constant the whole time instead
    of the corrected per-connection copy it was never given -- the two
    dicts have identical keys, so nothing about that bug was visible
    from a quick read of either side alone."""

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
        self._definitions = METER_DEFINITIONS  # pre-connection fallback -- see class docstring

    @property
    def meter_type(self):
        return self._meter_type

    def set_definitions(self, definitions):
        """Swaps in RadioWorker._setup_meters()'s per-connection-
        corrected METER_DEFINITIONS copy -- called once per widget right
        after a radio connects (main_window.py's _on_meters_ready)."""
        self._definitions = definitions
        self.update()

    def set_meter_type(self, meter_type):
        if meter_type not in self._definitions:
            return
        self._meter_type = meter_type
        self._raw_value = 0
        self.update()

    def set_value(self, raw_value):
        self._raw_value = raw_value
        self.update()

    def mouseDoubleClickEvent(self, event):
        menu = QMenu(self)
        for key, definition in self._definitions.items():
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
        definition = self._definitions[self._meter_type]
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
