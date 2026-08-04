"""Narrative extraction and sectioning for Richmond Fed Fifth District survey releases.

The record unit is the release's **own narrative block**, exactly as in `48_dallas_tmos`
(thematic paragraph) and `08_bls_cpi` (per-section) -- but Richmond makes the split easier
than either, because for 1997-2017 the release prints its own section headings:

    1997-2004 (archive HTML)  <P><U>Current Activity</U> ... Employment ... Expectations
                              ... Prices, and for the service sector Overall / Retail /
                              Non-retail Services
    2008-2017 (2-column PDF)  bold Overview / Current Activity / Employment /
                              Expectations / Prices
    2018-2026 (chart PDF)     no headings at all -- four to five short paragraphs, so the
                              block is the paragraph, which is the unit `48` shipped

So `blocks()` returns heading-delimited sections where the release provides headings and
paragraphs where it does not. Nothing is templated or generated: a block's text is the
Bank's own words, and the builder appends `<ts></ts>` to it directly.

`channels_named()` then decides which indicators a block is actually about, per sentence,
including whether the sentence is talking about the current reading or the six-months-ahead
expectation. That is what lets a record carry only its own block's channels -- the
`08_bls_cpi` rule that a record can never be one indicator's prose beside another's window.
"""
from __future__ import annotations

import html as _html
import re
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

# --- page furniture, boilerplate and chart labels --------------------------

_DROP_LINE = re.compile(
    r"^(regional surveys of business activity"
    r"|fifth district survey of (manufacturing|service sector|non-?manufacturing) activity"
    r"|the survey of fifth district manufacturing-sector activity"
    r"|fifth district (manufacturing|service-?sector|non-?manufacturing) activity"
    r"|federal reserve bank of richmond|the federal reserve bank of richmond office"
    r"|index, sa|percent change,? (nsa|sa|annualized)|monthly|quarterly"
    r"|3-month moving average|prices paid\s+prices received"
    r"|technical notes?|notes?:|for more information|contact|judy cox|jeannette plamp"
    r"|senior economic analyst|economic analyst|regional economics department"
    r"|ph:|fax:|www\.|https?://|[a-z.]+@[a-z.]+|richmond office|research dept"
    r"|\d{3}[.·]\d{3}[.·]\d{4}|baltimore office|charlotte office"
    r"|next release|recent releases|release schedule|about the survey"
    # 1990s site chrome on the archive pages: nav bars, footer links, a survey-moved notice
    r"|manufacturing survey tables|service.?sector survey tables|survey tables"
    r"|general information|back to main menu|send comments|notice:"
    r"|for further information|the survey of fifth district"
    r"|\d{1,2}\s*$|page \d+)", re.I)

#: The 1997 archive pages wrap the whole document in a navigation bar whose items are
#: separated by '||'. It is long enough to clear the furniture length gate, so it is matched
#: on its own shape instead.
_NAV_BAR = re.compile(r"\|\|")

# chart axis labels: 'Jun-21', a run of standalone axis numbers, 'Series1'
_AXIS = re.compile(r"^(?:[A-Z][a-z]{2}-\d\d[\s,]*)+$|^(?:-?\d{1,3}\s+){2,}-?\d{1,3}$"
                   r"|^series\d$|^-?\d{1,3}$", re.I)

# The heading vocabulary the releases actually use. A block heading is accepted when it is
# short *and* set off (underlined/bold in the source); this list normalises it and is also
# what `--census-headings` reports against, so an unseen heading shows up rather than
# being silently swallowed into the previous section.
HEADINGS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^overview$", re.I), "Overview"),
    (re.compile(r"^current activity$|^current conditions$", re.I), "Current Activity"),
    (re.compile(r"^employment$|^labor market", re.I), "Employment"),
    (re.compile(r"^expectations?$|^looking (ahead|forward)$", re.I), "Expectations"),
    (re.compile(r"^prices?$|^price trends$", re.I), "Prices"),
    (re.compile(r"^overall$", re.I), "Overall"),
    (re.compile(r"^retail$|^retail (activity|sector)$", re.I), "Retail"),
    (re.compile(r"^non-?retail( services| sector)?$|^services firms$", re.I), "Non-retail"),
    (re.compile(r"^revenues?$", re.I), "Revenues"),
    (re.compile(r"^wages?$", re.I), "Wages"),
    (re.compile(r"^capital expenditures$", re.I), "Capital Expenditures"),
    (re.compile(r"^inventories$", re.I), "Inventories"),
    (re.compile(r"^demand$", re.I), "Demand"),
]


def _norm(s: str) -> str:
    s = _html.unescape(s or "")
    for d in "‐‑‒–—−":
        s = s.replace(d, "-")
    return re.sub(r"\s+", " ", s.replace(" ", " ")).strip()


_DATELINE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November"
    r"|December)\s+(\d{1,2}),?\s+((?:19|20)\d\d)\b")
_MONTH_NUM = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"])}


def document_date(raw: bytes, kind: str) -> Optional[str]:
    """The release date the document itself prints, as 'YYYY-MM-DD'.

    Used in preference to the filename, because the retired archive's filenames are
    ambiguous and at least one is wrong: `mfg0709.html` decodes as July 2009 under the
    `mfg{MM}{YY}` scheme every other file in that directory follows, but the document is
    the **July 9, 2002** release -- the Bank named that one `{MM}{DD}`. Trusting the
    filename filed it as a 2009 release reporting June 2009, injecting 2002 values into
    the 2009 vintage, and left July 2002 missing. The document's own dateline is
    unambiguous, so the filename is now only a fallback.
    """
    if kind in ("narr_html", "tbl_html", "page"):
        s = raw.decode("utf8", "ignore")
        s = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", s)
        text = _norm(re.sub(r"(?s)<[^>]+>", " ", s))[:3000]
    else:
        import subprocess
        text = subprocess.run(["pdftotext", "-f", "1", "-l", "1", "-", "-"], input=raw,
                              capture_output=True).stdout.decode("utf8", "ignore")[:3000]
    m = _DATELINE.search(text)
    if not m:
        return None
    return f"{int(m.group(3)):04d}-{_MONTH_NUM[m.group(1)]:02d}-{int(m.group(2)):02d}"


def normalise_heading(text: str) -> Optional[str]:
    t = _norm(text).strip(" :.")
    if len(t) > 40 or not t:
        return None
    for pat, name in HEADINGS:
        if pat.match(t):
            return name
    return None


class Block(NamedTuple):
    heading: Optional[str]      # the release's own section heading, normalised
    raw_heading: Optional[str]  # verbatim, for the census
    text: str
    ordinal: int


# --- era A: the 1997-2004 archive narrative page ---------------------------


def _html_paragraphs(raw: bytes) -> List[Tuple[str, bool]]:
    """-> [(text, is_set_off)]. Set-off = the whole paragraph is inside <U>/<B>/<STRONG>,
    which is how the archive pages mark their section headings (`<P><U>Prices</U>`)."""
    s = raw.decode("utf8", "ignore")
    s = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    parts = re.split(r"(?i)<p\b[^>]*>|</p>|<tr\b[^>]*>|<h[1-6]\b[^>]*>", s)
    out: List[Tuple[str, bool]] = []
    for part in parts:
        inner = re.sub(r"(?is)<(u|b|strong|em|i)\b[^>]*>(.*?)</\1>", r"\2", part)
        stripped = _norm(re.sub(r"(?s)<[^>]+>", " ", part))
        if not stripped:
            continue
        bare = _norm(re.sub(r"(?s)<[^>]+>", " ", re.sub(
            r"(?is)<(u|b|strong|em|i)\b[^>]*>.*?</\1>", " ", part)))
        set_off = bool(stripped) and not bare and stripped != inner.strip()
        out.append((stripped, set_off or bool(re.search(r"(?is)^\s*<(u|b|strong)\b", part.strip()))))
    return out


# --- era C/D: the PDF releases --------------------------------------------


def narrative_pages(raw: bytes, release: Optional[str] = None) -> int:
    """How many leading pages hold narrative, i.e. everything before the results table.

    Measured per document rather than fixed: the 2018+ layout puts the narrative on page 1
    and the table on page 2, while the 2008-2017 layout runs the narrative over pages 1-2
    with the table on page 3. A fixed `max_pages` therefore either truncates the older
    releases or drags the table's row labels into the newer ones' prose.
    """
    import richtab
    page_of: List[int] = []
    try:
        import io
        import warnings
        warnings.filterwarnings("ignore")
        import pdfplumber
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            n = len(pdf.pages)
    except Exception:
        return 2
    for i in range(n):
        try:
            sub = richtab._pdf_lines_page(raw, i)
        except Exception:
            break
        if any(richtab._header_months_words(l, release) for l in sub):
            page_of.append(i)
    return max(1, min(page_of)) if page_of else n


def _pdf_paragraphs(raw: bytes, max_pages: Optional[int] = None,
                    release: Optional[str] = None) -> List[Tuple[str, bool]]:
    import colex
    return colex.paragraphs(raw, max_pages=max_pages or narrative_pages(raw, release))


# --- shared: furniture filtering + block assembly -------------------------


#: A run of two or more `Mon-YY` tokens is a chart's year axis. Occasionally the masking in
#: `colex` misses one and the axis is appended to the paragraph it sits beside
#: ("Employment continued to grow on pace with last S -40 Jan-06 Jan-07 Jan-08 …"), which
#: puts numbers in the text that belong to no series at all. The block is cut at the axis.
_AXIS_RUN = re.compile(r"\s*(?:\b[A-Z][a-z]{2}-\d\d\b[\s,]*){2,}")


def strip_axis_run(text: str) -> str:
    m = _AXIS_RUN.search(text)
    if not m:
        return text
    head = text[:m.start()].rstrip()
    # drop a dangling axis fragment the run left behind ("… with last S -40")
    head = re.sub(r"\s+(?:[A-Z]\s+)?-?\d{1,3}(?:\.\d+)?$", "", head)
    return head.rstrip()


#: A furniture marker only disqualifies a *short* line. Every running header, contact block
#: and chart caption in these releases is a standalone line well under this length, whereas
#: the lede paragraph opens "Fifth District manufacturing activity was flat in June,
#: according to …" -- which matches the running-header pattern at its start. Matching on the
#: prefix alone silently deleted the lede of every 2018+ release: the same class of
#: false positive that cost 24.6% of the narrative in an earlier package, so the rule here
#: is length-gated and `--census-drops` reports what it removed.
_FURNITURE_MAX_CHARS = 130

#: Boilerplate whose *opening* is unmistakable, dropped regardless of length. The table
#: page's methodology note runs to ~1,000 characters, so the length gate above cannot reach
#: it; conversely these openings never begin a narrative paragraph, so no length gate is
#: needed. Keeping the two lists separate is what stops one rule from having to be both
#: permissive enough for the lede and strict enough for the notes.
_DROP_BLOCK = re.compile(
    r"^(technical notes?\b|notes?:|each index equals the percentage"
    r"|expectations refer to the time period|current price changes are"
    r"|price changes are expressed|results are based on responses"
    r"|the (manufacturing|composite) index is a gauge"
    r"|all firms surveyed are located"
    # The retired-era pages close with a one-line contact block that runs past the length
    # gate: "For further information, contact: Judy Higgins - phone (804) 697-8152 - fax
    # (804) 697-8287 - Federal Reserve Bank of Richmond - Research Department". Left in, its
    # phone numbers are the single largest source of figures in the text that are not in the
    # attached series -- i.e. it corrupts the alignment measurement as well as the prose.
    r"|for (further|more) information"
    # the retired press-release header block, which runs past the length gate
    r"|federal reserve bank of richmond\s+(the\s+)?(manufacturing|services?-?sector)"
    r"|press release)", re.I)


def _keep(text: str) -> bool:
    t = _norm(text)
    if len(t) < 3:
        return False
    if _AXIS.match(t) or _DROP_BLOCK.match(t) or _NAV_BAR.search(t):
        return False
    if _DROP_LINE.match(t) and len(t) <= _FURNITURE_MAX_CHARS and t.count(". ") < 2:
        return False
    # a date line ("January 23, 2018") or a bare running header
    if re.fullmatch(r"[A-Z][a-z]+ \d{1,2},? \d{4}", t):
        return False
    # mostly digits: a stray table row or axis
    letters = sum(c.isalpha() for c in t)
    digits = sum(c.isdigit() for c in t)
    return letters >= 2 * digits and letters >= 12


# A heading the release prints inline, at the head of its section's first paragraph. The
# 2008-2017 PDFs set headings in the body font at the body pitch, so the extractor sees
# "Overview Fifth District manufacturing activity grew mildly…" as one paragraph. Split
# only when the next word starts a new sentence (capitalised): "Employment Manufacturing
# employment strengthened" is a heading plus prose, "Employment grew faster" is prose.
_INLINE_HEADING = re.compile(
    r"^(Overview|Current Activity|Current Conditions|Employment|Expectations"
    r"|Price Trends|Prices|Overall|Retail|Non-?retail(?: Services| Sector)?"
    r"|Services Firms|Revenues|Wages|Capital Expenditures|Inventories|Demand"
    r"|Labor Markets?|Looking (?:Ahead|Forward))\s+(?=[A-Z“‘(])")


def split_inline_heading(text: str) -> Tuple[Optional[str], str]:
    m = _INLINE_HEADING.match(_norm(text))
    if not m:
        return None, _norm(text)
    return m.group(1), _norm(text)[m.end():]


def blocks(raw: bytes, kind: str, max_pages: Optional[int] = None,
           release: Optional[str] = None) -> Tuple[List[Block], Optional[str]]:
    """Release document -> (narrative blocks, headline).

    The headline ("Manufacturing Activity Declined Sharply in April") is returned
    separately rather than prepended to the first block: in the 2018+ layout the lede
    paragraph restates it almost verbatim, so folding it in would put a near-duplicate
    sentence in one record of every release. It is kept in `meta.headline`.
    """
    paras = (_html_paragraphs(raw) if kind in ("narr_html", "page")
             else _pdf_paragraphs(raw, max_pages, release))
    headline: Optional[str] = None
    items: List[Tuple[Optional[str], Optional[str], str]] = []
    cur_head: Optional[str] = None
    cur_raw: Optional[str] = None
    for text, set_off in paras:
        t = _norm(text)
        h = normalise_heading(t)
        if h and (set_off or kind in ("narr_html", "page")):
            cur_head, cur_raw = h, t
            continue
        t = strip_axis_run(t)
        inline, rest = split_inline_heading(t)
        if inline:
            cur_head, cur_raw = normalise_heading(inline) or inline, inline
            t = rest
        if not _keep(t):
            continue
        # the headline: the first set-off, sentence-less line before any body prose
        if headline is None and not items and (set_off or kind in ("narr_html", "page")) \
                and not t.endswith(".") and 20 <= len(t) <= 200:
            headline = t
            continue
        items.append((cur_head, cur_raw, t))

    out: List[Block] = []
    for head, rawh, text in items:
        if out and out[-1].heading == head and rawh == out[-1].raw_heading and head is not None:
            out[-1] = Block(head, rawh, out[-1].text + " " + text, out[-1].ordinal)
        else:
            out.append(Block(head, rawh, text, len(out)))
    return out, headline


# --- which indicators is a block about? -----------------------------------

# prose phrasing -> canonical channel. Longest match wins, so "new orders" is not eaten by
# "orders" and "raw materials prices" is not eaten by "prices".
#: Ordered **most specific first**: the combined pattern is a single alternation, so at any
#: position the first listed group that matches wins. Ordering is therefore load-bearing --
#: "firms struggled to find employees with the skills they needed, as this indicator fell
#: from -3 in May to -14 in June" was attaching the *employment* channel and pairing the
#: skills indicator's numbers with it, the cross-indicator contamination
#: `47_philadelphia_mbos` had to fix. Compound phrases now precede their component words.
PROSE: List[Tuple[str, str]] = [
    # -- prices, before any bare "prices"/"raw materials"/"finished goods" word
    (r"prices paid|prices (?:they )?pa(?:y|id)|raw materials?,? prices"
     r"|prices (?:of|for) raw materials|input prices|supplier prices"
     r"|prices (?:they )?pay suppliers", "prices_paid"),
    (r"prices received|finished goods?,? prices|prices (?:of|for) finished goods"
     r"|output prices", "prices_received"),
    # the retired service survey asks a single price question per sector -- see
    # richtab._PRICE_ROW_BY_SECTOR. `channels_named` falls back to whichever of
    # prices / prices_paid / prices_received the release's own table actually printed.
    (r"service[- ]sector prices|prices at (?:retail|services firms)|price (?:index|levels?)"
     r"|prices", "prices"),
    # -- skills, before "employees"
    (r"employees with the skills(?: they need(?:ed)?)?|availability of skills(?: needed)?"
     r"|skills? availability|skills (?:they )?need(?:ed)?|skills index"
     r"|workers with (?:the )?necessary skills", "skills_availability"),
    # -- the retired era calls the inventory indexes "the finished goods index" etc.
    (r"finished goods inventor(?:y|ies)|finished goods index|finished goods stocks?",
     "finished_goods_inventories"),
    (r"raw materials inventor(?:y|ies)|raw materials index|raw materials stocks?",
     "raw_materials_inventories"),
    (r"composite (?:manufacturing )?index|manufacturing (?:composite )?index"
     r"|broadest (?:measure|indicator)s? of (?:manufacturing )?activity", "composite_index"),
    (r"(?:volume of )?new orders", "new_orders"),
    (r"(?:orders? )?backlogs?(?: of (?:new )?orders)?|backlog of orders", "order_backlog"),
    (r"capacity utili[sz]ation", "capacity_utilization"),
    (r"vendor (?:lead[- ]?time|delivery time)s?|delivery times?|lead[- ]?times?",
     "vendor_lead_time"),
    (r"average workweek|workweek|factory hours|hours worked", "average_workweek"),
    (r"local business conditions", "local_business_conditions"),
    # The modern service-sector releases call the forward-looking demand index simply "the
    # expectations index", and the skills indicator "the skills index".
    (r"expectations? index", "demand"),
    (r"skills index", "skills_availability"),
    (r"capital expenditures?|capital spending", "capital_expenditures"),
    (r"equipment (?:and|&) software spending", "equipment_software_spending"),
    (r"(?:business )?services expenditures?", "services_expenditures"),
    (r"inventor(?:y|ies)|stocks?", "inventories"),
    (r"big[- ]ticket sales", "big_ticket_sales"),
    (r"shopper traffic", "shopper_traffic"),
    (r"shipments?", "shipments"),
    (r"revenues?|sales revenues?", "revenues"),
    (r"demand", "demand"),
    (r"number of employees|employment|jobs index|hiring|payrolls?|employees",
     "employment"),
    (r"wages?|wage growth|average wage", "wages"),
]
_PROSE_RE = re.compile("|".join(f"(?P<g{i}>{p})" for i, (p, _t) in enumerate(PROSE)), re.I)
_PROSE_TAG = {f"g{i}": t for i, (_p, t) in enumerate(PROSE)}

_FUT = re.compile(r"\b(future|expect\w*|anticipat\w*|six months|6 months|looking ahead"
                  r"|looking forward|forward[- ]looking|coming (?:six )?months|next six months"
                  r"|planned|outlook|forecast\w*|in coming months|over the next)\b", re.I)
#: Headings that name a *sector* and so fix the prefix for their whole section. Topic
#: headings are deliberately absent: the "Prices" section walks through the district-wide,
#: retail and non-retail price series in consecutive sentences, so forcing one prefix across
#: it left two of the three quoted series with no channel in the record.
SECTOR_HEADINGS = {"Retail": "retail_", "Non-retail": "nonretail_", "Overall": ""}

_RETAIL = re.compile(r"\bretail(?:ers?|ing)?\b", re.I)
_NONRETAIL = re.compile(r"\bnon-?retail\b|\bservices firms?\b", re.I)


def sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", text) if s.strip()]


def channels_named(block: Block, allowed: Set[str]) -> Set[Tuple[str, str]]:
    """(channel, half) pairs this block is about, decided sentence by sentence.

    `half` is 'fut' when the sentence carries a forward-looking marker ("the future
    indexes for shipments", "producers expected"), else 'cur'; a section headed
    Expectations defaults its unmarked sentences to 'fut'. Retail / non-retail prefixes
    come from the sentence too, for the service-sector eras that split those out.
    `allowed` is the channel set the release's own table actually printed, so a phrase
    can never conjure a channel the record has no series for.
    """
    out: Set[Tuple[str, str]] = set()
    head_fut = block.heading in ("Expectations",)
    # The retail / non-retail prefix comes from the section heading the Bank prints, and
    # only falls back to sentence wording where there is no heading. Deciding it per
    # sentence attached the wrong sector's series: under the "Retail" heading, "Shopper
    # traffic increased 24 points" names no sector, so it resolved to the district-wide
    # channel, which the retired table does not carry, and the claim was dropped; under
    # "Overall", "an upsurge in revenues at services firms" pulled in the non-retail
    # revenues series while the sentence's subject was the aggregate index.
    head_pre = SECTOR_HEADINGS.get(block.heading)
    for sent in sentences(block.text):
        fut = bool(_FUT.search(sent)) or head_fut
        if head_pre is not None:
            pre = head_pre
        elif _NONRETAIL.search(sent):
            pre = "nonretail_"
        elif _RETAIL.search(sent):
            pre = "retail_"
        else:
            pre = ""
        for m in _PROSE_RE.finditer(sent):
            tag = next(_PROSE_TAG[g] for g, v in m.groupdict().items() if v)
            variants = (tag, "prices_paid", "prices_received") if tag == "prices" else (tag,)
            cands = [p + v for v in variants for p in ((pre, "") if pre else ("",))]
            for cand in cands:
                for half in (("fut",) if fut else ("cur",)):
                    if (cand, half) in allowed:
                        out.add((cand, half))
                        break
                else:
                    continue
                break
    return out


#: A figure is an index reading or a price rate. Excluded: shares of respondents ("54
#: percent of firms"), and durations -- "over the coming 6 months" was being counted as a
#: quoted value of 6 and then reported as a figure missing from the series.
_FIG = re.compile(r"(?<![\w.])(-?\d{1,3}(?:\.\d+)?)"
                  r"(?![\w.]|\s*percent(?:age)? of|[- ](?:month|week|year|day|quarter)s?\b)")


class Figure(NamedTuple):
    value: float
    pos: int
    decimals: int          # decimal places the prose actually wrote


def figures_with_pos(text: str) -> List[Figure]:
    """Numbers the prose quotes as index readings or price rates, with position.

    Excludes shares of respondents ("54 percent of firms"), and excludes years, which the
    releases use freely ("since May 1997"). `decimals` is retained because the prose
    rounds: a price change the table prints as 2.14 is quoted as "2.1" or even "2", so a
    figure can only be compared with a series value at the precision the prose used.
    """
    out: List[Figure] = []
    for m in _FIG.finditer(text):
        lit = m.group(1)
        v = float(lit)
        if 1900 <= v <= 2100 and "." not in lit:
            continue
        out.append(Figure(v, m.start(), len(lit.split(".")[1]) if "." in lit else 0))
    return out


def figures(text: str) -> List[float]:
    return [f.value for f in figures_with_pos(text)]


def quotes(fig: Figure, value: Optional[float]) -> bool:
    """Does `fig` quote `value`, allowing for the precision the prose chose?

    A diffusion index is an integer in the table and quoted as one, so this is exact for
    the index channels; it only loosens for the price-change channels, which the table
    prints to two decimals and the prose rounds. Comparing those exactly reported 216
    figures as "not in the attached series" when the series held the unrounded value.
    """
    if value is None:
        return False
    return round(value, fig.decimals) == round(fig.value, fig.decimals)


def recites_ordered_pair(text: str, channels: List[Tuple[str, List[Optional[float]]]],
                         span: int = 140) -> Optional[str]:
    """Does the prose quote a channel's last two points *in order*, close together?

    Returns the channel name that matched, or None. This is the `recites` test, and it is
    an ordered pair rather than a single value on purpose: these are bounded small integers
    (roughly -60..60), so "does any quoted number equal any terminal value" has a high
    coincidence floor -- measured here, a permutation control fired 23.7% of the time
    against a 60.1% true rate, i.e. more than a third of the tags would have been chance.
    The releases state the move as an ordered pair ("the composite manufacturing index
    decreased to 4 in June from 13 in May", "Shipments fell to 3 from 16"), so requiring
    terminal-then-previous within one clause tests the structure the prose actually has.
    Pairs whose two values are equal are skipped: they carry no ordering information.
    """
    figs = figures_with_pos(text)
    if not figs:
        return None
    for unit, vals in channels:
        if len(vals) < 2:
            continue
        cur, prev = vals[-1], vals[-2]
        if cur is None or prev is None or round(cur, 2) == round(prev, 2):
            continue
        for f1 in figs:
            if not quotes(f1, cur):
                continue
            for f2 in figs:
                if f1.pos < f2.pos <= f1.pos + span and quotes(f2, prev):
                    return unit
    return None


_POINTS = re.compile(r"(-?\d{1,3})[- ]?point", re.I)
_WORD_NUM = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
_WORD_POINTS = re.compile(
    r"\b(" + "|".join(_WORD_NUM) + r"|twenty[- ]\w+|thirty[- ]?\w*|forty[- ]?\w*)"
    r"[- ]?points?\b", re.I)
_UP = re.compile(r"\b(rose|increase\w*|gain\w*|climb\w*|advanc\w*|added|up|higher|improv\w*"
                 r"|strengthen\w*|expand\w*|picked up|jump\w*|surg\w*|quicker|faster)\b", re.I)
_DOWN = re.compile(r"\b(fell|decreas\w*|declin\w*|dropp?\w*|lost|shed|slipp\w*|down|lower"
                   r"|weaken\w*|soften\w*|moderat\w*|retreat\w*|pull\w*back|slow\w*"
                   r"|plummet\w*|contract\w*)\b", re.I)


def stated_points_change(text: str) -> List[float]:
    """Month-over-month moves the prose states in index points, digits or words.

    The releases write both ("gained four points to 14", "the index slipped four points",
    "added ten points to 42"), so a digits-only reader misses most of the retired era.
    """
    out = [float(m.group(1)) for m in _POINTS.finditer(text)]
    for m in _WORD_POINTS.finditer(text):
        w = m.group(1).lower().replace("-", " ")
        if w in _WORD_NUM:
            out.append(float(_WORD_NUM[w]))
        else:
            parts = w.split()
            tens = {"twenty": 20, "thirty": 30, "forty": 40}
            if parts[0] in tens:
                out.append(float(tens[parts[0]] + _WORD_NUM.get(parts[1] if len(parts) > 1 else "zero", 0)))
    return out


def stated_direction(text: str) -> Optional[int]:
    """+1 / -1 / None for the dominant direction word in the text."""
    up, dn = len(_UP.findall(text)), len(_DOWN.findall(text))
    if up == dn:
        return None
    return 1 if up > dn else -1
