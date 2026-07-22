#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from EIA Weekly Petroleum Status Report Highlights.

One record = one weekly report: the WPSR "Highlights" narrative (which describes the
week's crude / gasoline / distillate inventories, refinery inputs, utilization and
imports) paired with a trailing 52-week window of the exact national supply series it
describes. The prose *describes* the series → "describes". text_quality = "real".

Text : WPSR Highlights PDF, one per release date from the archive index (~779 back to
       Aug 2011). PDF -> text via the `pdftotext` CLI (poppler). The data week-ending
       date parsed from the text anchors the series window.
Series: EIA bulk PET.zip (keyless, public domain), national weekly channels, back to
        ~1982/1990 — so every report gets a full 52-week window.
License: EIA data + text are U.S. Government works → public domain. No gate.

Examples:
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required. pip install -r requirements.txt") from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

# shared v1-compliant record builder (self-validates against schema/validate.py --strict)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}


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


# --- HTTP ------------------------------------------------------------------

def http_get(url: str, ua: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL).read()


def download_cached(url: str, dest: Path, ua: str, timeout: int) -> Path:
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url} -> {dest.name} (cached after first run)...", file=sys.stderr)
    dest.write_bytes(http_get(url, ua, timeout))
    return dest


# --- EIA bulk time series --------------------------------------------------

def load_series(cfg, cache: Path) -> Dict[str, Dict[str, float]]:
    """Parse bulk PET.zip → {series_id: {period 'YYYYMMDD': value}} for wanted channels."""
    d = cfg["data"]
    zp = download_cached(d["bulk_pet_url"], cache / "PET.zip", d["user_agent"], int(d["timeout_s"]))
    wanted = {c["series_id"] for c in d["channels"]}
    out: Dict[str, Dict[str, float]] = {}
    z = zipfile.ZipFile(zp)
    inner = z.namelist()[0]
    with z.open(inner) as fh:
        for line in io.TextIOWrapper(fh, encoding="utf-8"):
            if '"series_id"' not in line:
                continue
            sid = None
            for w in wanted:
                if f'"{w}"' in line:
                    sid = w
                    break
            if not sid:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            out[o["series_id"]] = {str(p): float(v) for p, v in o.get("data", [])
                                   if p is not None and v is not None}
            if len(out) == len(wanted):
                break
    return out


# --- WPSR Highlights text --------------------------------------------------

def archive_dates(cfg, cache: Path) -> List[str]:
    """All WPSR release dates (YYYY_MM_DD), newest first, from the archive index."""
    d = cfg["data"]
    idx = download_cached(d["archive_index_url"], cache / "archive_index.html",
                          d["user_agent"], int(d["timeout_s"]))
    html = idx.read_text(encoding="utf-8", errors="replace")
    dates = sorted(set(re.findall(r"20\d\d_\d\d_\d\d", html)), reverse=True)
    return dates


def pdftotext(pdf: bytes) -> str:
    p = subprocess.run(["pdftotext", "-", "-"], input=pdf,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.decode("utf-8", "replace")


# a dropped-in table caption line, e.g. "Refinery Activity (Thousand Barrels per Day)"
_CAPTION = re.compile(r"^[A-Z][A-Za-z /&,]+\((?:Thousand Barrels(?: per Day)?|Million Barrels|"
                      r"Percent|Dollars[^)]*|Days[^)]*)\)\s*$")
_TABLE_ROW = re.compile(r"\d{1,2}/\d{1,2}/\d{2}")  # e.g. 12/29/23 — table column header


def clean_highlights(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (narrative_prose, data_week_ending 'YYYYMMDD') from Highlights PDF text."""
    lines = raw.splitlines()
    # start after the "Highlights" heading
    start = 0
    for i, l in enumerate(lines):
        if l.strip().lower() == "highlights":
            start = i + 1
            break
    kept: List[str] = []
    for l in lines[start:]:
        if _TABLE_ROW.search(l):     # first MM/DD/YY table header → tables begin
            break
        s = l.strip()
        if not s or _CAPTION.match(s):
            continue
        # skip page footers
        if s.lower().startswith("weekly petroleum status report"):
            continue
        kept.append(s)
    prose = re.sub(r"\s+", " ", " ".join(kept)).strip()
    # de-hyphenate line-break splits like "distil- late" (rare with plain pdftotext)
    prose = re.sub(r"(\w)-\s+(\w)", r"\1\2", prose) if False else prose

    m = re.search(r"week ending ([A-Z][a-z]+) (\d{1,2}), (\d{4})", prose)
    week = None
    if m:
        mon, day, yr = _MONTHS.get(m.group(1)), int(m.group(2)), int(m.group(3))
        if mon:
            week = f"{yr:04d}{mon:02d}{day:02d}"
    return (prose or None), week


# --- record construction ---------------------------------------------------

def window_values(series: Dict[str, float], week_end: str, n: int) -> Optional[List[float]]:
    """Trailing n weekly values with period <= week_end, oldest→newest."""
    periods = sorted(p for p in series if p <= week_end)
    if len(periods) < n:
        return None
    return [round(series[p], 3) for p in periods[-n:]]


def build_record(date: str, text_raw: str, series: Dict[str, Dict[str, float]],
                 cfg) -> Tuple[Optional[dict], Optional[str]]:
    d, t = cfg["data"], cfg["text"]
    n = int(d["window_weeks"])
    prose, week_end = clean_highlights(text_raw)
    if not prose or len(prose) < int(t.get("min_text_chars", 300)):
        return None, "short text"
    if not week_end:
        return None, "no week-ending"

    channels = []
    for c in d["channels"]:
        s = series.get(c["series_id"])
        if not s:
            return None, f"missing series {c['series_id']}"
        vals = window_values(s, week_end, n)
        if vals is None:
            return None, "short window"
        channels.append({"values": vals, "unit": c["unit"], "freq": "1W"})

    wk_iso = f"{week_end[:4]}-{week_end[4:6]}-{week_end[6:8]}"
    report_date = f"{date[:4]}-{date[5:7]}-{date[8:10]}"
    report_url = d["highlights_url_template"].format(year=date[:4], date=date)
    win_start = window_start_iso(series, d["channels"], week_end, n)
    intro = t["ts_intro_sentence"].format(n=n, week=wk_iso)
    text = f"{prose}\n\n{intro}"

    try:
        rec = emit_record(
            text=text,
            timeseries=channels,
            alignment="describes",
            license="public-domain-us-gov",
            text_source="first_party_official",
            source=report_url,
            dataset="eia_petroleum_weekly",
            series_id=f"eiapet_{week_end}",
            domain="energy",
            region="US",
            period_start=win_start or wk_iso,
            period_end=wk_iso,
            meta={
                "report_date": report_date,
                "data_week_ending": wk_iso,
                "window_weeks": n,
                "report_url": report_url,
            },
        )
    except ValueError as exc:
        return None, f"invalid: {exc}"
    return rec, None


def window_start_iso(series: Dict[str, Dict[str, float]], channels: Sequence[dict],
                     week_end: str, n: int) -> Optional[str]:
    """Week-ending date (ISO) of the first point in the trailing n-week window."""
    for c in channels:
        s = series.get(c["series_id"])
        if not s:
            continue
        periods = sorted(p for p in s if p <= week_end)
        if len(periods) < n:
            continue
        p0 = periods[-n]
        return f"{p0[:4]}-{p0[4:6]}-{p0[6:8]}"
    return None


# --- pipeline --------------------------------------------------------------

def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    d, out_cfg = cfg["data"], cfg["output"]
    if not shutil.which("pdftotext"):
        raise SystemExit("pdftotext not found. Install poppler (brew install poppler / "
                         "apt-get install poppler-utils). See requirements.txt.")
    cache = rp(d["cache_dir"])
    n = int(d["window_weeks"])
    maxrec = out_cfg.get("max_records")

    series = load_series(cfg, cache)
    dates = archive_dates(cfg, cache)

    stats = {"dates": len(dates), "scanned": 0, "emitted": 0,
             "no_pdf": 0, "short_text": 0, "short_window": 0, "invalid": 0}
    records: List[dict] = []
    pdf_dir = cache / "highlights"

    for date in dates:
        if maxrec is not None and len(records) >= int(maxrec):
            break
        stats["scanned"] += 1
        fp = pdf_dir / f"{date}.pdf"
        if not fp.exists():
            url = d["highlights_url_template"].format(year=date[:4], date=date)
            try:
                raw = http_get(url, d["user_agent"], int(d["timeout_s"]))
            except Exception:
                stats["no_pdf"] += 1
                continue
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(raw)
            time.sleep(float(d.get("request_delay_s", 0.3)))
        text_raw = pdftotext(fp.read_bytes())
        rec, err = build_record(date, text_raw, series, cfg)
        if rec is None:
            if err == "short window":
                stats["short_window"] += 1
            elif err and err.startswith("invalid:"):
                stats["invalid"] += 1
            else:
                stats["short_text"] += 1
            continue
        records.append(rec)
        stats["emitted"] += 1

    report = {
        "archive_index_url": d["archive_index_url"],
        "bulk_pet_url": d["bulk_pet_url"],
        "window_weeks": n,
        "channels": [c["name"] for c in d["channels"]],
        "stats": stats,
        "config_snapshot": cfg,
        "dry_run": dry,
    }

    if dry:
        if records:
            print("\n--- sample record ---")
            r0 = dict(records[0]); r0["text"] = r0["text"][:600] + "…"
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:2600])
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
    ap = argparse.ArgumentParser(description="Build EIA WPSR Highlights + weekly supply → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s['emitted']} records (scanned {s['scanned']}/{s['dates']} dates, "
          f"no_pdf={s['no_pdf']}, short_text={s['short_text']}, "
          f"short_window={s['short_window']}, invalid={s['invalid']}).", file=sys.stderr)


if __name__ == "__main__":
    main()
