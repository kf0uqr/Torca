"""
Tiny shared helpers used by app.py and the per-feature route modules
(routes_tools.py, routes_satellite.py, ...) -- split out to avoid a
circular import (app.py mounts those routers, so they can't import
back from app.py).
"""


def find_radio_window(dashboard, radio_id):
    for window in dashboard._connected_radios:
        if getattr(window, "remote_id", None) == radio_id:
            return window
    return None


# Three access roles, each backed by its own secret (see
# ham_dashboard.py's three remote_access_token_* QSettings keys):
#   operator -- full control, no supervision requirement (this is the
#               licensee/control operator's own remote-control link).
#   guest    -- can transmit ONLY while an operator session is
#               concurrently connected to the same radio (47 CFR
#               97.115's "direct supervision" requirement for a third
#               party), gated by RadioRemoteState.can_transmit.
#   viewer   -- read-only. Never allowed to issue any state-changing
#               command (97.109/97.213: only the control operator or
#               someone they've authorized may operate the station).
ROLE_OPERATOR = "operator"
ROLE_GUEST = "guest"
ROLE_VIEWER = "viewer"

# Commands that key the transmitter (directly or by sending TX audio/
# CW/APRS traffic) -- these are the ones RadioRemoteState.can_transmit
# gates for guests, on top of the plain "not a viewer" check every
# other mutating command gets.
TX_COMMANDS = frozenset({"ptt", "send_text", "send_position"})


def make_role_checker(operator_token=None, guest_token=None, viewer_token=None):
    """Returns a role_for(candidate) closure resolving a bearer/query
    token to one of ROLE_OPERATOR/ROLE_GUEST/ROLE_VIEWER, or None if it
    matches nothing. A role whose own token is set is only reachable by
    the matching candidate. If NONE of the three are configured at all
    (all falsy), every candidate resolves to ROLE_OPERATOR -- the same
    "no auth configured means anything passes" convenience the old
    single-token make_token_check had, extended to the role system
    (ham_dashboard.py always generates all three in real use, so this
    only matters for local/dev/test setups that pass no tokens at all)."""
    if not operator_token and not guest_token and not viewer_token:
        return lambda candidate: ROLE_OPERATOR

    def role_for(candidate):
        if operator_token and candidate == operator_token:
            return ROLE_OPERATOR
        if guest_token and candidate == guest_token:
            return ROLE_GUEST
        if viewer_token and candidate == viewer_token:
            return ROLE_VIEWER
        return None
    return role_for
