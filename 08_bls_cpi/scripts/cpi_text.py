#!/usr/bin/env python3
"""Turn one BLS CPI news release into its own narrative sections.

A CPI release is natively sectioned, and the sectioning changed once, in 2010:

* **Group era (data months 1994-01 .. 2009-11)** — a two-paragraph lede reciting the
  CPI-U and CPI-W index *levels*, a `CPI for All Urban Consumers (CPI-U)` section
  whose paragraphs walk the major expenditure groups one at a time (food and
  beverages, housing, transportation, apparel, medical care, recreation, education
  and communication, other goods and services), and a closing `CPI for Urban Wage
  Earners and Clerical Workers (CPI-W)` section.
* **Heading era (data month 2009-12 onward)** — a summary, then the explicit headings
  `Food` / `Energy` / `All items less food and energy` /
  `Not seasonally adjusted CPI measures`.

Either way the split is the source's own: each section is a self-contained block of
prose about one group of CPI indexes. Nothing here writes prose — every section is a
verbatim slice of the release.

Two traps this module exists to avoid:
  * The releases are hard-wrapped at ~70 columns, so any anchor phrase can straddle a
    newline ("the Consumer Price Index for All\\nUrban Consumers"). Every phrase here
    is compiled through `ph()`, which makes each space `\\s+`.
  * The narrative is not all in `<pre>[0]`. 2010-2018 pages keep the whole release in
    one block, 2019+ pages split header/summary from the sections, and 2008-2010 pages
    append six table-only blocks. Reading only the first block (the previous builder's
    behaviour) silently dropped every section paragraph on the modern pages.
"""
from __future__ import annotations

import html as htmllib
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS)}
MONTH_ALT = "|".join(MONTHS)


def ph(phrase: str, anchor: bool = True) -> re.Pattern:
    """Compile a phrase so it survives the release's ~70-column hard wrapping."""
    body = r"\s+".join(re.escape(w) for w in phrase.split())
    return re.compile((r"^\s*" if anchor else "") + body, re.IGNORECASE)


WAYBACK_TOOLBAR = re.compile(
    r"<!--\s*BEGIN WAYBACK TOOLBAR INSERT\s*-->.*?<!--\s*END WAYBACK TOOLBAR INSERT\s*-->",
    re.DOTALL | re.IGNORECASE)
PRE_RE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
RULE_LINE = re.compile(r"^[\s_\-=~*]{4,}$")

# "CONSUMER PRICE INDEX - MAY 2026" / "Consumer Price Index -April 2010" /
# "CONSUMER PRICE INDEX--JANUARY 1994" / "Consumer Price Index: May 2009"
TITLE_RE = re.compile(
    r"Consumer\s+Price\s+Index\s*[-:–—−\s]{0,6}(" + MONTH_ALT + r")\s*,?\s+(\d{4})",
    re.IGNORECASE)
# fallback: the release always names the month it reports in "Data for April 2010"
TITLE_FALLBACK_RE = re.compile(
    r"Consumer\s+Price\s+Index\s+Data\s+for\s+(" + MONTH_ALT + r")\s+(\d{4})", re.IGNORECASE)

LEDE_RE = ph("The Consumer Price Index for All Urban Consumers (CPI-U)", anchor=False)

# The narrative always ends at one of these. The upcoming-release note is the tightest
# bound and appears in both eras; the rest are era- or year-specific.
HARD_END = [
    # the optional leading "The" matters: without it the cut leaves a dangling "The"
    # welded onto the end of the last real paragraph
    re.compile(r"(?:The\s+)?Consumer\s+Price\s+Index\s+(?:data|news\s+release)\s+for\s+[A-Z][a-z]+"
               r"(?:\s+\d{4})?\s+(?:will\s+be|is\s+scheduled|are\s+scheduled)", re.IGNORECASE),
    re.compile(r"(?:The\s+)?Consumer\s+Price\s+Index\s+for\s+[A-Z][a-z]+\s+\d{4}\s+is\s+scheduled",
               re.IGNORECASE),
    # standalone heading only: February releases mention "see the Technical Note"
    # inside the seasonal-factor note, mid-narrative
    re.compile(r"^[ \t]*Technical\s+Notes?[ \t]*$", re.MULTILINE),
    ph("Facilities for Sensory Impaired", anchor=False),
    ph("Brief Explanation of the CPI", anchor=False),
    re.compile(r"Table\s+1\.\s+Consumer\s+Price\s+Index", re.IGNORECASE),
    re.compile(r"\bCPI\s+\(Old\s+(?:Series|Weights)\)", re.IGNORECASE),
    ph("Last Modified Date:", anchor=False),
]

HEADING_ERA_SECTIONS = {
    "food": "Food",
    "energy": "Energy",
    "core": "All items less food and energy",
    "nsa": "Not seasonally adjusted CPI measures",
    "annual_review": "Year in Review",
}
HEADING_ERA_MATCH = {k: ph(v) for k, v in HEADING_ERA_SECTIONS.items()}
# Some captures run a heading straight into its first sentence with no blank line —
# "Food The food index increased 0.1 percent in August, ..." in the 2013 HTML, and every
# group-era heading in the PDF rendering. Left glued, the heading is invisible, so the
# section (or, for `CPI for All Urban Consumers (CPI-U)`, the era detection) is lost.
GLUED_HEADING = [
    (text, re.compile(r"^\s*" + r"\s+".join(re.escape(w) for w in text.split())
                      + r"\s+(?=[A-Z])"))
    for text in list(HEADING_ERA_SECTIONS.values()) + [
        "CPI for All Urban Consumers (CPI-U)",
        "CPI for Urban Wage Earners and Clerical Workers (CPI-W)",
        "Chained Consumer Price Index for All Urban Consumers (C-CPI-U)",
    ]
]

HEAD_CPIU = ph("CPI for All Urban Consumers (CPI-U)")
HEAD_CPIW = ph("CPI for Urban Wage Earners and Clerical Workers (CPI-W)")
HEAD_CCPIU = ph("Chained Consumer Price Index for All Urban Consumers (C-CPI-U)")
CCPIU_OPENER = re.compile(r"^\s*The\s+C(?:hained\s+Consumer\s+Price\s+Index|-CPI-U)\b",
                          re.IGNORECASE)

# Openers for the group era's expenditure-group paragraphs.
GROUP_OPENERS: List[Tuple[str, re.Pattern]] = [
    ("food", re.compile(
        r"^\s*(?:The\s+(?:index\s+for\s+)?food(?:\s+and\s+beverages?)?(?:\s+index)?\b"
        r"|The\s+food\s+and\s+beverages?\s+index\b|Food\s+(?:prices|costs)\b"
        r"|The\s+grocery\s+store\s+food\s+index\b)", re.IGNORECASE)),
    ("housing", re.compile(
        r"^\s*(?:The\s+(?:index\s+for\s+)?housing(?:\s+component|\s+index)?\b"
        r"|Housing\s+(?:costs|prices)\b)", re.IGNORECASE)),
    ("transportation", re.compile(
        r"^\s*(?:The\s+(?:index\s+for\s+)?transportation(?:\s+component|\s+index)?\b"
        r"|Transportation\s+(?:costs|prices)\b)", re.IGNORECASE)),
    ("apparel", re.compile(
        r"^\s*(?:The\s+(?:index\s+for\s+)?apparel(?:\s+and\s+upkeep)?(?:\s+index)?\b"
        r"|Apparel\s+(?:costs|prices)\b)", re.IGNORECASE)),
    ("medical", re.compile(
        r"^\s*(?:The\s+(?:index\s+for\s+)?medical\s+care(?:\s+index)?\b"
        r"|Medical\s+care\s+(?:costs|prices)\b)", re.IGNORECASE)),
    ("recreation", re.compile(
        r"^\s*(?:The\s+(?:index\s+for\s+)?(?:recreation|entertainment)(?:\s+index)?\b"
        r"|(?:Recreation|Entertainment)\s+(?:costs|prices)\b)", re.IGNORECASE)),
    ("educ_comm", re.compile(
        r"^\s*(?:The\s+(?:index\s+for\s+)?education(?:\s+and\s+communication)?(?:\s+index)?\b"
        r"|Education(?:\s+and\s+communication)?\s+(?:costs|prices)\b)", re.IGNORECASE)),
    ("other", re.compile(
        r"^\s*(?:The\s+(?:index\s+for\s+)?other\s+goods\s+and\s+services(?:\s+index)?\b"
        r"|Other\s+goods\s+and\s+services\s+(?:costs|prices|rose|fell|increased|declined)\b)",
        re.IGNORECASE)),
]

# December releases append an unheaded calendar-year review after the NSA section
# ("The all items CPI-U rose 1.4 percent in 2020."). It is real release prose but a
# different subject from the reported month, so it becomes its own section instead of
# quietly tripling the `nsa` block.
ANNUAL_REVIEW_RE = re.compile(
    r"^\s*The\s+(?:all\s+items\s+)?(?:CPI-U|CPI|index)\b.{0,90}?\b(?:rose|increased|fell|"
    r"declined|advanced|was\s+unchanged)\b.{0,70}?\bin\s+((?:19|20)\d\d)\b", re.IGNORECASE)

SA_CPIW_RE = re.compile(
    r"^\s*(?:On\s+a\s+seasonally\s+adjusted\s+basis,?\s+)?(?:The\s+)?"
    r"(?:CPI\s+for\s+Urban\s+Wage\s+Earners|Consumer\s+Price\s+Index\s+for\s+Urban\s+Wage"
    r"|CPI-W\b)", re.IGNORECASE)
SA_SUMMARY_RE = ph("On a seasonally adjusted basis")

# Prose blocks that repeat verbatim every month — dropped so a section stays
# release-specific rather than part template.
BOILERPLATE = [
    ph("Note: Seasonal factors have been recalculated"),
    ph("NOTE: Index applies to a month as a whole"),
    ph("As previously announced"),
    ph("Please note that the indexes for the"),
    ph("See table"),
    ph("(See table"),
    ph("Information from this release will be made available"),
    ph("Beginning with January"),
    ph("Like other seasonally adjusted CPI data"),
    re.compile(r"^\s*\(?\d\)?\s+Not\s+seasonally\s+adjusted", re.IGNORECASE),
]

# A group paragraph may open with an adverbial clause before naming its group
# ("In June, for the second consecutive month, the index for other goods and
# services was unchanged."), so openers get a second try past a leading clause.
LEAD_CLAUSE = re.compile(r"^\s*[^,.]{0,60},\s+")
MIN_PROSE_CHARS = 60

# The 2009 releases collapse the small groups into one paragraph ("Among other CPI
# groups, the index for apparel turned down in March ... The medical care index rose
# 0.2 percent ... The index for recreation ... education and communication ... other
# goods and services ..."). Its first clause names apparel, so an opener will claim it
# and pair five groups' worth of prose with the apparel series alone. A paragraph that
# names three or more distinct expenditure groups belongs to no single one.
GROUP_SUBJECTS = {
    "food": ph(r"food", anchor=False),
    "housing": ph("housing", anchor=False),
    "transportation": ph("transportation", anchor=False),
    "apparel": ph("apparel", anchor=False),
    "medical": ph("medical care", anchor=False),
    "recreation": re.compile(r"\b(?:recreation|entertainment)\b", re.IGNORECASE),
    "educ_comm": ph("education and communication", anchor=False),
    "other": ph("other goods and services", anchor=False),
}
MAX_GROUPS_PER_PARAGRAPH = 2


def names_multiple_groups(block: str) -> bool:
    return sum(1 for pat in GROUP_SUBJECTS.values()
               if pat.search(block)) > MAX_GROUPS_PER_PARAGRAPH

# the PDF rendering prefixes a page marker to the running head: "-2- Table A. ..."
TABLE_LEAD = re.compile(r"^\s*(?:-\s*\d+\s*-\s*)?Table\s+[A-Z0-9]{1,3}\s*[\.\:]",
                        re.IGNORECASE)
DOTS_RE = re.compile(r"\.{3,}")
NUMCOL_RE = re.compile(r"-?\.?\d+\.?\d*\s{2,}-?\.?\d+\.?\d*\s{2,}-?\.?\d+")


# ---------------------------------------------------------------------------
# document -> plain text
# ---------------------------------------------------------------------------


def decode(raw: bytes) -> str:
    """BLS pages are a mix of UTF-8 and windows-1252; the en dash in the 2018-era
    title line is 0x96, which utf-8 decoding turns into U+FFFD and no month regex
    can then cross."""
    for enc in ("utf-8", "windows-1252"):
        try:
            return normalize_newlines(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return normalize_newlines(raw.decode("utf-8", errors="replace"))


def normalize_newlines(text: str) -> str:
    r"""101 of the 389 documents are line-terminated `\r\r\n` (a double-CR artifact of
    the archive capture). Naive CRLF folding turns each of those into a blank line, so
    every wrapped line becomes its own paragraph and the section split collapses."""
    return text.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")


def html_to_text(html: str) -> str:
    html = WAYBACK_TOOLBAR.sub("", html)
    html = re.sub(r"https?://web\.archive\.org/web/\d+[a-z_]*/", "", html)
    pres = PRE_RE.findall(html)
    if pres:
        blocks, seen = [], set()
        for p in pres:
            t = htmllib.unescape(TAG_RE.sub("", p))
            key = re.sub(r"\s+", " ", t[:400])
            if key in seen:
                continue
            seen.add(key)
            blocks.append(t)
        return "\n\n".join(blocks)
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?i)</(p|div|h[1-6]|tr|li)>", "\n\n", html)
    return htmllib.unescape(TAG_RE.sub("", html))


def pdf_to_text(path: Path) -> str:
    try:
        out = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                             capture_output=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return ""
    return decode(out.stdout)


def load_text(path: Path) -> str:
    if path.suffix == ".pdf":
        return pdf_to_text(path)
    text = decode(path.read_bytes())
    if path.suffix in (".htm", ".html") or "<html" in text[:3000].lower():
        return html_to_text(text)
    return text


# ---------------------------------------------------------------------------
# data month
# ---------------------------------------------------------------------------


def parse_data_month(text: str) -> Optional[str]:
    """The month the release is *about*, read off its own title line.

    Not derivable from the release date: the December 1995 CPI slipped to 1996-02-01
    when the shutdown pushed collection back, so `release month - 1` labels that
    release 1996-01 and silently collides with the real 1996-01 release.
    """
    head = text[:20000]
    for pat in (TITLE_RE, TITLE_FALLBACK_RE):
        m = pat.search(head)
        if m:
            return f"{int(m.group(2)):04d}-{MONTH_NUM[m.group(1).lower()]:02d}"
    return None


# ---------------------------------------------------------------------------
# narrative region + blocks
# ---------------------------------------------------------------------------


def narrative_region(text: str) -> str:
    """Everything from the release title line to the first end marker."""
    start = None
    for pat in (TITLE_RE, TITLE_FALLBACK_RE):
        m = pat.search(text[:20000])
        if m:
            start = m.end()
            break
    if start is None:
        m = LEDE_RE.search(text)
        if not m:
            return ""
        brk = text.rfind("\n\n", 0, m.start())
        start = brk + 2 if brk >= 0 else m.start()
    tail = text[start:]
    end = len(tail)
    for pat in HARD_END:
        m = pat.search(tail, 120)
        if m:
            end = min(end, m.start())
    return tail[:end]


def _strip_rules(block: str) -> str:
    return "\n".join(ln for ln in block.split("\n") if not RULE_LINE.match(ln))


def is_table_block(block: str) -> bool:
    if TABLE_LEAD.match(block):
        return True
    if block.count("|") >= 3:
        return True
    if len(DOTS_RE.findall(block)) >= 2:
        return True
    if NUMCOL_RE.search(block):
        return True
    lines = [ln for ln in block.split("\n") if ln.strip()]
    if not lines:
        return True
    if sum(c.isdigit() for c in block) / max(1, len(block)) > 0.14:
        return True
    if sum(1 for ln in lines if re.search(r"\S\s{4,}\S", ln)) / len(lines) > 0.55:
        return True
    return False


KNOWN_HEADINGS = [h for h, _ in GLUED_HEADING]
HEADING_LINE = re.compile(
    r"^[ \t]*(" + "|".join(re.escape(h) for h in KNOWN_HEADINGS) + r")[ \t]*$",
    re.IGNORECASE | re.MULTILINE)


def isolate_headings(region: str) -> str:
    """Force a blank line around any line that is exactly a known heading.

    Paragraph splitting is blank-line based, and some renderings (every PDF, some
    HTML) put a heading on its own line but with no blank line before it, so the
    heading ends up buried mid-block and the section is never opened.
    """
    return HEADING_LINE.sub(lambda m: "\n\n" + m.group(1) + "\n\n", region)


def blocks_of(region: str) -> List[Tuple[str, str]]:
    """[(kind, text)] with kind in {'prose','table','heading','boiler'}.

    Headings are recognised on the whitespace-collapsed block because the group era
    wraps `CPI for Urban Wage Earners and Clerical Workers (CPI-W)` over two lines
    and underlines it with a rule.
    """
    region = normalize_newlines(region)
    region = isolate_headings(region)
    region = re.sub(r"\n[ \t]*\n+", "\n\n", region)
    out: List[Tuple[str, str]] = []
    for raw in region.split("\n\n"):
        block = _strip_rules(raw)
        block = "\n".join(ln.strip() for ln in block.split("\n")).strip()
        if not block:
            continue
        flat = re.sub(r"\s*\n\s*", " ", block)
        if len(flat) < 70 and not flat.endswith((".", ":", ",", ";")) \
                and not DOTS_RE.search(flat):
            out.append(("heading", flat))
            continue
        if is_table_block(block):
            out.append(("table", block))
            continue
        if any(p.match(flat) for p in BOILERPLATE):
            out.append(("boiler", flat))
            continue
        if len(flat) < MIN_PROSE_CHARS:
            out.append(("boiler", flat))
            continue
        for text, pat in GLUED_HEADING:
            m = pat.match(flat)
            if m:
                out.append(("heading", text))
                flat = flat[m.end():]
                break
        out.append(("prose", flat))
    return out


# ---------------------------------------------------------------------------
# sectioning
# ---------------------------------------------------------------------------


def detect_era(blocks: List[Tuple[str, str]]) -> str:
    heads = [b for k, b in blocks if k == "heading"]
    n_named = sum(1 for h in heads for pat in HEADING_ERA_MATCH.values() if pat.match(h))
    if n_named >= 3:
        return "heading"
    if any(HEAD_CPIU.match(h) for h in heads):
        return "group"
    return "heading" if n_named >= 2 else "unknown"


def split_heading_era(blocks: List[Tuple[str, str]]) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {}
    current = "all_items"
    for kind, block in blocks:
        if kind == "heading":
            for key, pat in HEADING_ERA_MATCH.items():
                if pat.match(block):
                    current = key
                    break
            continue
        if kind != "prose":
            continue
        if ANNUAL_REVIEW_RE.match(block):
            current = "annual_review"
        sections.setdefault(current, []).append(block)
    return sections


def split_group_era(blocks: List[Tuple[str, str]]) -> Dict[str, List[str]]:
    """Strict: a group paragraph is claimed only by its own opener.

    The 2009 releases condense the small groups into one "Among other CPI groups, the
    index for medical care rose ..." paragraph. Letting an unmatched block extend
    whatever topic came before it silently filed that multi-group paragraph under
    `transportation`, so unmatched blocks in the group run are dropped instead.
    """
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = "all_items"
    tail_section: Optional[str] = None   # set by a trailing CPI-W / C-CPI-U heading
    for kind, block in blocks:
        if kind == "heading":
            if HEAD_CPIW.match(block):
                tail_section = current = "cpi_w"
            elif HEAD_CCPIU.match(block):
                tail_section = current = "c_cpi_u"
            elif HEAD_CPIU.match(block):
                tail_section = None
                current = "all_items"
            elif HEADING_ERA_MATCH["annual_review"].match(block):
                tail_section = None
                current = "annual_review"
            continue
        if kind != "prose":
            continue
        if CCPIU_OPENER.match(block):
            sections.setdefault("c_cpi_u", []).append(block)
            continue
        if tail_section or SA_CPIW_RE.match(block):
            sections.setdefault(tail_section or "cpi_w", []).append(block)
            continue
        if ANNUAL_REVIEW_RE.match(block):
            current = "annual_review"
            sections.setdefault(current, []).append(block)
            continue
        if SA_SUMMARY_RE.match(block) or LEDE_RE.match(block):
            current = "all_items"
            sections.setdefault(current, []).append(block)
            continue
        if names_multiple_groups(block):
            continue                # multi-group summary -> belongs to no single topic
        probe = block
        hit = next((name for name, pat in GROUP_OPENERS if pat.match(probe)), None)
        if hit is None:
            probe = LEAD_CLAUSE.sub("", block, count=1)
            hit = next((name for name, pat in GROUP_OPENERS if pat.match(probe)), None)
        if hit is not None:
            current = hit
        elif current in ("all_items", None):
            current = None          # past the lede, unclaimed -> drop
        else:
            continue                # multi-group / stray paragraph -> drop
        if current:
            sections.setdefault(current, []).append(block)
    return sections


def sections_of(text: str) -> Tuple[str, Dict[str, str], Dict[str, int]]:
    """(era, {topic: verbatim prose}, diagnostics)."""
    region = narrative_region(text)
    if not region:
        return "unknown", {}, {"no_title_no_lede": 1, "region_chars": 0}
    blocks = blocks_of(region)
    era = detect_era(blocks)
    if era == "group":
        sec = split_group_era(blocks)
    elif era == "heading":
        sec = split_heading_era(blocks)
    else:
        sec = {}
    diag = {
        "region_chars": len(region),
        "n_prose": sum(1 for k, _ in blocks if k == "prose"),
        "n_table": sum(1 for k, _ in blocks if k == "table"),
        "n_heading": sum(1 for k, _ in blocks if k == "heading"),
        "n_boiler": sum(1 for k, _ in blocks if k == "boiler"),
    }
    return era, {k: "\n\n".join(v) for k, v in sec.items()}, diag
