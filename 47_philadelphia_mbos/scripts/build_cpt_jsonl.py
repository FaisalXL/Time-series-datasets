#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from the Philadelphia Fed Manufacturing Business
Outlook Survey (MBOS).

One record = one release month: the MBOS monthly release narrative (which recites the
diffusion-index readings) paired with a trailing window of those indices. The prose
*describes* the series → "describes" (value-reciting, EIA/BLS-tier). text_quality "real".

Series: `bos_dif.csv` diffusion indices (May 1968 → present), stdlib CSV.
Text  : monthly release PDF `…/mbos/{YYYY}/bos{MMYY}.pdf` → pdftotext (poppler). Real PDFs
        ~2010→present; older months are HTML shells and are skipped.

One of the Federal Reserve regional business surveys (see docs/fed_surveys_discovery.md); the
sibling surveys (NY Empire State, Richmond, Dallas TMOS, KC, …) are separate packages.

License: Federal Reserve Bank publications are U.S. public domain. NB the index series are
also on FRED (see NOTION_PAGE.md overlap note); the novel element is the pairing.

Examples:
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
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


# --- series (diffusion-index CSV) ------------------------------------------

def parse_mmm_yy(s: str) -> Optional[str]:
    """'Jun-26' -> '2026-06'; 'May-68' -> '1968-05'. Pivot yy<40 => 20yy."""
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
    fp = cache / "bos_dif.csv"
    download_cached(url, fp, ua, timeout)
    out: Dict[str, Dict[str, float]] = {}
    rows = list(csv.reader(io.StringIO(fp.read_text(encoding="utf-8", errors="replace"))))
    hdr = rows[0]
    for r in rows[1:]:
        if not r or not r[0].strip():
            continue
        ym = parse_mmm_yy(r[0])
        if not ym:
            continue
        rec = {}
        for i, col in enumerate(hdr[1:], start=1):
            if i < len(r):
                try:
                    rec[col.strip()] = float(r[i])
                except (ValueError, IndexError):
                    pass
        out[ym] = rec
    return out


# --- narrative extraction --------------------------------------------------

_STOP = re.compile(r"^(the diffusion index is computed|note:|source:|special question|"
                   r"return to (top|the survey)|for more information|the manufacturing "
                   r"business outlook survey is a monthly survey)", re.I)


def extract_mbos(txt: str) -> Optional[str]:
    """MBOS release PDF → the narrative prose (summary + detail sections), dropping chart
    axis blocks, captions and the methodology footer. Layout-robust across 2010–2026."""
    paras = re.split(r"\n[ \t]*\n", txt)
    kept: List[str] = []
    for p in paras:
        joined = re.sub(r"\s+", " ", " ".join(l.strip() for l in p.splitlines())).strip()
        if not joined:
            continue
        if _STOP.match(joined):
            break                                   # methodology footer / appendix
        if re.search(r"\b(19|20)\d\d\s+(19|20)\d\d\b", joined):
            break                                   # chart year-axis leaked into prose
        if joined.startswith(("Release Date", "Note:", "Chart ", "Source:")):
            continue
        if re.match(r"^[A-Z][a-z]+ \d{4}( to [A-Z][a-z]+ \d{4})?$", joined):
            continue                                # "June 2026" / date-range caption
        digits = sum(c.isdigit() for c in joined)
        alpha = sum(c.isalpha() for c in joined)
        if alpha < 40 or alpha < 3 * digits:        # axis / number / year blocks
            continue
        if "." not in joined and len(joined) < 60:  # bare chart labels / short headers
            continue
        kept.append(joined)
        if sum(len(k) for k in kept) > 4000:        # cap runaway
            break
    text = "\n\n".join(kept).strip()
    if not text:
        return None
    text = re.sub(r"^[A-Z][a-z]+ \d{4}\s*", "", text)
    text = re.sub(r"^Note:.*?collected from[^.]*\.\s*", "", text, flags=re.I)
    text = re.sub(r"‐\s*", "", text)                # justified-text soft hyphen (U+2010)
    return text.strip() or None


def pdftotext(pdf: bytes) -> str:
    p = subprocess.run(["pdftotext", "-", "-"], input=pdf,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.decode("utf-8", "replace")


def fetch_narrative(d: dict, ym: str, cache: Path) -> Optional[str]:
    yr, mon = ym.split("-")
    url = d["pdf_url_template"].format(year=yr, mmyy=f"{mon}{yr[2:]}")
    fp = cache / "releases" / f"{ym}.pdf"
    if fp.exists():
        raw = fp.read_bytes()
    else:
        try:
            raw = http_get(url, d["user_agent"], int(d["timeout_s"]))
        except Exception:
            return None
        if not raw.startswith(b"%PDF"):             # soft-404 HTML shell → no release PDF
            return None
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(raw)
        time.sleep(float(d.get("request_delay_s", 0.3)))
    if not raw.startswith(b"%PDF"):
        return None
    return extract_mbos(pdftotext(raw))


# --- pipeline --------------------------------------------------------------

def build(cfg: Dict[str, Any]) -> Tuple[List[dict], Dict[str, int]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    cache = rp(d["cache_dir"])
    win = int(d["window_months"])
    chans = d["channels"]
    maxrec = out_cfg.get("max_records")
    ua, timeout = d["user_agent"], int(d["timeout_s"])

    series = load_series(d["data_csv_url"], cache, ua, timeout)
    months = sorted(series)
    idx = {ym: i for i, ym in enumerate(months)}

    stat = {"months_with_window": 0, "emitted": 0, "no_pdf": 0, "short_text": 0,
            "short_window": 0, "invalid": 0}
    records: List[dict] = []

    for ym in reversed(months):                     # newest first (PDFs exist for recent months)
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
            stat["no_pdf"] += 1
            continue
        if len(narr) < int(t.get("min_text_chars", 200)):
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
            "dataset": "philadelphia_mbos",
            "source": f"{d['bank']} {d['survey_title']} (U.S. public domain)",
            "license": "Public domain (U.S. Government / Federal Reserve)",
            "series_id": f"mbos_{ym}",
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
    ap = argparse.ArgumentParser(description="Build Philadelphia MBOS → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s['emitted']} records (windows {s['months_with_window']}, "
          f"no_pdf={s['no_pdf']}, short_text={s['short_text']}, invalid={s['invalid']}).",
          file=sys.stderr)


if __name__ == "__main__":
    main()
