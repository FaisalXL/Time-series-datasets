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
import tabex                                                        # noqa: E402
import txtex                                                        # noqa: E402

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


def fetch_release(d: dict, ym: str, cache: Path) -> Tuple[Optional[str], Optional[bytes]]:
    """One month's release document as ('pdf'|'txt', bytes).

    Order: the live media tree, then the Bank's era archive, then the retired
    plain-text layout via Wayback. The last is not a nicety — it is the only source
    for 1997-2001, whose archive PDFs are image scans.
    """
    rel = cache / "releases"
    rel.mkdir(parents=True, exist_ok=True)
    fp, tp, miss = rel / f"{ym}.pdf", rel / f"{ym}.txt", rel / f"{ym}.missing"
    if fp.exists():
        raw = fp.read_bytes()
        if raw.startswith(b"%PDF") and has_text_layer(raw):
            return "pdf", raw
    elif not miss.exists():
        raw = b""
        yr, mon = ym.split("-")
        if ym >= "2008-01":                         # the live media tree starts here
            url = d["pdf_url_template"].format(year=yr, mmyy=f"{mon}{yr[2:]}")
            try:
                raw = http_get(url, d["user_agent"], int(d["timeout_s"]))
            except Exception:
                raw = b""
            time.sleep(float(d.get("request_delay_s", 0.4)))
        if not raw.startswith(b"%PDF"):             # soft-404 HTML shell, or pre-2008
            hit = archive_index(d, cache).get(ym)
            if hit:
                with zipfile.ZipFile(hit[0]) as zf:
                    raw = zf.read(hit[1])
        if raw.startswith(b"%PDF"):
            fp.write_bytes(raw)
            if has_text_layer(raw):
                return "pdf", raw
        else:
            miss.write_text("no release PDF on the media tree or in the era archives\n")
    # PDF absent, or present but scanned -> the retired plain-text layout
    txt = fetch_wayback_text(d, ym, cache) if d.get("wayback_enabled", True) else None
    if txt:
        return "txt", txt
    return (None, None) if not fp.exists() else ("scan", None)


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


def narrative(raw: bytes) -> List[str]:
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

def harvest(cfg: Dict[str, Any]) -> Tuple[Dict[str, dict], Dict[str, List[str]], Dict[str, int]]:
    """Every release month -> its as-published table and its narrative paragraphs."""
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
        kind, raw = fetch_release(d, ym, cache)
        if kind is None:
            stat["no_release"] += 1
            continue
        if kind == "scan":
            # a release exists but only as an image scan and Wayback never captured the
            # text version: pdfplumber sees zero words. OCR tier, and this is where the
            # package's exhaustion wall sits.
            stat["no_text_layer"] += 1
            continue
        table = tabex.parse_table(raw) if kind == "pdf" else txtex.parse_table(raw)
        if not table:
            stat["no_table"] += 1
            continue
        paras = narrative(raw) if kind == "pdf" else txtex.paragraphs(raw)
        if not paras:
            stat["no_narrative"] += 1
            continue
        stat["src_" + kind] = stat.get("src_" + kind, 0) + 1
        vintage[ym] = table
        prose[ym] = paras
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
    stat.update({"topic_no_prose": 0, "topic_no_table_row": 0, "topic_failed_identity": 0,
                 "topic_no_anchor": 0, "short_snippet": 0, "short_window": 0,
                 "sparse_topic_window": 0, "clause_quotes_other_series": 0,
                 "invalid": 0, "emitted": 0})
    months = sorted(vintage)
    have = set(months)

    records: List[dict] = []
    for ym in reversed(months):                     # newest first, so a demo cap is recent
        if maxrec is not None and len(records) >= int(maxrec):
            break
        window = trailing(have, ym, win, max_gap, recent)
        sentences = [s for p in prose[ym] for s in split_sentences(p)]
        # a sentence naming 3+ indicators is a release summary, not this topic's own prose
        keep = [s for s in sentences
                if sum(1 for tp in topics if sentence_matches(s, tp["match"])) <= max_topics]
        for tp in topics:
            if maxrec is not None and len(records) >= int(maxrec):
                break
            row = vintage[ym].get(tp["tag"])
            if row is None:
                stat["topic_no_table_row"] += 1
                continue
            half = "cur" if tp["anchor"].startswith("cur") else "fut"
            if not tabex.check(row, half):
                stat["topic_failed_identity"] += 1
                continue
            anchor = row.get(tp["anchor"])
            if anchor is None:
                stat["topic_no_anchor"] += 1
                continue
            if not window:
                stat["short_window"] += 1
                continue

            # The month-level window test is not enough: a month can be usable and
            # still have no row for THIS indicator (the pre-2003 table omits capital
            # expenditures' current half, some eras drop inventories), so the anchor's
            # own gaps have to be counted before the record is allowed out.
            anchor_vals = [vintage.get(m, {}).get(tp["tag"], {}).get(tp["anchor"])
                           for m in window]
            if (anchor_vals[-1] is None
                    or any(v is None for v in anchor_vals[-recent:])
                    or sum(1 for v in anchor_vals if v is None) > max_gap):
                stat["sparse_topic_window"] += 1
                continue

            series, nulls = [], 0
            for c in chans:
                vals = [vintage.get(m, {}).get(tp["tag"], {}).get(c["cell"]) for m in window]
                n_null = sum(1 for v in vals if v is None)
                if vals[-1] is None or n_null > max_gap:
                    continue
                series.append({"values": [None if v is None else round(v, 3) for v in vals],
                               "unit": c["unit"], "freq": "1M"})
                nulls += n_null
            if not any(c["unit"].startswith("diffusion") for c in series):
                stat["sparse_topic_window"] += 1
                continue

            # the release's own words about THIS indicator, cut back to the clauses that
            # are about it, then held to one rule: every figure the text quotes must be a
            # value this record's series actually contains
            in_series = {f"{v:.1f}" for c in series for v in c["values"] if v is not None}
            picked = []
            for sent in keep:
                if not sentence_matches(sent, tp["match"]):
                    continue
                cl = clauses_for(sent, tp["tag"], topics)
                if cl and all(n in in_series for n in _FIGURE.findall(cl)):
                    picked.append(cl)
                elif cl:
                    stat["clause_quotes_other_series"] += 1
            snippet = " ".join(picked)
            if not snippet:
                stat["topic_no_prose"] += 1
                continue
            if len(snippet) < int(t.get("min_snippet_chars", 120)):
                stat["short_snippet"] += 1
                continue

            # no generated text: the release's own sentences, then the splice point
            text = f"{snippet}\n\n<ts></ts>"
            alignment = "recites" if states(snippet, anchor) else "describes"

            try:
                rec = emit_record(
                    text=text,
                    timeseries=series,
                    alignment=alignment,
                    license="public-domain-us-gov",
                    text_source="first_party_official",
                    source=d["source_url"],
                    dataset="philadelphia_mbos",
                    series_id=f"mbos_{ym}_{tp['tag']}",
                    domain="macro_econ",
                    region="US-PA",
                    period_start=f"{window[0]}-01",
                    period_end=f"{ym}-01",
                    meta={
                        "bank": d["bank"],
                        "survey": d["survey_title"],
                        "district": d.get("district"),
                        "sector": d["domain"],
                        "release_month": ym,
                        "topic": tp["tag"],
                        "anchor_cell": tp["anchor"],
                        "anchor_value_as_published": anchor,
                        "series_vintage": "as_first_published",
                        "window_start": window[0],
                        "n_points": len(window),
                        "n_null_points": nulls,
                        "channels": [c["unit"] for c in series],
                    },
                )
            except ValueError:
                stat["invalid"] += 1
                continue
            if local_check(rec):
                stat["invalid"] += 1
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
    """The scanned months and the topic units must both balance, or the build is lying."""
    scanned = stat["months_scanned"]
    accounted = (stat["releases_usable"] + stat["no_release"] + stat["no_text_layer"]
                 + stat["no_table"] + stat["no_narrative"])
    if scanned != accounted:
        raise SystemExit(f"reconcile FAILED on months: {scanned} scanned vs {accounted}")
    units = stat["releases_usable"] * n_topics
    used = (stat["emitted"] + stat["topic_no_prose"] + stat["topic_no_table_row"]
            + stat["topic_failed_identity"] + stat["topic_no_anchor"]
            + stat["short_snippet"] + stat["short_window"]
            + stat["sparse_topic_window"] + stat["invalid"])
    if units != used:
        raise SystemExit(f"reconcile FAILED on topic units: {units} available vs {used}")
    stat["reconcile"] = f"{scanned} months = {accounted} ✓ ; {units} topic units = {used} ✓"


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
              "record_unit": "(release month x indicator)",
              "series": "as-first-published vintage, stitched from each release's own table",
              "window": f"trailing {d['trailing_window_months']} months, no gaps",
              "topics": [tp["tag"] for tp in d["topics"]],
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
