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


def _to_float(v: str) -> Optional[float]:
    try:
        return float(v.replace(",", "").rstrip("*").strip())
    except ValueError:
        return None


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
    for ti, at, my, fm, val in rows:
        if title_match in ti and at == attribute and _norm_my(my) == market_year and fm == want:
            f = _to_float(val)
            if f is not None:
                return f
    return None


def headline_my(rows, title_match, attribute) -> Optional[str]:
    """The newest 'Proj.' marketing year present for this commodity/attribute."""
    proj = [_norm_my(my) for ti, at, my, fm, val in rows
            if title_match in ti and at == attribute and "Proj." in my]
    return max(proj) if proj else None


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

def _http(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL).read()


def enumerate_releases(d: dict) -> List[Tuple[str, str, Optional[str]]]:
    """Paginate the ESMIS API for WASDE releases. Return [(report_ym, xml_url, pdf_url)] for
    releases that ship an .xml (the machine-readable ~2010→ era), newest-first."""
    base, ident = d["api_base"], d["publication_identifier"]
    delay = float(d.get("request_delay_s", 0.2))
    out: List[Tuple[str, str, Optional[str]]] = []
    page = 0
    while True:
        url = f"{base}/release/findByIdentifier/{ident}?page={page}"
        try:
            doc = json.loads(_http(url))
        except Exception as e:
            print(f"  API page {page} failed: {e}", file=sys.stderr)
            break
        results = doc.get("results", [])
        for r in results:
            files = r.get("files", []) or []
            xml_url = next((f for f in files if f.endswith(".xml")), None)
            if not xml_url:
                continue  # PDF-only (pre-~2010 scan); skip until OCR tier
            pdf_url = next((f for f in files if f.endswith(".pdf")), None)
            dt = (r.get("release_datetime") or "")[:7]  # YYYY-MM
            if re.match(r"\d{4}-\d{2}", dt):
                out.append((dt, xml_url, pdf_url))
        pager = doc.get("pager", {})
        if page >= int(pager.get("total_pages", 1)) - 1 or not results:
            break
        page += 1
        time.sleep(delay)
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

def build(cfg) -> Tuple[List[dict], Dict[str, Any]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    maxrec = out_cfg.get("max_records")
    win_max, min_series = int(d["window_max"]), int(d["min_series"])

    # enumerate releases via the ESMIS API, fetch xml (series) + pdf (prose) into the cache
    cache = rp(d["cache_dir"])
    delay = float(d.get("request_delay_s", 0.2))
    releases = enumerate_releases(d)
    maxr = d.get("max_reports")
    if maxr is not None:
        releases = releases[: int(maxr)]           # newest N
    reports: Dict[str, dict] = {}   # ym -> {rows, pdf}
    for ym, xml_url, pdf_url in releases:
        xmlp = download_cached(xml_url, cache / f"{ym}.xml", delay)
        if not xmlp:
            continue
        _, rows = parse_report_xml(xmlp)
        pdfp = download_cached(pdf_url, cache / f"{ym}.pdf", delay) if pdf_url else None
        reports[ym] = {"rows": rows, "pdf": pdfp}
    months = sorted(reports)

    stat = {"reports": len(months), "candidates": 0, "emitted": 0,
            "no_pdf": 0, "no_prose": 0, "short_series": 0, "no_value": 0, "invalid": 0}
    records: List[dict] = []

    text_cache: Dict[str, str] = {}   # memoize pdftotext per report (else 1 call per commodity×report)

    def report_text(ym: str) -> str:
        if ym not in text_cache:
            pdf = reports[ym]["pdf"]
            text_cache[ym] = pdf_text(pdf) if pdf else ""
        return text_cache[ym]

    for com in d["commodities"]:
        tm, attr = com["title_match"], com["attribute"]
        style = com.get("month_style", "abbr")
        for i, ym in enumerate(months):
            if maxrec is not None and len(records) >= int(maxrec):
                break
            rows = reports[ym]["rows"]
            my = headline_my(rows, tm, attr)   # endpoint report's headline MY (label + endpoint)
            if not my:
                continue
            stat["candidates"] += 1
            # CONTINUOUS series: for each prior report, take ITS OWN then-current (headline)
            # marketing-year this-month projection, stitched chronologically. Crosses new-crop
            # transitions by design (real regime steps); endpoint == this report's headline value.
            series, series_months, series_mys = [], [], []
            for pm in months[: i + 1]:
                pm_rows = reports[pm]["rows"]
                pm_my = headline_my(pm_rows, tm, attr)
                if not pm_my:
                    continue
                v = this_month_value(pm_rows, tm, attr, pm, pm_my, style)
                if v is not None:
                    series.append(v)
                    series_months.append(pm)
                    series_mys.append(pm_my)
            series = series[-win_max:]
            series_months = series_months[-win_max:]
            series_mys = series_mys[-win_max:]
            if len(series) < min_series:
                stat["short_series"] += 1
                continue
            if this_month_value(rows, tm, attr, ym, my, style) is None:
                stat["no_value"] += 1
                continue
            if not reports[ym]["pdf"]:
                stat["no_pdf"] += 1
                continue
            prose = prose_block(report_text(ym), com["prose_start"], com["prose_end"])
            if not prose or len(prose) < 120:
                stat["no_prose"] += 1
                continue

            attr_label = attr.lower()
            intro = t["ts_intro"].format(my=my, commodity=com["key"], attr_label=attr_label,
                                         n=len(series), month=ym)
            text = f"{prose}\n\n{intro}"
            values = [round(v, 3) for v in series]
            try:
                rec = emit_record(
                    text=text,
                    timeseries=[{"values": values, "unit": com["channel"], "freq": "1m"}],
                    alignment=com.get("alignment", "recites"),
                    license="public-domain-us-gov",
                    source=d["source_url"],
                    dataset="wasde",
                    series_id=f"wasde_{com['key']}_{my.replace('/', '')}_{ym}",
                    domain="agriculture",
                    region="US",
                    period_start=f"{series_months[0]}-01",
                    period_end=f"{ym}-01",
                    meta={
                        "commodity": com["key"],
                        "attribute": attr,
                        "marketing_year": my,   # endpoint (headline) marketing year, recited by prose
                        "series_note": ("continuous monthly current-marketing-year this-month "
                                        "projection; crosses new-crop transitions (real regime steps)"),
                        "report_month": ym,
                        "vintage_months": series_months,
                        "vintage_marketing_years": series_mys,
                        "marketing_years_spanned": sorted(set(series_mys)),
                        "new_crop_resets": len(set(series_mys)) - 1,
                        "window": len(series),
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
    print(f"\nDone: {s['emitted']} records from {s['reports']} reports "
          f"(short_series={s['short_series']}, no_pdf={s['no_pdf']}, no_prose={s['no_prose']}, "
          f"invalid={s['invalid']}).", file=sys.stderr)


if __name__ == "__main__":
    main()
