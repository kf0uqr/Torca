"""Round-trip test for aprs.py -- no radio/Qt/asyncio involved. Synthesizes
a known-good AX.25/APRS Bell 202 AFSK signal for a test packet, feeds it
through AprsDecoder in irregular chunks, and checks the decoded packet
comes back out correctly."""

import math
import struct

import aprs
from aprs import AprsDecoder, MARK_HZ, SPACE_HZ, BAUD, FLAG_BYTE, _crc16_x25, build_position_packet_pcm

SAMPLE_RATE = 22050


def _encode_address(callsign, ssid, is_last, is_first_in_chain=False):
    call = callsign.upper()
    if "-" in call:
        call = call.split("-")[0]
    call = call.ljust(6)[:6]
    raw = bytearray(b << 1 for b in call.encode("ascii"))
    ssid_byte = 0b01100000 | ((ssid & 0x0F) << 1) | (1 if is_last else 0)
    raw.append(ssid_byte)
    return bytes(raw)


def _bit_stuff(bits):
    out = []
    ones_run = 0
    for bit in bits:
        out.append(bit)
        if bit:
            ones_run += 1
            if ones_run == 5:
                out.append(False)
                ones_run = 0
        else:
            ones_run = 0
    return out


def _bytes_to_bits(data):
    bits = []
    for byte in data:
        for i in range(8):
            bits.append(bool((byte >> i) & 1))
    return bits


def _byte_to_bits(byte):
    return [bool((byte >> i) & 1) for i in range(8)]


def _split_ssid(callsign):
    if "-" in callsign:
        call, ssid_text = callsign.split("-", 1)
        return call, int(ssid_text)
    return callsign, 0


def build_ax25_frame(source, dest, info: bytes, digipeaters=()):
    """Returns the fully-framed bit sequence (including opening/closing
    flags, bit-stuffed) ready for NRZI/AFSK synthesis."""
    addresses = [_encode_address(dest, 0, False)]
    chain = [_split_ssid(source)] + [_split_ssid(d) for d in digipeaters]
    for i, (call, ssid) in enumerate(chain):
        is_last = i == len(chain) - 1
        addresses.append(_encode_address(call, ssid, is_last))
    payload = b"".join(addresses) + bytes([0x03, 0xF0]) + info
    crc = _crc16_x25(payload)
    fcs = bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    full_payload = payload + fcs

    flag_bits = _byte_to_bits(FLAG_BYTE)
    data_bits = _bit_stuff(_bytes_to_bits(full_payload))
    return flag_bits + data_bits + flag_bits


def nrzi_encode(bits, start_tone_is_mark=True):
    """NRZI: 1 = no tone change, 0 = tone change. Returns a list of
    booleans (True=mark) per bit."""
    tones = []
    current = start_tone_is_mark
    for bit in bits:
        if not bit:
            current = not current
        tones.append(current)
    return tones


def synthesize_afsk(tones):
    """Continuous-phase Bell 202 AFSK audio for a sequence of per-bit
    tone booleans (True=mark/1200Hz, False=space/2200Hz). Tracks
    cumulative ideal sample count (not a fixed round() per bit) so
    per-bit rounding error doesn't accumulate into real drift over a
    long packet -- SAMPLE_RATE/BAUD is rarely an integer (e.g.
    22050/1200 = 18.375)."""
    samples_per_bit = SAMPLE_RATE / BAUD
    phase = 0.0
    out = []
    emitted = 0.0
    for i, tone in enumerate(tones):
        freq = MARK_HZ if tone else SPACE_HZ
        target_total = (i + 1) * samples_per_bit
        n = round(target_total - emitted)
        emitted += n
        for _ in range(n):
            out.append(int(round(12000 * math.sin(phase))))
            phase += 2 * math.pi * freq / SAMPLE_RATE
    return out


def encode_position_packet(source, dest, lat_text, lon_text, sym_table, sym_code, comment, digipeaters=()):
    info = ("!" + lat_text + sym_table + lon_text + sym_code + comment).encode("ascii")
    bits = build_ax25_frame(source, dest, info, digipeaters)
    # Idle flag-byte padding before AND after, like a real transmission's
    # preamble/tail -- the trailing padding matters here specifically so
    # the very last bit of the closing flag has enough trailing audio
    # samples for its own Goertzel window to actually complete (without
    # it, the signal ends mid-window and that final bit is silently
    # never recovered at all).
    flags = _byte_to_bits(FLAG_BYTE) * 8
    tones = nrzi_encode(flags + bits + flags)
    samples = synthesize_afsk(tones)
    return struct.pack(f"<{len(samples)}h", *samples)


def _feed_in_chunks(decoder, pcm_bytes, chunk_sizes):
    packets = []
    i = 0
    while i < len(pcm_bytes):
        size = chunk_sizes[i % len(chunk_sizes)]
        packets.extend(decoder.feed(pcm_bytes[i:i + size]))
        i += size
    return packets


def test_basic_position_report():
    pcm = encode_position_packet(
        "N0CALL", "APRS", "4903.50N", "07201.75W", "/", "-", "Test 001234",
    )
    decoder = AprsDecoder(SAMPLE_RATE)
    packets = _feed_in_chunks(decoder, pcm, [37, 101, 250, 512])
    assert len(packets) == 1, f"expected exactly 1 packet, got {len(packets)}: {packets}"
    packet = packets[0]
    assert packet["source"] == "N0CALL", packet
    assert packet["destination"] == "APRS", packet
    info = packet["info"]
    assert info["type"] == "position", info
    assert abs(info["lat"] - 49.058333) < 1e-4, info
    assert abs(info["lon"] - (-72.029167)) < 1e-4, info
    assert info["symbol_table"] == "/" and info["symbol_code"] == "-", info
    assert info["comment"] == "Test 001234", info


def test_with_ssid_and_digipeaters():
    info = b"!3722.10S/14512.30E>Testing"
    bits = build_ax25_frame("VK2ABC-9", "APZ001", info, digipeaters=["WIDE1-1", "WIDE2-2"])
    flags = _byte_to_bits(FLAG_BYTE) * 8
    tones = nrzi_encode(flags + bits + flags)
    samples = synthesize_afsk(tones)
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    decoder = AprsDecoder(SAMPLE_RATE)
    packets = _feed_in_chunks(decoder, pcm, [64, 200])
    assert len(packets) == 1
    packet = packets[0]
    assert packet["source"] == "VK2ABC-9", packet
    assert packet["digipeaters"] == ["WIDE1-1", "WIDE2-2"], packet


def test_noise_never_produces_garbage():
    import random
    rng = random.Random(42)
    noise = struct.pack(f"<{SAMPLE_RATE}h", *(rng.randint(-3000, 3000) for _ in range(SAMPLE_RATE)))
    decoder = AprsDecoder(SAMPLE_RATE)
    packets = decoder.feed(noise)
    assert len(packets) == 0, f"expected no packets from noise, got {packets}"


def test_back_to_back_packets():
    decoder = AprsDecoder(SAMPLE_RATE)
    pcm1 = encode_position_packet("W1AAA", "APRS", "4000.00N", "07500.00W", "/", "#", "First")
    pcm2 = encode_position_packet("W2BBB", "APRS", "4100.00N", "07600.00W", "/", "#", "Second")
    packets1 = _feed_in_chunks(decoder, pcm1, [128])
    packets2 = _feed_in_chunks(decoder, pcm2, [128])
    assert len(packets1) == 1 and packets1[0]["source"] == "W1AAA"
    assert len(packets2) == 1 and packets2[0]["source"] == "W2BBB"


def test_production_encoder_round_trips_through_production_decoder():
    """Unlike the tests above (which use THIS FILE's own independent
    encoder to validate the decoder), this exercises aprs.py's real
    send-side encoder (build_position_packet_pcm, used by aprs_window.py's
    Send Packet dialog) against aprs.py's real decoder -- the actual
    code path an operator sending a packet from this app would use."""
    pcm = build_position_packet_pcm(
        "N0CALL-9", "APRS", 49.058333, -72.029167, "/", "-", "Test 001234",
        sample_rate=SAMPLE_RATE, digipeaters=["WIDE1-1", "WIDE2-1"],
    )
    decoder = AprsDecoder(SAMPLE_RATE)
    packets = _feed_in_chunks(decoder, pcm, [37, 101, 512, 1200])
    assert len(packets) == 1, packets
    packet = packets[0]
    assert packet["source"] == "N0CALL-9", packet
    assert packet["destination"] == "APRS", packet
    assert packet["digipeaters"] == ["WIDE1-1", "WIDE2-1"], packet
    info = packet["info"]
    assert info["type"] == "position", info
    assert abs(info["lat"] - 49.058333) < 1e-3, info
    assert abs(info["lon"] - (-72.029167)) < 1e-3, info
    assert info["comment"] == "Test 001234", info


# ---- Comment "Data Extensions" (APRS101.PDF Chapter 7) -- worked
# examples taken verbatim from the spec itself (verified via pdftotext
# extraction of the real PDF, not guessed), not synthetic test data.


def test_comment_extension_phg_spec_example():
    # "PHG5132 means a power of 25 watts, an antenna height of 20 feet
    # above the average local terrain, an antenna gain of 3 dB, and
    # maximum gain due east." -- APRS101.PDF Chapter 7's own example.
    ext = aprs.parse_comment_extensions("PHG5132")
    assert ext == {"phg": {"power_w": 25, "height_ft": 20, "gain_db": 3, "directivity_deg": 90}}, ext


def test_comment_extension_rng_spec_example():
    # "RNG0050 indicates a radio range of 50 miles." -- spec's own example.
    ext = aprs.parse_comment_extensions("RNG0050")
    assert ext == {"range_miles": 50}, ext


def test_comment_extension_course_speed_spec_example():
    # "088/036 represents a course 88 degrees, traveling at 36 knots." -- spec's own example.
    ext = aprs.parse_comment_extensions("088/036")
    assert ext == {"course_deg": 88, "speed_knots": 36}, ext


def test_comment_extension_altitude_spec_example():
    # "/A=001234" -- spec's own example, altitude = 1234 ft.
    ext = aprs.parse_comment_extensions("Test /A=001234")
    assert ext == {"altitude_ft": 1234}, ext


def test_comment_extension_altitude_found_anywhere_in_comment():
    # Per the spec, altitude "may appear anywhere in the comment" --
    # unlike course/speed/PHG/RNG/DFS, which only ever occupy the
    # fixed leading 7 bytes.
    ext = aprs.parse_comment_extensions("some text before /A=005000 and after")
    assert ext == {"altitude_ft": 5000}, ext


def test_comment_extension_course_speed_and_altitude_together():
    ext = aprs.parse_comment_extensions("088/036/A=001234 moving")
    assert ext["course_deg"] == 88
    assert ext["speed_knots"] == 36
    assert ext["altitude_ft"] == 1234


def test_comment_extension_dfs_spec_example():
    # "DFS2230/comments" -- weak signal (2), 3dB gain, 40ft, omni --
    # spec's own worked example for the Omni-DF format.
    ext = aprs.parse_comment_extensions("DFS2230/comments")
    assert ext == {"df": {"strength_s": 2, "height_ft": 40, "gain_db": 3, "directivity_deg": None}}, ext


def test_comment_extension_none_for_ordinary_comment():
    assert aprs.parse_comment_extensions("Just a normal comment, nothing structured here") == {}


def test_comment_extension_height_code_beyond_9():
    # "the Height character may be any ASCII character 0-9 and above...
    # : is the height code for 10240 feet" -- spec's own example.
    ext = aprs.parse_comment_extensions("PHG5:32")
    assert ext["phg"]["height_ft"] == 10240, ext


def test_parse_aprs_info_includes_comment_extension():
    info = aprs.parse_aprs_info(b"!4903.50N/07201.75W#PHG5132 test")
    assert info["comment"] == "PHG5132 test"
    assert info["comment_extension"]["phg"]["power_w"] == 25


if __name__ == "__main__":
    test_basic_position_report()
    test_with_ssid_and_digipeaters()
    test_noise_never_produces_garbage()
    test_back_to_back_packets()
    test_production_encoder_round_trips_through_production_decoder()
    test_comment_extension_phg_spec_example()
    test_comment_extension_rng_spec_example()
    test_comment_extension_course_speed_spec_example()
    test_comment_extension_altitude_spec_example()
    test_comment_extension_altitude_found_anywhere_in_comment()
    test_comment_extension_course_speed_and_altitude_together()
    test_comment_extension_dfs_spec_example()
    test_comment_extension_none_for_ordinary_comment()
    test_comment_extension_height_code_beyond_9()
    test_parse_aprs_info_includes_comment_extension()
    print("All aprs tests passed.")
