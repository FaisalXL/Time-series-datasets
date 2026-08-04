"""Split a C3S bulletin page into its own narrative sections, and say what each is about.

The bulletin is natively sectioned in HTML, and the sections are a **topic x period grid**:
`<h2>` names the topic (Global average / European average / Regional overview / Sea surface
temperature; Arctic / Antarctic; Europe / Globe / Longer term trends) and `<h3>` names the
period (the reported month, "The last 12 months - X to Y", "Boreal summer - ...", "Year to
date"). Two `<h3>`s on one page routinely carry the *same* text ("July 2025" appears under
Global average, under European average and under Regional overview), so a leaf heading is
only unique together with its parent -- the section key carries both.

The banked build took the whole theme page as one record, which is why the corpus audit
measured only ~1.9 topics per bulletin.

**Captions are identified structurally, not by keyword.** C3S italicises every figure caption
("Time series of monthly mean Arctic sea ice extent anomalies for all August months from 1979
to 2024 ... Data source: ERA5. Credit: C3S/ECMWF."). Measured over a 70-page sample: 379 of
380 italicised paragraphs are captions and 1,126 of 1,127 non-italicised ones are narrative,
so the `<em>` wrapper decides it and a keyword rule is only the belt-and-braces for the single
outlier. This matters because captions are exactly the text that must not end up spliced
before `<ts></ts>` -- the `55_noaa_stock_assessments` defect.
"""
from __future__ import annotations

import html as _html
import re
from typing import Dict, List, NamedTuple, Optional, Tuple

NAV_HEADINGS = {
    "secondary navigation", "main navigation", "copernicus", "footer",
    "bulletin navigation", "table of contents", "follow us", "share",
    "newsletter", "further reading", "notes to editors", "about the data and analysis",
}

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
_MONTH_RE = "|".join(MONTHS)


class Section(NamedTuple):
    topic: str            # normalised topic (see TOPICS)
    period: str           # 'month' | '12months' | 'season' | 'ytd' | 'other'
    heading: str          # the leaf heading, verbatim
    parent: Optional[str]  # the enclosing <h2>, verbatim
    text: str
    ordinal: int


#: <h2>/<h3> text -> canonical topic. Matched on a squashed form because the wording drifts
#: ("Global average" -> "Global-average temperature" in 2026-05, "Non-retail"-style renames).
TOPICS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^globalaverage(temperature)?$|^globe$|^global$"), "global"),
    (re.compile(r"^european(average|temperature)$|^europe$|^europeanaverage(temperature)?$"), "europe"),
    (re.compile(r"^regionaloverview$"), "regional"),
    (re.compile(r"^seasurfacetemperature$|^sst$"), "sst"),
    (re.compile(r"^arctic$"), "arctic"),
    (re.compile(r"^antarctic$"), "antarctic"),
    (re.compile(r"^longertermtrends?.*$"), "trends"),
    (re.compile(r"^europe.*\d*$"), "europe"),
    (re.compile(r"^globe.*\d*$"), "global"),
]

_PERIOD: List[Tuple[re.Pattern, str]] = [
    (re.compile(rf"(?i)^the\s*last\s*12\s*months"), "12months"),
    (re.compile(rf"(?i)^last\s*12\s*months"), "12months"),
    (re.compile(rf"(?i)^boreal\s*(winter|spring|summer|autumn)"), "season"),
    (re.compile(rf"(?i)^year\s*to\s*date"), "ytd"),
    (re.compile(rf"(?i)^({_MONTH_RE})\s*\d{{4}}$"), "month"),
    (re.compile(rf"(?i)^({_MONTH_RE})$"), "month"),
    (re.compile(rf"(?i)^\w+\s*-\s*({_MONTH_RE})\s*\d{{4}}$"), "month"),
    (re.compile(rf"(?i)^({_MONTH_RE})\s*\d{{4}}\s*(to|-)\s*({_MONTH_RE})"), "12months"),
]


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def topic_of(heading: str, fallback: str) -> str:
    t = _squash(heading)
    for pat, name in TOPICS:
        if pat.match(t):
            return name
    return fallback


def period_of(heading: str) -> str:
    h = _norm(heading)
    for pat, name in _PERIOD:
        if pat.match(h):
            return name
    if re.search(rf"(?i)({_MONTH_RE})\s*\d{{4}}\s*(to|–|-)\s*({_MONTH_RE})", h):
        return "12months"
    if re.search(rf"(?i)\b({_MONTH_RE})\b", h):
        return "month"
    return "other"


# --- paragraph extraction --------------------------------------------------

_JUNK = re.compile(
    r"^(?:[\w-]+\s+\.a\{fill|!function\(\)|window\.|var\s|\{\"|/\*|\.a\{)|"
    r"DOWNLOAD IMAGE|datawrapper-height", re.I)
_CAPTION_KEYWORDS = re.compile(r"Data source:|Credit:|\(Credit", re.I)


def _paragraph_blocks(html_fragment: str) -> List[str]:
    """Narrative paragraphs of a fragment, captions and script blobs removed."""
    out: List[str] = []
    frag = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html_fragment)
    for m in re.finditer(r"(?is)<(p|li)\b[^>]*>(.*?)</\1>", frag):
        inner = m.group(2)
        txt = _norm(re.sub(r"<[^>]+>", " ", inner))
        if len(txt) < 60 or "." not in txt:
            continue
        if _JUNK.search(txt):
            continue
        bare = _norm(re.sub(r"<[^>]+>", " ",
                            re.sub(r"(?is)<(em|i)\b[^>]*>.*?</\1>", " ", inner)))
        italic_fraction = 1 - len(bare) / max(1, len(txt))
        if italic_fraction > 0.8:
            continue                     # a figure caption -- see the module docstring
        if _CAPTION_KEYWORDS.search(txt) and len(txt) < 400:
            continue                     # the 1-in-380 caption C3S did not italicise
        if not out or out[-1] != txt:
            out.append(txt)
    return out


def sections(html: str, theme_fallback: str) -> List[Section]:
    """Page HTML -> its own narrative sections, in reading order."""
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    parts = re.split(r"(?is)(<h([23])\b[^>]*>.*?</h\2>)", s)
    # parts = [pre, h_tag, level, body, h_tag, level, body, ...]
    out: List[Section] = []
    parent: Optional[str] = None
    for i in range(1, len(parts) - 1, 3):
        htag, level, body = parts[i], parts[i + 1], parts[i + 2]
        heading = _norm(re.sub(r"<[^>]+>", " ", htag))
        if not heading or heading.lower() in NAV_HEADINGS:
            if level == "2":
                parent = None
            continue
        if level == "2":
            parent = heading
        paras = _paragraph_blocks(body)
        if not paras:
            continue
        topic = topic_of(heading, "")
        if not topic:
            topic = topic_of(parent or "", theme_fallback)
        period = period_of(heading)
        if period == "other" and parent:
            period = period_of(parent)
        out.append(Section(topic or theme_fallback, period, heading, parent,
                           "\n\n".join(paras), len(out)))
    if not out:
        # Pre-2019 pages carry no content headings at all: the whole page is one section.
        paras = _paragraph_blocks(s)
        if paras:
            out.append(Section(theme_fallback, "month", "", None, "\n\n".join(paras), 0))
    return out


# --- figures the prose quotes ---------------------------------------------

_FIG = re.compile(r"(?<![\w.])(-?\d{1,4}(?:\.\d+)?)\s*°?\s*(?:C\b|degC)?")


class Figure(NamedTuple):
    value: float
    pos: int
    decimals: int


#: What a number in this prose is *not* an anomaly reading. Each was measured as a share of
#: the figures that failed the "is it in the record's own series" check, and each is excluded
#: because the series could not contain it by construction -- not to flatter the statistic:
#:   coordinates       147  "the Bellingshausen Sea (60-120°W)", "60°S-60°N"
#:   durations         ~600 "the 12 months ending in August 2019", "12-month average"
#:   absolute extents  240  "12.1 million km2, that is 1.2 million km2 below" (the *anomaly*
#:                          1.2 is in the series; the absolute 12.1 is a different quantity)
#:   policy thresholds 127  the 1.5 °C and 2 °C targets, and IPCC offsets
#:   area/population % 117  "+243% above average", "affects 77.8% of the population"
_COORD = re.compile(r"^\s*°?\s*[-–]?\s*\d*\s*°?\s*[SNWE]\b")
_DURATION = re.compile(r"^[\s-]*(?:month|year|day|week|hour)s?\b", re.I)
_ABSOLUTE = re.compile(r"^\s*(?:million|thousand|billion)\s*(?:km|square)", re.I)
_PERCENT_OF = re.compile(r"^\s*%\s*(?:of|above|below)|^\s*per ?cent\s*(?:of|above|below)", re.I)
#: impact reporting and meteorological quantities in other units -- rainfall totals, wind
#: speeds, pressures, casualties. The bulletins carry a lot of this and an anomaly series
#: cannot hold any of it.
_OTHER_UNITS = re.compile(
    r"^\s*(?:mm|cm|m\b|metres|meters|km/h|kmh|kt\b|hPa|mbar|lives|deaths|people|"
    r"persons|inhabitants|residents|million\s+people|homes|hectares|ha\b)", re.I)
_THRESHOLD = re.compile(r"(?i)(?:warming of|target|limit|threshold|IPCC|Paris)\D{0,30}$")


def figures(text: str) -> List[Figure]:
    """Numbers the prose quotes as anomaly readings.

    Years are excluded (the bulletins cite them constantly -- "from 1979 to 2024"), as are
    ranking ordinals ("6th"), and the five classes listed above, which are quantities an
    anomaly series cannot hold.
    """
    out: List[Figure] = []
    for m in re.finditer(r"(?<![\w.])(-?\d{1,4}(?:\.\d+)?)(?![\w.])", text):
        lit = m.group(1)
        v = float(lit)
        if "." not in lit and 1850 <= v <= 2100:
            continue
        after = text[m.end():m.end() + 26]
        if re.match(r"(?i)\s*(?:st|nd|rd|th)\b", after[:3]):
            continue
        if _COORD.match(after) or _DURATION.match(after) or _ABSOLUTE.match(after) \
                or _PERCENT_OF.match(after) or _OTHER_UNITS.match(after):
            continue
        if _THRESHOLD.search(text[max(0, m.start() - 40):m.start()]):
            continue
        out.append(Figure(v, m.start(), len(lit.split(".")[1]) if "." in lit else 0))
    return out


_BASELINE_IN_TEXT = re.compile(r"\b(1991[-–]2020|1981[-–]2010|1850[-–]1900)\b")


def baselines_named(text: str) -> List[str]:
    """Reference periods the section's own prose quotes against, in order of appearance.

    This matters for pairing: the bulletins switched their headline baseline from 1981-2010
    to 1991-2020, and a section that says "0.46°C warmer than the 1981-2010 average" must be
    paired with the 1981-2010 series, not whichever file the build happened to prefer. It was
    the single largest genuine cause of prose figures missing from the attached series.
    """
    return [m.group(1).replace("–", "-") for m in _BASELINE_IN_TEXT.finditer(text)]


def quotes(fig: Figure, value: Optional[float]) -> bool:
    """Does `fig` quote `value` at the precision the prose chose?"""
    if value is None:
        return False
    return round(value, fig.decimals) == round(fig.value, fig.decimals)


_RANK = re.compile(
    r"(?i)\b(warmest|coolest|coldest|highest|lowest|wettest|driest|second|third|fourth|fifth|"
    r"sixth|seventh|eighth|ninth|tenth|joint|record)\b")


def states_ranking(text: str) -> bool:
    """Does the section make a rank-in-the-record claim? That is the claim the
    calendar-month-across-years series is the evidence for."""
    return bool(_RANK.search(text))


_STATED_RANK = re.compile(
    r"(?i)\b(?:the\s+)?(?:(\d{1,2})(?:st|nd|rd|th)|(first|second|third|fourth|fifth|sixth|"
    r"seventh|eighth|ninth|tenth|eleventh|twelfth))[\s-]+(warmest|hottest|coolest|coldest|"
    r"highest|lowest|driest|wettest)\b")
_WORD_ORD = {w: i for i, w in enumerate(
    "zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh "
    "twelfth".split())}


def stated_ranks(text: str) -> List[Tuple[int, bool]]:
    """(rank, is_high_extreme) for every explicit rank-in-the-record claim.

    "the sixth warmest June on record" -> (6, True); "the 12th lowest" -> (12, False). This is
    the claim a calendar-month-across-years series is direct evidence for, and it is exactly
    checkable: the terminal point's position in its own record either is the stated rank or is
    not.
    """
    out = []
    for m in _STATED_RANK.finditer(text):
        n = int(m.group(1)) if m.group(1) else _WORD_ORD.get((m.group(2) or "").lower(), 0)
        if n:
            out.append((n, m.group(3).lower() in ("warmest", "hottest", "highest", "wettest")))
    return out


_ABOVE = re.compile(r"(?i)\babove[- ]average|above the .{0,25}average|warmer than average"
                    r"|higher than average|wetter than average")
_BELOW = re.compile(r"(?i)\bbelow[- ]average|below the .{0,25}average|cooler than average"
                    r"|colder than average|lower than average|drier than average")


def stated_anomaly_sign(text: str) -> Optional[int]:
    """+1 / -1 / None for the dominant above-or-below-average characterisation.

    Anomaly series make this exactly checkable: the terminal point's sign either agrees with
    what the prose says about that month or it does not, against a 50% chance baseline.
    """
    a, b = len(_ABOVE.findall(text)), len(_BELOW.findall(text))
    if a == b:
        return None
    return 1 if a > b else -1
