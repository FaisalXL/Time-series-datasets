"""Column-aware narrative extraction for Federal Reserve regional-survey release PDFs.

Written for the Philadelphia Fed MBOS releases (47) and reused unchanged for Dallas TMOS
(48) and the Richmond Fifth District surveys (49/50) -- all of them 2-column with charts
interleaved. The only Richmond-specific change is in `_body_size`; see its docstring.

The releases are 2-column with full-width charts interleaved; poppler's default
reading order zips the two columns line-by-line and shreds sentences
("The de(10 percent), although 76 percent…mand for manufactured goods").
This reconstructs the flow: body-font words only -> per-page column split ->
left column top-to-bottom, then right column -> de-hyphenate -> paragraphs.
"""
from __future__ import annotations

import collections
import re
import statistics
import warnings
from typing import Dict, List, Optional, Tuple

import pdfplumber

warnings.filterwarnings("ignore")

# boilerplate lines that float inside the text frame and must not break a paragraph
_BOILER = re.compile(
    r"^(released\s*:|release date|the (next )?[a-z]+ \d{4} manufacturing business outlook survey"
    r"|the next manufacturing business outlook survey"
    r"|note\s*:\s*survey responses|for more regional economic"
    r"|federal reserve bank of (philadelphia|richmond)|research dept\.? ?/ ?regional economics"
    r"|www\.|https?://|prepared by|contact\s*:|chart \d\s*[.:]|table \d\s*[.:]"
    r"|\*\s*percentage of respondents"
    r"|source\s*:|see the (special questions|technical)|page \d)", re.I)

# page furniture that sits *inside* the text frame as its own line: drop the line,
# but unlike _BOILER do not swallow the paragraph it interrupts
_FURNITURE = re.compile(
    r"^(?:[A-Z][a-z]+ \d{4}|\d{1,2}|MANUFACTURING|BUSINESS OUTLOOK SURVEY|"
    r"Diffusion Index\*?|Research Department|Department|\W{0,3})$")


def _body_size(words: List[dict]) -> float:
    """Modal font size of the *prose*, weighted by alphabetic characters.

    Counting words instead (the original) picks the wrong size on the Richmond 2018+
    layout, whose page 1 carries four charts: 173 axis-label words at 9.0pt outnumber the
    158 narrative words at 11.5pt, so the narrative is then treated as non-body and
    discarded, and what survives is the axis labels. Weighting by alphabetic characters in
    tokens of three or more letters separates prose from axis furniture -- month
    abbreviations and numbers contribute almost nothing.
    """
    c: collections.Counter = collections.Counter()
    for w in words:
        letters = sum(ch.isalpha() for ch in w["text"])
        if letters >= 3:
            c[round(w["size"], 1)] += letters
    if not c:
        c = collections.Counter(round(w["size"], 1) for w in words)
    return c.most_common(1)[0][0]


def _column_bounds(words: List[dict], width: float,
                   min_gutter: float = 5.0, min_words: int = 18) -> List[float]:
    """Gutter x-positions splitting the page into 1..N text columns.

    MBOS uses two columns from 2009 and *three* in the 2008-and-earlier layout, so
    the count cannot be assumed. A gutter is a full-height band that (almost) no
    body word crosses; a candidate is kept only if both neighbouring slabs hold
    real text. The tolerance matters: a single page-footer word spanning the
    gutter ("… | Research Department") is enough to hide it otherwise.
    """
    W = int(width)
    tol = max(1, int(0.012 * len(words)))
    occ = [0] * (W + 2)
    for w in words:
        for x in range(max(0, int(w["x0"]) + 1), min(int(w["x1"]), W + 1)):
            occ[x] += 1
    runs, s = [], None
    for x in range(W + 1):
        if occ[x] <= tol:
            if s is None:
                s = x
        else:
            if s is not None and x - s >= min_gutter:
                runs.append((s, x))
            s = None
    if s is not None and W + 1 - s >= min_gutter:
        runs.append((s, W + 1))
    cands = [(a + b) / 2 for a, b in runs if 0.10 * W < (a + b) / 2 < 0.92 * W]
    bounds: List[float] = []
    for c in sorted(cands):
        lo = bounds[-1] if bounds else 0
        left = sum(1 for w in words if lo <= (w["x0"] + w["x1"]) / 2 < c)
        right = sum(1 for w in words if (w["x0"] + w["x1"]) / 2 >= c)
        if left >= min_words and right >= min_words:
            bounds.append(c)
    return bounds


def _graphic_masks(page, body: Optional[List[dict]] = None
                   ) -> List[Tuple[float, float, float, float]]:
    """Bounding boxes of chart / table graphics, so their labels never enter the prose.

    Charts are drawn either as one framed rect (2008-2015 layouts) or as ~1,400
    tiny curves (2016+); clustering the graphics on a coarse grid catches both.
    """
    objs = list(page.curves) + list(page.lines) + list(page.rects)
    if not objs:
        return []
    W, H = page.width, page.height
    cell = 12.0
    grid: Dict[Tuple[int, int], None] = {}
    for o in objs:
        if (o["x1"] - o["x0"]) > 0.9 * W and (o["bottom"] - o["top"]) > 0.9 * H:
            continue                                   # page background
        for gx in range(int(o["x0"] // cell), int(o["x1"] // cell) + 1):
            for gy in range(int(o["top"] // cell), int(o["bottom"] // cell) + 1):
                grid[(gx, gy)] = None
    seen, boxes = set(), []
    for start in grid:
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            gx, gy = stack.pop()
            comp.append((gx, gy))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (gx + dx, gy + dy)
                    if n in grid and n not in seen:
                        seen.add(n)
                        stack.append(n)
        x0 = min(c[0] for c in comp) * cell
        x1 = (max(c[0] for c in comp) + 1) * cell
        y0 = min(c[1] for c in comp) * cell
        y1 = (max(c[1] for c in comp) + 1) * cell
        area = (x1 - x0) * (y1 - y0)
        if not ((x1 - x0) >= 120 and (y1 - y0) >= 60 and area < 0.55 * W * H):
            continue
        # A framed *text* box (the release-date panel) is graphically identical to a
        # chart frame; what separates them is how much body prose sits inside. Charts
        # hold only a title, so masking on body-word density keeps prose safe.
        if body:
            n = sum(1 for w in body
                    if x0 <= (w["x0"] + w["x1"]) / 2 <= x1 and y0 <= (w["top"] + w["bottom"]) / 2 <= y1)
            if n / (area / 10000.0) > 2.5:
                continue
        boxes.append((x0, y0, x1, y1))
    return boxes


def _lines(words: List[dict]) -> List[Tuple[float, float, str, bool]]:
    """Group words into lines -> (top, x0, text, is_bold), ordered top-to-bottom."""
    if not words:
        return []
    hs = [w["bottom"] - w["top"] for w in words]
    tol = max(2.0, statistics.median(hs) * 0.5)
    rows: List[List[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(w["top"] - rows[-1][0]["top"]) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    out = []
    for r in rows:
        r.sort(key=lambda w: w["x0"])
        # respect real inter-word gaps: pdfplumber splits tightly-kerned tokens
        # ("Six‐Month" -> "Six‐", "Month"), and a blind " ".join re-inserts a space
        parts = [r[0]["text"]]
        for prev, w in zip(r, r[1:]):
            parts.append(("" if w["x0"] - prev["x1"] < 1.0 else " ") + w["text"])
        txt = "".join(parts).strip()
        bold = sum(1 for w in r if "bold" in w["fontname"].lower()) >= max(1, len(r) // 2)
        out.append((r[0]["top"], r[0]["x0"], txt, bold))
    return out


def page_lines(page) -> List[Tuple[float, str, bool]]:
    """One page -> reading-ordered (gap_before, line_text, is_bold)."""
    words = page.extract_words(extra_attrs=["size", "fontname"])
    if not words:
        return []
    bs = _body_size(words)
    body = [w for w in words if abs(w["size"] - bs) < 0.6]
    masks = _graphic_masks(page, body)
    if masks:
        def inside(w):
            cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
            return any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in masks)
        body = [w for w in body if not inside(w)]
    if len(body) < 30:
        return []
    edges = [0.0] + _column_bounds(body, page.width) + [page.width + 1]
    groups = [[w for w in body if edges[i] <= (w["x0"] + w["x1"]) / 2 < edges[i + 1]]
              for i in range(len(edges) - 1)]
    groups = [g for g in groups if g]
    out: List[Tuple[float, str, bool]] = []
    for g in groups:
        ls = _lines(g)
        prev_bottom = None
        heights = [1.0]
        for i, (top, x0, txt, bold) in enumerate(ls):
            gap = 0.0 if prev_bottom is None else top - prev_bottom
            out.append((gap, txt, bold))
            if i + 1 < len(ls):
                heights.append(ls[i + 1][0] - top)
            prev_bottom = top
        # mark a hard break between columns
        out.append((999.0, "", False))
    return out


def paragraphs(pdf_path_or_bytes, max_pages: int = 3) -> List[Tuple[str, bool]]:
    """PDF -> [(paragraph_text, is_heading)] in reading order."""
    import io
    src = pdf_path_or_bytes
    if isinstance(src, (bytes, bytearray)):
        src = io.BytesIO(src)
    with pdfplumber.open(src) as pdf:
        raw: List[Tuple[float, str, bool]] = []
        for p in pdf.pages[:max_pages]:
            raw.extend(page_lines(p))

    # median line pitch, for paragraph-gap detection
    gaps = [g for g, t, b in raw if t and 0 < g < 40]
    pitch = statistics.median(gaps) if gaps else 12.0

    paras: List[Tuple[str, bool]] = []
    cur: List[str] = []
    cur_bold = False

    def flush():
        nonlocal cur, cur_bold
        if cur:
            paras.append((_join(cur), cur_bold))
        cur, cur_bold = [], False

    def open_sentence() -> bool:
        return bool(cur) and not re.search(r'[.!?"”\')\]]\s*$', _join(cur))

    def continues(nxt: str, nxt_bold: bool) -> bool:
        """Does `nxt` continue the sentence left hanging at a column/page break?"""
        if nxt_bold or nxt.isupper():
            return False
        # A digit also continues: a sentence broken at a column boundary often resumes on
        # its closing figure ("…above last month's gauge" | "23. Additionally, …"), which
        # the letters-only test left stranded as a paragraph opening with a bare number.
        return bool(re.match(r"[a-z(0-9]|Chart \d\)", nxt))

    # index of the next real line, for the look-ahead
    nxt_of: List[Tuple[str, bool]] = [("", False)] * len(raw)
    nxt = ("", False)
    for i in range(len(raw) - 1, -1, -1):
        nxt_of[i] = nxt
        if raw[i][1] and not _FURNITURE.match(raw[i][1]):
            nxt = (raw[i][1], raw[i][2])

    skipping = False
    for i, (gap, txt, bold) in enumerate(raw):
        if not txt:
            # column / page boundary. A paragraph that runs over the boundary
            # ("…the firms reported a de-" | "crease in employment…") must stay one
            # paragraph, or every break costs a shredded sentence — but only when the
            # next block really is the continuation, not the start of the data tables.
            if not (open_sentence() and continues(*nxt_of[i])):
                flush()
            skipping = False
            continue
        new_para = gap > pitch * 1.45
        if bold and not cur_bold and cur:
            new_para = True
        if open_sentence() and not bold and re.match(r"[a-z(]", txt):
            new_para = False           # mid-sentence carry-over, see the flush above
        if new_para:
            flush()
            cur_bold = bold
            skipping = False
        if _FURNITURE.match(txt):
            continue
        if _BOILER.match(txt):
            # a boilerplate block floats inside the text frame; drop its
            # continuation lines too, or "…collected from" leaves "March 6 to
            # March 13." stranded as a paragraph of its own
            skipping = True
        if skipping:
            continue
        cur.append(txt)
    flush()
    return [(t, b) for t, b in paras if t]


def _join(lines: List[str]) -> str:
    out = ""
    for ln in lines:
        ln = ln.strip()
        if not out:
            out = ln
            continue
        m = re.search(r"(\w*)[‐‑\u00ad-]$", out)
        if m:
            # hyphen at a line break. The codepoint does not discriminate (2008 and
            # 2013 use U+002D for both soft and compound hyphens), so drop it unless
            # it is a numeric compound ("29-point") or the next fragment is capitalised
            # ("Six-Month"). Residual cost: "One-half" joins as "Onehalf".
            keep = m.group(1).isdigit() or (ln[:1].isupper())
            out = re.sub(r"[‐‑\u00ad-]$", "-" if keep else "", out.rstrip()) + ln
        else:
            out = out + " " + ln
    return re.sub(r"\s+", " ", out).strip()
