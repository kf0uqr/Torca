import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import ft8_waterfall as wf


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("rate", [8000, 12000, 48000])
def test_a_tone_lands_in_the_right_column(rate):
    t = np.arange(rate * 2) / rate
    pcm = (np.sin(2 * np.pi * 1234 * t) * 9000).astype("<i2").tobytes()
    rows = wf.SpectrumRows(rate).feed(pcm)
    assert len(rows) >= 3 and all(len(r) == wf.COLUMNS for r in rows)
    peak = int(np.argmax(rows[1])) * wf.MAX_HZ / wf.COLUMNS
    assert abs(peak - 1234) <= 8


def test_partial_chunks_accumulate_into_rows():
    spectrum = wf.SpectrumRows(8000)
    chunk = b"\x01\x02" * 500                      # 500 samples: a row needs 2048
    assert [len(spectrum.feed(chunk)) for _ in range(5)] == [0, 0, 0, 0, 1]


def test_clicks_set_receive_transmit_or_both_where_the_pointer_is(app):
    widget = wf.WaterfallWidget()
    widget.resize(600, 200)
    got = []
    widget.rx_requested.connect(lambda hz: got.append(("rx", hz)))
    widget.tx_requested.connect(lambda hz: got.append(("tx", hz)))
    widget.both_requested.connect(lambda hz: got.append(("both", hz)))
    x = lambda hz: int(hz / wf.MAX_HZ * 600)
    QTest.mouseClick(widget, Qt.LeftButton, Qt.NoModifier, QPoint(x(1000), 100))
    QTest.mouseClick(widget, Qt.RightButton, Qt.NoModifier, QPoint(x(2000), 100))
    QTest.mouseDClick(widget, Qt.LeftButton, Qt.NoModifier, QPoint(x(500), 100))
    kinds = [k for k, _ in got]
    assert kinds[0] == "rx" and abs(got[0][1] - 1000) <= 5
    assert kinds[1] == "tx" and abs(got[1][1] - 2000) <= 5
    assert "both" in kinds and abs([hz for k, hz in got if k == "both"][0] - 500) <= 5


def test_rows_scroll_in_from_the_top_and_show_signals_brighter_than_noise(app):
    widget = wf.WaterfallWidget()
    quiet = np.full(wf.COLUMNS, -20.0, dtype=np.float32)
    loud = quiet.copy(); loud[250] = 30.0
    widget.add_rows([quiet] * 5 + [loud])
    top = widget._image
    assert top.pixelColor(250, 0).red() > top.pixelColor(100, 0).red()          # the signal row, at the top, is bright
    assert top.pixelColor(250, 1).red() == top.pixelColor(100, 1).red()          # the quiet row under it is not
    widget.clear()
    assert widget._image.pixelColor(250, 0).red() == 0
    widget.resize(600, 200); widget.grab()                                       # painting does not raise
