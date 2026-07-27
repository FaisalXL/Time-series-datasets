"""Parse the as-published diffusion-index tables out of a TMOS release.

Why: `index_sa.xls` is the *revised* series. The Dallas Fed re-estimates the seasonal
factors once a year and revises the whole history -- the release itself says so ("Once
per year, the Federal Reserve Bank of Dallas revises the historical data ... after
calculating new seasonal adjustment factors"). Measured over six releases spanning
2008-2024, the value the prose quotes matches the current workbook in 0 of 14 cases
(median drift 1.6 index points). Each release, though, prints the table of what it
published that month, which is exactly the vintage its own prose quotes.

Unlike the Philadelphia MBOS table (one row, ten columns, current and future side by
side), TMOS prints *separate* tables for "Current (versus previous month)" and "Six
Months Ahead", each split across three captions:
    Business Indicators Relating to Facilities and Products in Texas
    General Business Conditions
    (an unlabelled Outlook Uncertainty table, 2018+)

Three column layouts are in use and each announces itself in the header row, so the
header is read rather than guessed:
    2010-2017 : Indicator | M Index | M-1 Index | Change | Indicator Direction | Trend | %Inc | %NoChg | %Dec
    2018-2026 : Indicator | M Index | M-1 Index | Change | Series Average      | Trend | %Inc | %NoChg | %Dec
    (the General Business Conditions tables say Improved/Worsened, not Increase/Decrease)
"""
from __future__ import annotations

import html as _html
import io
import re
import subprocess
from typing import Dict, Optional

# canonical channel name <- the label the release prints
ROW_LABELS = [
    (re.compile(r"^production$", re.I), "production"),
    (re.compile(r"^capacity utili[sz]ation$", re.I), "capacity_utilization"),
    (re.compile(r"^(volume of )?new orders$", re.I), "new_orders"),
    (re.compile(r"^growth rate of orders$", re.I), "growth_rate_of_orders"),
    (re.compile(r"^unfilled orders$", re.I), "unfilled_orders"),
    (re.compile(r"^(volume of )?shipments$", re.I), "shipments"),
    (re.compile(r"^delivery times?$", re.I), "delivery_time"),
    (re.compile(r"^materials inventories$", re.I), "materials_inventories"),
    (re.compile(r"^finished goods inventories$", re.I), "finished_goods_inventories"),
    (re.compile(r"^prices paid for raw materials$", re.I), "prices_raw_materials"),
    (re.compile(r"^prices received for finished goods$", re.I), "prices_finished_goods"),
    (re.compile(r"^wages and benefits$", re.I), "wages_benefits"),
    (re.compile(r"^(number of )?employees$|^employment$", re.I), "employment"),
    (re.compile(r"^(average employee workweek|hours worked)$", re.I), "hours_worked"),
    (re.compile(r"^capital expenditures$", re.I), "capital_expenditures"),
    (re.compile(r"^company outlook$", re.I), "company_outlook"),
    (re.compile(r"^general business activity$", re.I), "general_business_activity"),
    (re.compile(r"^(outlook )?uncertainty$", re.I), "outlook_uncertainty"),
]

_DASHES = "‐‑‒–—−"
_NUM = re.compile(r"^[-+]?\d{1,3}(\.\d+)?$")


def _norm(s: str) -> str:
    s = _html.unescape(s)
    for d in _DASHES:
        s = s.replace(d, "-")
    return re.sub(r"\s+", " ", s).replace(" ", " ").strip()


def _num(s: str) -> Optional[float]:
    s = _norm(s).replace("+", "").replace(",", "")
    if not s or s in {"-", "--", "n/a", "na"}:
        return None
    return float(s) if _NUM.match(s) else None


def _label(cell: str) -> Optional[str]:
    t = _norm(cell).rstrip("*").strip()
    for pat, tag in ROW_LABELS:
        if pat.match(t):
            return tag
    return None


def _map_header(cells) -> Optional[Dict[str, int]]:
    """Map a header row to column indices. Returns None if it is not a results header."""
    col = {}
    for i, c in enumerate(cells):
        t = _norm(c).lower().rstrip("*").strip()
        if re.search(r"reporting (increase|improved)", t):
            col["inc"] = i
        elif re.search(r"reporting no change", t):
            col["nochg"] = i
        elif re.search(r"reporting (decrease|worsened)", t):
            col["dec"] = i
        elif re.fullmatch(r"change", t):
            col["change"] = i
        elif re.search(r"\bindex\b", t):
            # "Jun Index" then "May Index" -- current month first, prior month second
            col.setdefault("idx", i)
            if col["idx"] != i:
                col.setdefault("prev", i)
            elif "idx_seen" in col:
                col.setdefault("prev", i)
            col["idx_seen"] = 1
    if "inc" not in col or "dec" not in col or "idx" not in col:
        return None
    col.pop("idx_seen", None)
    return col


def _header_idx_cols(cells):
    """Positions of the two '<Mon> Index' columns, in order."""
    return [i for i, c in enumerate(cells) if re.search(r"\bindex\b", _norm(c), re.I)]


def _rows_from_html(tab: str):
    trs = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", tab)
    out = []
    for tr in trs:
        cells = re.findall(r"(?is)<t[hd][^>]*>(.*?)</t[hd]>", tr)
        out.append([_norm(re.sub(r"(?s)<[^>]+>", " ", c)) for c in cells])
    return out


def _caption_half(text: str) -> Optional[str]:
    t = _norm(text).lower()
    if len(t) > 90:                       # a caption, not a paragraph mentioning "current"
        return None
    if "six months ahead" in t or "six-months ahead" in t:
        return "fut"
    if "versus previous month" in t or re.fullmatch(r"current", t):
        return "cur"
    return None


def _ingest(rows, half_hint, out):
    """Read a table's rows into out[indicator][half] given a detected header."""
    hdr = None
    idxcols = []
    half = half_hint
    for cells in rows:
        joined = " ".join(cells)
        h = _caption_half(joined)
        if h and not any(_label(c) for c in cells):
            half = h
        m = _map_header(cells)
        if m:
            hdr = m
            idxcols = _header_idx_cols(cells)
            continue
        if not hdr or not cells:
            continue
        tag = _label(cells[0])
        if not tag:
            continue
        idx = _num(cells[idxcols[0]]) if len(idxcols) > 0 and idxcols[0] < len(cells) else None
        prev = _num(cells[idxcols[1]]) if len(idxcols) > 1 and idxcols[1] < len(cells) else None
        inc = _num(cells[hdr["inc"]]) if hdr["inc"] < len(cells) else None
        noc = _num(cells[hdr["nochg"]]) if hdr.get("nochg", 99) < len(cells) else None
        dec = _num(cells[hdr["dec"]]) if hdr["dec"] < len(cells) else None
        if idx is None:
            continue
        rec = {"idx": idx, "prev": prev, "inc": inc, "nochg": noc, "dec": dec}
        out.setdefault(tag, {})[half or "cur"] = rec


_LABEL_ALT = "|".join(
    r"(?:volume of )?new orders|growth rate of orders|unfilled orders|"
    r"(?:volume of )?shipments|capacity utili[sz]ation|delivery times?|"
    r"materials inventories|finished goods inventories|"
    r"prices paid for raw materials|prices received for finished goods|"
    r"wages and benefits|number of employees|average employee workweek|"
    r"hours worked|capital expenditures|company outlook|"
    r"general business activity|outlook uncertainty|production|employment".split("|"))
_FLAT_ROW = re.compile(r"(?i)\b(" + _LABEL_ALT + r")\b((?:\s*[-+]?\d{1,3}(?:\.\d+)?){4,12})")


def _ingest_flat(text: str, out: Dict[str, Dict[str, dict]]) -> None:
    """The retired summary page prints the table as running text, not <table> markup:
    'Production -21.4 16.5 45.6 37.9 0.0 25.5 41.2 43.1 15.7 14.8' -- ten numbers,
    the current half then the six-months-ahead half, each ordered
    index / %increase / no change / %decrease / previous index.
    """
    for m in _FLAT_ROW.finditer(_norm(text)):
        tag = _label(m.group(1))
        if not tag:
            continue
        nums = [float(x) for x in re.findall(r"[-+]?\d{1,3}(?:\.\d+)?", m.group(2))]
        halves = []
        if len(nums) >= 10:
            halves = [("cur", nums[0:5]), ("fut", nums[5:10])]
        elif len(nums) >= 5:
            halves = [("cur", nums[0:5])]
        for half, n in halves:
            rec = {"idx": n[0], "inc": n[1], "nochg": n[2], "dec": n[3], "prev": n[4]}
            if _ok(rec):
                out.setdefault(tag, {}).setdefault(half, rec)


def parse_html(raw: bytes) -> Dict[str, Dict[str, dict]]:
    s = raw.decode("utf8", "ignore")
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    out: Dict[str, Dict[str, dict]] = {}
    # a caption often sits just before its <table>; carry the preceding text as a hint
    for m in re.finditer(r"(?is)<table.*?</table>", s):
        before = _norm(re.sub(r"(?s)<[^>]+>", " ", s[max(0, m.start() - 400):m.start()]))
        _ingest(_rows_from_html(m.group(0)), _caption_half(before), out)
    if sum(1 for v in out.values() if "cur" in v) < 10:
        _ingest_flat(_html.unescape(re.sub(r"(?s)<[^>]+>", " ", s)), out)
    return out


def _rows_from_layout(text: str):
    """Split pdftotext -layout lines into cells on runs of 2+ spaces."""
    for line in text.split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        cells = [c for c in re.split(r"\s{2,}", line.strip()) if c]
        yield [_norm(c) for c in cells]


def _ingest_positional(rows, out):
    """PDF path: the header wraps over three physical lines ('%' / 'Reporting' /
    'Increase'), so it cannot be split into cells that align with the data rows.
    Read the numerics by position instead -- first two are this month and last month,
    last three are the increase / no-change / decrease shares, whatever sits between
    (Indicator Direction, Trend, Series Average) -- and let the diffusion identity
    decide whether the read was right. A mis-parsed or shifted row fails it.
    """
    half = None
    for cells in rows:
        if not cells:
            continue
        h = _caption_half(" ".join(cells))
        if h:
            half = h
            continue
        tag = _label(cells[0])
        if not tag:
            continue
        nums = [v for v in (_num(c) for c in cells[1:]) if v is not None]
        if len(nums) < 5:
            continue
        # Two column orders are in use and neither announces itself in a way that
        # survives the header wrap, so both are tried and the diffusion identity
        # decides. 2010+ release PDF puts the shares last; the retired summary page
        # puts them immediately after the index and the previous month last.
        cands = [
            {"idx": nums[0], "prev": nums[1],
             "inc": nums[-3], "nochg": nums[-2], "dec": nums[-1]},
            {"idx": nums[0], "inc": nums[1], "nochg": nums[2],
             "dec": nums[3], "prev": nums[4]},
        ]
        rec = next((c for c in cands if _ok(c)), None)
        if rec is None:
            continue
        out.setdefault(tag, {}).setdefault(half or "cur", rec)


def parse_pdf(raw: bytes) -> Dict[str, Dict[str, dict]]:
    txt = subprocess.run(["pdftotext", "-layout", "-", "-"], input=raw,
                         capture_output=True).stdout.decode("utf8", "ignore")
    out: Dict[str, Dict[str, dict]] = {}
    _ingest_positional(list(_rows_from_layout(txt)), out)
    return out


def parse(raw: bytes) -> Dict[str, Dict[str, dict]]:
    return parse_pdf(raw) if raw[:4] == b"%PDF" else parse_html(raw)


def _ok(rec: dict, tol: float = 0.35) -> bool:
    """idx == %increase - %decrease, the definition of a diffusion index.

    Holds exactly (to rounding) in the source and fails immediately if a row is
    mis-parsed or shifted by one, which is the failure mode this guards against.
    inc+nochg+dec == 100 is deliberately NOT checked: respondents omit items.
    """
    i, d, x = rec.get("inc"), rec.get("dec"), rec.get("idx")
    if None in (i, d, x):
        return False
    return abs((i - d) - x) <= tol


def parse_table(raw: bytes):
    """47-compatible flat shape: {tag: {cur_idx, cur_inc, ..., fut_idx, ...}}."""
    out = {}
    for tag, halves in parse(raw).items():
        flat = {}
        for half, rec in halves.items():
            for k, v in rec.items():
                flat[f"{half}_{k}"] = v
        out[tag] = flat
    return out


def check(row, half: str, tol: float = 0.35) -> bool:
    """Same signature and meaning as 47's tabex.check."""
    i, n, d, x = (row.get(half + k) for k in ("_inc", "_nochg", "_dec", "_idx"))
    if None in (i, n, d, x):
        return False
    return abs((i - d) - x) <= tol and 0 <= i + n + d <= 101.0
