"""Tests for workspace_state.py: the saved-workspace format (pure Python)."""

import json
import os
import stat

import workspace_state as ws
from workspace_host import H, V, tree_for_count


def test_layout_tree_round_trips_through_json():
    for tree in (0, (H, (0, 1)), (V, ((H, (0, 1)), 2)), (H, ((V, (0, 2)), 1)), tree_for_count(4)):
        as_json = json.loads(json.dumps(ws.tree_to_json(tree)))          # through real JSON text
        assert ws.tree_from_json(as_json) == tree


def test_bad_layouts_are_rejected():
    bad = [
        None, "h", 5, -1, 4, True, [], ["h"], ["x", [0, 1]], ["h", []], ["h", [0, 0]],   # shape / range / duplicates
        ["h", [1, 2]],                                                                    # workspace 1 missing
        ["h", [0, 1, 2, 3, 3]], {"o": "h"},
    ]
    for data in bad:
        assert ws.tree_from_json(data) is None, data


def test_clean_state_keeps_good_data_and_drops_bad_pieces():
    state = {
        "layout": ["h", [0, 1]],
        "workspaces": {
            "0": {"tabs": ["map", "page:Contests", 7, None], "current": "page:Contests"},
            "1": {"tabs": ["radio:IC-9700@h:1"], "current": "not-a-tab"},
            "3": {"tabs": ["map"], "current": "map"},         # workspace 4 is not in the layout
            "x": {"tabs": []},
        },
        "active": 1,
        "radios": [{"key": "radio:a", "identity": {"radio_model": "IC-9700"}}, {"key": 3}, "junk", {"key": "k"}],
    }
    cleaned = ws.clean_state(state)
    assert cleaned["layout"] == ["h", [0, 1]]
    assert cleaned["workspaces"] == {
        "0": {"tabs": ["map", "page:Contests"], "current": "page:Contests"},
        "1": {"tabs": ["radio:IC-9700@h:1"], "current": None},
    }
    assert cleaned["active"] == 1
    assert cleaned["radios"] == [{"key": "radio:a", "identity": {"radio_model": "IC-9700"}}]


def test_clean_state_rejects_unusable_input():
    assert ws.clean_state(None) is None and ws.clean_state("x") is None
    assert ws.clean_state({"layout": ["h", [1, 2]]}) is None
    assert ws.clean_state({}) is None


def test_active_defaults_to_workspace_one_when_invalid():
    assert ws.clean_state({"layout": 0, "active": 3})["active"] == 0


def test_save_and_load_per_profile_keeps_other_profiles(tmp_path):
    path = tmp_path / "state.json"
    a = {"layout": 0, "workspaces": {"0": {"tabs": ["map"], "current": "map"}}, "active": 0, "radios": []}
    b = {"layout": ["h", [0, 1]], "workspaces": {"1": {"tabs": ["logbook"], "current": "logbook"}}, "active": 1, "radios": []}
    assert ws.save_state("Home", a, path) and ws.save_state("POTA", b, path)
    assert ws.load_state("Home", path)["layout"] == 0
    assert ws.load_state("POTA", path)["layout"] == ["h", [0, 1]]
    ws.save_state("Home", b, path)                                         # overwriting one leaves the other alone
    assert ws.load_state("POTA", path) is not None and ws.load_state("Home", path)["layout"] == ["h", [0, 1]]


def test_missing_corrupt_or_unknown_profile_means_no_state(tmp_path):
    path = tmp_path / "state.json"
    assert ws.load_state("Home", path) is None                              # no file yet
    path.write_text("{ not json")
    assert ws.load_state("Home", path) is None                              # corrupt
    assert ws.save_state("Home", {"layout": 0, "workspaces": {}, "active": 0, "radios": []}, path)   # and it can recover
    assert ws.load_state("Home", path) is not None
    assert ws.load_state("Nobody", path) is None and ws.load_state("", path) is None and ws.load_state(None, path) is None


def test_file_is_owner_only_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "state.json"
    ws.save_state("Home", {"layout": 0, "workspaces": {}, "active": 0, "radios": []}, path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_radio_identity_has_no_secrets_and_matches_its_saved_connection_profile():
    details = {"radio_model": "IC-9700", "role": "full_duplex", "connection_type": "network", "host": "192.0.2.5",
               "port": 50011, "username": "me", "password": "hunter2", "addr": 0xA2, "audio_input_device": 3}
    identity = ws.radio_identity(details)
    assert "hunter2" not in json.dumps(identity) and "username" not in identity and "audio_input_device" not in identity
    assert ws.identity_key(identity) == "IC-9700@192.0.2.5:50011"

    saved = {
        "9700": {"radio_model": "IC-9700", "connection_type": "network", "host": "192.0.2.5", "port": 50011, "password": "hunter2"},
        "705": {"radio_model": "IC-705", "connection_type": "network", "host": "192.0.2.6", "port": 50004},
        "7300": {"radio_model": "IC-7300", "connection_type": "usb", "serial_port": "/dev/ttyUSB0"},
        "junk": "not a dict",
    }
    assert ws.find_connection_profile(identity, saved) is saved["9700"]
    usb = ws.radio_identity({"radio_model": "IC-7300", "role": "non_sat", "connection_type": "usb", "serial_port": "/dev/ttyUSB0"})
    assert ws.find_connection_profile(usb, saved) is saved["7300"]
    assert ws.identity_key(usb) == "IC-7300@/dev/ttyUSB0"
    other_host = dict(identity, host="192.0.2.99")
    assert ws.find_connection_profile(other_host, saved) is None            # a different radio of the same model
    assert ws.find_connection_profile(dict(identity, radio_model="IC-7610"), saved) is None


def test_remote_server_radios_are_identified_by_their_server():
    identity = ws.radio_identity({"radio_model": "IC-7300", "role": "non_sat", "connection_type": "remote",
                                  "remote_host": "shack.example", "remote_port": 8080, "remote_token": "secret"})
    assert "secret" not in json.dumps(identity)
    assert ws.identity_key(identity) == "IC-7300@shack.example:8080"
    saved = {"r": {"radio_model": "IC-7300", "connection_type": "remote", "remote_host": "shack.example", "remote_port": 8080}}
    assert ws.find_connection_profile(identity, saved) is saved["r"]


def test_extent_counts_columns_and_rows():
    assert ws.tree_extent(0) == (1, 1)
    assert ws.tree_extent(("h", (0, 1))) == (2, 1)
    assert ws.tree_extent(("v", (0, 1))) == (1, 2)
    assert ws.tree_extent(("v", (("h", (0, 1)), 2))) == (2, 2)
    assert ws.tree_extent(("h", (("v", (0, 2)), 1))) == (2, 2)
    assert ws.tree_extent(("v", (("h", (0, 1)), ("h", (2, 3))))) == (2, 2)
    assert ws.tree_extent(("h", (0, 1, 2))) == (3, 1)
    assert ws.tree_extent(("v", (0, 1, 2))) == (1, 3)
    assert ws.tree_extent(("h", (0, ("v", (1, ("h", (2, 3))))))) == (3, 2)      # nesting adds up: 1 + 2 columns


def test_only_layouts_within_two_wide_and_two_tall_are_valid():
    ok = [0, ("h", (0, 1)), ("v", (0, 1)), ("v", (("h", (0, 1)), 2)), ("h", (("v", (0, 2)), 1)),
          ("v", (("h", (0, 1)), ("h", (2, 3)))), ("h", (("v", (0, 2)), ("v", (1, 3))))]
    too_wide_or_tall = [("h", (0, 1, 2)), ("v", (0, 1, 2)), ("h", (0, 1, 2, 3)), ("v", (0, 1, 2, 3)),
                        ("h", (0, ("v", (1, ("h", (2, 3))))))]
    assert all(ws.fits_grid(tree) for tree in ok)
    assert not any(ws.fits_grid(tree) for tree in too_wide_or_tall)


def test_a_saved_layout_wider_or_taller_than_2x2_is_rejected_on_load():
    assert ws.tree_from_json(["h", [0, 1, 2]]) is None                       # three columns (an older version allowed these)
    assert ws.tree_from_json(["v", [0, 1, 2]]) is None
    assert ws.tree_from_json(["h", [0, 1]]) == ("h", (0, 1))
    assert ws.clean_state({"layout": ["h", [0, 1, 2]], "workspaces": {}}) is None   # so a whole such state is simply not restored



# ---- extra windows and geometry ----

GEOMETRY = {"screen": "DP-1", "x": 100, "y": 50, "width": 1280, "height": 720, "maximized": False}


def test_extra_windows_and_geometry_survive_cleaning():
    state = {
        "layout": ["h", [0, 1]], "workspaces": {"0": {"tabs": ["map"], "current": "map"}}, "active": 0,
        "geometry": GEOMETRY,
        "windows": [
            {"layout": 0, "workspaces": {"0": {"tabs": ["page:Parks", 5], "current": "page:Parks"}}, "active": 0, "geometry": GEOMETRY},
            {"layout": ["v", [0, 1]], "workspaces": {"1": {"tabs": ["logbook"], "current": None}}, "active": 1},
        ],
    }
    cleaned = ws.clean_state(state)
    assert cleaned["geometry"] == GEOMETRY
    assert len(cleaned["windows"]) == 2
    assert cleaned["windows"][0]["workspaces"]["0"] == {"tabs": ["page:Parks"], "current": "page:Parks"}
    assert cleaned["windows"][0]["geometry"] == GEOMETRY and cleaned["windows"][1]["geometry"] is None
    assert cleaned["windows"][1]["layout"] == ["v", [0, 1]] and cleaned["windows"][1]["active"] == 1


def test_a_file_from_before_multiple_windows_still_loads_as_one_window():
    old = {"layout": ["h", [0, 1]], "workspaces": {"0": {"tabs": ["map"], "current": "map"}}, "active": 0, "radios": []}
    cleaned = ws.clean_state(old)
    assert cleaned["windows"] == [] and cleaned["geometry"] is None and cleaned["layout"] == ["h", [0, 1]]


def test_a_bad_extra_window_is_dropped_but_the_rest_is_kept():
    state = {"layout": 0, "workspaces": {}, "windows": [
        {"layout": ["h", [0, 1, 2]], "workspaces": {}},          # too wide
        "junk",
        {"layout": 0, "workspaces": {"0": {"tabs": ["logbook"], "current": "logbook"}}},
    ]}
    cleaned = ws.clean_state(state)
    assert len(cleaned["windows"]) == 1 and cleaned["windows"][0]["workspaces"]["0"]["tabs"] == ["logbook"]


def test_the_number_of_extra_windows_is_capped():
    state = {"layout": 0, "workspaces": {}, "windows": [{"layout": 0, "workspaces": {}} for _ in range(40)]}
    assert len(ws.clean_state(state)["windows"]) == ws.MAX_EXTRA_WINDOWS


def test_geometry_is_validated():
    assert ws.clean_geometry(GEOMETRY) == GEOMETRY
    assert ws.clean_geometry(None) is None and ws.clean_geometry("x") is None
    assert ws.clean_geometry(dict(GEOMETRY, width=10)) is None                # silly size
    assert ws.clean_geometry(dict(GEOMETRY, x="0")) is None and ws.clean_geometry(dict(GEOMETRY, x=True)) is None
    missing_screen = {k: v for k, v in GEOMETRY.items() if k != "screen"}
    assert ws.clean_geometry(missing_screen)["screen"] == ""
