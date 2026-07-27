"""Parse a pre-2002 MBOS release from the retired phil.frb.org plain-text layout.

Those months exist as PDFs in the Bank's era archives too, but every pre-2002 PDF is
an image scan with no text layer. The retired site published the same release as
`bos{mmyy}.txt` (and an HTML wrapper around the same text), which the Wayback Machine
captured — clean ASCII, narrative and table both.

    General Business          17.1   45.4   37.5   20.4    15.3   49.3   33.4   18.1
    Conditions
                              \\__ Dec  No ch  Inc  Index _/  \\__ six months from now _/
"""
from __future__ import annotations

import html
import re
from typing import Dict, List, Optional, Tuple

ROW_LABELS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^general business(\s+conditions)?\b", re.I), "general_activity"),
    (re.compile(r"^what is your evaluation of the level", re.I), "general_activity"),
    (re.compile(r"^new orders\b", re.I), "new_orders"),
    (re.compile(r"^shipments\b", re.I), "shipments"),
    (re.compile(r"^unfilled orders\b", re.I), "unfilled_orders"),
    (re.compile(r"^delivery times?\b", re.I), "delivery_times"),
    (re.compile(r"^inventories\b", re.I), "inventories"),
    (re.compile(r"^prices paid\b", re.I), "prices_paid"),
    (re.compile(r"^prices received\b", re.I), "prices_received"),
    (re.compile(r"^number of employees\b", re.I), "employment"),
    (re.compile(r"^avg\.? employee workweek\b", re.I), "average_workweek"),
    (re.compile(r"^average employee workweek\b", re.I), "average_workweek"),
    (re.compile(r"^capital expenditures\b", re.I), "capital_expenditures"),
]

# the eight-column layout this era uses; see tabex.LAYOUTS for the modern one
FIELDS = ["cur_dec", "cur_nochg", "cur_inc", "cur_idx",
          "fut_dec", "fut_nochg", "fut_inc", "fut_idx"]

_CELL = re.compile(r"-?\d{1,3}\.\d|--?|N/?A", re.I)
_TABLE_START = re.compile(r"^\s*(Business Outlook Survey|Summary of Returns)\s*$", re.I | re.M)
_NOTES = re.compile(r"^\s*Notes?:", re.I | re.M)


def to_text(raw: bytes) -> str:
    """Bytes of a .txt or .html capture -> plain text."""
    s = raw.decode("utf-8", "replace")
    if "<" in s[:400].lower() and re.search(r"<html|<pre|<body", s[:4000], re.I):
        pre = re.findall(r"<pre[^>]*>(.*?)</pre>", s, re.S | re.I)
        s = "\n\n".join(pre) if pre else re.sub(r"<br\s*/?>", "\n", s)
        s = re.sub(r"<[^>]+>", "", s)
        s = html.unescape(s)
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _table_region(text: str) -> Tuple[str, int]:
    """(the table block, its start offset). The table always follows the narrative."""
    starts = [m.start() for m in _TABLE_START.finditer(text)]
    lo = starts[-1] if starts else 0
    m = _NOTES.search(text, lo)
    return text[lo: m.start() if m else len(text)], lo


def parse_table(raw: bytes) -> Dict[str, Dict[str, Optional[float]]]:
    text = to_text(raw)
    block, _ = _table_region(text)
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for line in block.split("\n"):
        s = line.strip()
        if not s:
            continue
        for pat, tag in ROW_LABELS:
            if not pat.match(s):
                continue
            cells = _CELL.findall(s[pat.match(s).end():])
            if len(cells) != len(FIELDS):
                break
            out.setdefault(tag, {f: (None if not re.match(r"-?\d", c) or c in ("-", "--")
                                     else float(c))
                                 for f, c in zip(FIELDS, cells)})
            break
    return out if len(out) >= 8 else {}


def paragraphs(raw: bytes) -> List[str]:
    """The narrative prose, i.e. everything before the table block."""
    text = to_text(raw)
    _, lo = _table_region(text)
    head = text[:lo] if lo else text
    out = []
    for para in re.split(r"\n\s*\n", head):
        joined = re.sub(r"\s+", " ", para).strip()
        if not joined:
            continue
        digits = sum(c.isdigit() for c in joined)
        alpha = sum(c.isalpha() for c in joined)
        if alpha < 60 or alpha < 3.5 * digits:
            continue
        out.append(joined)
    return out
