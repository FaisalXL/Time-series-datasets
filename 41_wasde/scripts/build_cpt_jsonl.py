#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from USDA WASDE reports.

One record = (commodity x release month): the release's per-commodity prose block (which
recites the balance-sheet figures) paired with a trailing window of the CONTINUOUS monthly
current-marketing-year projection for that attribute. alignment: recites; text real.

Series : report XML (structured) — Report[@sub_report_title] -> attribute -> market_year ->
         forecast_month -> Cell[@cell_value]. Per report we take the "this-month" value
         (forecast_month == the report's own month) for that report's THEN-CURRENT (headline
         "Proj.") marketing year, and stitch across reports chronologically into a continuous
         monthly line. It deliberately crosses new-crop transitions (a real regime step, e.g.
         938 -> 762) so the series is a long monthly signal, not a ~12-point single-crop stub.
         The endpoint is the report's own headline figure (which its prose recites).
Text   : report PDF (pdftotext) — the per-commodity narrative block (e.g. "WHEAT: ...").

Reports are enumerated + fetched from the ESMIS REST API (release/findByIdentifier/wasde),
which lists every WASDE release with its .xml (series) + .pdf (prose) URLs — see README.

Examples:
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE
_UA = "cpt-dataset-builder/1.0 (research; flnu@usc.edu)"
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required. pip install -r requirements.txt") from exc

# shared v1-compliant record builder (self-validates against schema/validate.py)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_NUM = {m: i + 1 for i, m in enumerate(_MONTH_ABBR)}
_MONTH_FULL_NAMES = ["January", "February", "March", "April", "May", "June",
                     "July", "August", "September", "October", "November", "December"]
_MONTH_FULL = {m: i + 1 for i, m in enumerate(_MONTH_FULL_NAMES)}


# --- config helpers (same conventions as the other packages) ---------------

def deep_merge(base, over):
    m = dict(base)
    for k, v in over.items():
        m[k] = deep_merge(m[k], v) if k in m and isinstance(m[k], dict) and isinstance(v, dict) else v
    return m


def coerce(raw: str):
    low = raw.strip().lower()
    if low in {"true", "yes"}: return True
    if low in {"false", "no"}: return False
    if low in {"null", "none", "~"}: return None
    if re.fullmatch(r"-?\d+", raw): return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw): return float(raw)
    return raw


def parse_sets(sets: Sequence[str]):
    out: Dict[str, Any] = {}
    for it in sets:
        k, v = it.split("=", 1)
        cur = out
        for p in k.split(".")[:-1]:
            cur = cur.setdefault(p, {})
        cur[k.split(".")[-1]] = coerce(v)
    return out


def load_config(path: Path, sets):
    cfg = yaml.safe_load(path.read_text())
    return deep_merge(cfg, parse_sets(sets)) if sets else cfg


def rp(s: str) -> Path:
    p = Path(s)
    return p if p.is_absolute() else ROOT / p


# --- XML series parse -------------------------------------------------------

def _strip(tag: str) -> str:
    return tag.split("}")[-1]


def parse_report_xml(path: Path) -> Tuple[Optional[str], List[Tuple[str, str, str, str, str]]]:
    """Return (report_ym 'YYYY-MM', rows) where each row is
    (sub_report_title, attribute, market_year, forecast_month, value)."""
    root = ET.parse(str(path)).getroot()
    parent = {c: p for p in root.iter() for c in p}

    def up(el, pred):
        n = el
        while n is not None:
            for k, v in n.attrib.items():
                if pred(_strip(k)) and v and v.strip():
                    return v.strip()
            n = parent.get(n)
        return None

    rmonth = None
    rows: List[Tuple[str, str, str, str, str]] = []
    for cell in root.iter():
        if _strip(cell.tag) != "Cell":
            continue
        val = next((x for k, x in cell.attrib.items()
                    if "cell_value" in _strip(k) and x and x.strip()), None)
        if not val:
            continue
        fm = up(cell, lambda k: "forecast_month" in k)
        my = up(cell, lambda k: "market_year" in k)
        at = up(cell, lambda k: k.startswith("attribute") and k[-1].isdigit())
        ti = up(cell, lambda k: k == "sub_report_title")
        if rmonth is None:
            rm = up(cell, lambda k: k == "Report_Month")   # e.g. "July 2026"
            if rm:
                parts = rm.split()
                if len(parts) == 2 and parts[0] in _MONTH_FULL:
                    rmonth = f"{int(parts[1]):04d}-{_MONTH_FULL[parts[0]]:02d}"
        if ti and at and my and fm:
            rows.append((ti, at, my, fm, val.strip()))
    return rmonth, rows


def _norm_my(my: str) -> str:
    """'2026/27 Proj.' / '2025/26 Est.' / '2024/25' -> '2026/27'."""
    m = re.match(r"(\d{4}/\d{2})", my)
    return m.group(1) if m else my.strip()


def _norm_attr(s: str) -> str:
    """Normalize a balance-sheet attribute label for matching: collapse whitespace/newlines and
    strip trailing footnote markers ('Beginning Stocks 2/', 'Exports, Total 4/ 6/' -> 'Beginning
    Stocks', 'Exports, Total'). Footnote numbers drift across years, so we never match on them."""
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+\d+/.*$", "", s)   # drop " 2/", " 4/ 6/ 9/", etc.
    return s.strip()


def _to_float(v: str) -> Optional[float]:
    try:
        return float(v.replace(",", "").rstrip("*").strip())
    except ValueError:
        return None


def endpoint_recited(prose: str, ep: float) -> bool:
    """True iff the series endpoint value appears as a number in the prose — comma/decimal/billions
    aware. WASDE narratives recite the level for some (commodity, attribute, era) combos and state
    only the month-over-month change for others (esp. older wheat/corn), so alignment is tagged PER
    RECORD: 'recites' when the endpoint is stated, else 'describes' (the block still describes the
    commodity's balance sheet). Small bare integers (<10) are ignored to avoid false positives."""
    forms = set()
    iv = int(round(ep))
    if abs(ep) >= 10:
        forms.add(f"{iv}")
        forms.add(f"{iv:,}")
    if abs(ep) >= 1000:                      # billions notation ("1.881 billion")
        b = ep / 1000.0
        for dec in (3, 2, 1):
            forms.add(f"{b:.{dec}f} billion")
    forms.add(f"{ep:.1f}")                    # one-decimal (cotton 4.1, rice 30.9)
    forms.add(f"{ep:.2f}")
    forms.add(f"{round(ep, 1):g}")
    return any(re.search(r"(?<![\d.,])" + re.escape(f) + r"(?![\d])", prose, re.I) for f in forms)


def this_month_value(rows, title_match, attribute, report_ym, market_year,
                     month_style="abbr") -> Optional[float]:
    """The report's own-month projection for (title, attribute, market_year).

    WASDE tables can stack two measure panels under one sub_report_title, keyed by the
    forecast-month spelling: abbreviated ("Jul") vs full ("July"). For the combined
    "Feed Grain and Corn" table the abbreviated panel is feed-grain METRIC TONS and the full
    panel is corn BUSHELS; for wheat/soybeans the abbreviated panel is the one the prose
    recites. So each commodity declares which style to read (config `month_style`)."""
    mn = int(report_ym[5:7])
    want = _MONTH_ABBR[mn - 1] if month_style == "abbr" else _MONTH_FULL_NAMES[mn - 1]
    attr_n = _norm_attr(attribute)
    for ti, at, my, fm, val in rows:
        if title_match in ti and _norm_attr(at) == attr_n and _norm_my(my) == market_year and fm == want:
            f = _to_float(val)
            if f is not None:
                return f
    return None


def headline_my(rows, title_match, attribute) -> Optional[str]:
    """The newest 'Proj.' marketing year present for this commodity/attribute."""
    attr_n = _norm_attr(attribute)
    proj = [_norm_my(my) for ti, at, my, fm, val in rows
            if title_match in ti and _norm_attr(at) == attr_n and "Proj." in my]
    return max(proj) if proj else None


# --- TXT series parse (1995–2009 machine-readable text reports) -------------
# Older WASDE releases ship no .xml but a plain-text .txt (both narrative + fixed-width tables).
# Table layout is stable: 4 numeric columns (two historical MYs, then the headline MY's last-month
# and this-month projections). The LAST numeric column is the report's own-month projection; the
# headline MY is the "YYYY/YY Projections" header. Combined tables (Feed Grain & Corn) stack a
# metric-tons panel then a BUSHELS panel under a subsection marker ("CORN") — txt_subsection picks it.

def _txt_norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _txt_table_region(lines: List[str], txt_title: str) -> Optional[Tuple[int, int]]:
    """(start, end) line span of the U.S. `txt_title` table (excludes World tables)."""
    start = None
    for i, l in enumerate(lines):
        nl = _txt_norm(l)
        if txt_title in nl and "World" not in nl and re.search(r"U\.?\s*S\.", nl):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 3, len(lines)):
        nl = _txt_norm(lines[j])
        if "Supply and Use" in nl and re.search(r"(U\.?\s*S\.|World)", nl):
            end = j
            break
    return (start, end)


def txt_extract(text: str, txt_title: str, txt_subsection: Optional[str],
                txt_label: str) -> Tuple[Optional[str], Optional[float]]:
    """(headline_my, this_month_value) from a .txt report's U.S. `txt_title` table.
    this-month = last numeric column; headline MY = 'YYYY/YY Projections' header. A subsection
    marker (e.g. 'CORN') narrows to the bushels panel of a stacked metric/bushels table."""
    lines = text.splitlines()
    reg = _txt_table_region(lines, txt_title)
    if not reg:
        return (None, None)
    region = lines[reg[0]:reg[1]]
    my = None
    for l in region[:12]:
        m = re.search(r"(\d{4}/\d{2})\s+Projections", l)
        if m:
            my = m.group(1)
            break
    body = region
    if txt_subsection:
        for k, l in enumerate(region):
            s = l.strip()
            if s.upper().startswith(txt_subsection.upper()) and not re.search(r"\d", s):
                body = region[k:]
                break
    val = None
    want = _norm_attr(txt_label).lower()
    for l in body:
        if ":" not in l:
            continue
        lab = _norm_attr(l.split(":")[0]).lower()   # footnote-tolerant ("Beginning stocks 2/" -> "beginning stocks")
        if lab and lab.startswith(want):            # tolerant of ", total" suffix (corn "Ending stocks, total")
            rhs = ":".join(l.split(":")[1:])
            ns = re.findall(r"-?\d[\d,]*\.?\d*", rhs)
            if ns:
                val = _to_float(ns[-1])   # last numeric column == this-month projection
            break
    return (my, val)


# --- prose (PDF) ------------------------------------------------------------

def pdf_text(path: Path) -> str:
    p = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.decode("utf-8", "replace")


def prose_block(txt: str, start: str, end: str) -> Optional[str]:
    i = txt.find(start)
    if i < 0:
        return None
    j = txt.find(end, i + len(start))
    block = txt[i: j if j > 0 else i + 4000]
    block = re.sub(r"\s+", " ", block).strip()
    block = re.sub(r"\s*WASDE\s*-\s*\d+\s*-\s*\d+\s*", " ", block)  # strip page-break footers ("WASDE-673-5")
    # split on real sentence boundaries (punct + space + capital) so decimals ("1.881 billion")
    # and dates ("2026/27") stay intact; keep prose-like sentences, drop stray fragments.
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", block)]
    out = " ".join(s for s in sents if 15 <= len(s) <= 700 and re.search(r"[a-z]{3,}", s))
    return out or None


# --- report discovery: ESMIS REST API --------------------------------------

def _http(url: str, timeout: int = 60, tries: int = 4) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    for i in range(tries):
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=_SSL).read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))   # transient 5xx/timeout backoff


def enumerate_releases(d: dict) -> List[Tuple[str, str, str, Optional[str]]]:
    """Paginate the ESMIS API for WASDE releases. Return [(report_ym, kind, series_url, prose_url)]
    newest-first. kind='xml' when a release ships structured .xml (2010→ era; series from .xml,
    prose from .pdf). Else, when use_txt_tier is on, kind='txt' for releases with a machine-readable
    .txt (1995–2009; series + prose both from the one .txt). Pre-1995 pdf-only scans are skipped
    (OCR tier)."""
    base, ident = d["api_base"], d["publication_identifier"]
    delay = float(d.get("request_delay_s", 0.2))
    use_txt = bool(d.get("use_txt_tier", True))
    txt_min = str(d.get("txt_min_ym", "1995-01"))
    out: List[Tuple[str, str, str, Optional[str]]] = []
    page = 0
    total_pages = None
    failed_pages = 0
    while total_pages is None or page < total_pages:
        url = f"{base}/release/findByIdentifier/{ident}?page={page}"
        try:
            doc = json.loads(_http(url))
        except Exception as e:
            # skip this page but keep paginating (don't abort the whole archive on one 5xx)
            print(f"  API page {page} failed after retries, skipping: {e}", file=sys.stderr)
            failed_pages += 1
            page += 1
            if total_pages is None:      # couldn't even get page 0 to learn the count
                if page > 30:
                    break
            time.sleep(delay)
            continue
        total_pages = int(doc.get("pager", {}).get("total_pages", total_pages or 1))
        results = doc.get("results", [])
        for r in results:
            files = r.get("files", []) or []
            dt = (r.get("release_datetime") or "")[:7]  # YYYY-MM
            if not re.match(r"\d{4}-\d{2}", dt):
                continue
            xml_url = next((f for f in files if f.endswith(".xml")), None)
            if xml_url:
                pdf_url = next((f for f in files if f.endswith(".pdf")), None)
                out.append((dt, "xml", xml_url, pdf_url))
            elif use_txt and dt >= txt_min:
                txt_url = next((f for f in files
                                if f.lower().endswith(".txt") and "readme" not in f.lower()), None)
                if txt_url:
                    out.append((dt, "txt", txt_url, txt_url))
        page += 1
        time.sleep(delay)
    if failed_pages:
        print(f"  NOTE: {failed_pages} API page(s) skipped after retries — coverage may be partial.",
              file=sys.stderr)
    out.sort(key=lambda x: x[0], reverse=True)   # newest first
    return out


def download_cached(url: str, dest: Path, delay: float) -> Optional[Path]:
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.write_bytes(_http(url))
        time.sleep(delay)
        return dest
    except Exception as e:
        print(f"  download failed {url}: {e}", file=sys.stderr)
        return None


# --- pipeline ---------------------------------------------------------------

ANCHOR_ATTR = "Ending Stocks"   # window is anchored on this line (present for every commodity)


def _spec_key(commodity: str, xml_name: str) -> str:
    return f"{commodity}|{xml_name}"


def _expand_specs(commodities) -> List[dict]:
    """Flatten config commodities × their channels into per-channel specs (used to precompute the
    per-report value of every balance-sheet line; the emit loop then bundles a commodity's channels
    into one multi-channel record)."""
    specs = []
    for com in commodities:
        for ch in com["channels"]:
            specs.append({
                "commodity": com["commodity"],
                "xml_title": com["xml_title"],
                "xml_month_style": com.get("xml_month_style", "abbr"),
                "txt_title": com.get("txt_title"),          # None => no .txt tier for this commodity
                "txt_subsection": com.get("txt_subsection"),
                "xml_name": ch["xml_name"],
                "txt_label": ch.get("txt_label", ch["xml_name"]),
                "key": _spec_key(com["commodity"], ch["xml_name"]),
            })
    return specs


def _humanize_channels(humans: List[str]) -> str:
    """['beginning stocks','production','exports'] -> 'beginning stocks, production, and exports'."""
    if len(humans) == 1:
        return humans[0]
    return ", ".join(humans[:-1]) + ", and " + humans[-1]


def build(cfg) -> Tuple[List[dict], Dict[str, Any]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    maxrec = out_cfg.get("max_records")
    win_max, min_series = int(d["window_max"]), int(d["min_series"])

    # enumerate releases via the ESMIS API; fetch series+prose per kind into the cache
    cache = rp(d["cache_dir"])
    delay = float(d.get("request_delay_s", 0.2))
    releases = enumerate_releases(d)
    maxr = d.get("max_reports")
    if maxr is not None:
        releases = releases[: int(maxr)]           # newest N
    reports: Dict[str, dict] = {}   # ym -> {"kind", "rows"/"txt", "pdf"}
    for ym, kind, series_url, prose_url in releases:
        if kind == "xml":
            xmlp = download_cached(series_url, cache / f"{ym}.xml", delay)
            if not xmlp:
                continue
            _, rows = parse_report_xml(xmlp)
            pdfp = download_cached(prose_url, cache / f"{ym}.pdf", delay) if prose_url else None
            reports[ym] = {"kind": "xml", "rows": rows, "pdf": pdfp}
        else:  # txt (series + prose share the one .txt)
            txtp = download_cached(series_url, cache / f"{ym}.txt", delay)
            if not txtp:
                continue
            reports[ym] = {"kind": "txt",
                           "txt": txtp.read_text(encoding="utf-8", errors="replace")}
    months = sorted(reports)
    specs = _expand_specs(d["commodities"])

    # per report, per series: (headline_my, this-month value). Precomputed once (else the
    # continuous inner loop would re-parse each report O(reports) times).
    def series_point(ym: str, spec: dict) -> Tuple[Optional[str], Optional[float]]:
        r = reports[ym]
        if r["kind"] == "xml":
            my = headline_my(r["rows"], spec["xml_title"], spec["xml_name"])
            if not my:
                return (None, None)
            return (my, this_month_value(r["rows"], spec["xml_title"], spec["xml_name"],
                                         ym, my, spec["xml_month_style"]))
        if not spec.get("txt_title"):
            return (None, None)   # commodity not wired for the .txt tier
        return txt_extract(r["txt"], spec["txt_title"], spec.get("txt_subsection"), spec["txt_label"])

    points: Dict[str, Dict[str, Tuple[Optional[str], Optional[float]]]] = {ym: {} for ym in months}
    for ym in months:
        for spec in specs:
            points[ym][spec["key"]] = series_point(ym, spec)

    prose_cache: Dict[str, str] = {}   # memoize prose source per report

    def report_prose_text(ym: str) -> str:
        if ym not in prose_cache:
            r = reports[ym]
            prose_cache[ym] = (pdf_text(r["pdf"]) if r.get("pdf") else "") if r["kind"] == "xml" else r["txt"]
        return prose_cache[ym]

    stat = {"reports": len(months), "reports_xml": sum(1 for y in months if reports[y]["kind"] == "xml"),
            "reports_txt": sum(1 for y in months if reports[y]["kind"] == "txt"),
            "series_specs": len(specs), "candidates": 0, "emitted": 0,
            "recites": 0, "describes": 0, "channels_emitted": 0,
            "no_prose": 0, "short_series": 0, "no_value": 0, "invalid": 0}
    records: List[dict] = []

    # ONE multi-channel record per (commodity, release month): the commodity's balance-sheet lines
    # bundled as channels under a single <ts>. Window anchored on Ending Stocks; each channel is that
    # line's continuous current-crop monthly projection over the SAME 24-month axis (index-aligned).
    for com in d["commodities"]:
        commodity = com["commodity"]
        channels = com["channels"]
        anchor_key = _spec_key(commodity, ANCHOR_ATTR)
        for i, ym in enumerate(months):
            if maxrec is not None and len(records) >= int(maxrec):
                break
            amy, aval = points[ym].get(anchor_key, (None, None))
            if not amy or aval is None:
                stat["no_value"] += 1
                continue
            stat["candidates"] += 1
            # window = trailing months where the anchor has a value (continuous, both eras)
            win_months, win_mys = [], []
            for pm in months[: i + 1]:
                pmy, pv = points[pm].get(anchor_key, (None, None))
                if pmy and pv is not None:
                    win_months.append(pm)
                    win_mys.append(pmy)
            win_months = win_months[-win_max:]
            win_mys = win_mys[-win_max:]
            if len(win_months) < min_series:
                stat["short_series"] += 1
                continue
            prose = prose_block(report_prose_text(ym), com["prose_start"], com["prose_end"])
            if not prose or len(prose) < 120:
                stat["no_prose"] += 1
                continue

            # build each channel over the window; keep only channels populated at EVERY window month
            # (so all kept channels are equal-length + index-aligned, as the schema requires)
            ts, used_humans, endpoints = [], [], []
            for ch in channels:
                k = _spec_key(commodity, ch["xml_name"])
                vals = [points[pm].get(k, (None, None))[1] for pm in win_months]
                if any(v is None for v in vals):
                    continue
                ts.append({"values": [round(v, 3) for v in vals], "unit": ch["channel"], "freq": "1m"})
                used_humans.append(ch["human"])
                endpoints.append(vals[-1])
            if not ts:
                stat["no_value"] += 1
                continue

            # alignment PER RECORD: recites if ANY channel's endpoint value is stated in the prose,
            # else describes (the block still describes the balance sheet).
            align = "recites" if any(endpoint_recited(prose, e) for e in endpoints) else "describes"
            stat["recites" if align == "recites" else "describes"] += 1
            stat["channels_emitted"] += len(ts)

            intro = t["ts_intro"].format(commodity=commodity, channels=_humanize_channels(used_humans),
                                         my=amy, n=len(win_months), month=ym)
            text = f"{prose}\n\n{intro}"
            try:
                rec = emit_record(
                    text=text,
                    timeseries=ts,                       # single <ts>, multi-channel balance sheet
                    alignment=align,
                    license="public-domain-us-gov",
                    source=d["source_url"],
                    dataset="wasde",
                    series_id=f"wasde_{commodity}_{amy.replace('/', '')}_{ym}",
                    domain="agriculture",
                    region="US",
                    period_start=f"{win_months[0]}-01",
                    period_end=f"{ym}-01",
                    meta={
                        "commodity": commodity,
                        "attributes": used_humans,
                        "n_channels": len(ts),
                        "marketing_year": amy,
                        "series_note": ("continuous monthly current-marketing-year balance-sheet projection "
                                        "(multi-channel); crosses new-crop transitions (real regime steps); "
                                        "stitched across the .txt (1995-2009) and .xml (2010+) eras"),
                        "report_month": ym,
                        "source_format": reports[ym]["kind"],
                        "vintage_months": win_months,
                        "vintage_marketing_years": win_mys,
                        "marketing_years_spanned": sorted(set(win_mys)),
                        "new_crop_resets": len(set(win_mys)) - 1,
                        "window": len(win_months),
                    },
                )
            except ValueError:
                stat["invalid"] += 1
                continue
            records.append(rec)
            stat["emitted"] += 1
    return records, stat


def run(cfg, dry: bool) -> Dict[str, Any]:
    import shutil
    if not shutil.which("pdftotext"):
        raise SystemExit("pdftotext not found. Install poppler (brew install poppler / apt-get install poppler-utils).")
    records, stats = build(cfg)
    report = {"dataset": "wasde", "stats": stats, "config_snapshot": cfg, "dry_run": dry}
    if dry:
        if records:
            r0 = dict(records[0]); r0["text"] = r0["text"][:700] + "…"
            print("\n--- sample record ---")
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:2400])
        print("\n" + json.dumps(stats, indent=2))
        return report
    op = rp(cfg["output"]["output_path"]); op.parent.mkdir(parents=True, exist_ok=True)
    with op.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if records and cfg["output"].get("samples_path"):
        sp = rp(cfg["output"]["samples_path"]); sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("w", encoding="utf-8") as fh:
            json.dump(records[:3], fh, ensure_ascii=False, indent=2); fh.write("\n")
    rpath = rp(cfg["output"]["report_path"]); rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    ap = argparse.ArgumentParser(description="Build USDA WASDE -> CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s['emitted']} multi-channel records from {s['reports']} reports "
          f"({s['reports_xml']} xml + {s['reports_txt']} txt), {s['channels_emitted']} channels total "
          f"[{s['recites']} recites + {s['describes']} describes] "
          f"(short_series={s['short_series']}, no_prose={s['no_prose']}, invalid={s['invalid']}).",
          file=sys.stderr)


if __name__ == "__main__":
    main()
