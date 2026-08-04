"""Parse the as-published diffusion-index table out of a Richmond Fed survey release.

Why this exists: `mfg_historicaldata.xlsx` / `nmf_historicaldata.xlsx` are the *revised*
series. The release itself says so -- "Seasonal adjustment factors are recalculated every
July and the entire series is revised to better reflect current economic trends" -- so the
workbook disagrees with what every older release printed, and pairing real release prose
with today's numbers pairs the text with values it never quoted. Each release, though,
prints its own table, which is exactly the vintage its prose quotes. Same finding as
`47_philadelphia_mbos` and `48_dallas_tmos`; third time in this family.

Four source formats, all of which print **three consecutive months** per indicator, so
consecutive releases overlap and stitch into a continuous as-first-published vintage:

  1. `pdf`  2008-10 .. present release PDF, page 2/3 "Business Activity Indexes".
            Column headings are bare month names (2008-2017) or `Mon-YY` (2018+).
  2. `csv`  2008-2015 `*_busindex_*.csv` -- the same table, machine-readable, with a
            `section_type` column and no month labels at all.
  3. `tbl_html` 1997-2004 archive table page: month *names* in the header, sub-tables
            headed "Now vs. a month ago" / "Now vs. Six months from now", and a
            "3-Month Average" column that gives a free within-row checksum.
  4. `page` the 2008-2017 release page, when the PDF is missing.

Column assignment is positional-nearest, not order-based: `pdftotext -layout` keeps
horizontal offsets, so every number is assigned to the header month whose column centre
it is nearest. That is the column-wise form of the trap `47` hit on rows -- a fixed
tolerance let a row swallow its neighbour's cells and shifted a whole table by one
indicator, unnoticed until a ground-truth check. A number that lands between columns
fails the row instead of silently shifting it.
"""
from __future__ import annotations

import csv
import html as _html
import io
import re
import subprocess
from typing import Dict, List, NamedTuple, Optional, Tuple

_DASHES = "‐‑‒–—−"
_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]
_MON3 = {m[:3]: i + 1 for i, m in enumerate(_MONTHS)}

# Canonical channel <- the label the release prints, in any era.
#
# Matching is on a *squashed* form of the label (lower-cased, every non-alphanumeric
# character removed) rather than on word-separated text. Reason: the results page of some
# releases is emitted with per-glyph positioning, so word boundaries have to be recovered
# from inter-character gaps and a narrow space renders "Vendor Lead Time" as one token.
# Squashing makes the label vocabulary independent of that reconstruction; without it
# every multi-word label silently fails and the table halves in size.
ROW_LABELS: List[Tuple[Tuple[str, ...], str]] = [
    # -- manufacturing
    (("manufacturingindex", "fifthdistrictmanufacturingindex", "compositeindex",
      "fifthdistrictmanufacturingcompositeindex"), "composite_index"),
    (("shipments",), "shipments"),
    (("volumeofneworders", "neworders"), "new_orders"),
    (("backlogoforders", "orderbacklog", "orderbacklogs", "backlogofneworders"),
     "order_backlog"),
    (("capacityutilization", "capacityutilisation"), "capacity_utilization"),
    (("vendorleadtime", "vendorleadtimes", "vendordeliverytime"), "vendor_lead_time"),
    (("averageworkweek", "workweek"), "average_workweek"),
    (("finishedgoodsinventories", "finishedgoodsinventory"), "finished_goods_inventories"),
    (("rawmaterialsinventories", "rawmaterialsinventory"), "raw_materials_inventories"),
    # -- service sector
    (("revenues", "salesrevenues", "revenuessales", "servicesectorrevenues"), "revenues"),
    (("demand", "productdemandduringnextsixmonths", "productdemand", "expecteddemand",
      "expecteddemandforgoods", "expecteddemandforservices",
      "demandforservices", "demandforgoods"), "demand"),
    (("inventories",), "inventories"),
    (("bigticketsales",), "big_ticket_sales"),
    (("shoppertraffic",), "shopper_traffic"),
    # -- shared
    (("numberofemployees", "employment", "employees"), "employment"),
    (("wages", "averagewage", "averagewages", "wage"), "wages"),
    (("localbusinessconditions",), "local_business_conditions"),
    (("capitalexpenditures", "capitalexpenditure"), "capital_expenditures"),
    (("equipmentsoftwarespending", "equipmentandsoftwarespending"),
     "equipment_software_spending"),
    (("servicesexpenditures", "businessservicesexpenditures"), "services_expenditures"),
    (("availabilityofskillsneeded", "availabilityofskills"), "skills_availability"),
    (("pricespaid", "pricepaid"), "prices_paid"),
    (("pricesreceived", "pricereceived"), "prices_received"),
]
_LABEL_INDEX = {v: tag for variants, tag in ROW_LABELS for v in variants}

# Retired-era service tables repeat the same labels under three group headings, so the
# group has to qualify the channel or Retail revenues overwrite District revenues.
GROUP_PREFIX: List[Tuple[str, str]] = [
    ("retailindicators", "retail_"),
    ("nonretailindicators", "nonretail_"),
    ("servicesfirmsindicators", "nonretail_"),        # the 2005-2018 name for non-retail
    ("servicesectorindicators", ""),
    ("overallservicesectorindicators", ""),
    # manufacturing group headings carry no disambiguating force (labels are unique),
    # but they must still clear a stale prefix left by a previous group
    ("generalbusinessassessment", ""),
    ("companyconditions", ""),
    ("inventorylevels", ""),
    ("inventorieslevels", ""),
    ("pricetrends", ""),
    ("employment", ""),
]

# "cur" = current conditions, "fut" = six months ahead
_HALF_CUR = ("nowvsamonthago", "comparedtothepreviousmonth", "currentconditions",
             "currenttrends", "inventorylevels", "inventorieslevels")
_HALF_FUT = ("sixmonthsfromnow", "sixmonthsahead", "expectations", "expectedtrends",
             "nowvssixmonthsfromnow")

_NUM = re.compile(r"^[-+]?\d{1,3}(\.\d+)?$")

#: Indicators the survey only ever asks about as a *current* level, even where the retired
#: table prints them trailing the six-months-ahead block with no heading of their own.
#:
#: Measured, not assumed: across the 82 retired table pages only 2 print an inventories row
#: in both halves, and in the 2000-01 layout the current-conditions block omits inventories
#: entirely while the rows that trail the six-months block hold the values the release's own
#: prose reports as current ("both finished goods and raw materials inventories increased
#: more slowly to settle at 11 and 5" -> the trailing rows read 11 and 5). Without this the
#: inventories records carried the expectations window beside current-level prose. Contrast
#: `capital_expenditures`, which genuinely appears only in the six-months block because that
#: is the only way the survey asks it -- so "appears once" cannot be the rule.
_ALWAYS_CURRENT = {"finished_goods_inventories", "raw_materials_inventories"}

#: Inside a price section the service-sector tables label rows by *sector* rather than by
#: what the price is ("Service Sector" / "Retail" / "Services Firms", and just "Services" in
#: the retired layout). The older survey asks one price question per sector, not the modern
#: paid/received pair, so those rows become a distinct `prices` channel rather than being
#: guessed into `prices_paid` or `prices_received` -- there is nothing in the source that
#: says which, and inventing the distinction would put the wrong series beside the prose.
_PRICE_SECTION = ("pricetrends", "currentpricetrends", "expectedpricetrends",
                  "currenttrends", "expectedtrends", "currrenttrends")
_PRICE_ROW_BY_SECTOR = {
    "servicesector": "prices", "services": "prices", "manufacturing": "prices",
    "allfirms": "prices", "total": "prices",
    "retail": "retail_prices", "retailtrade": "retail_prices",
    "servicesfirms": "nonretail_prices", "nonretail": "nonretail_prices",
}


def _price_row_label(cell: str) -> Optional[str]:
    return _PRICE_ROW_BY_SECTOR.get(_squash(cell))


class Table(NamedTuple):
    months: List[str]                       # ['YYYY-MM', ...] newest first; [] if unlabelled
    cells: Dict[Tuple[str, str], List[Optional[float]]]   # (channel, half) -> per-month values
    avg: Dict[Tuple[str, str], Optional[float]]           # 3-month average where printed
    fmt: str
    rejected: int                           # rows dropped for un-assignable numbers


def _norm(s: str) -> str:
    s = _html.unescape(s or "")
    for d in _DASHES:
        s = s.replace(d, "-")
    return re.sub(r"\s+", " ", s.replace(" ", " ")).strip()


def _num(s: str) -> Optional[float]:
    s = _norm(s).replace("+", "").replace(",", "").replace("%", "")
    if not s or s in {"-", "--", "---", "n/a", "na", "nan", "*"}:
        return None
    return float(s) if _NUM.match(s) else None


def _squash(s: str) -> str:
    """Lower-case, drop every non-alphanumeric. See the note on ROW_LABELS."""
    return re.sub(r"[^a-z0-9]+", "", _norm(s).lower())


def _label(cell: str) -> Optional[str]:
    t = _squash(cell)
    t = re.sub(r"\d+$", "", t)                    # trailing footnote marker(s)
    return _LABEL_INDEX.get(t)


#: A row label that names its own horizon. The retired service table prints "Product demand
#: during next six months" inside the current-conditions block -- there is no separate
#: expectations block in that layout -- so the row has to be filed as an expectations series
#: from its label, not from the block it sits in. Without this, prose about the
#: forward-looking demand index requested a channel the record did not carry, and the demand
#: figures the section quotes were the largest single group of unmatched figures.
_LABEL_IS_FUTURE = re.compile(r"nextsixmonths|sixmonthsfromnow|expected|expectations")


def _label_half(cell: str, default: str) -> str:
    return "fut" if _LABEL_IS_FUTURE.search(_squash(cell)) else default


def _group_prefix(text: str) -> Optional[str]:
    t = _squash(text)
    for key, pre in GROUP_PREFIX:
        if t.startswith(key):
            return pre
    return None


def _half_of(text: str) -> Optional[str]:
    t = _squash(text)
    fut = any(k in t for k in _HALF_FUT)
    cur = any(k in t for k in _HALF_CUR)
    if fut and not cur:
        return "fut"
    if cur and not fut:
        return "cur"
    if fut and cur:
        return None            # a header row carrying both captions: not a section marker
    # Fallback for a mislabelled section. The release CSVs head their current-price block
    # "Currrent trends" -- with three r's -- in every file of that era, so an exact-keyword
    # reader dropped both current-price rows from all 99 CSV releases and left the price
    # prose quoting values the record had no channel for. Anything that says "trends" and
    # does not say "expected" is the current block.
    if "trends" in t and "expect" not in t:
        return "cur"
    return None


def _shift_year(year: int, month: int, back: int) -> Tuple[int, int]:
    m = month - back
    while m <= 0:
        m += 12
        year -= 1
    return year, m


# --- format 1/4: PDF and release-page HTML ---------------------------------


def _ym_back(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    y, m = _shift_year(y, m, k)
    return f"{y:04d}-{m:02d}"


def _runs(seq: List[str]) -> List[List[int]]:
    """Indices of `seq` grouped into maximal consecutive-descending-month runs."""
    if not seq:
        return []
    runs, cur = [], [0]
    for i in range(1, len(seq)):
        if seq[i] == _ym_back(seq[i - 1], 1):
            cur.append(i)
        else:
            runs.append(cur)
            cur = [i]
    runs.append(cur)
    return runs


# releases whose own header mislabels a column (see _normalise_header)
HEADER_ANOMALIES: List[str] = []


def _normalise_header(seq: List[str]) -> Optional[Tuple[List[str], List[List[int]]]]:
    """Validate a candidate header's month labels -> (months per column, half index runs).

    What this separates: the results-table header (`Jun-26 May-26 Apr-26 | Jun-26 May-26
    Apr-26` -- three consecutive months, newest first, printed once per half) from a
    chart's year axis on page 1 (`Jun-21 Jun-22 Jun-23 …`, ascending and annual). Reading
    an axis as the header assigns every row to columns that do not exist.

    The Expectations half is taken to repeat the Current half's three *survey* months by
    position rather than by its printed labels, because some releases mislabel it -- the
    2016-01 manufacturing table heads its expectations columns `Jan-15 Dec-15 Nov-15`
    when the release is January 2016. The table's own note ("Expectations refer to the
    time period six months out from the survey period") fixes the meaning structurally,
    so the column is kept and the mismatch counted rather than the whole table dropped.
    """
    if len(seq) not in (3, 6):
        return None
    first = seq[:3]
    if _runs(first) != [[0, 1, 2]]:
        return None
    if len(seq) == 3:
        return first, [[0, 1, 2]]
    if seq[3:] != first:
        HEADER_ANOMALIES.append(f"{first[0]}:{'/'.join(seq[3:])}")
    return first + first, [[0, 1, 2], [3, 4, 5]]


def _header_months(line: str, release: Optional[str]) -> List[Tuple[int, str]]:
    """Header line -> [(char_offset, 'YYYY-MM')] for every month column, left to right.

    Two labellings are in use and both are read rather than assumed: `Jun-26` (2018+)
    carries its own year; a bare `Jan` (2008-2017) does not, so it is resolved against
    the release date -- which is why `release` is required for that era.
    """
    out: List[Tuple[int, str]] = []
    for m in re.finditer(r"\b([A-Z][a-z]{2})[a-z]*-(\d{2})\b", line):
        mo = _MON3.get(m.group(1).lower())
        if mo:
            out.append((m.start(), f"{2000 + int(m.group(2)):04d}-{mo:02d}"))
    if not out and release:
        ry, rm = int(release[:4]), int(release[5:7])
        for m in re.finditer(r"\b([A-Z][a-z]{2})[a-z]*\.?\b", line):
            mo = _MON3.get(m.group(1).lower())
            if not mo:
                continue
            # the release's own month, or the nearest earlier occurrence of that month
            y = ry if mo <= rm else ry - 1
            out.append((m.start(), f"{y:04d}-{mo:02d}"))
    if _normalise_header([ym for _, ym in out]) is None:
        return []
    return out


def _assign(row: str, cols: List[Tuple[int, str]], tol: int = 8
            ) -> Optional[Dict[int, Optional[float]]]:
    """Assign every numeric token in `row` to its nearest header column.

    Returns None if any token is farther than `tol` characters from every column centre,
    or if two tokens claim one column -- either means the row was mis-read, and failing
    is the point (a silently shifted row is the failure mode this guards against).
    Only the part of the line at or after the first column is scanned, so the footnote
    marker glued to a label ("Fifth District Manufacturing Index 3") is not a cell.
    """
    lo = max(0, cols[0][0] - tol)
    got: Dict[int, Optional[float]] = {}
    for m in re.finditer(r"(?<![\w.])(-{2,}|[-+]?\d{1,3}(?:\.\d+)?)(?![\w.%])", row):
        if m.start() < lo:
            continue
        tok = m.group(1)
        centre = (m.start() + m.end() - 1) / 2.0
        best, bd = None, 1e9
        for i, (off, _ym) in enumerate(cols):
            # a right-aligned column: the header token starts at `off` and spans ~6 chars
            c = off + 2.0
            if abs(centre - c) < bd:
                best, bd = i, abs(centre - c)
        if best is None or bd > tol:
            return None
        if best in got:
            return None
        got[best] = _num(tok)
    return got or None


def parse_layout(txt: str, release: Optional[str]) -> Table:
    lines = txt.split("\n")
    cells: Dict[Tuple[str, str], List[Optional[float]]] = {}
    cols: List[Tuple[int, str]] = []
    half_spans: List[Tuple[int, int, str]] = []
    months: List[str] = []
    rejected = 0
    group = ""
    for ln in lines:
        if not ln.strip():
            continue
        hm = _header_months(ln, release)
        if len(hm) >= 3:
            cols = hm
            months = [ym for _, ym in hm]
            # the two halves sit side by side; split them where the month sequence restarts
            half_spans = [(r, "cur" if k == 0 else "fut")
                          for k, r in enumerate(_runs([ym for _, ym in hm]))]
            continue
        pre = _group_prefix(ln)
        if pre is not None:
            group = pre
        if not cols:
            continue
        cellsplit = re.split(r"\s{2,}", ln.strip())
        tag = _label(cellsplit[0]) if cellsplit else None
        if not tag:
            continue
        tag = group + tag
        got = _assign(ln, cols)
        if got is None:
            rejected += 1
            continue
        for idxs, half in half_spans:
            vals = [got.get(i) for i in idxs]
            if any(v is not None for v in vals):
                cells.setdefault((tag, half), vals)
    curmonths = [cols[i][1] for i in half_spans[0][0]] if half_spans else months[:3]
    return Table(curmonths, cells, {}, "layout", rejected)


def _pdf_lines(raw: bytes, only_page: Optional[int] = None) -> List[List[dict]]:
    """PDF -> lines of words, each word `{text, x0, x1, cx}`, in reading order.

    Word geometry rather than `pdftotext -layout`: the results table sets its header in a
    different size from its cells, so the layout renderer's space padding puts a header
    month and the column it heads up to six characters apart. Assigning cells to columns
    off that text is how a whole table ends up shifted by one -- the row-wise version of
    which is the trap `47_philadelphia_mbos` documents. Point coordinates are exact.

    Words are assembled from *characters* rather than taken from `extract_words`, because
    on 21 of the 102 cached non-manufacturing releases (and none of the manufacturing
    ones) the results page is emitted with per-glyph positioning -- `extract_words`
    returns 'B','u','s','i','n','e','s','s' as eight words and the header line never
    forms, so the whole table silently reads as absent. Clustering on the inter-character
    gap handles both emitters with one code path.
    """
    import io
    import statistics as _st
    import warnings
    warnings.filterwarnings("ignore")
    import pdfplumber
    out: List[List[dict]] = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        pages = pdf.pages if only_page is None else pdf.pages[only_page:only_page + 1]
        for page in pages:
            chars = [c for c in page.chars if (c.get("text") or "").strip()]
            if not chars:
                continue
            hs = [c["bottom"] - c["top"] for c in chars] or [10.0]
            ltol = max(2.0, _st.median(hs) * 0.5)
            rows: List[List[dict]] = []
            for c in sorted(chars, key=lambda c: (round(c["top"], 1), c["x0"])):
                if rows and abs(c["top"] - rows[-1][0]["top"]) <= ltol:
                    rows[-1].append(c)
                else:
                    rows.append([c])
            for r in rows:
                r.sort(key=lambda c: c["x0"])
                words: List[dict] = []
                cur = [r[0]]
                for prev, c in zip(r, r[1:]):
                    gap = c["x0"] - prev["x1"]
                    if gap > max(1.0, 0.30 * (c["size"] or 10.0)):
                        words.append(cur)
                        cur = [c]
                    else:
                        cur.append(c)
                words.append(cur)
                out.append([{"text": "".join(x["text"] for x in w),
                             "x0": w[0]["x0"], "x1": w[-1]["x1"],
                             "cx": (w[0]["x0"] + w[-1]["x1"]) / 2.0} for w in words])
    return out


def _pdf_lines_page(raw: bytes, page_index: int) -> List[List[dict]]:
    """`_pdf_lines` restricted to one page (used to locate the results-table page)."""
    return _pdf_lines(raw, only_page=page_index)


def _header_months_words(line: List[dict], release: Optional[str]) -> List[Tuple[float, str]]:
    cols: List[Tuple[float, str]] = []
    dated = False
    for w in line:
        m = re.fullmatch(r"([A-Z][a-z]{2})[a-z]*-(\d{2})", w["text"])
        if m and _MON3.get(m.group(1).lower()):
            cols.append((w["cx"], f"{2000 + int(m.group(2)):04d}-{_MON3[m.group(1).lower()]:02d}"))
            dated = True
    if not dated and release:
        ry, rm = int(release[:4]), int(release[5:7])
        for w in line:
            m = re.fullmatch(r"([A-Z][a-z]{2})[a-z]*\.?", w["text"])
            mo = _MON3.get(m.group(1).lower()) if m else None
            if not mo:
                continue
            y = ry if mo <= rm else ry - 1
            cols.append((w["cx"], f"{y:04d}-{mo:02d}"))
    if _normalise_header([ym for _, ym in cols]) is None:
        return []
    return cols


_AVG_COL = re.compile(r"3-?month|average", re.I)

AVG_KEY = -1        # `got` slot for a trailing "3-Month Average" column


def _avg_x(line: List[dict]) -> Optional[float]:
    """x-centre of a trailing '3-Month Average' header column, if the table prints one.

    Without this the average column's value is a number sitting past the last month
    column, `_assign_words` cannot place it, and the row is rejected -- which would drop
    every row of the service-sector layout used up to 2018-01 and through the retired era.
    Captured instead, it also supplies the free within-row checksum `check_average` uses.
    """
    toks = [w for w in line if _AVG_COL.search(w["text"] or "")]
    if not toks:
        return None
    return sum(w["cx"] for w in toks) / len(toks)


def _assign_words(line: List[dict], cols: List[Tuple[float, str]], tol: float = 16.0,
                  avg_x: Optional[float] = None) -> Optional[Dict[int, Optional[float]]]:
    """-> {column_index: value}. Indexed by *column*, not by month: the Current and
    Expectations halves print the same three month labels, so a month-keyed result makes
    the two halves collide and every row of every release is silently rejected."""
    got: Dict[int, Optional[float]] = {}
    targets = [(c, i) for i, (c, _ym) in enumerate(cols)]
    if avg_x is not None:
        targets.append((avg_x, AVG_KEY))
    lo = cols[0][0] - 3 * tol
    for w in line:
        if w["cx"] < lo:
            continue                        # label text and its footnote marker
        t = _norm(w["text"])
        if not re.fullmatch(r"-{2,}|[-+]?\d{1,3}(?:\.\d+)?", t):
            if re.fullmatch(r"[-+]?\d{1,3}(?:\.\d+)?[a-z%,]+", t, re.I):
                return None                 # a value fused to a word: mis-read row
            continue
        best, bd = None, 1e9
        for c, i in targets:
            if abs(w["cx"] - c) < bd:
                best, bd = i, abs(w["cx"] - c)
        if best is None or bd > tol:
            return None
        if best in got:
            return None
        got[best] = _num(t)
    return got or None


def parse_pdf(raw: bytes, release: Optional[str]) -> Table:
    lines = _pdf_lines(raw)
    cells: Dict[Tuple[str, str], List[Optional[float]]] = {}
    avg: Dict[Tuple[str, str], Optional[float]] = {}
    cols: List[Tuple[float, str]] = []
    half_spans: List[Tuple[List[int], str]] = []
    months: List[str] = []
    avgx: Optional[float] = None
    rejected = 0
    group = ""
    caption_half = "cur"
    for line in lines:
        joined = " ".join(w["text"] for w in line)
        hm = _header_months_words(line, release)
        if hm:
            months, runs = _normalise_header([ym for _, ym in hm])
            cols = [(cx, ym) for (cx, _), ym in zip(hm, months)]
            avgx = _avg_x(line)
            if len(runs) == 2:
                half_spans = [(r, "cur" if k == 0 else "fut") for k, r in enumerate(runs)]
            else:
                # A single-half header: which half it is comes from the caption above it
                # ("Current Conditions" / "Expectations"), because the service-sector
                # layout in use to 2018-01 prints the two halves as separate sub-tables.
                half_spans = [(runs[0], caption_half)]
            continue
        pre = _group_prefix(joined)
        if pre is not None:
            group = pre
        label_x = (cols[0][0] - 3 * 16.0) if cols else 1e9
        label = " ".join(w["text"] for w in line if w["cx"] < label_x)
        tag = _label(label)
        if not tag:
            h = _half_of(joined)
            if h and not cols:
                caption_half = h
            elif h and len(half_spans) == 1:
                caption_half = h
                half_spans = [(half_spans[0][0], h)]
            continue
        if not cols:
            continue
        if not any(w["cx"] >= label_x and re.fullmatch(r"-{2,}|[-+]?\d{1,3}(?:\.\d+)?",
                                                       _norm(w["text"])) for w in line):
            # "Employment" is both a group heading in the modern table and a row label in
            # the retired one. A heading carries no cells, so it is not a row -- counting
            # it as a rejected row hid the fact that `rejected` is otherwise always 0,
            # which is the signal that a real parse failure needs.
            continue
        got = _assign_words(line, cols, avg_x=avgx)
        if got is None:
            rejected += 1
            continue
        for idxs, half in half_spans:
            vals = [got.get(i) for i in idxs]
            if any(v is not None for v in vals):
                key = (group + tag, _label_half(label, half))
                if key not in cells:
                    cells[key] = vals
                    avg[key] = got.get(AVG_KEY)
    curmonths = [cols[i][1] for i in half_spans[0][0]] if half_spans else months[:3]
    return Table(curmonths, cells, avg, "pdf", rejected)


# --- format 2: the release's own CSV --------------------------------------


def parse_csv(raw: bytes, release: Optional[str]) -> Table:
    """`section_type,item_title,current_month,month_1,month_2,average`.

    Carries no month labels, so the caller supplies them -- and because 99 releases have
    both a CSV and a PDF, the month convention is *measured* against the PDF's own
    headings rather than assumed (see `build --audit-convention`).
    """
    text = raw.decode("utf8", "ignore")
    rows = list(csv.reader(text.splitlines()))
    hdr = [_squash(c) for c in (rows[0] if rows else [])][:2]
    if hdr[:2] != ["sectiontype", "itemtitle"]:
        # The other export layout: a spreadsheet dump of the same table, with its own month
        # names in the header and the label in whichever column the sheet used. 22 of the 198
        # cached CSVs are this shape, and the `section_type` reader returns nothing for them --
        # which is why some releases appeared to have no table at all.
        cleaned = [[c for c in r if _norm(c)] for r in rows]
        return parse_cellrows([r for r in cleaned if r], release, "csv_grid", label_cols=3)
    cells: Dict[Tuple[str, str], List[Optional[float]]] = {}
    avg: Dict[Tuple[str, str], Optional[float]] = {}
    rejected = 0
    group = ""
    group_is_section = False
    for row in rows:
        if len(row) < 5:
            continue
        sect, item = _norm(row[0]).strip('"'), _norm(row[1]).strip('"')
        sect = re.sub(r"<[^>]+>", " ", sect)
        pre = _group_prefix(sect)
        if pre is not None:
            group = pre
            group_is_section = True
        tag = _label(item.strip('"'))
        if not tag:
            if item and not sect.lower().startswith("section"):
                rejected += 1
            continue
        half = _half_of(sect)
        if half is None and group_is_section:
            # The two surveys key `section_type` differently: the manufacturing CSVs use a
            # horizon caption ("Compared to the previous month" / "Six months from now"),
            # the service-sector CSVs use the indicator *group* ("Service-Sector
            # Indicators" / "Retail Indicators"). Requiring a horizon caption rejected
            # every row of all 99 service-sector CSVs -- which is why that survey had no
            # table at all for 2008-10..2009-06, 2011-09..2012-08 and 2014-01. Where the
            # section names a group, the horizon is current unless the row's own label says
            # otherwise ("Product demand during next six months"), per `_label_half`.
            half = "cur"
        if half is None:
            rejected += 1
            continue
        vals = [_num(row[2]), _num(row[3]), _num(row[4])]
        if all(v is None for v in vals):
            continue
        h = _label_half(item, half)
        cells[(group + tag, h)] = vals
        avg[(group + tag, h)] = _num(row[5]) if len(row) > 5 else None
    return Table([], cells, avg, "csv", rejected)


# --- format 3: the 1997-2004 archive table page ---------------------------


def _html_rows(s: str) -> List[List[str]]:
    out = []
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", s):
        cells = [_norm(re.sub(r"(?s)<[^>]+>", " ", c))
                 for c in re.findall(r"(?is)<t[hd][^>]*>(.*?)</t[hd]>", tr)]
        if any(cells):
            out.append(cells)
    return out


_MONTH_ANY = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November"
    r"|December)\b", re.I)


def _is_state_header(cells: List[str]) -> bool:
    """The 1997 layout heads its District block with the response shares `<  =  >`."""
    got = {_norm(c) for c in cells}
    return {"<", "=", ">"} <= got


def _ingest_state(rows: List[List[str]], release: Optional[str], cells, avg, out_months
                  ) -> int:
    """The 1997 per-state layout: `< = > | {M} Index | {M-1} Index` for the Fifth District,
    then two Index columns for each of MD / NC / SC / VA / WV.

    Kept as its own reader because the generic three-month reader silently misread it: it
    took the first three numbers of each row -- the decrease / no-change / increase
    *percentages* -- as three months of index values, and nothing failed, because this
    layout prints no 3-month average to checksum against. What it does print is the
    response shares, so the **diffusion identity** arbitrates instead, exactly as in
    `47_philadelphia_mbos`: index == %increase - %decrease, on every accepted row.

    Only the Fifth District columns are read; the per-state indexes have no counterpart in
    the Bank's own district-level workbook and no narrative of their own.
    """
    rejected = 0
    half = "cur"
    months: List[str] = []
    ry = int(release[:4]) if release else None
    rm = int(release[5:7]) if release else None
    pending_label = ""
    recent: List[List[str]] = []
    for cellrow in rows:
        joined = " ".join(cellrow)
        recent.append(cellrow)
        recent = recent[-4:]
        h = _half_of(joined)
        if h and not any(_label(c) for c in cellrow):
            half = h
        if _is_state_header(cellrow):
            names: List[str] = []
            for cand in recent[::-1]:
                names = [m.group(1) for c in cand for m in [_MONTH_ANY.search(c)] if m]
                if len(names) >= 2:
                    break
            seq: List[str] = []
            if names and ry:
                y, m = ry, rm
                for nm in names[:2]:
                    mo = _MON3[nm[:3].lower()]
                    yy = y if mo <= m else y - 1
                    seq.append(f"{yy:04d}-{mo:02d}")
                    y, m = _shift_year(yy, mo, 1)
            if len(seq) == 2:
                months = seq
                if not out_months:
                    out_months.extend(seq)
            continue
        if not months:
            continue
        lead = _norm(cellrow[0]) if cellrow else ""
        tag = _label(lead) or _label(pending_label + " " + lead)
        # Numbers are read from the row as a whole, not cell by cell: the `<pre>` variant
        # packs the three response shares into one space-separated field ("27 48 25")
        # while the HTML variant gives each its own cell.
        after = joined[len(cellrow[0]):] if cellrow else joined
        nums = [float(x) for x in re.findall(r"(?<![\w.])-?\d{1,3}(?:\.\d+)?(?![\w.])",
                                            _norm(after))]
        if not tag:
            # a label wrapped onto its own line ("Finished goods" / " inventories 28 52 …")
            if lead and not nums:
                pending_label = lead
            continue
        pending_label = ""
        if len(nums) < 5:
            rejected += 1
            continue
        dec, _noc, inc, idx_m, idx_prev = nums[0], nums[1], nums[2], nums[3], nums[4]
        if abs((inc - dec) - idx_m) > 1.01:
            rejected += 1
            continue
        cells.setdefault((tag, half), [idx_m, idx_prev])
        avg.setdefault((tag, half), None)
    return rejected


#: Strip only real HTML tags. A blanket `<[^>]+>` cannot be used on these pages: the 1997
#: press releases print the response-share legend with **unescaped** angle brackets
#: ("The symbols <, =, and > indicate…", and a header row of `<  =  >`), so the blanket
#: pattern deletes the very columns that identify the layout -- which is how the per-state
#: table came to be read as three months of index values.
_TAG = re.compile(r"(?s)</?[a-zA-Z][^>]*>")


def _pre_rows(s: str) -> List[List[str]]:
    """Cell rows from a fixed-width `<pre>` table (the 1997-01..05 press-release layout)."""
    out: List[List[str]] = []
    for m in re.finditer(r"(?is)<pre[^>]*>(.*?)</pre>", s):
        body = _html.unescape(_TAG.sub("", m.group(1)))
        for line in body.split("\n"):
            if not line.strip():
                continue
            out.append([_norm(c) for c in re.split(r"\s{2,}", line.strip()) if c.strip()])
    return out


def parse_cellrows(rows: List[List[str]], release: Optional[str], fmt: str,
                   label_cols: int = 2) -> Table:
    """Generic reader for a table already reduced to cell rows: a month-name header, group
    headings, one label plus three months (and often a 3-month average) per row.

    Shared by the 1997-2004 archive table pages and by the *other* release-CSV layout -- some
    releases ship the results table as a spreadsheet dump (a dateline row, a `September /
    August / July / 3-Month Average` header, group rows, and the label in whichever column
    the sheet happened to use) rather than as the `section_type,item_title,...` export. Both
    are the same table, so they get the same reader; `label_cols` is how many leading cells to
    search for the row label, since the sheet layout is ragged.
    """
    cells: Dict[Tuple[str, str], List[Optional[float]]] = {}
    avg: Dict[Tuple[str, str], Optional[float]] = {}
    months: List[str] = []
    half = "cur"
    group = ""
    in_prices = False
    rejected = 0
    ry, rm = (int(release[:4]), int(release[5:7])) if release else (None, None)
    for cellrow in rows:
        joined = " ".join(cellrow)
        sq = _squash(joined)
        if any(k in sq for k in _PRICE_SECTION):
            in_prices = True
        elif _group_prefix(joined) is not None:
            in_prices = False
        if not any(_label(c) for c in cellrow):
            h = _half_of(joined)
            if h == "fut":
                half = "fut"
                continue
            if h == "cur":
                half = "cur"
        pre = _group_prefix(joined)
        if pre is not None:
            group = pre
        # header: three month names (+ '3-Month Average')
        names = [(_MON3.get(_norm(c)[:3].lower()), c) for c in cellrow]
        mons = [n for n, _ in names if n]
        if len(mons) >= 3 and ry:
            seq = []
            y, m = ry, rm
            for k, mo in enumerate(mons[:3]):
                # walk back from the release month to the nearest earlier occurrence
                yy = y if mo <= m else y - 1
                seq.append(f"{yy:04d}-{mo:02d}")
                y, m = _shift_year(yy, mo, 1)
            months = months or seq
            continue
        # data row: the label may be in cell 0 or cell 1 (the archive pages are ragged)
        tag = None
        for c in cellrow[:label_cols]:
            tag = tag or _label(c)
        if not tag and in_prices:
            for c in cellrow[:label_cols]:
                tag = tag or _price_row_label(c)
            if tag:
                group = ""            # the sector is already in the price channel name
        if not tag:
            continue
        nums = [_num(c) for c in cellrow if _num(c) is not None or _norm(c) in {"-", "--"}]
        nums = [_num(c) for c in cellrow]
        nums = [v for v in nums if v is not None]
        if len(nums) < 3:
            rejected += 1
            continue
        h = _label_half(" ".join(cellrow[:label_cols]), half)
        if tag in _ALWAYS_CURRENT and (tag, "cur") not in cells:
            h = "cur"
        cells[(group + tag, h)] = nums[:3]
        avg[(group + tag, h)] = nums[3] if len(nums) > 3 else None
    return Table(months, cells, avg, fmt, rejected)


def parse_tbl_html(raw: bytes, release: Optional[str]) -> Table:
    s = raw.decode("utf8", "ignore")
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    rows = _html_rows(s)
    if not rows:
        rows = _pre_rows(s)
    if any(_is_state_header(r) for r in rows):
        cells: Dict[Tuple[str, str], List[Optional[float]]] = {}
        avg: Dict[Tuple[str, str], Optional[float]] = {}
        months: List[str] = []
        rej = _ingest_state(rows, release, cells, avg, months)
        return Table(months, cells, avg, "tbl_html_state", rej)
    return parse_cellrows(rows, release, "tbl_html")


# --- dispatch + checks ----------------------------------------------------


def parse(raw: bytes, kind: str, release: Optional[str] = None) -> Table:
    if kind == "csv":
        return parse_csv(raw, release)
    if kind == "tbl_html":
        return parse_tbl_html(raw, release)
    if raw[:4] == b"%PDF":
        return parse_pdf(raw, release)
    if kind in ("page", "narr_html"):
        # release page: the table is real markup on some captures, running text on others
        t = parse_tbl_html(raw, release)
        if t.cells:
            return t
        txt = re.sub(r"(?s)<[^>]+>", " ", raw.decode("utf8", "ignore"))
        return parse_layout(_html.unescape(txt), release)
    return parse_pdf(raw, release)


def check_average(t: Table, tol: float = 0.7) -> Tuple[int, int]:
    """Rows where the printed 3-month average equals the mean of the three months.

    A free within-row checksum on the two formats that print it (CSV, 1997-2004 HTML):
    it fails immediately if a row was shifted or a column mis-assigned. Returns
    (n_ok, n_checked).
    """
    ok = n = 0
    for key, a in t.avg.items():
        vals = t.cells.get(key)
        if a is None or not vals or any(v is None for v in vals):
            continue
        n += 1
        if abs(sum(vals) / len(vals) - a) <= tol:
            ok += 1
    return ok, n


def overlap_agreement(a: Table, b: Table, tol: float = 0.0
                      ) -> Tuple[int, int]:
    """Agreement on months two consecutive releases both print.

    The Richmond analogue of the diffusion identity `47`/`48` used: each release prints
    three months, so release M and release M-1 share two. Within one seasonal-adjustment
    vintage they must agree exactly; they legitimately differ across the July
    re-benchmark, which is itself the vintage finding. Returns (n_equal, n_compared).
    """
    if not a.months or not b.months:
        return 0, 0
    eq = n = 0
    for key, va in a.cells.items():
        vb = b.cells.get(key)
        if not vb:
            continue
        for ym, x in zip(a.months, va):
            if ym in b.months and x is not None:
                y = vb[b.months.index(ym)]
                if y is None:
                    continue
                n += 1
                if abs(x - y) <= tol:
                    eq += 1
    return eq, n
