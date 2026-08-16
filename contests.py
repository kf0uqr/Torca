"""
WA7BNM Contest Calendar -- via its own published PUBLIC Google Calendar
(linked directly from contestcalendar.com's own home page: calendar ID
9o3or51jjdsantmsqoadmm949k@group.calendar.google.com), fetched as a
standard iCalendar (.ics) feed -- the well-documented, always-available
mechanism for any public Google Calendar
(calendar.google.com/calendar/ical/<id>/public/basic.ics), no API key
or registration needed. WA7BNM's own JSON/XML feed requires a separate
written agreement with the site owner (confirmed via
contestcalendar.com/terms.php: "may be available... upon execution of
a written agreement") -- this sidesteps that entirely by using the
same calendar data they already publish openly for calendar apps.

Pure, no-Qt, mirrors pota.py/pskreporter.py's style. Includes a
minimal RFC 5545 iCalendar VEVENT parser (line-unfolding plus SUMMARY/
DTSTART/DTEND extraction only -- every other VEVENT property is
ignored) rather than adding a new third-party dependency, matching
adif.py's own "write the small parser we actually need" convention.
Confirmed live against a real fetch (5MB, ~12,500 events spanning
2017-2026): every DTSTART/DTEND in this feed is UTC
("YYYYMMDDTHHMMSSZ"), never a floating/local time or an all-day
"YYYYMMDD" date-only value -- _parse_ics_datetime still tolerates
both of those forms defensively, but the confirmed-live case is the
"Z"-suffixed one.
"""

import datetime
import urllib.error
import urllib.request

CONTEST_CALENDAR_ICS_URL = (
    "https://calendar.google.com/calendar/ical/"
    "9o3or51jjdsantmsqoadmm949k%40group.calendar.google.com/public/basic.ics"
)

# Same identifying-User-Agent convention used for every other external
# fetch in this app.
_USER_AGENT = "Radione/1.0 (desktop ham radio control application)"


def _unfold_ics_lines(text: str) -> list:
    """RFC 5545 line unfolding: a line beginning with a single space or
    tab is a continuation of the previous line -- the leading
    whitespace character itself is NOT part of the content and is
    stripped, the rest is appended directly (no space inserted)."""
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    unfolded = []
    for line in lines:
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _parse_ics_datetime(value: str):
    """Parses an ICS DTSTART/DTEND value. This feed's own values are
    always the UTC form (YYYYMMDDTHHMMSSZ, confirmed live) -- the
    floating-local-time (no "Z") and all-day-date-only (no "T" at all)
    forms are also handled defensively (treated as UTC, since RFC 5545
    doesn't attach a timezone to either on its own) even though
    they've never actually been observed in this specific feed."""
    value = value.strip()
    if value.endswith("Z"):
        return datetime.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
    if "T" in value:
        return datetime.datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.strptime(value, "%Y%m%d").replace(tzinfo=datetime.timezone.utc)


def _unescape_ics_text(value: str) -> str:
    """RFC 5545 TEXT values escape backslash/comma/semicolon/newline
    with a leading backslash (e.g. "CVA DX Contest\\, CW" for a literal
    comma in the name) -- confirmed live, several real contest names in
    this feed contain one. A single left-to-right pass (rather than
    chained .replace() calls) so an escaped backslash immediately
    followed by a comma is handled correctly instead of potentially
    unescaping twice."""
    result = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            nxt = value[i + 1]
            if nxt in ("n", "N"):
                result.append("\n")
            elif nxt in (",", ";", "\\"):
                result.append(nxt)
            else:
                result.append(nxt)  # unknown escape -- drop the backslash, keep the literal char
            i += 2
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def parse_contest_calendar_ics(text: str) -> list:
    """Returns every VEVENT in `text` as {"name", "start" (UTC
    datetime), "end" (UTC datetime)} -- an event missing SUMMARY,
    DTSTART, or DTEND, or with an unparseable date value, is skipped
    rather than guessed."""
    events = []
    current = None
    for line in _unfold_ics_lines(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current and all(key in current for key in ("SUMMARY", "DTSTART", "DTEND")):
                try:
                    events.append({
                        "name": _unescape_ics_text(current["SUMMARY"]),
                        "start": _parse_ics_datetime(current["DTSTART"]),
                        "end": _parse_ics_datetime(current["DTEND"]),
                    })
                except ValueError:
                    pass
            current = None
            continue
        if current is None:
            continue
        if ":" not in line:
            continue
        prop, _sep, value = line.partition(":")
        prop = prop.split(";", 1)[0]  # drop any ";PARAM=..." suffix (e.g. "DTSTART;VALUE=DATE")
        if prop in ("SUMMARY", "DTSTART", "DTEND"):
            current[prop] = value
    return events


def fetch_contests() -> list:
    """Fetches and parses the full contest calendar feed. Raises on
    transport/parse failure -- callers catch and report, same
    convention as every other fetch in this app. Returns EVERY event
    in the feed (years of history, since this is the same feed
    calendar apps subscribe to, not a windowed query) -- callers
    should filter to whatever window they actually want to display
    (see ham_dashboard.py's _build_contest_rows, which keeps only
    events still in progress or upcoming)."""
    request = urllib.request.Request(CONTEST_CALENDAR_ICS_URL, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8", errors="replace")
    return parse_contest_calendar_ics(text)
