"""Saved workspace state: what tabs were open, and where, per operator profile.

Pure Python (no Qt), so the format and its validation are easy to test. Lives
in ~/.icom_radio_app_cache/workspace_state.json next to the other saved
profiles, as {"version": 1, "profiles": {<profile name>: <state>}}.

A state is:

    {
      "layout":     the workspace split tree (see workspace_host.tree_*),
      "workspaces": {"0": {"tabs": [<tab key>, ...], "current": <tab key>}, ...},
      "active":     index of the last-used workspace,
      "radios":     [{"key": <tab key>, "identity": {...}}, ...],
    }

Every restorable tab has a stable string key: "map", "page:Contests",
"logbook", "radio:<identity>", "tool:<identity>:<tool name>". A radio's
"identity" says which radio it was (model, how it connects, its role) --
deliberately WITHOUT credentials: to reconnect, the identity is matched to
the radio's entry in the saved connection profiles
(connection_profiles.json), which is where the password already lives.
"""

import json
import os
import pathlib

STATE_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "workspace_state.json"
STATE_VERSION = 1
MAX_WORKSPACES = 4


# ---- layout tree <-> JSON ----------------------------------------------------

def tree_to_json(tree):
    """(orientation, children) tuples become [orientation, [children]] lists."""
    if isinstance(tree, int):
        return tree
    orientation, children = tree
    return [orientation, [tree_to_json(child) for child in children]]


def tree_from_json(data):
    """The inverse, validated: returns a layout tree, or None if `data` is
    not a sane layout (wrong shape, unknown orientation, a workspace index
    out of range or used twice, workspace 1 missing, more than four)."""
    def parse(node):
        if isinstance(node, bool):
            raise ValueError
        if isinstance(node, int):
            return node
        if isinstance(node, list) and len(node) == 2 and node[0] in ("h", "v") and isinstance(node[1], list) and node[1]:
            return (node[0], tuple(parse(child) for child in node[1]))
        raise ValueError

    try:
        tree = parse(data)
    except (ValueError, TypeError):
        return None
    leaves = _leaves(tree)
    if len(leaves) != len(set(leaves)) or 0 not in leaves:
        return None
    if any(not 0 <= leaf < MAX_WORKSPACES for leaf in leaves):
        return None
    return tree


def _leaves(tree):
    if isinstance(tree, int):
        return [tree]
    return [leaf for child in tree[1] for leaf in _leaves(child)]


# ---- validating a whole saved state -----------------------------------------

def clean_state(state):
    """Returns a sanitized copy of a loaded state, or None if it is unusable.
    Anything malformed inside an otherwise good state is dropped rather than
    failing the whole restore."""
    if not isinstance(state, dict):
        return None
    layout = tree_from_json(state.get("layout"))
    if layout is None:
        return None
    leaves = set(_leaves(layout))
    workspaces = {}
    raw_workspaces = state.get("workspaces")
    for index_text, workspace in (raw_workspaces.items() if isinstance(raw_workspaces, dict) else []):
        try:
            index = int(index_text)
        except (TypeError, ValueError):
            continue
        if index not in leaves or not isinstance(workspace, dict):
            continue
        tabs = [key for key in workspace.get("tabs", []) if isinstance(key, str)]
        current = workspace.get("current")
        workspaces[str(index)] = {"tabs": tabs, "current": current if current in tabs else None}
    radios = []
    for radio in state.get("radios", []) if isinstance(state.get("radios"), list) else []:
        if isinstance(radio, dict) and isinstance(radio.get("key"), str) and isinstance(radio.get("identity"), dict):
            radios.append({"key": radio["key"], "identity": radio["identity"]})
    active = state.get("active")
    return {
        "layout": tree_to_json(layout),
        "workspaces": workspaces,
        "active": active if isinstance(active, int) and active in leaves else 0,
        "radios": radios,
    }


# ---- the file ----------------------------------------------------------------

def _read_all(path=None):
    path = pathlib.Path(path or STATE_PATH)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    profiles = data.get("profiles") if isinstance(data, dict) else None
    return profiles if isinstance(profiles, dict) else {}


def load_state(profile_name, path=None):
    """The cleaned saved state for `profile_name`, or None if there is none
    (or it is unreadable) -- in which case the app just opens normally."""
    if not profile_name:
        return None
    return clean_state(_read_all(path).get(profile_name))


def save_state(profile_name, state, path=None):
    """Writes one profile's state, keeping every other profile's. Atomic
    (written to a temp file and renamed) so a crash mid-write can't leave a
    half-written file; owner-only permissions. Returns True on success."""
    if not profile_name:
        return False
    path = pathlib.Path(path or STATE_PATH)
    profiles = _read_all(path)
    profiles[profile_name] = state
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps({"version": STATE_VERSION, "profiles": profiles}, indent=2))
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        return True
    except OSError as exc:
        print(f"[ERROR] Workspace state: couldn't save ({exc}).")
        return False


# ---- radio identity ----------------------------------------------------------

def radio_identity(details):
    """The non-secret facts that say which radio a connection was: model,
    role, and where it connects. Used to re-find its saved connection
    profile on the next launch."""
    connection_type = details.get("connection_type")
    identity = {
        "radio_model": details.get("radio_model"),
        "role": details.get("role"),
        "connection_type": connection_type,
    }
    if connection_type == "network":
        identity.update(host=details.get("host"), port=details.get("port"))
    elif connection_type == "remote":
        identity.update(remote_host=details.get("remote_host"), remote_port=details.get("remote_port"))
    else:
        identity.update(serial_port=details.get("serial_port"))
    return identity


def identity_key(identity):
    """A short stable string for an identity, used inside tab keys."""
    model = identity.get("radio_model")
    kind = identity.get("connection_type")
    if kind == "network":
        where = f"{identity.get('host')}:{identity.get('port')}"
    elif kind == "remote":
        where = f"{identity.get('remote_host')}:{identity.get('remote_port')}"
    else:
        where = f"{identity.get('serial_port')}"
    return f"{model}@{where}"


def find_connection_profile(identity, connection_profiles):
    """The saved connection profile (connection_profiles.json entry) for this
    identity, or None. Matches on model + endpoint (not role: the role last
    used is applied separately)."""
    kind = identity.get("connection_type")
    fields = {
        "network": ("host", "port"),
        "remote": ("remote_host", "remote_port"),
    }.get(kind, ("serial_port",))
    for profile in connection_profiles.values():
        if not isinstance(profile, dict):
            continue
        if profile.get("radio_model") != identity.get("radio_model") or profile.get("connection_type") != kind:
            continue
        if all(profile.get(field) == identity.get(field) for field in fields):
            return profile
    return None
