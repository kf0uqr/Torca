"""Tests for direwolf_backend.py's pure-logic pieces (KISS framing,
port selection) plus a real end-to-end run against an actual direwolf
subprocess when one is installed (skipped otherwise, e.g. CI without
direwolf on PATH)."""

import shutil
import sys

from direwolf_backend import _kiss_unescape, _find_free_port, FEND, FESC, TFEND, TFESC


def test_kiss_unescape_no_escapes():
    assert _kiss_unescape(bytes([0x00, 0x01, 0x02])) == bytes([0x00, 0x01, 0x02])


def test_kiss_unescape_fend_escape():
    assert _kiss_unescape(bytes([FESC, TFEND, 0x01])) == bytes([FEND, 0x01])


def test_kiss_unescape_fesc_escape():
    assert _kiss_unescape(bytes([FESC, TFESC, 0x01])) == bytes([FESC, 0x01])


def test_kiss_unescape_trailing_incomplete_escape():
    # A dangling FESC with nothing after it (truncated frame) -- must
    # not raise, just pass the byte through.
    assert _kiss_unescape(bytes([0x01, FESC])) == bytes([0x01, FESC])


def test_find_free_port_in_direwolf_range():
    for _ in range(20):
        port = _find_free_port()
        assert 20000 <= port <= 49151


def test_live_direwolf_decode():
    if shutil.which("direwolf") is None:
        print("SKIPPED (direwolf not installed)")
        return

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from direwolf_backend import DirewolfBackend
    from aprs import build_position_packet_pcm

    app = QApplication.instance() or QApplication([])
    backend = DirewolfBackend(8000, mycall="N0CALL")
    errors = []
    packets = []
    backend.error.connect(errors.append)
    backend.packet_received.connect(packets.append)
    backend.start()

    pcm = build_position_packet_pcm("N0CALL", "APRS", 40.0, -105.0, "/", "-", "test", sample_rate=8000)
    CHUNK = 320

    def send_chunks():
        i = [0]

        def send_one():
            chunk = pcm[i[0]:i[0] + CHUNK]
            if not chunk:
                QTimer.singleShot(2000, finish)
                return
            backend.feed_audio(chunk)
            i[0] += CHUNK
            QTimer.singleShot(20, send_one)

        send_one()

    def finish():
        backend.stop()
        app.quit()

    QTimer.singleShot(1000, send_chunks)
    QTimer.singleShot(15000, finish)
    app.exec()

    assert not errors, f"unexpected backend errors: {errors}"
    assert len(packets) == 1, f"expected exactly 1 decoded packet, got {packets}"
    assert packets[0]["source"] == "N0CALL"
    assert packets[0]["info"]["type"] == "position"


if __name__ == "__main__":
    tests = [
        test_kiss_unescape_no_escapes,
        test_kiss_unescape_fend_escape,
        test_kiss_unescape_fesc_escape,
        test_kiss_unescape_trailing_incomplete_escape,
        test_find_free_port_in_direwolf_range,
        test_live_direwolf_decode,
    ]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    print("All direwolf_backend tests passed.")
    sys.exit(0)
