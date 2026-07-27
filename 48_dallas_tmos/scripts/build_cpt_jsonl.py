#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from the Philadelphia Fed Manufacturing Business
Outlook Survey (MBOS).

One record = ONE INDICATOR from one monthly release: the sentence(s) of that release's
narrative that describe a single diffusion index, paired with a trailing window of that
indicator's own readings.

Series = the AS-FIRST-PUBLISHED vintage, stitched from each release's own table. The
Bank revises the whole diffusion-index history whenever it re-estimates the seasonal
factors, so `bos_dif.csv` disagrees with every release older than the current calendar
year (June 2015 states 15.2; the CSV now says 8.2). See `config.example.yaml`.

Text  : release PDF -> `scripts/colex.py` (column-aware; poppler's own reading order
        zips the two columns and shreds sentences).
Table : release PDF -> `scripts/tabex.py` (column-geometry parse, guarded by the
        identity index == %increase - %decrease).

License: Federal Reserve Bank publications are U.S. public domain.

Examples:
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import json
import html as _html
import re
import ssl
import sys
import time
import urllib.request
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required. pip install -r requirements.txt") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
import colex                                                        # noqa: E402
import tmostab                                                      # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record                                        # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]
_ABBR = "\x00"


# --- config helpers (same conventions as the other packages) ---------------

def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    m = dict(base)
    for k, v in over.items():
        m[k] = deep_merge(m[k], v) if k in m and isinstance(m[k], dict) and isinstance(v, dict) else v
    return m


def coerce(raw: str) -> Any:
    low = raw.strip().lower()
    if low in {"true", "yes"}: return True
    if low in {"false", "no"}: return False
    if low in {"null", "none", "~"}: return None
    if re.fullmatch(r"-?\d+", raw): return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw): return float(raw)
    return raw


def parse_sets(sets: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for it in sets:
        k, v = it.split("=", 1)
        cur = out
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = coerce(v)
    return out


def load_config(path: Path, sets: Sequence[str]) -> Dict[str, Any]:
    cfg = yaml.safe_load(path.read_text())
    return deep_merge(cfg, parse_sets(sets)) if sets else cfg


def rp(s: str) -> Path:
    p = Path(s)
    return p if p.is_absolute() else ROOT / p


# --- month helpers ---------------------------------------------------------

def month_range(first: str, last: str) -> List[str]:
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def shift(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) - 1 + k
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def pretty(ym: str) -> Tuple[str, str]:
    return MONTHS[int(ym[5:7]) - 1], ym[:4]


# --- HTTP ------------------------------------------------------------------

def http_get(url: str, ua: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL).read()


_ARCHIVE: Dict[str, Tuple[Path, str]] = {}


def archive_index(d: dict, cache: Path) -> Dict[str, Tuple[Path, str]]:
    """{'YYYY-MM': (zip_path, member)} over the Bank's own per-era release archives."""
    global _ARCHIVE
    if _ARCHIVE:
        return _ARCHIVE
    adir = cache / "archives"
    adir.mkdir(parents=True, exist_ok=True)
    for url in d.get("archive_zip_urls") or []:
        zp = adir / url.rsplit("/", 1)[-1]
        if not zp.exists():
            try:
                print(f"Downloading {zp.name}…", file=sys.stderr)
                zp.write_bytes(http_get(url, d["user_agent"], int(d["timeout_s"])))
                time.sleep(float(d.get("request_delay_s", 0.4)))
            except Exception as exc:
                print(f"  archive {zp.name} unavailable ({exc})", file=sys.stderr)
                continue
        try:
            with zipfile.ZipFile(zp) as zf:
                names = zf.namelist()
        except zipfile.BadZipFile:
            zp.unlink(missing_ok=True)
            continue
        for n in names:
            m = re.search(r"(?:^|/)bos(\d{2})(\d{2})\.pdf$", n, re.I)
            if not m:
                continue
            mm, yy = int(m.group(1)), int(m.group(2))
            if not 1 <= mm <= 12:
                continue
            ym = f"{1900 + yy if yy >= 60 else 2000 + yy}-{mm:02d}"
            _ARCHIVE.setdefault(ym, (zp, n))
    return _ARCHIVE


_WAYBACK: Dict[str, Dict[str, List[str]]] = {}


def wayback_index(d: dict, cache: Path) -> Dict[str, Dict[str, List[str]]]:
    """{'YYYY-MM': {'txt'|'html'|'pdf': [url, timestamp]}} for the retired layouts.

    Needed because every pre-2002 PDF in the Bank's archives is an image scan, while
    the retired site published the same release as plain text that Wayback captured.
    Four path prefixes are in play — the site moved the survey twice.
    """
    global _WAYBACK
    if _WAYBACK:
        return _WAYBACK
    fp = cache / "wayback_index.json"
    if fp.exists():
        _WAYBACK = json.loads(fp.read_text())
        return _WAYBACK
    found: Dict[str, Dict[str, List[str]]] = {}
    for pref in d.get("wayback_prefixes") or []:
        q = d["wayback_cdx_template"].format(prefix=pref)
        try:
            rows = json.loads(http_get(q, d["user_agent"], int(d["timeout_s"])).decode("utf-8", "replace"))
        except Exception as exc:
            print(f"  Wayback CDX unavailable for {pref} ({exc})", file=sys.stderr)
            continue
        time.sleep(1.0)
        for r in rows[1:]:
            url, ts = r[0], r[1]
            m = re.search(r"/bos(\d{2})(\d{2})\.(pdf|txt|html?)$", url, re.I)
            if not m:
                continue
            mm, yy = int(m.group(1)), int(m.group(2))
            if not 1 <= mm <= 12:
                continue
            ym = f"{2000 + yy if yy < 40 else 1900 + yy}-{mm:02d}"
            found.setdefault(ym, {}).setdefault(m.group(3).lower(), [url, ts])
    if found:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(found, indent=0))
    _WAYBACK = found
    return found


def fetch_wayback_text(d: dict, ym: str, cache: Path) -> Optional[bytes]:
    """The retired plain-text release for one month, cached."""
    fp = cache / "releases" / f"{ym}.txt"
    if fp.exists():
        return fp.read_bytes() or None
    idx = wayback_index(d, cache).get(ym) or {}
    for kind in ("txt", "html"):
        if kind not in idx:
            continue
        url, ts = idx[kind]
        try:
            raw = http_get(d["wayback_raw_template"].format(ts=ts, url=url),
                           d["user_agent"], int(d["timeout_s"]))
        except Exception:
            raw = b""
        time.sleep(float(d.get("request_delay_s", 0.4)))
        if raw and txtex.parse_table(raw):
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(raw)
            return raw
    return None


def _doc_score(kind: str, raw: bytes) -> int:
    """How much of a release this document actually yields: table rows + narrative."""
    try:
        table = tmostab.parse_table(raw)
    except Exception:
        return 0
    rows = sum(1 for r in table.values() if tmostab.check(r, "cur"))
    paras = narrative_html(raw) if kind == "html" else narrative_pdf(raw)
    return rows * 10 + min(len(paras), 10)


def fetch_release(d: dict, ym: str, cache: Path) -> Tuple[Optional[str], Optional[bytes]]:
    """One month's release document as ('pdf'|'html', bytes), read from the cache.

    Both forms can exist for the same month and neither is reliably the better one:
    from 2016-03 to 2020-12 the Bank's own PDF is drawn as vector graphics with no text
    layer at all (45 of 181 cached PDFs, 12-16 extractable words per page), and for
    those months the Wayback capture of the release page is the only readable source.
    Before 2016 the PDF is the richer document. So both are scored on what they
    actually yield -- identity-passing table rows first, narrative paragraphs second --
    and the better one wins, rather than fixing a precedence by era.
    """
    rel = cache / "releases"
    best: Tuple[int, Optional[str], Optional[bytes]] = (0, None, None)
    for kind, suffix in (("html", ".html"), ("pdf", ".pdf")):
        f = rel / f"{ym}{suffix}"
        if not f.exists():
            continue
        raw = f.read_bytes()
        if kind == "pdf" and not raw.startswith(b"%PDF"):
            continue
        sc = _doc_score(kind, raw)
        if sc > best[0]:
            best = (sc, kind, raw)
    if best[1] is None:
        # a document exists but yields nothing readable -> image-scan tier
        any_doc = any((rel / f"{ym}{x}").exists() for x in (".pdf", ".html"))
        return ("scan", None) if any_doc else (None, None)
    return best[1], best[2]


# --- text ------------------------------------------------------------------

_TABLE_ROW = re.compile(r"Previous\s+Diffusion|Diffusion\s+Index\s+Increase|"
                        r"vs\.\s+(January|February|March|April|May|June|July|August|"
                        r"September|October|November|December)|"
                        r"(?:[-‐−]?\d+(?:\.\d)?\s+){3}[-‐−]?\d+(?:\.\d)?", re.I)
# a numbered special question ("2a. Are you currently experiencing shortages…"): the
# answer tables under it name indicators and carry numbers, so it must not be mined
_QUESTION = re.compile(r"^\d{1,2}[a-c]?[.)]\s")
# the pre-2016 page-2 chart puts its year axis in front of the column it interrupts
_AXIS = re.compile(r"^(?:[-‐−]?\d{1,3}\s+){3,}")



# Site furniture and footnotes that sit inside the release page but are not the
# month's narrative. The trailing "*Shown is the number of consecutive months..."
# notes explain the Trend column of the table, not the indicators.
_HTML_DROP = re.compile(
    r"^\s*\*|supplemental questions|special questions|seasonal factor revision"
    r"|globalization institute|community development|read our publications"
    r"|subscribe|e-?mail alert|privacy policy|download indexes"
    r"|the dallas fed conducts|survey responses are used to calculate"
    r"|data have been seasonally adjusted|data were collected", re.I)
# The narrative is entirely about the indexes; page furniture is not.
_HTML_KEEP = re.compile(r"\bindex(es)?\b|\brespondents\b|\bpercent of (firms|respondents)\b", re.I)


def narrative_html(raw: bytes) -> List[str]:
    """Release page -> the prose paragraphs of the narrative.

    Used for 2016-2026, where the release page is either the live document or the only
    readable capture of a month whose PDF has no text layer.
    """
    s = raw.decode("utf8", "ignore")
    s = re.sub(r"(?is)<(script|style|nav|footer).*?</\1>", " ", s)
    out = []
    for para in re.findall(r"(?is)<p[^>]*>(.*?)</p>", s):
        txt = re.sub(r"\s+", " ", _html.unescape(re.sub(r"(?s)<[^>]+>", " ", para))).strip()
        if len(txt) < 100 or _HTML_DROP.search(txt) or not _HTML_KEEP.search(txt):
            continue
        out.append(txt)
    return out


def narrative_pdf(raw: bytes) -> List[str]:
    """Release PDF -> the prose paragraphs of the narrative pages.

    Pages 1-2: the modern layout runs the future-indicators and Summary paragraphs onto
    page 2, while the 2008-2015 layout puts the data table there — so the page count
    cannot decide it and the table has to be filtered on its own shape.
    """
    out = []
    for para, _bold in colex.paragraphs(raw, max_pages=2):
        para = _AXIS.sub("", para)
        if para[:1].islower():
            # the axis interrupted mid-word, so the leading sentence is a fragment
            para = " ".join(para.split(". ")[1:])
        digits = sum(c.isdigit() for c in para)
        alpha = sum(c.isalpha() for c in para)
        if alpha < 60 or alpha < 3.5 * digits:      # table row / chart axis remnant
            continue
        if _TABLE_ROW.search(para) or _QUESTION.match(para):
            continue
        out.append(para)
    return out


def has_text_layer(raw: bytes, min_words: int = 200) -> bool:
    import io as _io
    try:
        with colex.pdfplumber.open(_io.BytesIO(raw)) as pdf:
            return sum(len(p.extract_words()) for p in pdf.pages[:2]) >= min_words
    except Exception:
        return False


def split_sentences(prose: str) -> List[str]:
    p = prose.replace("U.S.", "U" + _ABBR + "S" + _ABBR)
    p = re.sub(r"\b([Nn]o|[A-Z]|Mr|Ms|Dr|Inc|Corp|vs|approx|Fig)\.(\s)", r"\1" + _ABBR + r"\2", p)
    return [s.replace(_ABBR, ".").strip() for s in re.split(r"(?<=[.!?])\s+", p) if s.strip()]


def sentence_matches(sentence: str, groups: Sequence[Sequence[str]]) -> bool:
    sl = sentence.lower()
    return any(all(w.lower() in sl for w in grp) for grp in groups)


_CLAUSE = re.compile(r",\s+(?=and\s|but\s|while\s|whereas\s|although\s)|;\s+")
_LEAD = re.compile(r"^(and|but|while|whereas|although)\s+", re.I)


def clauses_for(sentence: str, tag: str, topics: Sequence[dict]) -> str:
    """The part of `sentence` that is about `tag`.

    The Bank routinely narrates two indicators in one sentence — "The new orders index
    rose 9 points to 14.4, and the shipments index dropped 15 points to -8.7". Kept
    whole, that sentence puts the shipments value inside the new-orders record, where
    the attached series does not contain it. Single-topic sentences are left verbatim;
    only genuinely joint ones are cut at the conjunction.
    """
    parts = _CLAUSE.split(sentence)
    if len(parts) == 1:
        return sentence
    owners = [{tp["tag"] for tp in topics if sentence_matches(p, tp["match"])} for p in parts]
    for i, o in enumerate(owners):                  # a clause naming nobody continues the
        if not o:                                   # previous one ("…, and it fell again")
            owners[i] = owners[i - 1] if i else next((x for x in owners if x), set())
    if all(tag in o for o in owners):
        return sentence
    keep = [_LEAD.sub("", p).strip() for p, o in zip(parts, owners) if tag in o]
    if not keep:
        return ""
    out = " ".join(keep)
    return out if out.endswith((".", "!", "?")) else out + "."


_FIGURE = re.compile(r"(?<![\d.])-?\d{1,3}\.\d(?![\d])")


def states(text: str, value: Optional[float]) -> bool:
    """Does the prose quote this value verbatim? Handles the U+2010 minus sign."""
    if value is None:
        return False
    pat = re.escape(f"{value:.1f}").replace(r"\-", r"[-‐‑−]")
    return re.search(r"(?<![\d.])" + pat + r"(?![\d])", text) is not None


# --- pipeline --------------------------------------------------------------

def candidates(ym: str, cache: Path):
    """Every cached document for one month, as (kind, raw).

    A month can have up to three: the Bank's release PDF, a Wayback capture of the
    release page, and -- for 2006-2010 -- the retired tmos{yymm}summ.html "Summary of
    Results" page, which carries the results table for an era whose release page
    printed prose only.
    """
    rel = cache / "releases"
    out = []
    for kind, suffix in (("pdf", ".pdf"), ("html", ".html"), ("summ", ".summ.html")):
        f = rel / f"{ym}{suffix}"
        if not f.exists():
            continue
        raw = f.read_bytes()
        if kind == "pdf" and not raw.startswith(b"%PDF"):
            continue
        out.append((kind, raw))
    return out


def harvest(cfg: Dict[str, Any]) -> Tuple[Dict[str, dict], Dict[str, List[str]], Dict[str, int]]:
    """Every release month -> its as-published table and its narrative paragraphs.

    Table and narrative are chosen independently, because no single document is reliably
    the best source for both: from 2016-03 to 2020-12 the Bank's own PDF is drawn as
    vector graphics with no text layer (45 of 181 cached PDFs) and only the Wayback
    capture is readable, while in 2006-2010 the release page has the prose and the
    separate summary page has the table. Each is scored on what it actually yields.
    """
    d = cfg["data"]
    cache = rp(d["cache_dir"])
    today = date.today()
    last = d.get("last_month") or f"{today.year:04d}-{today.month:02d}"
    stat = {"months_scanned": 0, "no_release": 0, "no_text_layer": 0,
            "no_table": 0, "no_narrative": 0}
    vintage: Dict[str, dict] = {}
    prose: Dict[str, List[str]] = {}
    for ym in month_range(d["first_month"], last):
        stat["months_scanned"] += 1
        docs = candidates(ym, cache)
        if not docs:
            stat["no_release"] += 1
            continue
        tab, tab_n, tab_src = {}, 0, None
        par, par_n, par_src = [], 0, None
        for kind, raw in docs:
            try:
                cand = tmostab.parse_table(raw)
            except Exception:
                cand = {}
            n = sum(1 for r in cand.values() if tmostab.check(r, "cur"))
            if n > tab_n:
                tab, tab_n, tab_src = cand, n, kind
            if kind == "summ":            # a table page, not prose
                continue
            try:
                ps = narrative_pdf(raw) if kind == "pdf" else narrative_html(raw)
            except Exception:
                ps = []
            if len(ps) > par_n:
                par, par_n, par_src = ps, len(ps), kind
        if tab_n == 0 and par_n == 0:
            # documents exist but none has a text layer: image-scan tier, and this is
            # where the package's exhaustion wall sits.
            stat["no_text_layer"] += 1
            continue
        if tab_n:
            # every month with an as-published table feeds later windows, whether or not
            # its own narrative survived -- a first print is a first print.
            vintage[ym] = tab
            stat["tab_" + tab_src] = stat.get("tab_" + tab_src, 0) + 1
        if tab_n == 0:
            stat["no_table"] += 1
            continue
        if par_n == 0:
            stat["no_narrative"] += 1
            continue
        stat["src_" + par_src] = stat.get("src_" + par_src, 0) + 1
        prose[ym] = par
    return vintage, prose, stat


def trailing(have: set, end: str, n: int, max_gap: int, recent: int = 12) -> List[str]:
    """The n months ending at `end`, tolerating up to `max_gap` missing ones.

    A vintage series cannot be interpolated — a month with no release has no
    as-published value — so gaps stay `null` rather than being filled. Seven months of
    2007 were never published to the web in any form, and one 2005 PDF has a corrupted
    text layer; without this tolerance each of those would void 60 consecutive windows.
    """
    want = month_range(shift(end, -(n - 1)), end)
    if any(m not in have for m in want[-recent:]):
        # the stretch the prose actually talks about must be dense, whatever the
        # tolerance allows further back
        return []
    return want if sum(1 for m in want if m not in have) <= max_gap else []


def build(cfg: Dict[str, Any]) -> Tuple[List[dict], Dict[str, Any]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    maxrec = out_cfg.get("max_records")
    win = int(d["trailing_window_months"])
    max_gap = int(d.get("max_gap_months", 0))
    recent = int(d.get("require_recent_complete", 3))
    max_topics = int(d.get("max_topics_per_sentence", 2))
    topics, chans = d["topics"], d["channels"]

    vintage, prose, stat = harvest(cfg)
    max_inds = int(d.get("max_indicators_per_paragraph", 5))
    stat.update({"paragraphs_scanned": 0, "para_no_indicator": 0, "para_is_summary": 0,
                 "short_paragraph": 0, "short_window": 0, "para_no_usable_indicator": 0,
                 "indicator_failed_identity": 0, "indicator_sparse_window": 0,
                 "figures_in_text": 0, "figures_not_in_series": 0,
                 "invalid": 0, "emitted": 0})
    have = set(vintage)          # every month with a table feeds windows
    months = sorted(prose)       # only months with narrative can emit a record

    records: List[dict] = []
    for ym in reversed(months):                     # newest first, so a demo cap is recent
        if maxrec is not None and len(records) >= int(maxrec):
            break
        window = trailing(have, ym, win, max_gap, recent)
        for p_i, para in enumerate(prose[ym]):
            stat["paragraphs_scanned"] += 1
            if maxrec is not None and len(records) >= int(maxrec):
                break
            named = [tp for tp in topics
                     if sentence_matches(para, tp["match"]) and tp["tag"] in vintage[ym]]
            if not named:
                stat["para_no_indicator"] += 1
                continue
            # A paragraph naming six or more indicators is a release summary or a
            # methodology note, not one theme's prose -- the #08 guard, which there was
            # "drop any paragraph naming >=3 expenditure groups".
            if len(named) > max_inds:
                stat["para_is_summary"] += 1
                continue
            if len(para) < int(t.get("min_snippet_chars", 120)):
                stat["short_paragraph"] += 1
                continue
            if not window:
                stat["short_window"] += 1
                continue

            # Which half of the survey is this paragraph about? The expectations
            # paragraph quotes the six-months-ahead indexes while naming the same
            # indicators, so the half is read off the prose rather than assumed: whichever
            # half's terminal values the paragraph actually quotes more often wins.
            hits = {}
            for h in ("cur", "fut"):
                hits[h] = sum(1 for tp in named
                              if states(para, vintage[ym].get(tp["tag"], {}).get(h + "_idx")))
            half = "fut" if hits["fut"] > hits["cur"] else "cur"
            acell = half + "_idx"

            series, kept, nulls = [], [], 0
            for tp in named:
                tag = tp["tag"]
                row = vintage[ym].get(tag)
                if not row or not tmostab.check(row, half):
                    stat["indicator_failed_identity"] += 1
                    continue
                anchor_vals = [vintage.get(m, {}).get(tag, {}).get(acell) for m in window]
                if (anchor_vals[-1] is None
                        or any(v is None for v in anchor_vals[-recent:])
                        or sum(1 for v in anchor_vals if v is None) > max_gap):
                    stat["indicator_sparse_window"] += 1
                    continue
                added = 0
                for c in chans:
                    vals = [vintage.get(m, {}).get(tag, {}).get(c["cell"]) for m in window]
                    n_null = sum(1 for v in vals if v is None)
                    if vals[-1] is None or n_null > max_gap:
                        continue
                    series.append({"values": [None if v is None else round(v, 3) for v in vals],
                                   "unit": f"{tag}__{c['unit']}", "freq": "1M"})
                    nulls += n_null
                    added += 1
                if added:
                    kept.append(tp)
            if not kept:
                stat["para_no_usable_indicator"] += 1
                continue

            # no generated text: the Bank's own paragraph, verbatim, then the splice point
            text = f"{para}\n\n<ts></ts>"
            anchors = {tp["tag"]: vintage[ym][tp["tag"]].get(acell) for tp in kept}
            alignment = ("recites" if any(states(para, v) for v in anchors.values())
                         else "describes")
            # measured, not filtered: a figure in the prose that no attached indicator has
            in_series = {f"{v:.1f}" for c in series for v in c["values"] if v is not None}
            stat["figures_in_text"] += len(_FIGURE.findall(para))
            stat["figures_not_in_series"] += sum(
                1 for n in _FIGURE.findall(para) if n not in in_series)

            try:
                rec = emit_record(
                    text=text,
                    timeseries=series,
                    alignment=alignment,
                    license="public-domain-us-gov",
                    text_source="first_party_official",
                    source=d["source_url"],
                    dataset=d["dataset"],
                    # the paragraph ordinal is part of the id: a release can narrate the same
                    # indicator set in two paragraphs (43 collisions without it)
                    series_id=f"{d['series_id_prefix']}_{ym}_p{p_i}_{'-'.join(tp['tag'] for tp in kept)}_{half}",
                    domain="macro_econ",
                    region=d["region"],
                    period_start=f"{window[0]}-01",
                    period_end=f"{ym}-01",
                    # NOT multi_series: that contract wants one <ts></ts> splice point per
                    # series (SCHEMA §204). A paragraph is one splice point carrying several
                    # channels, exactly like 47's 6-channel records and 08's 7.
                    meta={
                        "bank": d["bank"],
                        "survey": d["survey_title"],
                        "district": d.get("district"),
                        "sector": d["domain"],
                        "release_month": ym,
                        "indicators": [tp["tag"] for tp in kept],
                        "anchor_cell": acell,
                        "anchor_values_as_published": anchors,
                        "series_vintage": "as_first_published",
                        "window_start": window[0],
                        "n_points": len(window),
                        "n_null_points": nulls,
                        "channels": [c["unit"] for c in series],
                    },
                )
            except Exception as exc:
                # never swallow the reason: a silent `invalid` counter hid 737 rejects
                # behind one number on the first paragraph-unit build
                stat["invalid"] += 1
                _why(stat, f"{type(exc).__name__}: {str(exc)[:110]}")
                continue
            errs = local_check(rec)
            if errs:
                stat["invalid"] += 1
                for e in errs:
                    _why(stat, e[:90])
                continue
            records.append(rec)
            stat["emitted"] += 1

    stat["releases_usable"] = len(months)
    stat["span"] = f"{months[0]} .. {months[-1]}" if months else "—"
    stat["recites"] = sum(1 for r in records if r["alignment"] == "recites")
    stat["describes"] = sum(1 for r in records if r["alignment"] == "describes")
    if maxrec is None:
        reconcile(stat, len(topics))
    return records, stat


def reconcile(stat: Dict[str, Any], n_topics: int) -> None:
    """The scanned months and the scanned paragraphs must both balance, or the build is lying."""
    scanned = stat["months_scanned"]
    accounted = (stat["releases_usable"] + stat["no_release"] + stat["no_text_layer"]
                 + stat["no_table"] + stat["no_narrative"])
    if scanned != accounted:
        raise SystemExit(f"reconcile FAILED on months: {scanned} scanned vs {accounted}")
    paras = stat["paragraphs_scanned"]
    used = (stat["emitted"] + stat["para_no_indicator"] + stat["para_is_summary"]
            + stat["short_paragraph"] + stat["short_window"]
            + stat["para_no_usable_indicator"] + stat["invalid"])
    if paras != used:
        raise SystemExit(f"reconcile FAILED on paragraphs: {paras} scanned vs {used}")
    stat["reconcile"] = (f"{scanned} months = {accounted} \u2713 ; "
                         f"{paras} paragraphs = {used} \u2713")


def _why(stat: Dict[str, Any], reason: str) -> None:
    """Tally a reject reason so `invalid` is never an opaque number."""
    stat.setdefault("invalid_reasons", {})
    stat["invalid_reasons"][reason] = stat["invalid_reasons"].get(reason, 0) + 1


def local_check(rec: dict) -> List[str]:
    e = []
    if rec["text"].count("<ts></ts>") != 1:
        e.append("ts token count")
    lens = {len(c["values"]) for c in rec["timeseries"]}
    if len(lens) != 1:
        e.append(f"channel lengths differ: {sorted(lens)}")
    if len({c["unit"] for c in rec["timeseries"]}) != len(rec["timeseries"]):
        e.append("duplicate unit")
    return e


def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    d, out_cfg = cfg["data"], cfg["output"]
    records, stats = build(cfg)
    report = {"survey": d["survey_title"], "bank": d["bank"],
              "record_unit": "(release month x narrative paragraph)",
              "series": "as-first-published vintage, stitched from each release's own table",
              "window": f"trailing {d['trailing_window_months']} months, no gaps",
              "indicators": [tp["tag"] for tp in d["topics"]],
              "stats": stats, "config_snapshot": cfg, "dry_run": dry}

    if dry:
        if records:
            print("\n--- sample record ---")
            r0 = dict(records[0])
            r0["timeseries"] = [{**c, "values": c["values"][:4] + ["…"]} for c in r0["timeseries"]]
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:3400])
        print("\n" + json.dumps(stats, indent=2))
        return report

    op = rp(out_cfg["output_path"]); op.parent.mkdir(parents=True, exist_ok=True)
    with op.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if records and out_cfg.get("samples_path"):
        sp = rp(out_cfg["samples_path"]); sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("w", encoding="utf-8") as fh:
            json.dump(records[:3], fh, ensure_ascii=False, indent=2); fh.write("\n")
    rpath = rp(out_cfg["report_path"]); rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Build Philadelphia MBOS → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s['emitted']} records from {s['releases_usable']} releases "
          f"({s['span']}) — {s['recites']} recites / {s['describes']} describes.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
