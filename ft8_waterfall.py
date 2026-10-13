"""The FT8 tool's waterfall: the radio's audio passband (0 - 3000 Hz) as colour
over time, newest at the top, with a green RX marker and a red TX marker like
WSJT-X's. Click it to place them:

    left click    set the RECEIVE frequency
    right click   set the TRANSMIT frequency
    double-click  set both

The markers are drawn as brackets as wide as the signal itself (about 50 Hz for
FT8, 83 Hz for FT4), so you can see at a glance whether you are on top of
someone.

SpectrumRows turns raw audio into rows of levels (pure numpy, no Qt, so it can
run on the audio thread and be tested exactly); WaterfallWidget draws them.
"""

import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

MAX_HZ = 3000
COLUMNS = 750                 # 4 Hz per column
HISTORY_ROWS = 160
DYNAMIC_RANGE_DB = 40.0
SIGNAL_WIDTH_HZ = {"FT8": 50.0, "FT4": 83.0}
_AXIS_HEIGHT = 16


SILENCE_DB = -40.0   # a row whose median level is below this is silence, not signal


class SpectrumRows:
    """Turns a stream of mono 16-bit PCM into spectrum rows: each is COLUMNS
    levels (dB) covering 0..MAX_HZ, one every ~0.25 s."""

    def __init__(self, sample_rate, row_seconds=0.25):
        self.sample_rate = sample_rate
        size = 1 << max(8, int(round(np.log2(max(sample_rate * row_seconds, 256)))))
        self._size = size
        self._window = np.hanning(size)
        bins = np.arange(size // 2 + 1) * (sample_rate / size)
        self._columns = np.linspace(0, MAX_HZ, COLUMNS, endpoint=False)
        self._bins = bins
        self._buffer = np.zeros(0, dtype=np.float64)

    def feed(self, pcm):
        """Returns a list of rows (numpy arrays of COLUMNS dB values), usually 0 or 1."""
        usable = len(pcm) - (len(pcm) % 2)
        if not usable:
            return []
        self._buffer = np.concatenate([self._buffer, np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float64)])
        rows = []
        while len(self._buffer) >= self._size:
            frame, self._buffer = self._buffer[:self._size], self._buffer[self._size:]
            power = np.abs(np.fft.rfft(frame * self._window)) ** 2 / self._size
            db = 10.0 * np.log10(power + 1e-9)
            rows.append(np.interp(self._columns, self._bins, db).astype(np.float32))
        return rows


def _palette():
    """256 colours: black -> blue -> cyan -> yellow -> red, like a typical waterfall."""
    stops = [(0.0, (0, 0, 0)), (0.25, (0, 0, 160)), (0.5, (0, 190, 220)), (0.75, (240, 230, 40)), (1.0, (255, 40, 0))]
    table = []
    for i in range(256):
        t = i / 255.0
        for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
            if t <= t1:
                f = (t - t0) / (t1 - t0)
                table.append(tuple(int(a + (b - a) * f) for a, b in zip(c0, c1)))
                break
    return table


_PALETTE = _palette()


class WaterfallWidget(QWidget):
    rx_requested = Signal(float)     # left click: Hz
    tx_requested = Signal(float)     # right click: Hz
    both_requested = Signal(float)   # double-click: Hz

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMouseTracking(False)
        self.setToolTip("Left click: set receive frequency   Right click: set transmit frequency   Double-click: set both")
        self._image = QImage(COLUMNS, HISTORY_ROWS, QImage.Format_RGB32)
        self._image.fill(Qt.black)
        self._floor = None
        self.rx_hz = 1500.0
        self.tx_hz = 1500.0
        self.mode = "FT8"

    # ---- data ----

    def add_rows(self, rows):
        for row in rows:
            floor = float(np.median(row))
            silent = floor < SILENCE_DB   # the radio sends no receive audio while transmitting
            if not silent:
                # Slow, so signals stand out -- but snap when the level jumps (the first rows
                # after a transmission), or the whole row would paint saturated red.
                if self._floor is None or abs(floor - self._floor) > 12.0:
                    self._floor = floor
                else:
                    self._floor = self._floor * 0.95 + floor * 0.05
            if silent or self._floor is None:
                level = np.zeros(len(row))
            else:
                level = np.clip((row - self._floor) / DYNAMIC_RANGE_DB, 0.0, 1.0)
            indices = (level * 255).astype(int)
            self._image = self._image.copy(0, 0, COLUMNS, HISTORY_ROWS - 1)
            shifted = QImage(COLUMNS, HISTORY_ROWS, QImage.Format_RGB32)
            painter = QPainter(shifted)
            painter.drawImage(0, 1, self._image)
            painter.end()
            for x in range(COLUMNS):
                r, g, b = _PALETTE[indices[x]]
                shifted.setPixel(x, 0, (0xFF << 24) | (r << 16) | (g << 8) | b)
            self._image = shifted
        if rows:
            self.update()

    def clear(self):
        self._image.fill(Qt.black)
        self._floor = None
        self.update()

    def set_markers(self, rx_hz=None, tx_hz=None):
        if rx_hz is not None:
            self.rx_hz = float(rx_hz)
        if tx_hz is not None:
            self.tx_hz = float(tx_hz)
        self.update()

    # ---- geometry ----

    def _plot_rect(self):
        return QRect(0, _AXIS_HEIGHT, self.width(), max(self.height() - _AXIS_HEIGHT, 1))

    def freq_at(self, x):
        return max(0.0, min(float(MAX_HZ), x / max(self.width(), 1) * MAX_HZ))

    def x_at(self, hz):
        return hz / MAX_HZ * self.width()

    # ---- mouse ----

    def mousePressEvent(self, event):
        hz = round(self.freq_at(event.position().x()))
        if event.button() == Qt.LeftButton:
            self.rx_requested.emit(hz)
        elif event.button() == Qt.RightButton:
            self.tx_requested.emit(hz)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.both_requested.emit(round(self.freq_at(event.position().x())))

    # ---- drawing ----

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101418"))
        plot = self._plot_rect()
        painter.drawImage(plot, self._image)

        painter.setPen(QColor("#aaa"))
        for hz in range(0, MAX_HZ + 1, 500):
            x = int(self.x_at(hz))
            painter.drawLine(x, _AXIS_HEIGHT - 4, x, _AXIS_HEIGHT)
            painter.drawText(QRect(x - 20, 0, 40, _AXIS_HEIGHT - 3), Qt.AlignCenter, str(hz))

        width_hz = SIGNAL_WIDTH_HZ.get(self.mode, 50.0)
        for hz, color in ((self.rx_hz, QColor("#38d84a")), (self.tx_hz, QColor("#ff3b30"))):
            left, right = int(self.x_at(hz)), int(self.x_at(hz + width_hz))
            painter.setPen(QPen(color, 2))
            painter.drawLine(left, plot.top(), left, plot.bottom())
            painter.drawLine(left, plot.top(), right, plot.top())
            painter.drawLine(right, plot.top(), right, plot.top() + 8)
        painter.setPen(QColor("#38d84a")); painter.drawText(QPoint(int(self.x_at(self.rx_hz)) + 4, plot.bottom() - 4), "Rx")
        painter.setPen(QColor("#ff3b30")); painter.drawText(QPoint(int(self.x_at(self.tx_hz)) + 4, plot.bottom() - 18), "Tx")
