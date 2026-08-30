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


def make_token_check(token):
    """Returns a token_ok(candidate) closure -- same "no token
    configured means anything passes" behavior every WS endpoint in
    this app already uses."""
    def token_ok(candidate):
        return not token or candidate == token
    return token_ok
