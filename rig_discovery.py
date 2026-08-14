"""
A single small shared helper: probing a connected rigplane radio object
for the first attribute name (from a list of guesses) that actually
exists. rigplane's documentation is sparse enough that this "try several
plausible names, use whichever one is real" pattern shows up throughout
this app -- AudioBridge (TX audio method resolution), RadioWorker (level/
meter/control getter and setter discovery). Kept in its own module,
separate from constants.py, since it's logic rather than data, and both
audio.py and radio_worker.py need to import it without either depending
on the other.
"""


def find_method_name(obj, candidates):
    """Returns the first candidate name that exists as an attribute on
    obj, or None if none of them do."""
    for name in candidates:
        if hasattr(obj, name):
            return name
    return None
