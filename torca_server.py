"""
Thin wrapper around rigplane's own `web` CLI (invoked by torca-server)
that disables one specific rigplane behavior: RadioPoller's periodic
"unselected VFO slot" refresh (see README.md's "VFO A/B flicker +
audio glitch" section).

That refresh genuinely swaps the radio to VFO B, reads it, and swaps
back via real CI-V commands roughly every 5 seconds per receiver, to
keep VFO B's frequency/mode known for split display. On radios whose
USB audio output follows the currently-active VFO (confirmed live
against a real IC-7300), that brief real swap also produces an
audible glitch in RX audio streamed over the web audio channel, on
the same cadence -- confirmed by testing, not guessed.

rigplane has no public flag or config option to disable this refresh
(confirmed by reading its source -- no CLI flag, no environment
variable, nothing in web/radio_poller.py besides the hardcoded
interval below). This patches that interval directly.

DELIBERATELY fragile: RadioPoller._UNSELECTED_SLOT_INTERVAL is a
private, underscore-prefixed rigplane internal, not a stable API. If a
future rigplane release renames or restructures it, the patch below
just becomes a no-op (caught, reported once, non-fatal) -- the
periodic VFO swap and its audio glitch would return, but nothing here
crashes. Direct consequence of skipping the refresh: VFO B's
frequency/mode goes stale in this server's own state for any client
relying on it (e.g. split display).
"""

import sys

try:
    from rigplane.web.radio_poller import RadioPoller

    RadioPoller._UNSELECTED_SLOT_INTERVAL = float("inf")
except Exception as exc:
    print(
        f"torca_server: couldn't patch out rigplane's unselected-VFO-slot "
        f"refresh ({exc}) -- falling back to normal rigplane behavior "
        f"(periodic VFO swap / audio glitch every ~5s may return).",
        file=sys.stderr,
    )

from rigplane.cli import main

if __name__ == "__main__":
    sys.exit(main())
