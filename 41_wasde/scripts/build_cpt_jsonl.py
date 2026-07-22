#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from USDA WASDE reports.

One record = (commodity x release month): the release's per-commodity prose block (which
recites the balance-sheet figures) paired with a trailing window of that attribute's monthly
forecast vintages for the report's HEADLINE marketing year. alignment: recites; text real.

Series : report XML (structured) — Report[@sub_report_title] -> attribute -> market_year ->
         forecast_month -> Cell[@cell_value]. We take, per report, the "this-month" value
         (forecast_month == the report's own month) for the headline marketing year, and stitch
         across reports chronologically. Tracking a single marketing year avoids the sawtooth at
         new-crop transitions.
Text   : report PDF (pdftotext) — the per-commodity narrative block (e.g. "WHEAT: ...").

Reports are read from a LOCAL folder (the WASDE archive list is JS-gated, so headless full
enumeration is blocked — see README). Supply wasde{MMYY}.xml + wasde{MMYY}.pdf per report.

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
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
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
_MONTH_FULL = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
               "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12}


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


def this_month_value(rows, title_match, attribute, report_ym, market_year) -> Optional[float]:
    """The report's own-month projection for (title, attribute, market_year)."""
    want_abbr = _MONTH_ABBR[int(report_ym[5:7]) - 1]
    for ti, at, my, fm, val in rows:
        if title_match in ti and at == attribute and _norm_my(my) == market_year and fm == want_abbr:
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
    # keep well-formed sentences (drop stray page/table fragments)
    sents = re.findall(r"[A-Z][^.]{15,600}?\.(?=\s|$)", block)
    out = " ".join(s.strip() for s in sents if re.search(r"[a-z]{3,}", s))
    return out or None


# --- report discovery -------------------------------------------------------

def find_pdf(report_ym: str, pdf_dirs) -> Optional[Path]:
    mmyy = f"{report_ym[5:7]}{report_ym[2:4]}"
    cands = []
    for d in pdf_dirs:
        base = rp(d)
        cands += [base / f"wasde{mmyy}.pdf", base / f"wasde{mmyy}v2.pdf",
                  base / f"{report_ym}.pdf"]
    for c in cands:
        if c.exists():
            return c
    return None


# --- pipeline ---------------------------------------------------------------

def build(cfg) -> Tuple[List[dict], Dict[str, Any]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    maxrec = out_cfg.get("max_records")
    win_max, min_series = int(d["window_max"]), int(d["min_series"])

    # parse every report XML once
    xmls = sorted(glob.glob(str(rp(d["data_dir"]) / "*.xml")))
    reports: Dict[str, dict] = {}   # ym -> {rows, pdf}
    for x in xmls:
        ym, rows = parse_report_xml(Path(x))
        if not ym:
            continue
        reports[ym] = {"rows": rows, "pdf": find_pdf(ym, d["pdf_dirs"])}
    months = sorted(reports)

    stat = {"reports": len(months), "candidates": 0, "emitted": 0,
            "no_pdf": 0, "no_prose": 0, "short_series": 0, "no_value": 0, "invalid": 0}
    records: List[dict] = []

    for com in d["commodities"]:
        tm, attr = com["title_match"], com["attribute"]
        for i, ym in enumerate(months):
            if maxrec is not None and len(records) >= int(maxrec):
                break
            rows = reports[ym]["rows"]
            my = headline_my(rows, tm, attr)
            if not my:
                continue
            stat["candidates"] += 1
            # series = this-month value of `my` across all reports up to ym that carry it
            series, series_months = [], []
            for pm in months[: i + 1]:
                v = this_month_value(reports[pm]["rows"], tm, attr, pm, my)
                if v is not None:
                    series.append(v)
                    series_months.append(pm)
            series, series_months = series[-win_max:], series_months[-win_max:]
            if len(series) < min_series:
                stat["short_series"] += 1
                continue
            if this_month_value(rows, tm, attr, ym, my) is None:
                stat["no_value"] += 1
                continue
            pdf = reports[ym]["pdf"]
            if not pdf:
                stat["no_pdf"] += 1
                continue
            prose = prose_block(pdf_text(pdf), com["prose_start"], com["prose_end"])
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
                    alignment="recites",
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
                        "marketing_year": my,
                        "series_note": "monthly forecast vintages (this-month projection per report)",
                        "report_month": ym,
                        "vintage_months": series_months,
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
