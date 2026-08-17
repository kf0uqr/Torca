"""
QRZ.com Logbook API client -- https://www.qrz.com/docs/logbook/QRZLogbookAPI.html
(confirmed directly against QRZ's own docs, not guessed). A thin,
synchronous urllib client mirroring satellite_tracking.py's
fetch_transponders()/fetch_amateur_tles() style exactly: no new HTTP
library dependency, raises on failure, callers catch and report.

Every request is POST https://logbook.qrz.com/api with URL-encoded
KEY=<api key>&ACTION=<INSERT|DELETE|FETCH|STATUS> plus action-specific
parameters. The response body is itself URL-encoded name=value pairs
(NOT JSON) -- parsed with urllib.parse.parse_qsl for every action
EXCEPT FETCH, whose ADIF= value contains literal, non-percent-encoded
'&' characters that break parse_qsl outright -- see
_split_qrz_fetch_response's docstring, confirmed against a real
account's response.
"""

import html
import urllib.error
import urllib.parse
import urllib.request

import adif

QRZ_LOGBOOK_API_URL = "https://logbook.qrz.com/api"

# QRZ requires an identifiable User-Agent -- "ApplicationName/version.number",
# max 128 chars -- confirmed via their own docs; a generic/default urllib
# user agent risks rate limiting.
_USER_AGENT = "TORCA/1.0"


class QrzApiError(Exception):
    """Raised when QRZ's own response reports RESULT=FAIL (or an
    unrecognized RESULT) -- as opposed to a transport-level failure
    (network error, timeout, bad key rejected before QRZ even parses
    it), which raises the underlying urllib/OSError instead. Either
    way, callers catch and report -- same convention as
    satellite_tracking.py's fetch_transponders()."""


def _parse_qrz_response(text: str) -> dict:
    """QRZ's responses are URL-encoded name=value pairs, not JSON --
    e.g. "RESULT=OK&LOGID=130877825&COUNT=1". Keys are upper-cased on
    return for consistent lookup regardless of QRZ's own casing."""
    return {key.upper(): value for key, value in urllib.parse.parse_qsl(text, keep_blank_values=True)}


def _qrz_raw_request(api_key: str, action: str, **params) -> str:
    """The raw response TEXT, before any &-delimited parsing -- FETCH
    needs this directly (see _split_qrz_fetch_response) rather than
    going through _parse_qrz_response, which corrupts a FETCH
    response's ADIF value (see that function's docstring)."""
    data = {"KEY": api_key, "ACTION": action, **params}
    request = urllib.request.Request(
        QRZ_LOGBOOK_API_URL,
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers={"User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        text = response.read().decode("utf-8", errors="replace")
    # Prints the RAW, unparsed wire response -- STATUS coming back
    # RESULT=OK with a genuinely empty DATA (reported live) means
    # something is off between what QRZ actually sends and what
    # _parse_qrz_response/callers expect, and guessing further through
    # that parsing layer without seeing ground truth risks another
    # blind miss. Truncated defensively in case a FETCH response is
    # large (a full-logbook ADIF dump).
    print(f"[QRZ API] {action} raw response ({len(text)} bytes): {text[:2000]!r}")
    return text


def _qrz_request(api_key: str, action: str, **params) -> dict:
    return _parse_qrz_response(_qrz_raw_request(api_key, action, **params))


def _split_qrz_fetch_response(text: str):
    """Returns (parsed_head_dict, raw_adif_value) for a FETCH response.

    Confirmed live against a real account: QRZ's FETCH response embeds
    the ADIF payload with LITERAL, non-percent-encoded '&' characters
    (from its own &lt;/&gt; HTML-escaping of every ADIF tag -- see
    qrz_fetch), which breaks urllib.parse.parse_qsl's normal &-
    delimited splitting completely -- parse_qsl("RESULT=OK&COUNT=2438
    &ADIF=&lt;call:5&gt;N7PHY...") reads the ADIF value as EMPTY and
    then produces several more bogus, meaningless key=value pairs from
    the fragments after each stray '&' (confirmed: this, not the
    later html.unescape gap, was the actual reason FETCH always
    returned 0 records even after that fix landed).

    ADIF is always the LAST field in every real response seen -- this
    finds the literal "ADIF=" marker once and treats everything after
    it as the raw ADIF value, completely unparsed/unsplit; only the
    (short, safely &-delimited) portion before that marker goes
    through the normal parser."""
    marker = "ADIF="
    index = text.find(marker)
    if index == -1:
        return _parse_qrz_response(text), ""
    head = text[:index].rstrip("&")
    return _parse_qrz_response(head), text[index + len(marker):]


def qrz_insert(api_key: str, adif_record: str, replace: bool = False) -> dict:
    """Inserts one QSO (one ADIF record, from adif.format_adif_record).
    Returns {"result": "OK"/"REPLACE", "logid": int|None, "count": int}.
    Raises QrzApiError on RESULT=FAIL (with QRZ's own REASON text, if
    given) or an unrecognized RESULT."""
    params = {"ADIF": adif_record}
    if replace:
        params["OPTION"] = "REPLACE"
    parsed = _qrz_request(api_key, "INSERT", **params)
    result = parsed.get("RESULT")
    if result not in ("OK", "REPLACE"):
        raise QrzApiError(parsed.get("REASON") or f"QRZ INSERT failed (RESULT={result!r})")
    # Docs show a single LOGID on INSERT's example response, but also
    # LOGIDS (plural) elsewhere in the same doc -- accept either key
    # rather than assume only one is ever used.
    logid = parsed.get("LOGID") or parsed.get("LOGIDS")
    return {
        "result": result,
        "logid": int(logid) if logid else None,
        "count": int(parsed.get("COUNT", 0) or 0),
    }


def qrz_delete(api_key: str, logids) -> dict:
    """Deletes one or more QSOs by LOGID (an iterable of ints). Returns
    {"result": "OK"/"PARTIAL", "count": int}. Raises QrzApiError on
    RESULT=FAIL."""
    parsed = _qrz_request(api_key, "DELETE", LOGIDS=",".join(str(logid) for logid in logids))
    result = parsed.get("RESULT")
    if result not in ("OK", "PARTIAL"):
        raise QrzApiError(parsed.get("REASON") or f"QRZ DELETE failed (RESULT={result!r})")
    return {"result": result, "count": int(parsed.get("COUNT", 0) or 0)}


def qrz_fetch(api_key: str, option: str = "ALL") -> list:
    """Fetches QSO records matching `option` (QRZ's own filter syntax,
    e.g. "ALL", "AFTERLOGID:1000,MAX:250") as a list of ADIF field
    dicts (via adif.parse_adif_records), each carrying QRZ's own
    per-record LOGID under APP_QRZLOG_LOGID -- confirmed live against
    a real account. Raises QrzApiError on RESULT=FAIL."""
    text = _qrz_raw_request(api_key, "FETCH", OPTION=option)
    parsed, adif_text = _split_qrz_fetch_response(text)
    result = parsed.get("RESULT")
    if result != "OK":
        raise QrzApiError(parsed.get("REASON") or f"QRZ FETCH failed (RESULT={result!r})")
    if not adif_text:
        return []
    # Confirmed live: QRZ HTML-entity-escapes the ADIF payload inside
    # its response (&lt;/&gt; instead of literal </>) -- unescape
    # before parsing or every ADIF tag just looks like plain text.
    adif_text = html.unescape(adif_text)
    return adif.parse_adif_records(adif_text)


def qrz_status(api_key: str) -> dict:
    """Returns logbook metadata as a plain dict. Confirmed live against
    a real account that QRZ's actual STATUS response is flat --
    COUNT/CONFIRMED/BOOK_NAME/OWNER/CALLSIGN/START_DATE/END_DATE/
    BOOKID/DXCC_COUNT etc. sit directly at the top level alongside
    RESULT/ACTION, not nested inside a separate DATA=name=value&...
    sub-string as an earlier reading of QRZ's docs assumed (that
    version always returned {} -- RESULT=OK, but no DATA key to dig
    into at all). Raises QrzApiError on RESULT=FAIL."""
    parsed = _qrz_request(api_key, "STATUS")
    result = parsed.get("RESULT")
    if result != "OK":
        raise QrzApiError(parsed.get("REASON") or f"QRZ STATUS failed (RESULT={result!r})")
    return parsed
