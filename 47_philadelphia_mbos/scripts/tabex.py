"""Parse the as-published diffusion-index table out of an MBOS release PDF.

Why: `bos_dif.csv` is the *revised* series. The Philadelphia Fed re-estimates the
seasonal factors and revises the whole history, so the 2015-06 release's stated 15.2
now reads 8.2 in the published CSV — only the current calendar year still matches.
Each release, though, prints the table of what it published that month, which is
exactly the vintage its own prose quotes.

Table shape (stable 1997->2026): one row per indicator, ten numeric columns —
    prev_idx  inc  no_chg  dec  idx   |   prev_idx  inc  no_chg  dec  idx
    \\______ this month vs last _______/   \\______ six months from now _____/

Rows are recovered by column geometry rather than by line, because the
general-activity row's label wraps over two lines with its numbers on neither.
"""
from __future__ import annotations

import re
import statistics
import warnings
from typing import Dict, List, Optional, Tuple

import pdfplumber

warnings.filterwarnings("ignore")

ROW_LABELS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"what is your evaluation of the level", re.I), "general_activity"),
    (re.compile(r"^general business activity", re.I), "general_activity"),
    (re.compile(r"^new orders\b", re.I), "new_orders"),
    (re.compile(r"^shipments\b", re.I), "shipments"),
    (re.compile(r"^unfilled orders\b", re.I), "unfilled_orders"),
    (re.compile(r"^delivery times?\b", re.I), "delivery_times"),
    (re.compile(r"^inventories\b", re.I), "inventories"),
    (re.compile(r"^prices paid\b", re.I), "prices_paid"),
    (re.compile(r"^prices received\b", re.I), "prices_received"),
    (re.compile(r"^number of employees\b", re.I), "employment"),
    (re.compile(r"^average employee workweek\b", re.I), "average_workweek"),
    (re.compile(r"^average workweek\b", re.I), "average_workweek"),
    (re.compile(r"^capital expenditures\b", re.I), "capital_expenditures"),
]

# Two column layouts are in use and neither announces itself reliably, so both are
# tried and the one that satisfies the diffusion identity on more rows wins.
#   2004->2026 : Previous Index | Increase | No Change | Decrease | Diffusion Index
#   1997->2003 :                  Decrease | No Change | Increase | Diffusion Index
LAYOUTS: List[List[str]] = [
    ["cur_prev", "cur_inc", "cur_nochg", "cur_dec", "cur_idx",
     "fut_prev", "fut_inc", "fut_nochg", "fut_dec", "fut_idx"],
    ["cur_dec", "cur_nochg", "cur_inc", "cur_idx",
     "fut_dec", "fut_nochg", "fut_inc", "fut_idx"],
    ["cur_inc", "cur_nochg", "cur_dec", "cur_idx",
     "fut_inc", "fut_nochg", "fut_dec", "fut_idx"],
]
FIELDS = LAYOUTS[0]

_NUM = re.compile(r"^-?\d{1,3}(\.\d)?$")
_DASH = re.compile(r"^-{1,2}$|^N/?A$", re.I)


def _norm(t: str) -> str:
    return (t.replace("‐", "-").replace("‑", "-").replace("−", "-")
             .replace("–", "-").replace(",", "").strip())


def _grouped_rows(words, tol: float):
    rows: List[List[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(w["top"] - rows[-1][0]["top"]) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w["x0"]) for r in rows]


def _cluster(xs: List[float], gap: float = 14.0) -> List[Tuple[float, float]]:
    out: List[List[float]] = []
    for x in sorted(xs):
        if out and x - out[-1][-1] <= gap:
            out[-1].append(x)
        else:
            out.append([x])
    return [(min(c), max(c)) for c in out]


def parse_page(page) -> Dict[str, Dict[str, Optional[float]]]:
    words = page.extract_words()
    if not words:
        return {}
    hs = [w["bottom"] - w["top"] for w in words]
    tol = max(2.0, statistics.median(hs) * 0.55)
    rows = _grouped_rows(words, tol)

    # 1. label rows
    labels: List[Tuple[float, float, str]] = []      # (y, label_end_x, tag)
    for r in rows:
        line = " ".join(w["text"] for w in r)
        for pat, tag in ROW_LABELS:
            m = pat.search(line)
            if not m:
                continue
            end = 0
            pos = 0
            for w in r:
                pos = line.find(w["text"], pos)
                if pos >= m.end():
                    break
                end = w["x1"]
                pos += len(w["text"])
            labels.append((r[0]["top"], end, tag))
            break
    # 2. keep only the run of labels that forms the diffusion table: indicator names
    #    also occur in chart captions and special-question rows elsewhere on the page
    labels.sort()
    runs, cur = [], []
    for lab in labels:
        if cur and lab[0] - cur[-1][0] > 60:
            runs.append(cur)
            cur = []
        cur.append(lab)
    if cur:
        runs.append(cur)
    labels = max(runs, key=lambda r: len({t for _, _, t in r})) if runs else []
    if len({t for _, _, t in labels}) < 8:
        return {}

    left = min(e for _, e, _ in labels)
    top, bot = labels[0][0] - 8, labels[-1][0] + 12
    # 3. numeric tokens inside the table band, right of the label column
    nums = [(w["top"], w["x0"], _norm(w["text"])) for w in words
            if w["x0"] > left - 2 and top <= w["top"] <= bot
            and (_NUM.match(_norm(w["text"])) or _DASH.match(_norm(w["text"])))]
    if not nums:
        return {}
    cols = _cluster([x for _, x, _ in nums])
    layouts = [L for L in LAYOUTS if len(L) == len(cols)]
    if not layouts:
        return {}

    def col_of(x):
        for i, (a, b) in enumerate(cols):
            if a - 1 <= x <= b + 1:
                return i
        return None

    # 4. every numeric cell goes to its NEAREST label row. A fixed tolerance would
    #    let one row swallow its neighbour's cells and shift the whole table by a row
    #    (shipments would then carry the new-orders values, and so on down).
    ys = sorted({y for y, _, _ in labels})
    pitch = statistics.median([b - a for a, b in zip(ys, ys[1:])]) if len(ys) > 1 else 15.0
    cells: Dict[float, Dict[int, Optional[float]]] = {y: {} for y in ys}
    for ny, nx, t in nums:
        y = min(ys, key=lambda yy: abs(yy - ny))
        if abs(y - ny) > pitch * 0.75:
            continue
        c = col_of(nx)
        if c is None or c in cells[y]:
            continue
        cells[y][c] = None if _DASH.match(t) else float(t)

    best: Dict[str, Dict[str, Optional[float]]] = {}
    best_score = -1
    for fields in layouts:
        out: Dict[str, Dict[str, Optional[float]]] = {}
        for y, _, tag in labels:
            if tag in out or len(cells[y]) < len(fields) - 2:
                continue
            out[tag] = {f: cells[y].get(i) for i, f in enumerate(fields)}
        score = sum(1 for r in out.values() if check(r, "cur") or check(r, "fut"))
        if score > best_score:
            best, best_score = out, score
    return best


def parse_table(pdf_path_or_bytes) -> Dict[str, Dict[str, Optional[float]]]:
    import io
    src = pdf_path_or_bytes
    if isinstance(src, (bytes, bytearray)):
        src = io.BytesIO(src)
    best: Dict[str, Dict[str, Optional[float]]] = {}
    with pdfplumber.open(src) as pdf:
        for page in pdf.pages:
            got = parse_page(page)
            if len(got) > len(best):
                best = got
    return best


def check(row: Dict[str, Optional[float]], half: str, tol: float = 0.35) -> bool:
    """The identity the table must satisfy, per half: idx == %increase - %decrease.

    That is the definition of a diffusion index, so it holds exactly (to rounding) in
    the source and fails immediately if a row is mis-parsed or shifted by one — which
    is the failure mode this guards against.

    The tempting second identity, inc + no_chg + dec == 100, is NOT usable: the
    release's own footnote says "items may not add up to 100 percent because of
    omission by respondents", and measured over the cached releases the shortfall is a
    median 1.2 points and reaches 21. Only the shape is checked here.
    """
    i, n, d, x = (row.get(half + k) for k in ("_inc", "_nochg", "_dec", "_idx"))
    if None in (i, n, d, x):
        return False
    return abs((i - d) - x) <= tol and 0 <= i + n + d <= 101.0
