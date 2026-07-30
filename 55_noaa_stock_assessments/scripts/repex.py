#!/usr/bin/env python3
"""Narrative extraction for NOAA stock-assessment report PDFs.

Why this exists (and why it is not `pdfplumber.extract_text()` over every page, which is
what the demo build did): an assessment report is 4-2445 pages of which only a small part is
*narrative about the stock*. The rest is numeric tables, figure captions, model diagnostics,
appendices, references and page furniture. Pairing all of it with one stock's catch /
fishing-mortality / recruitment / abundance series buries the alignment; and on the 60% of
Stock SMART rows whose report is a multi-stock omnibus document, most of the prose is about
*other* stocks.

So extraction happens in three stages:

  1. `pages()`          -- pdftotext (~45x faster than pdfplumber, verified equivalent on
                           prose) split on the form-feed page separator.
  2. `clean_prose()`    -- drop page furniture (lines that repeat across pages), numeric
                           table rows, figure/table captions, TOC dot-leader lines, and
                           everything after the references heading.
  3. `narrative()`      -- keep the report's own *status narrative* sections, identified by
                           the four regional house formats' own headings (see SECTION_PATS),
                           and drop the methods/diagnostics sections. Falls back to the
                           document's leading prose (title-page abstract + introduction)
                           when no canonical heading is present.

For omnibus reports, `locate_scope()` narrows to the page range covering one stock's own
chapter before any of the above; it returns None when the stock cannot be isolated, which is
the honest outcome for e.g. the PFMC salmon reviews, where 64 Stock SMART stocks share ~28
sections (§2.6 "Puget Sound Chinook Stocks" alone covers 25 of them).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- page extraction

def pages(pdf_path: Path, timeout: int = 300) -> List[str]:
    """Per-page text via pdftotext. Empty list if the PDF has no text layer / fails."""
    try:
        out = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", str(pdf_path), "-"],
            capture_output=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if out.returncode != 0 and not out.stdout:
        return []
    return out.stdout.decode("utf-8", "replace").split("\x0c")


# ---------------------------------------------------------------- line classification

# A line is a table row when its tokens are mostly numeric. Assessment tables are the main
# source of noise: a caught-in-the-open table row reads as "2010 2011 2012 2013 ..." or
# "16,672 16,452 14,408 ...". Requiring >=3 numeric tokens keeps ordinary prose sentences
# that merely quote two figures.
_NUM = re.compile(r"^[-+(\[]?\$?[\d,]+(?:\.\d+)?[)\]%]?[a-z]?$", re.I)
_YEARISH = re.compile(r"^(19|20)\d{2}[a-z]?$")

def _is_table_row(line: str) -> bool:
    toks = line.split()
    if len(toks) < 3:
        return False
    nums = sum(1 for t in toks if _NUM.match(t))
    if nums >= 3 and nums / len(toks) >= 0.5:
        return True
    # A run of >=3 bare years is a table header even when the row is short.
    if sum(1 for t in toks if _YEARISH.match(t)) >= 3:
        return True
    return False


_CAPTION = re.compile(r"^\s*(figure|fig\.|table|tab\.|appendix table|appendix figure)\s*"
                      r"[A-Z]?[-.]?\d+", re.I)
# Dot leaders appear as ".......  12" (a TOC line) and as spaced ". . . . . ." inside a SAFE
# executive summary's own contents block, which has no trailing page number. NOT anchored to
# end-of-line: without -layout, pdftotext emits a whole contents block as one long line
# ("LIST OF ACRONYMS ..... iv ACKNOWLEDGEMENTS ..... v ..."), so an end-anchored test misses
# it. Prose never contains four consecutive periods -- an ellipsis is three.
_TOC_DOTS = re.compile(r"\.{4,}|(?:\.\s){3,}\.")
# Word's broken cross-references survive into the text layer.
_WORD_ERR = re.compile(r"Error!\s+(Bookmark|Reference source)", re.I)
_PAGENUM = re.compile(r"^\s*(page\s*)?[ivxlcdm\d]{1,6}\s*(of\s*\d+)?\s*$", re.I)
_URLISH = re.compile(r"^(https?://|www\.|doi:)", re.I)
# A lone numeric cell on its own line is a table cell that pdftotext broke out; the >=3-token
# table-row test cannot see it.
_LONE_NUM = re.compile(r"^[-+(\[]?\$?[\d,]+(?:\.\d+)?[)\]%*]?[a-z]?$", re.I)

# Sections whose prose is not about the stock's history -- cut at these.
_STOP_HEAD = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(references?|literature\s+cited|bibliography|works\s+cited|"
    r"appendix(?:es|ices)?\b.*|acknowledg(?:e)?ments?|"
    r"tables?\s*$|figures?\s*$|list\s+of\s+(?:tables|figures)|"
    r"table\s+of\s+contents|contents\s*$)\s*:?\s*$", re.I)


def clean_prose(page_texts: Sequence[str]) -> List[str]:
    """Return real prose lines: furniture, table rows, captions and back matter removed."""
    if not page_texts:
        return []
    # Running headers/footers: normalized lines that recur across many pages. Year/number
    # substitution catches "Page 12 of 90" style variants.
    from collections import Counter
    norm = lambda s: re.sub(r"\d+", "#", re.sub(r"\s+", " ", s.strip().lower()))
    counts: Counter = Counter()
    for pg in page_texts:
        for ln in {norm(l) for l in pg.split("\n") if l.strip()}:
            counts[ln] += 1
    npg = max(1, len(page_texts))
    furniture = {ln for ln, c in counts.items()
                 if c >= 3 and c / npg >= 0.25 and len(ln) < 200}

    out: List[str] = []
    stopped = False
    in_caption = False
    for pg in page_texts:
        if stopped:
            break
        for raw in pg.split("\n"):
            line = raw.rstrip()
            s = line.strip()
            if not s:
                in_caption = False
                out.append("")
                continue
            if norm(s) in furniture:
                continue
            if (_PAGENUM.match(s) or _TOC_DOTS.search(s) or _URLISH.match(s)
                    or _WORD_ERR.search(s)):
                continue
            if _CAPTION.match(s):
                # A caption runs until the next blank line. Dropping only its first line
                # leaves orphaned fragments ("...plotted in pounds whole weight. The line
                # color relates to the source of the data.") stranded in the narrative.
                in_caption = True
                continue
            if in_caption:
                continue
            if _LONE_NUM.match(s) or _is_table_row(s):
                continue
            if _STOP_HEAD.match(s):
                # Back matter starts here. Only honour it past the first fifth of the
                # document -- an early "Table of Contents" must not kill the whole report.
                if len(out) > 40:
                    stopped = True
                    break
                continue
            out.append(s)
    return out


def paragraphs(lines: Sequence[str]) -> List[str]:
    """Join wrapped lines into paragraphs, de-hyphenating across line breaks."""
    paras: List[str] = []
    buf: List[str] = []
    for ln in lines:
        if not ln.strip():
            if buf:
                paras.append(_join(buf)); buf = []
            continue
        buf.append(ln.strip())
    if buf:
        paras.append(_join(buf))
    return [p for p in paras if p]


def _join(buf: Sequence[str]) -> str:
    out = ""
    for part in buf:
        if out.endswith("-") and not out.endswith(("--", " -")):
            out = out[:-1] + part
        elif out:
            out += " " + part
        else:
            out = part
    return re.sub(r"\s+", " ", out).strip()


# ---------------------------------------------------------------- narrative sections

# The four regional house formats name their status narrative differently. Each entry is a
# heading regex; a match starts a narrative section that runs until the next heading-like
# line that is NOT in this list (or a _STOP_HEAD, already removed by clean_prose).
#
#   NEFSC management-track / operational assessments -> "State of Stock:", "Projections:",
#       "Special comments:", "Stock Distribution", "Assessment Summary"
#   SEFSC / SEDAR                                    -> "Executive Summary", "Stock Status",
#       "Management Advice"
#   AFSC SAFE (incl. the numbered crab-SAFE exec summary) -> "Executive Summary",
#       "Summary of Changes", "Catches:", "Stock biomass:", "Recruitment:",
#       "Management performance:", "Conclusions"
#   NWFSC / SWFSC (PFMC groundfish) -> "Executive Summary" with subsections "Stock",
#       "Catches", "Data and assessment", "Stock biomass", "Recruitment",
#       "Exploitation status", "Reference points", "Management performance"
SECTION_PATS: List[Tuple[str, re.Pattern]] = [
    ("state_of_stock",   re.compile(r"^state\s+of\s+(?:the\s+)?stock\b", re.I)),
    ("plain_terms",      re.compile(r"^plain\s+terms\s+summary\b", re.I)),
    ("tor_summary",      re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?terms?\s+of\s+reference.*\bsummary\b", re.I)),
    ("summary_results",  re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?summary\s+of\s+(?:results|major\s+changes|the\s+assessment)\b", re.I)),
    ("summary",          re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?summary\s*:?\s*$", re.I)),
    ("exec_summary",     re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?executive\s+summary\b", re.I)),
    ("assessment_summary", re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?assessment\s+summary\b", re.I)),
    ("stock_status",     re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?(?:stock\s+status|status\s+of\s+(?:the\s+)?stock|status\s+determination)\b", re.I)),
    ("summary_changes",  re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?summary\s+of\s+changes\b", re.I)),
    ("catches",          re.compile(r"^(?:\d+\.\s*)?catch(?:es)?\s*:", re.I)),
    ("stock_biomass",    re.compile(r"^(?:\d+\.\s*)?(?:stock\s+)?biomass\s*:", re.I)),
    ("abundance",        re.compile(r"^(?:\d+\.\s*)?(?:spawning\s+)?(?:stock\s+)?abundance\s*:", re.I)),
    ("recruitment",      re.compile(r"^(?:\d+\.\s*)?recruitment\s*:", re.I)),
    ("fishing_mortality", re.compile(r"^(?:\d+\.\s*)?(?:fishing\s+)?mortality\s*:", re.I)),
    ("exploitation",     re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?exploitation\s+(?:status|history|rates?)\b", re.I)),
    ("trends",           re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?(?:(?:abundance|biomass|catch|recruitment|population|stock)\s+"
                                    r"(?:and\s+\w+\s+)?trends?|trends?\s+in\s+(?:abundance|biomass|catch|recruitment|"
                                    r"fishing\s+mortality|stock\s+\w+))\b", re.I)),
    ("catch_history",    re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?(?:catch|fishery|harvest|landings)\s+history\b", re.I)),
    ("mgmt_performance", re.compile(r"^(?:\d+\.\s*)?management\s+performance\s*:", re.I)),
    ("mgmt_advice",      re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?management\s+advice\b", re.I)),
    ("projections",      re.compile(r"^projections?\s*:", re.I)),
    ("special_comments", re.compile(r"^special\s+comments?\s*:", re.I)),
    ("conclusions",      re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?conclusions?\b", re.I)),
    ("stock_distribution", re.compile(r"^stock\s+distribution\b", re.I)),
]

# Headings that end a narrative run (methods/diagnostics prose we do not want).
_METHOD_HEAD = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(introduction|methods?|materials\s+and\s+methods|data(?:\s+(?:sources|and\s+modeling|inputs))?|"
    r"model(?:s|ing)?(?:\s+(?:description|configuration|structure|diagnostics))?|"
    r"fishery[- ]independent|fishery[- ]dependent|life\s+history|stock\s+identification|"
    r"assessment\s+(?:methods?|model|history)|sensitivity|retrospective|research\s+recommendations?|"
    r"terms?\s+of\s+reference|review\s+(?:panel|workshop)|discussion|results)\b\s*:?\s*$", re.I)

_HEADINGISH = re.compile(r"^[A-Z0-9][A-Za-z0-9 ,'()/&.\-]{2,80}:?\s*$")


def _looks_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 90:
        return False
    if s.endswith((".", ";", ",")) and not s.endswith("..."):
        return False
    words = s.split()
    if len(words) > 12:
        return False
    return bool(_HEADINGISH.match(s))


def narrative(lines: Sequence[str], max_chars: int = 24000,
              min_section_chars: int = 120) -> Tuple[str, List[str]]:
    """Assemble the report's own status narrative. Returns (text, section labels used)."""
    sections: List[Tuple[str, List[str]]] = []
    cur_label: Optional[str] = None
    cur: List[str] = []
    for ln in lines:
        lab = None
        for name, pat in SECTION_PATS:
            if pat.match(ln.strip()):
                lab = name
                break
        if lab:
            if cur_label and cur:
                sections.append((cur_label, cur))
            # Heading on its own line, so it does not glue to the first sentence (the #08
            # buried-heading trap): "Executive SummaryThrough 2010, octopuses were..."
            cur_label, cur = lab, [ln.strip(), ""]
            continue
        if cur_label is not None:
            # A heading only closes a section once that section has real content. Several
            # house formats nest a subheading immediately under the section heading --
            # AFSC's "Executive Summary" is routinely followed by "Introduction", and
            # closing there left an empty section that fell through to the fallback.
            if _looks_heading(ln) and len(" ".join(cur)) > 200:
                sections.append((cur_label, cur)); cur_label, cur = None, []
                continue
            cur.append(ln)
    if cur_label and cur:
        sections.append((cur_label, cur))

    picked: List[str] = []
    labels: List[str] = []
    seen_text = set()
    for lab, body in sections:
        txt = "\n".join(paragraphs(body))
        if len(txt) < min_section_chars:
            continue
        key = txt[:200]
        if key in seen_text:      # SAFE reports repeat their exec summary in a front matter
            continue
        seen_text.add(key)
        picked.append(txt)
        labels.append(lab)
        if sum(len(p) for p in picked) >= max_chars:
            break
    if picked:
        return _trim_tail(_cap("\n\n".join(picked), max_chars)), labels

    # Fallback: the leading prose (title-page abstract + opening paragraphs). Still first
    # party and still about this stock -- just not delimited by a heading we know.
    paras = [p for p in paragraphs(lines) if _is_prose(p)]
    return _trim_tail(_cap("\n\n".join(paras[:12]), max_chars)), ["leading_prose"]


# Title pages, mailing addresses and acronym glossaries clear any length floor but are not
# narrative. Real prose ends a sentence and is not mostly capitalised tokens.
_ADDRESSY = re.compile(r"\b(\d{3,5}\s+\w+\s+(Boulevard|Street|Avenue|Road|Drive|Way|Blvd|St\.|Ave\.)"
                       r"|P\.?O\.?\s+Box|Or online at|available online at)\b", re.I)


def _is_prose(p: str) -> bool:
    if len(p) < 80:
        return False
    words = p.split()
    if len(words) < 12:
        return False
    if _ADDRESSY.search(p):
        return False
    # An acronym glossary ("WDFW: Washington Department of Fish and Wildlife YOY: ...") is
    # dense in ALL-CAPS tokens and colons.
    caps = sum(1 for w in words if len(w) > 1 and w.isupper())
    if caps / len(words) > 0.25 or p.count(":") >= 4:
        return False
    return bool(re.search(r"[.!?][\"')\]]?$", p.strip()))


def _trim_tail(text: str) -> str:
    """Drop trailing lines until the text ends on a finished sentence.

    `<ts></ts>` is appended straight after this text, and SCHEMA.md asks for the splice point
    to sit after a sentence that reads naturally. Left untrimmed, 29.7% of records ended on a
    table lead-in ("...are shown in the following table:"), a bare heading, or a row of model
    names -- so the placeholder dangled off page furniture.
    """
    _ends = re.compile(r"[a-zA-Z0-9\"')\]][.!?][\"')\]]?$")
    parts = [p for p in text.split("\n") if p.strip()]
    while parts:
        last = parts[-1].strip()
        if _ends.search(last) and not _looks_heading(last):
            break
        # A section is often ONE joined paragraph, so popping the line would delete the whole
        # section. Cut it back to its own last sentence end first, and only drop the line if
        # it holds no complete sentence at all.
        cut = max(last.rfind(". "), last.rfind("! "), last.rfind("? "))
        if cut > 60:
            parts[-1] = last[:cut + 1]
            continue
        parts.pop()
    return "\n".join(parts).strip()


def _cap(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text.strip()
    cut = text[:max_chars]
    dot = cut.rfind(". ")
    return (cut[:dot + 1] if dot > max_chars * 0.5 else cut).strip()


# ---------------------------------------------------------------- omnibus scoping

_STOPWORDS = {"salmon", "river", "coast", "coastal", "north", "south", "east", "west",
              "northern", "southern", "eastern", "western", "gulf", "bay", "sea", "island",
              "islands", "stock", "stocks", "of", "and", "the", "fall", "spring", "summer",
              "winter", "hatchery", "natural", "basin", "sound", "strait", "area", "late",
              "early", "mid", "upper", "lower", "central"}


def name_keys(stock_name: str) -> Tuple[List[str], List[str]]:
    """Split a Stock SMART stock name into (species tokens, area tokens).

    'Acadian redfish - Gulf of Maine / Georges Bank' -> (['acadian','redfish'],
                                                         ['maine','georges','bank'])
    """
    part = stock_name.split(" - ", 1)
    species = re.findall(r"[a-z]+", part[0].lower())
    area = re.findall(r"[a-z]+", part[1].lower()) if len(part) > 1 else []
    sp = [t for t in species if t not in _STOPWORDS and len(t) > 2]
    ar = [t for t in area if t not in _STOPWORDS and len(t) > 2]
    return (sp or species), (ar or area)


# A chapter opener in an omnibus report: "Chapter 5. Bristol Bay Red King Crab",
# "18. Assessment of the skate stock complex in the Bering Sea...", "C. OCEAN QUAHOG".
_CHAPTER_HEAD = re.compile(
    r"^\s*(?:chapter\s+\d+\w*\s*[.:-]?\s*|\d{1,2}\s*[.:]\s+|[A-Z]\s*[.:]\s+)"
    r"(?:assessment\s+of\s+(?:the\s+)?)?(.{4,90})$", re.I)
# The AFSC crab-SAFE executive summaries open with a machine-regular stock line.
_STOCK_LINE = re.compile(r"^\s*1\.\s*Stock\s*:\s*(.{4,120})$", re.I)


def _anchor_lines(page_texts: Sequence[str]) -> List[Tuple[int, str]]:
    """(page index, heading text) for every chapter-opener-looking line in the document."""
    out: List[Tuple[int, str]] = []
    for i, pg in enumerate(page_texts):
        for ln in pg.split("\n"):
            s = ln.strip()
            if not s or len(s) > 110:
                continue
            m = _STOCK_LINE.match(s) or _CHAPTER_HEAD.match(s)
            if m:
                out.append((i, m.group(1).strip()))
    return out


def locate_scope(page_texts: Sequence[str], stock_name: str,
                 rival_names: Sequence[str], min_pages: int = 2,
                 max_pages: int = 60) -> Optional[Tuple[int, int]]:
    """Find the page range of one stock's own chapter in a multi-stock report.

    Anchored on the document's own chapter headings, NOT on where the stock's name occurs
    most densely: in a 600-page SAFE the densest mentions are the figure legends and table
    headers of the appendix, so a density search reliably returns the wrong pages (measured
    -- it returned model-parameter dumps and chart legends).

    Returns (first_page, last_page) inclusive, or None when the stock cannot be isolated
    from its rivals -- in which case the caller must drop the row rather than pair a shared
    section with this stock's series.
    """
    sp, ar = name_keys(stock_name)
    if not sp:
        return None
    anchors = _anchor_lines(page_texts)
    if not anchors:
        return None
    rivals = [name_keys(n) for n in rival_names]

    def match_score(head: str, spec: Sequence[str], area: Sequence[str]) -> float:
        h = head.lower()
        sh = sum(1 for t in spec if t in h)
        if sh < len(spec):          # every species token must appear in the heading
            return 0.0
        ah = sum(1 for t in area if t in h) if area else 0
        return sh + 2.0 * ah

    scored = [(i, h, match_score(h, sp, ar)) for i, h in anchors]
    mine = [(i, h, s) for i, h, s in scored if s > 0]
    if not mine:
        return None
    # Prefer the heading that also matches the most area tokens; ties -> earliest.
    best_score = max(s for _, _, s in mine)
    mine = [m for m in mine if m[2] == best_score]
    # Reject when a rival stock matches this same heading at least as well: that is the
    # PFMC-salmon case, where one section ("Puget Sound Chinook Stocks") covers 25 stocks.
    for i, h, s in mine:
        for rsp, rar in rivals:
            if match_score(h, rsp, rar) >= s:
                return None
    start_page = mine[0][0]
    # The chapter ends at the next anchor that is not this stock's own.
    mine_pages = {i for i, _, _ in mine}
    later = [i for i, h in anchors if i > start_page and i not in mine_pages]
    end_page = (min(later) - 1) if later else len(page_texts) - 1
    if end_page < start_page:
        end_page = start_page
    if end_page - start_page + 1 < min_pages:
        return None
    return start_page, min(end_page, start_page + max_pages - 1)
