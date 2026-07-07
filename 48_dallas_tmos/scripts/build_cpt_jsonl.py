#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from the Dallas Fed Texas Manufacturing Outlook
Survey (TMOS).

One record = one release month: the TMOS monthly release narrative (which recites the
diffusion-index readings) paired with a trailing window of those indices. The prose
*describes* the series → "describes" (value-reciting, EIA/BLS-tier). text_quality "real".

Series: `index_sa.xls` (seasonally-adjusted diffusion indices; .xls but actually XLSX),
        parsed with the stdlib (zipfile + xml.etree) — no openpyxl. Monthly Jun-2004→.
Text  : release PDF `…/tmos/{YYYY}/tmos{YYMM}.pdf` (2007-2023) → pdftotext, else the HTML
        release page `…/research/surveys/tmos/{YYYY}/{YYMM}` (2024→present). Same narrative
        structure both ways: lead "Texas factory/manufacturing activity…" → index prose →
        "Next release:"/methodology boilerplate (dropped).

One of the Federal Reserve regional business surveys (see docs/fed_surveys_discovery.md); the
sibling surveys are separate packages.

License: Federal Reserve Bank publications are U.S. public domain. NB the index series are
also on FRED (see NOTION_PAGE.md overlap note); the novel element is the pairing.

Examples:
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import html as _html
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
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required. pip install -r requirements.txt") from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_MONTH_NAME = dict(zip(range(1, 13),
                   ["January", "February", "March", "April", "May", "June", "July",
                    "August", "September", "October", "November", "December"]))


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
    print(f"Downloading {url} -> {dest.name}...", file=sys.stderr)
    dest.write_bytes(http_get(url, ua, timeout))
    return dest


# --- XLSX series (stdlib) --------------------------------------------------

def _col_idx(ref: str) -> int:
    s = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(raw: bytes) -> List[List[str]]:
    """Parse the first worksheet into a list of row-lists (cells by column position)."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    ss: List[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(_NS + "si"):
            ss.append("".join(t.text or "" for t in si.iter(_NS + "t")))
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows: List[List[str]] = []
    for row in sheet.find(_NS + "sheetData").findall(_NS + "row"):
        cells: Dict[int, str] = {}
        maxc = -1
        for c in row.findall(_NS + "c"):
            ci = _col_idx(c.get("r"))
            v = c.find(_NS + "v")
            val = "" if v is None else (ss[int(v.text)] if c.get("t") == "s" else v.text)
            cells[ci] = val
            maxc = max(maxc, ci)
        rows.append([cells.get(i, "") for i in range(maxc + 1)])
    return rows


def parse_mmm_yy(s: str) -> Optional[str]:
    """'Jun-25' -> '2025-06'. Pivot yy<40 => 20yy (data spans 2004->)."""
    m = re.match(r"([A-Za-z]{3})-(\d{2})", s.strip())
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).title())
    if not mon:
        return None
    yy = int(m.group(2))
    yr = 2000 + yy if yy < 40 else 1900 + yy
    return f"{yr:04d}-{mon:02d}"


def load_series(url: str, cache: Path, ua: str, timeout: int) -> Dict[str, Dict[str, float]]:
    fp = download_cached(url, cache / "index_sa.xlsx", ua, timeout)
    rows = read_xlsx(fp.read_bytes())
    hdr = rows[0]
    out: Dict[str, Dict[str, float]] = {}
    for r in rows[1:]:
        if not r or not r[0].strip():
            continue
        ym = parse_mmm_yy(r[0])
        if not ym:
            continue
        rec = {}
        for i, col in enumerate(hdr):
            if i == 0 or i >= len(r):
                continue
            try:
                rec[col.strip()] = float(r[i])
            except (ValueError, TypeError):
                pass
        out[ym] = rec
    return out


# --- narrative extraction (PDF or HTML) ------------------------------------

def pdftotext(pdf: bytes) -> str:
    p = subprocess.run(["pdftotext", "-", "-"], input=pdf,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.decode("utf-8", "replace")


def html_to_text(raw: bytes) -> str:
    h = raw.decode("utf-8", "replace")
    h = re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
    return _html.unescape(re.sub(r"<[^>]+>", " ", h))


_ANCHOR = "responding to the texas manufacturing outlook survey"   # unique to the lead sentence
_END = ["next release", "data were collected", "the dallas fed conducts",
        "for more information", "read the special questions"]


def extract_tmos(text: str) -> Optional[str]:
    """The main narrative: the lead sentence (which cites the survey) through to the
    boilerplate footer. The lead subject wording varies by era ('Texas factory activity'
    in older PDFs, 'Texas manufacturing output growth' in recent HTML), so anchor on the
    invariant survey citation and back up to the last 'Texas' before it."""
    flat = re.sub(r"\s+", " ", text).strip()
    low = flat.lower()
    m = low.find(_ANCHOR)
    if m < 0:
        return None
    t = low.rfind("texas", 0, m)                     # start of the lead subject
    s = t if t >= 0 else m
    ends = [low.find(x, s) for x in _END if low.find(x, s) >= 0]
    e = min(ends) if ends else len(flat)
    narr = re.sub(r"‐\s*", "", flat[s:e]).strip()
    return narr or None


def fetch_narrative(d: dict, ym: str, cache: Path) -> Optional[str]:
    yr, mon = ym.split("-")
    yymm = f"{yr[2:]}{mon}"
    ua, timeout = d["user_agent"], int(d["timeout_s"])
    rel = cache / "releases"
    # 1) PDF (older releases)
    pf = rel / f"{ym}.pdf"
    if pf.exists():
        return extract_tmos(pdftotext(pf.read_bytes()))
    # 2) HTML (recent releases)
    hf = rel / f"{ym}.html"
    if hf.exists():
        return extract_tmos(html_to_text(hf.read_bytes()))
    rel.mkdir(parents=True, exist_ok=True)
    pdf_url = d["pdf_url_template"].format(year=yr, yymm=yymm)
    try:
        raw = http_get(pdf_url, ua, timeout)
    except Exception:
        raw = b""
    if raw.startswith(b"%PDF"):
        pf.write_bytes(raw)
        time.sleep(float(d.get("request_delay_s", 0.3)))
        return extract_tmos(pdftotext(raw))
    html_url = d["html_url_template"].format(year=yr, yymm=yymm)
    try:
        raw = http_get(html_url, ua, timeout)
    except Exception:
        return None
    if not raw or b"<html" not in raw[:2000].lower():
        return None
    hf.write_bytes(raw)
    time.sleep(float(d.get("request_delay_s", 0.3)))
    return extract_tmos(html_to_text(raw))


# --- pipeline --------------------------------------------------------------

def build(cfg: Dict[str, Any]) -> Tuple[List[dict], Dict[str, int]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    cache = rp(d["cache_dir"])
    win = int(d["window_months"])
    chans = d["channels"]
    maxrec = out_cfg.get("max_records")
    ua, timeout = d["user_agent"], int(d["timeout_s"])

    series = load_series(d["data_xlsx_url"], cache, ua, timeout)
    months = sorted(series)
    idx = {ym: i for i, ym in enumerate(months)}

    stat = {"months_with_window": 0, "emitted": 0, "no_text": 0, "short_text": 0,
            "short_window": 0, "invalid": 0}
    records: List[dict] = []

    for ym in reversed(months):                     # newest first
        if maxrec is not None and len(records) >= int(maxrec):
            break
        i = idx[ym]
        if i + 1 < win:
            stat["short_window"] += 1
            continue
        window_ms = months[i - win + 1: i + 1]
        chan_vals, ok = [], True
        for c in chans:
            vals = [series[m].get(c["col"]) for m in window_ms]
            if any(v is None for v in vals):
                ok = False
                break
            chan_vals.append([round(v, 3) for v in vals])
        if not ok:
            stat["short_window"] += 1
            continue
        stat["months_with_window"] += 1

        narr = fetch_narrative(d, ym, cache)
        if not narr:
            stat["no_text"] += 1
            continue
        if len(narr) < int(t.get("min_text_chars", 250)):
            stat["short_text"] += 1
            continue

        yr, mon = ym.split("-")
        month_label = f"{_MONTH_NAME[int(mon)]} {yr}"
        intro = t["ts_intro_sentence"].format(
            bank=d["bank"], survey_title=d["survey_title"], n=win, month=month_label)
        text = f"{narr}\n\n{intro}"
        timeseries = [{"values": chan_vals[j], "unit": chans[j]["name"], "freq": "1M"}
                      for j in range(len(chans))]

        rec = {
            "text": text,
            "timeseries": timeseries,
            "task_type": "world_knowledge",
            "text_quality": "real",
            "bank": d["bank"],
            "survey": d["survey_title"],
            "district": d.get("district"),
            "domain": d["domain"],
            "release_month": ym,
            "window_months": win,
            "channels": [c["name"] for c in chans],
            "dataset": "dallas_tmos",
            "source": f"{d['bank']} {d['survey_title']} (U.S. public domain)",
            "license": "Public domain (U.S. Government / Federal Reserve)",
            "series_id": f"tmos_{ym}",
        }
        verr = validate(rec, win)
        if verr:
            stat["invalid"] += 1
            continue
        records.append(rec)
        stat["emitted"] += 1
    return records, stat


def validate(rec: dict, win: int) -> List[str]:
    e = []
    if rec["text"].count("<ts></ts>") != 1:
        e.append("ts token count")
    lens = {len(c["values"]) for c in rec["timeseries"]}
    if len(lens) != 1 or next(iter(lens)) != win:
        e.append(f"window {sorted(lens)} != {win}")
    return e


def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    if not shutil.which("pdftotext"):
        raise SystemExit("pdftotext not found. Install poppler (brew install poppler / "
                         "apt-get install poppler-utils). See requirements.txt.")
    d, out_cfg = cfg["data"], cfg["output"]
    records, stats = build(cfg)
    report = {"survey": d["survey_title"], "bank": d["bank"],
              "window_months": int(d["window_months"]),
              "channels": [c["name"] for c in d["channels"]],
              "stats": stats, "config_snapshot": cfg, "dry_run": dry}

    if dry:
        if records:
            print("\n--- sample record ---")
            r0 = dict(records[0]); r0["text"] = r0["text"][:700] + "…"
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
    ap = argparse.ArgumentParser(description="Build Dallas TMOS → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s['emitted']} records (windows {s['months_with_window']}, "
          f"no_text={s['no_text']}, short_text={s['short_text']}, invalid={s['invalid']}).",
          file=sys.stderr)


if __name__ == "__main__":
    main()
