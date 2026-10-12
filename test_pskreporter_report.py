"""Reporting what we hear to PSKReporter: the exact bytes of the datagram, the
rate rules, and the path from a decoded FT8 message to the map and the upload."""

import os
import struct
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import ft8_qso
import pskreporter_report as pr


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def spot(call="W1AW", hz=14_074_500, snr=-12, mode="FT8", grid="FN31", epoch=1_700_000_000):
    return pr.Spot(call, hz, snr, mode, grid, epoch)


def test_template_sets_are_the_specified_bytes():
    spec = "00 03 00 24 99 92 00 03 00 01 80 02 FF FF 00 00 76 8F 80 04 FF FF 00 00 76 8F 80 08 FF FF 00 00 76 8F 00 00"
    assert pr._RECEIVER_TEMPLATE == bytes.fromhex(spec.replace(" ", ""))
    sender = ("00 02 00 44 99 93 00 08 80 01 FF FF 00 00 76 8F 80 05 00 04 00 00 76 8F 80 06 00 01 00 00 76 8F "
              "80 07 00 01 00 00 76 8F 80 0A FF FF 00 00 76 8F 80 0B 00 01 00 00 76 8F 80 03 FF FF 00 00 76 8F 00 96 00 04")
    assert pr._SENDER_TEMPLATE == bytes.fromhex(sender.replace(" ", ""))


def test_receiver_record_matches_the_spec_example():
    # from pskreporter.info/pskdev.html: callsign N1DQ, locator FN42hn, software "Homebrew v5.6"
    expected = bytes.fromhex("99 92 00 20 04 4E 31 44 51 06 46 4E 34 32 68 6E 0D 48 6F 6D 65 62 72 65 77 20 76 35 2E 36 00 00".replace(" ", ""))
    assert pr._receiver_record("N1DQ", "FN42hn", "Homebrew v5.6") == expected


def test_datagram_layout():
    (datagram, batch), = pr.build_datagrams("KF0UQR", "EM18ge", [spot()], session_id=0xCAFE, sequence=7, now=1_700_000_100)
    version, length, when, sequence, session = struct.unpack(">HHIII", datagram[:16])
    assert (version, length, when, sequence, session) == (10, len(datagram), 1_700_000_100, 7, 0xCAFE)
    assert len(datagram) % 4 == 0
    assert pr._RECEIVER_TEMPLATE in datagram and pr._SENDER_TEMPLATE in datagram
    block = datagram[datagram.index(b"\x99\x93", 16 + 36 + 68):]            # the sender data block, after the templates
    assert block[0:2] == b"\x99\x93" and struct.unpack(">H", block[2:4])[0] == len(block)
    body = block[4:]
    assert body[0] == 4 and body[1:5] == b"W1AW"
    assert struct.unpack(">I", body[5:9])[0] == 14_074_500
    assert struct.unpack(">b", body[9:10])[0] == -12 and body[10] == 0       # SNR, iMD
    assert body[11] == 3 and body[12:15] == b"FT8" and body[15] == 1          # mode, informationSource = automatic
    assert body[16] == 4 and body[17:21] == b"FN31"
    assert struct.unpack(">I", body[21:25])[0] == 1_700_000_000


def test_many_spots_are_split_into_datagrams_that_fit_and_count_up_the_sequence():
    spots = [spot(call=f"K{i}ABC", epoch=1_700_000_000 + i) for i in range(200)]
    parts = pr.build_datagrams("KF0UQR", "EM18ge", spots, 1, sequence=0, now=1_700_000_500)
    assert len(parts) > 1 and all(len(d) <= pr.MAX_DATAGRAM_BYTES for d, _ in parts)
    assert sum(len(b) for _, b in parts) == 200
    sequences = [struct.unpack(">I", d[8:12])[0] for d, _ in parts]
    assert sequences == [sum(len(b) for _, b in parts[:i]) for i in range(len(parts))]


def test_uploader_batches_dedupes_and_flushes(app):
    sent, now = [], {"t": 1000.0}
    uploader = pr.PskReporterUploader("kf0uqr", "EM18ge", send=sent.append, clock=lambda: now["t"])
    assert uploader.add(spot()) and not uploader.add(spot(snr=-5))              # same station, same band: once
    assert uploader.add(spot(hz=7_074_500)) and uploader.add(spot(call="K1ABC"))
    assert sent == []                                                           # nothing goes out until the timer
    assert uploader.flush() == 3 and len(sent) == 1
    now["t"] += pr.REPEAT_SECONDS + 1
    assert uploader.add(spot())                                                 # long enough ago: reportable again
    uploader.stop()
    assert len(sent) == 2 and uploader.sent_count == 4
    assert not uploader.add(pr.Spot("", 14_074_000, 0, "FT8", "", 0)) and not uploader.add(spot(hz=0))


def test_a_network_failure_loses_only_that_batch(app):
    def broken(datagram):
        raise OSError("network unreachable")
    uploader = pr.PskReporterUploader("KF0UQR", "EM18ge", send=broken)
    uploader.add(spot())
    assert uploader.flush() == 0 and "unreachable" in uploader.last_error
    uploader.stop()


def test_reports_go_out_while_still_fresh_and_the_interval_is_not_fixed(app):
    """PSKReporter treats a record more than a minute from its datagram as coming from a wrong
    clock, so the batching interval plus the longest decode delay must stay well under that."""
    uploader = pr.PskReporterUploader("KF0UQR", "EM18ge", send=lambda d: None)
    longest_delay_before_queued = 15 + 5            # an FT8 slot, plus decoding it
    assert pr.SEND_INTERVAL_SECONDS + pr.SEND_JITTER_SECONDS + longest_delay_before_queued < 60 and pr.SEND_JITTER_SECONDS > 0
    assert (pr.SEND_INTERVAL_SECONDS - pr.SEND_JITTER_SECONDS) * 1000 <= uploader._timer.interval() <= (pr.SEND_INTERVAL_SECONDS + pr.SEND_JITTER_SECONDS) * 1000
    uploader.stop()


def test_the_default_sender_works_off_the_gui_thread(app, monkeypatch):
    import threading
    where = []
    uploader = pr.PskReporterUploader("KF0UQR", "EM18ge")
    monkeypatch.setattr(uploader, "_send_blocking", lambda datagram: where.append(threading.current_thread().name))
    uploader.add(spot())
    uploader.stop()                                  # flushes, then waits for the sender thread
    assert where and where[0] != threading.main_thread().name


def test_heard_station_extracts_the_sender_and_any_locator():
    assert ft8_qso.heard_station("CQ DX W1AW FN31") == ("W1AW", "FN31")
    assert ft8_qso.heard_station("KF0UQR W9XYZ EM28") == ("W9XYZ", "EM28")
    assert ft8_qso.heard_station("KF0UQR W9XYZ -07") == ("W9XYZ", "")
    assert ft8_qso.heard_station("K1ABC W2DEF RR73") == ("W2DEF", "")
    assert ft8_qso.heard_station("TNX 73 GL") is None and ft8_qso.heard_station("<...> W2DEF -05") is None
