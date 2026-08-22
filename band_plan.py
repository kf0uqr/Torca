"""
US amateur radio band plan data -- which frequency ranges are amateur
allocations, which emission types (CW-only/CW+data/phone/unrestricted)
are authorized where, and which license class is the lowest that may
transmit there. Feeds widgets.py's BandPlanOverlayWidget (the strip
drawn under the waterfall in main_window.py).

Every boundary below is sourced directly from the two FCC rules that
actually govern it -- not a summary, not a remembered value -- fetched
and read in full (same "get the primary source, not an AI summary of
it" discipline this app already applies to protocol specs, see
aprs.py's own docstring):

- 47 CFR 97.301 (https://www.law.cornell.edu/cfr/text/47/97.301,
  fetched via `curl` + a script stripping HTML tags, not WebFetch's own
  summarizer -- a license-privilege table is exactly the kind of byte-
  exact content that's previously been confirmed lossy through that
  path): which license class(es) may transmit in each band. ONLY the
  ITU Region 2 column is used throughout -- the US is entirely Region
  2, and Region 1/3 values (also present in that table) do not apply
  here and are not transcribed.
- 47 CFR 97.305 (same fetch method, https://www.law.cornell.edu/cfr/
  text/47/97.305): which emission types (CW/RTTY-data/phone-image) are
  authorized in each sub-band. Where 97.305 doesn't split a band at
  all, every emission type is authorized throughout it ("all" below) --
  this is a real FCC fact (e.g. 160m, 630m, 2200m, 30m, and most of
  each VHF/UHF band), not a gap in this dataset.

Deliberate scope decisions:
- Only the license class BOUNDARY matters here (the lowest class with
  any privilege in a given cell) -- not power limits, bandwidth caps,
  or the sharing-requirement footnotes 97.301/97.305 attach to some
  entries (e.g. 60m's per-channel power limits, 10m's narrower
  Novice/Technician bandwidth). Real, but out of scope for a band-plan
  overlay whose job is "what class, what mode", not a full privileges
  reference -- operators still need the actual FCC rules or ARRL's own
  license-class chart for anything power/bandwidth-specific.
- 40m's narrow 7.075-7.100 MHz phone carve-out (97.305(c)(3)(v), itself
  cross-referencing standards (f)(9) and (f)(11) whose exact license-
  class scope isn't fully spelled out in 97.305's own table text) is
  deliberately NOT split out as its own cell -- it's folded into the
  surrounding CW/data cell instead, the safe direction for an
  ambiguous boundary (CW/data is unconditionally correct there for
  every class in that cell regardless of the phone carve-out's own
  exact scope; the alternative -- asserting a phone/license boundary
  this module can't fully verify -- risked being confidently wrong).
- Non-continuous-range allocations are skipped: 219-220 MHz (a separate,
  narrow, digital-message-forwarding-only 1.25m allocation distinct
  from the main 222-225 MHz 1.25m band) and every microwave band above
  23cm (2.3 GHz and up -- well outside any HF/VHF/UHF transceiver this
  app targets).
- 60m is genuinely channelized (5 fixed channels, not a continuous
  tunable range) -- represented as 5 narrow cells at the channel
  center frequencies (values transcribed from the ARRL's own printed
  band chart's "USB dial frequency" convention, cross-checked against
  97.305(c)(3)(iii)'s own slightly different carrier-frequency
  convention -- same channels, different reference point within each).

Effective as of the FCC's own last-amended date on 97.301
(91 FR 1430, Jan 14, 2026) -- current as of when this module was
written, not guaranteed to stay current forever; band plans do change.
"""

# License classes in ascending order of what they unlock -- a cell's
# "min_license" is the LOWEST class in this list that may transmit
# there (every class at or after it in this list may also transmit
# there, since FCC privileges are cumulative by design). Novice is
# deliberately not its own entry -- 97.301(e) grants Novice and
# Technician classes IDENTICAL HF privileges, and Novice licenses
# haven't been issued in decades, so "Technician" already covers every
# currently-issuable license with access to those cells.
LICENSE_CLASSES = ["Technician", "General", "Advanced", "Extra"]


def license_rank(license_class):
    return LICENSE_CLASSES.index(license_class)


# Mode/emission-type keys used in each band's "segments" -- deliberately
# not full emission-designator granularity, just the practical
# distinction a waterfall overlay is useful for:
# - "cw": CW/data emissions ARE the only thing authorized (default rule
#   per 97.305(a): CW is always authorized everywhere the operator has
#   any privilege at all -- these cells are ones where 97.305 doesn't
#   ALSO add data/phone/image on top of that default).
# - "cw_data": CW plus RTTY/data (no phone/image).
# - "phone": phone/image (CW is still always allowed alongside it, per
#   the same 97.305(a) default -- "phone" here means "phone is ALSO
#   authorized", not "phone only").
# - "all": every emission type authorized, no FCC-mandated split at all
#   within this cell (a real, common case -- see this module's own
#   docstring).
MODE_LABELS = {
    "cw": "CW only",
    "cw_data": "CW / RTTY / Data",
    "phone": "Phone / Image",
    "all": "All modes",
}

# amplitude_to_color-style anchor colors, one per mode key -- chosen to
# stay visually distinct from WaterfallWidget's own blue/cyan/yellow/red
# amplitude gradient (widgets.py's _COLOR_ANCHORS) so the overlay never
# reads as "more waterfall" at a glance.
MODE_COLORS = {
    "cw": (230, 140, 40),        # orange
    "cw_data": (150, 90, 220),   # violet
    "phone": (60, 170, 230),     # cyan-blue
    "all": (90, 170, 110),       # muted green
}


def _mhz(value):
    return int(round(value * 1_000_000))


def _khz(value):
    return int(round(value * 1_000))


# Each band: {"name", "start_hz", "end_hz", "segments": [(start_hz,
# end_hz, mode_key, min_license), ...]} -- segments are the
# intersection of 97.301's license cells and 97.305's mode cells (each
# segment boundary is a real boundary in at least one of those two
# rules), ordered low-to-high, contiguous, covering the band exactly
# once, per this module's own docstring.
BANDS = [
    {
        "name": "2200m", "start_hz": _khz(135.7), "end_hz": _khz(137.8),
        "segments": [(_khz(135.7), _khz(137.8), "all", "Extra")],
    },
    {
        "name": "630m", "start_hz": _khz(472), "end_hz": _khz(479),
        "segments": [(_khz(472), _khz(479), "all", "General")],
    },
    {
        "name": "160m", "start_hz": _khz(1800), "end_hz": _khz(2000),
        "segments": [(_khz(1800), _khz(2000), "all", "General")],
    },
    {
        "name": "80m/75m", "start_hz": _mhz(3.500), "end_hz": _mhz(4.000),
        "segments": [
            (_mhz(3.500), _mhz(3.525), "cw_data", "Extra"),
            (_mhz(3.525), _mhz(3.600), "cw_data", "Technician"),
            (_mhz(3.600), _mhz(3.800), "phone", "Advanced"),
            (_mhz(3.800), _mhz(4.000), "phone", "General"),
        ],
    },
    {
        # Channelized, not a continuous tunable range -- see module
        # docstring. "channelized" opts this band out of the
        # contiguous-coverage assumption every other band's segment
        # list satisfies (test_band_plan.py's own contiguity check
        # skips it accordingly) -- the gaps between channels are real,
        # not a data-entry gap.
        "name": "60m", "start_hz": _khz(5330.5), "end_hz": _khz(5406.5), "channelized": True,
        "segments": [
            (_khz(5330.5), _khz(5333.5), "all", "General"),
            (_khz(5346.5), _khz(5349.5), "all", "General"),
            (_khz(5351.5), _khz(5366.5), "all", "General"),
            (_khz(5371.5), _khz(5374.5), "all", "General"),
            (_khz(5403.5), _khz(5406.5), "all", "General"),
        ],
    },
    {
        "name": "40m", "start_hz": _mhz(7.000), "end_hz": _mhz(7.300),
        "segments": [
            (_mhz(7.000), _mhz(7.025), "cw_data", "Extra"),
            (_mhz(7.025), _mhz(7.125), "cw_data", "Technician"),
            (_mhz(7.125), _mhz(7.175), "phone", "Advanced"),
            (_mhz(7.175), _mhz(7.300), "phone", "General"),
        ],
    },
    {
        "name": "30m", "start_hz": _mhz(10.100), "end_hz": _mhz(10.150),
        "segments": [(_mhz(10.100), _mhz(10.150), "cw_data", "General")],
    },
    {
        "name": "20m", "start_hz": _mhz(14.000), "end_hz": _mhz(14.350),
        "segments": [
            (_mhz(14.000), _mhz(14.025), "cw_data", "Extra"),
            (_mhz(14.025), _mhz(14.150), "cw_data", "General"),
            (_mhz(14.150), _mhz(14.175), "phone", "Extra"),
            (_mhz(14.175), _mhz(14.225), "phone", "Advanced"),
            (_mhz(14.225), _mhz(14.350), "phone", "General"),
        ],
    },
    {
        "name": "17m", "start_hz": _mhz(18.068), "end_hz": _mhz(18.168),
        "segments": [
            (_mhz(18.068), _mhz(18.110), "cw_data", "General"),
            (_mhz(18.110), _mhz(18.168), "phone", "General"),
        ],
    },
    {
        "name": "15m", "start_hz": _mhz(21.000), "end_hz": _mhz(21.450),
        "segments": [
            (_mhz(21.000), _mhz(21.025), "cw_data", "Extra"),
            (_mhz(21.025), _mhz(21.200), "cw_data", "Technician"),
            (_mhz(21.200), _mhz(21.225), "phone", "Extra"),
            (_mhz(21.225), _mhz(21.275), "phone", "Advanced"),
            (_mhz(21.275), _mhz(21.450), "phone", "General"),
        ],
    },
    {
        "name": "12m", "start_hz": _mhz(24.890), "end_hz": _mhz(24.990),
        "segments": [
            (_mhz(24.890), _mhz(24.930), "cw_data", "General"),
            (_mhz(24.930), _mhz(24.990), "phone", "General"),
        ],
    },
    {
        "name": "10m", "start_hz": _mhz(28.000), "end_hz": _mhz(29.700),
        "segments": [
            (_mhz(28.000), _mhz(28.300), "cw_data", "Technician"),
            (_mhz(28.300), _mhz(28.500), "phone", "Technician"),
            (_mhz(28.500), _mhz(29.700), "phone", "General"),
        ],
    },
    {
        "name": "6m", "start_hz": _mhz(50.0), "end_hz": _mhz(54.0),
        "segments": [
            (_mhz(50.0), _mhz(50.1), "cw", "Technician"),
            (_mhz(50.1), _mhz(54.0), "all", "Technician"),
        ],
    },
    {
        "name": "2m", "start_hz": _mhz(144.0), "end_hz": _mhz(148.0),
        "segments": [
            (_mhz(144.0), _mhz(144.1), "cw", "Technician"),
            (_mhz(144.1), _mhz(148.0), "all", "Technician"),
        ],
    },
    {
        "name": "1.25m", "start_hz": _mhz(222.0), "end_hz": _mhz(225.0),
        "segments": [(_mhz(222.0), _mhz(225.0), "all", "Technician")],
    },
    {
        # 420-450 MHz is the ITU Region 2 (US) allocation -- Region 1/3
        # use 430-440 MHz instead, not applicable here.
        "name": "70cm", "start_hz": _mhz(420.0), "end_hz": _mhz(450.0),
        "segments": [(_mhz(420.0), _mhz(450.0), "all", "Technician")],
    },
    {
        "name": "33cm", "start_hz": _mhz(902.0), "end_hz": _mhz(928.0),
        "segments": [(_mhz(902.0), _mhz(928.0), "all", "Technician")],
    },
    {
        "name": "23cm", "start_hz": _mhz(1240.0), "end_hz": _mhz(1300.0),
        "segments": [(_mhz(1240.0), _mhz(1300.0), "all", "Technician")],
    },
]


def segments_in_range(start_hz, end_hz):
    """Every (band_name, seg_start_hz, seg_end_hz, mode_key,
    min_license) segment that overlaps [start_hz, end_hz] at all --
    the whole-span query BandPlanOverlayWidget's paintEvent runs once
    per repaint against whatever frequency range the waterfall is
    currently showing. Returned in ascending frequency order; a span
    covering no amateur allocation at all yields an empty list (the
    caller draws that as unallocated space, not an error)."""
    if end_hz < start_hz:
        start_hz, end_hz = end_hz, start_hz
    results = []
    for band in BANDS:
        if band["end_hz"] < start_hz or band["start_hz"] > end_hz:
            continue
        for seg_start, seg_end, mode, min_license in band["segments"]:
            if seg_end < start_hz or seg_start > end_hz:
                continue
            results.append((band["name"], seg_start, seg_end, mode, min_license))
    return results
