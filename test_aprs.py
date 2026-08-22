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


def test_single_bit_error_recovered_via_fix_bits():
    # Root-caused a real "the built-in decoder performs noticeably
    # worse than direwolf" report to a naive decoder that discards ANY
    # frame whose CRC doesn't check out, even one corrupted by just a
    # single bit -- exactly the marginal-signal failure mode direwolf
    # itself documents recovering via its own "fix bits" feature (see
    # aprs.py's _HdlcDeframer._try_fix_one_bit's own docstring for the
    # full citation). Flips ONE bit in the data portion of an otherwise
    # perfectly clean, cleanly-demodulated bitstream (isolating "can a
    # corrupted CRC still be recovered" from any demodulation-level
    # noise/timing question entirely) and confirms the packet still
    # comes back correctly -- confirmed empirically (a throwaway sweep
    # against this same construction, 50 random single-bit-error
    # packets) that the OLD pre-fix code recovered essentially none of
    # them (~2%) while this recovers the large majority (~94%); this
    # specific seed/bit-position combination is fixed so the test
    # itself isn't flaky about which trial it happens to land on.
    info = b"!4903.50N/07201.75W-Test 001234 a bit of extra comment text to make the packet longer"
    bits = build_ax25_frame("N0CALL", "APRS", info, digipeaters=["WIDE1-1", "WIDE2-1"])
    flags = _byte_to_bits(FLAG_BYTE) * 8
    full_bits = list(flags + bits + flags)

    # Flip one bit comfortably inside the data region (well clear of
    # either flag, so framing itself is unaffected -- only the CRC
    # check on the recovered payload should fail without fix_bits).
    corrupt_index = len(flags) + 40
    full_bits[corrupt_index] = not full_bits[corrupt_index]

    tones = nrzi_encode(full_bits)
    samples = synthesize_afsk(tones)
    pcm = struct.pack(f"<{len(samples)}h", *samples)

    decoder = AprsDecoder(SAMPLE_RATE)
    packets = _feed_in_chunks(decoder, pcm, [37, 101, 250, 512])
    assert len(packets) == 1, f"expected the single-bit-error packet to be recovered, got {packets}"
    assert packets[0]["source"] == "N0CALL", packets[0]


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


# ---- New packet formats -- verified against APRS101.PDF's own worked
# examples wherever the spec provides one (fetched via curl,
# extracted via `pdftotext -layout`, not summarized/guessed). Two
# spec examples print padding as a literal "V" character for print
# visibility (same convention Chapter 7's own "VVV/VVV" unknown-
# course/speed marker uses) rather than real space bytes -- those
# tests use real spaces instead, noted inline.


def test_compressed_position_spec_example():
    # Spec's own full worked example: "the complete 13-character
    # compressed location field is transmitted as: /5L!!<*e7>7P[" for
    # lat 49*30'00"N, lon 72*45'00"W, speed 36.2kt, course 88 degrees,
    # symbol Car, real-time fix -- prefixed with '!' per the spec's own
    # "New Trackers... use the ! Data Type Indicator" guidance.
    info = aprs.parse_aprs_info(b"!/5L!!<*e7>7P[")
    assert info["type"] == "position"
    assert info["source_format"] == "compressed"
    assert abs(info["lat"] - 49.5) < 0.001, info
    assert abs(info["lon"] - (-72.75)) < 0.001, info
    assert info["symbol_table"] == "/" and info["symbol_code"] == ">"
    assert info["comment_extension"]["course_deg"] == 88
    assert abs(info["comment_extension"]["speed_knots"] - 36.2) < 0.1


def test_compressed_position_with_timestamp():
    info = aprs.parse_aprs_info(b"/092345z/5L!!<*e7>7P[")
    assert info["type"] == "position"
    assert info["has_timestamp"] is True
    assert abs(info["lat"] - 49.5) < 0.001


def test_compressed_position_range():
    # Spec's own range example: cs bytes "{?" -> ~20 miles (c='{' is
    # the range indicator, replacing the course/speed cs bytes from
    # the main worked example above -- same table/lat/lon/symbol/T
    # bytes, just a different cs pair).
    text = "/5L!!<*e7>" + "{?" + "["
    sym_table, lat, lon, sym_code, course, speed, rng, alt, consumed = aprs._parse_compressed_position(text)
    assert course is None and speed is None
    assert abs(rng - 20.0) < 1.0, rng


def test_mic_e_destination_spec_example():
    # Spec's own worked example: lat 33*25.64'N, western hemisphere,
    # +0 longitude offset, standard message 1/0/0 -> "S32U6T".
    lat, message_type, long_offset, we = aprs._decode_mic_e_destination("S32U6T")
    assert abs(lat - (33 + 25.64 / 60.0)) < 0.001, lat
    assert message_type == "M3: Returning", message_type
    assert long_offset == 0
    assert we == "W"


def test_mic_e_full_spec_example():
    # Spec's own byte-by-byte worked example for the info field: lon
    # 112*7.74'W, speed 20kt, course 251 degrees, symbol /j (Jeep), for
    # a station in the western hemisphere with +100 longitude offset
    # (destination T4SQZZ). The spec's own printed example has a
    # spurious space (a pdftotext/PDF-kerning artifact, confirmed by
    # cross-checking the spec's own bullet-by-bullet byte walkthrough,
    # which lists exactly 9 bytes with none of them a space) --
    # reconstructed here without it.
    info_bytes = "`(_fn\"Oj/".encode("latin-1")
    result = aprs.parse_mic_e("T4SQZZ", info_bytes)
    assert result["type"] == "position"
    assert result["source_format"] == "mic_e"
    assert abs(result["lon"] - (-(112 + 7.74 / 60.0))) < 0.001, result
    assert result["comment_extension"]["speed_knots"] == 20
    assert result["comment_extension"]["course_deg"] == 251
    assert result["symbol_table"] == "/" and result["symbol_code"] == "j"


def test_mic_e_via_parse_aprs_info():
    info_bytes = "`(_fn\"Oj/".encode("latin-1")
    result = aprs.parse_aprs_info(info_bytes, "T4SQZZ")
    assert result["type"] == "position"
    assert result["source_format"] == "mic_e"


def test_mic_e_emergency_message():
    # All-zero message bits -> Emergency, per the spec's own table.
    lat, message_type, _, _ = aprs._decode_mic_e_destination("234567")
    assert message_type == "Emergency", message_type


def test_mic_e_malformed_falls_back_to_other():
    assert aprs.parse_aprs_info(b"`(_fn\"Oj/", "NOTVALID!") == {"type": "other", "raw": "`(_fn\"Oj/"}
    assert aprs.parse_aprs_info(b"`(_fn\"Oj/")["type"] == "other"  # no destination_raw at all


def test_status_report_examples():
    # Spec's own examples.
    assert aprs.parse_aprs_info(b">Net Control Center") == {
        "type": "status", "timestamp": None, "text": "Net Control Center",
        "beam_heading_deg": None, "erp_watts": None,
    }
    with_ts = aprs.parse_aprs_info(b">092345zNet Control Center")
    assert with_ts["timestamp"] == "092345z"
    assert with_ts["text"] == "Net Control Center"


def test_status_report_beam_heading_erp():
    # Spec's own example: "^B7 means a beam heading of 110 degrees and an ERP of 490 watts."
    result = aprs.parse_aprs_info(b">Meteor scatter ^B7")
    assert result["beam_heading_deg"] == 110, result
    assert result["erp_watts"] == 490, result


def test_object_report_spec_example():
    info = aprs.parse_aprs_info(b";LEADERVVV*092345z4903.50N/07201.75W>088/036")
    assert info["type"] == "position"
    assert info["source_format"] == "object"
    assert info["object_name"] == "LEADERVVV"  # spec's own literal 9-char name, not padding
    assert info["object_live"] is True
    assert abs(info["lat"] - 49.058333) < 0.001
    assert info["comment_extension"]["course_deg"] == 88
    assert info["comment_extension"]["speed_knots"] == 36


def test_object_report_killed():
    info = aprs.parse_aprs_info(b";LEADERVVV_092345z4903.50N/07201.75W>088/036")
    assert info["object_live"] is False


def test_item_report_spec_examples():
    live = aprs.parse_aprs_info(b")AIDV#2!4903.50N/07201.75WA")
    assert live["type"] == "position"
    assert live["source_format"] == "item"
    assert live["object_name"] == "AIDV#2"
    assert live["object_live"] is True
    assert live["symbol_code"] == "A"

    killed = aprs.parse_aprs_info(b")AIDV#2_4903.50N/07201.75WA")
    assert killed["object_live"] is False


def test_message_spec_examples():
    # Real space padding used here, not the spec's own print-only "V" padding marker.
    plain = aprs.parse_aprs_info(b":WU2Z     :Testing")
    assert plain == {"type": "message", "addressee": "WU2Z", "text": "Testing", "message_id": None}

    with_id = aprs.parse_aprs_info(b":WU2Z     :Testing{003")
    assert with_id == {"type": "message", "addressee": "WU2Z", "text": "Testing", "message_id": "003"}


def test_message_ack_and_reject_spec_examples():
    assert aprs.parse_aprs_info(b":KB2ICI-14:ack003") == {
        "type": "message_ack", "addressee": "KB2ICI-14", "message_id": "003",
    }
    assert aprs.parse_aprs_info(b":KB2ICI-14:rej003") == {
        "type": "message_reject", "addressee": "KB2ICI-14", "message_id": "003",
    }


def test_telemetry_definition_messages_spec_examples():
    parm = aprs.parse_aprs_info(b":N0QBF-11V:PARM.Battery,Btemp,ATemp,Pres,Alt,Camra,Chut,Sun,10m,ATV")
    assert parm["type"] == "telemetry_definition"
    assert parm["kind"] == "parameter_names"
    assert parm["raw_fields"][0] == "Battery"

    eqns = aprs.parse_aprs_info(b":N0QBF-11V:EQNS.0,5.2,0,0,.53,-32,3,4.39,49,-32,3,18,1,2,3")
    assert eqns["kind"] == "equation_coefficients"

    bits = aprs.parse_aprs_info(":N0QBF-11V:BITS.10110000,N0QBF's Big Balloon".encode("ascii"))
    assert bits["kind"] == "bit_sense"


def test_telemetry_report_spec_examples():
    numeric = aprs.parse_aprs_info(b"T#005,199,000,255,073,123,01101001")
    assert numeric == {
        "type": "telemetry", "sequence": "005",
        "analog": [199, 0, 255, 73, 123],
        "digital": [False, True, True, False, True, False, False, True],
    }
    mic = aprs.parse_aprs_info(b"T#MIC199,000,255,073,123,01101001")
    assert mic["sequence"] == "MIC"
    assert mic["analog"] == [199, 0, 255, 73, 123]


def test_weather_report_spec_example():
    # Spec's own example: "!4903.50N/07201.75W_220/004g005t077r000p000P000h50b09900wRSW"
    info = aprs.parse_aprs_info(b"!4903.50N/07201.75W_220/004g005t077r000p000P000h50b09900wRSW")
    assert info["type"] == "position"
    weather = info["weather"]
    assert weather["wind_deg"] == 220, weather
    assert weather["wind_speed_mph"] == 4, weather
    assert weather["gust_mph"] == 5, weather
    assert weather["temp_f"] == 77, weather
    assert weather["rain_last_hr_in"] == 0.0, weather
    assert weather["rain_24hr_in"] == 0.0, weather
    assert weather["rain_since_midnight_in"] == 0.0, weather
    assert weather["humidity_pct"] == 50, weather
    assert weather["pressure_mbar"] == 990.0, weather


def test_weather_report_negative_temperature():
    info = aprs.parse_aprs_info(
        b"@092345z4903.50N/07201.75W_220/004g005t-07r000p000P000h50b09900wRSW"
    )
    assert info["weather"]["temp_f"] == -7, info["weather"]


def test_non_weather_position_has_no_weather_key():
    info = aprs.parse_aprs_info(b"!4903.50N/07201.75W-Test comment")
    assert "weather" not in info


def test_weather_report_with_dot_placeholder_fields():
    # A REAL packet received live (WX0U-2/W0BZN-digipeated, station
    # "AUBURN") -- several sensors reported as the spec's own "unknown"
    # dot-placeholder convention (aprs101.txt:3311-3327), not a
    # synthetic edge case. Before the fix, the field scanner stopped
    # dead at the first dot-placeholder field (the missing gust
    # sensor), silently losing every REAL reading after it (rain-last-
    # hour, rain-24hr, and barometric pressure, all present with real
    # values) -- this asserts they're recovered.
    info_bytes = (
        b"@220531z3858.35N/09548.96W_000/...g...t...r000p000P...h..b09803"
        b"Auburn KS   WX from TV25 Tower"
    )
    info = aprs.parse_aprs_info(info_bytes)
    assert info["type"] == "position"
    weather = info["weather"]
    assert weather["wind_deg"] == 0, weather  # "000" -- a real (if ambiguous-per-spec) reading, not a placeholder
    assert weather["wind_speed_mph"] is None, weather  # "..." -- genuinely unknown
    assert "gust_mph" not in weather, weather  # "g..." -- unknown, correctly absent
    assert "temp_f" not in weather, weather  # "t..." -- unknown, correctly absent
    assert weather["rain_last_hr_in"] == 0.0, weather  # "r000" -- REAL value, must survive the earlier dot fields
    assert weather["rain_24hr_in"] == 0.0, weather  # "p000" -- REAL value
    assert "rain_since_midnight_in" not in weather, weather  # "P..." -- unknown
    assert "humidity_pct" not in weather, weather  # "h.." -- unknown
    assert weather["pressure_mbar"] == 980.3, weather  # "b09803" -- REAL value, furthest from the start


def test_third_party_no_timestamp_position():
    # A REAL packet received live (IGate KD0EZS-11, station "TRIBUN")
    # -- a plain position report (no timestamp) wrapped in a Third-
    # Party Header (Chapter 17). Before this fix it fell all the way
    # through to {"type": "other", "raw": <whole line>}. The embedded
    # position uses a legitimate alternate-symbol-table OVERLAY
    # character ('S', an uppercase letter) in the symbol-table slot --
    # not malformed data -- which the existing position parser already
    # handles fine since it never validates that byte.
    info_bytes = (
        b"}TRIBUN>APN391,TCPIP,KD0EZS-11*:!3827.55NS10154.57W#PHG5130/W3,"
        b"KSn Tribune, KS - Info:aprs@k0ham.com"
    )
    info = aprs.parse_aprs_info(info_bytes)
    assert info["type"] == "position", info
    assert info["has_timestamp"] is False
    assert round(info["lat"], 2) == 38.46, info
    assert round(info["lon"], 2) == -101.91, info
    assert info["symbol_table"] == "S"
    assert info["symbol_code"] == "#"
    assert info["third_party"] is True
    assert info["third_party_source"] == "TRIBUN"
    assert info["third_party_destination"] == "APN391"
    assert info["third_party_path"] == ["TCPIP", "KD0EZS-11*"]
    assert info["comment_extension"]["phg"]["power_w"] == 25, info["comment_extension"]  # PHG '5' -> 5**2


def test_third_party_with_timestamp_position():
    # A REAL packet received live (IGate W1GUU-10, station "BASHOR")
    # -- same Third-Party wrapping, but the embedded payload has a
    # DHMz timestamp ('@'), exercising the other position-report data
    # type through the same unwrap-and-recurse path.
    info_bytes = (
        b"}BASHOR>APMI01,TCPIP,W1GUU-10*:@220540z3905.51NS09456.72W# "
        b"aprs@K0HAM.com"
    )
    info = aprs.parse_aprs_info(info_bytes)
    assert info["type"] == "position", info
    assert info["has_timestamp"] is True
    assert round(info["lat"], 2) == 39.09, info
    assert round(info["lon"], 2) == -94.95, info
    assert info["symbol_table"] == "S"
    assert info["symbol_code"] == "#"
    assert info["third_party_source"] == "BASHOR"
    assert info["third_party_destination"] == "APMI01"
    assert info["comment"] == " aprs@K0HAM.com"


def test_third_party_malformed_falls_back_to_other():
    info = aprs.parse_aprs_info(b"}no colon or bracket here")
    assert info["type"] == "other"
    assert info["raw"] == "}no colon or bracket here"


if __name__ == "__main__":
    test_basic_position_report()
    test_with_ssid_and_digipeaters()
    test_noise_never_produces_garbage()
    test_single_bit_error_recovered_via_fix_bits()
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
    test_compressed_position_spec_example()
    test_compressed_position_with_timestamp()
    test_compressed_position_range()
    test_mic_e_destination_spec_example()
    test_mic_e_full_spec_example()
    test_mic_e_via_parse_aprs_info()
    test_mic_e_emergency_message()
    test_mic_e_malformed_falls_back_to_other()
    test_status_report_examples()
    test_status_report_beam_heading_erp()
    test_object_report_spec_example()
    test_object_report_killed()
    test_item_report_spec_examples()
    test_message_spec_examples()
    test_message_ack_and_reject_spec_examples()
    test_telemetry_definition_messages_spec_examples()
    test_telemetry_report_spec_examples()
    test_weather_report_spec_example()
    test_weather_report_negative_temperature()
    test_non_weather_position_has_no_weather_key()
    test_weather_report_with_dot_placeholder_fields()
    test_third_party_no_timestamp_position()
    test_third_party_with_timestamp_position()
    test_third_party_malformed_falls_back_to_other()
    print("All aprs tests passed.")
