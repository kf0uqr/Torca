import json
import time

import heard_spots

NOW = 2_000_000_000.0


def spot(call, age=60, band="20m"):
    return {"callsign": call, "locator": "FN31", "frequency_hz": 14075000, "band": band, "mode": "FT8", "snr": "-9",
            "flow_start_seconds": int(NOW - age), "direction": "hearing", "raw": {}}


def test_round_trip_drops_old_spots_and_keeps_the_rest(tmp_path):
    path = tmp_path / "sub" / "heard.json"
    assert heard_spots.save([spot("W1AW"), spot("K1ABC", age=90_000), spot("N0XYZ", age=3600)], path, now=NOW)
    back = heard_spots.load(path, now=NOW)
    assert [s["callsign"] for s in back] == ["N0XYZ", "W1AW"] and all(s["raw"] == {} for s in back)
    assert heard_spots.load(path, now=NOW + 90_000) == []                      # a day later, all of it has expired


def test_bad_or_missing_files_are_just_empty(tmp_path):
    assert heard_spots.load(tmp_path / "none.json") == []
    (tmp_path / "bad.json").write_text("{not json")
    assert heard_spots.load(tmp_path / "bad.json") == []
    (tmp_path / "odd.json").write_text(json.dumps({"spots": [1, {"callsign": "X"}, spot("OK")]}))
    assert [s["callsign"] for s in heard_spots.load(tmp_path / "odd.json", now=NOW)] == ["OK"]


def test_the_most_recent_spots_win_when_there_are_too_many(tmp_path, monkeypatch):
    monkeypatch.setattr(heard_spots, "MAX_SPOTS", 3)
    path = tmp_path / "h.json"
    heard_spots.save([spot(f"K{i}A", age=1000 - i) for i in range(10)], path, now=NOW)
    assert [s["callsign"] for s in heard_spots.load(path, now=NOW)] == ["K7A", "K8A", "K9A"]
