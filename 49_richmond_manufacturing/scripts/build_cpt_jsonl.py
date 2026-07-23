#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from the Richmond Fed Fifth District Survey of
Manufacturing Activity.

One record = one release month: the monthly release narrative (which states the latest
month's diffusion-index readings) paired with the FULL history of those indices up to the
release month (expanding window). The prose *describes* the trajectory → "describes"
(EIA/BLS-tier). text_quality "real".

Series: `mfg_historicaldata.xlsx` (composite + SA/NSA sub-indices, current + expectations,
        monthly from Nov-1993), parsed with the stdlib (zipfile + xml.etree). Dates are
        Excel serials. We use the SA current channels; composite + core run to 1993,
        capacity-utilization/wages from ~1997.
Text  : monthly release PDFs, named by RELEASE DATE `mfg_{MM}_{DD}_{YY}.pdf` (~4th Tuesday)
        under /{YYYY}/pdf/. No clean archive listing, so we COMPUTE the candidate release
        date and probe nearby days. Real PDFs exist ~2018 → present. Chart-heavy PDFs, so
        the extractor keeps prose lines and drops chart axis/label lines (best-effort).

One of the Federal Reserve regional business surveys (see docs/fed_surveys_discovery.md).

License: Federal Reserve Bank publications are U.S. public domain. NB the index series are
also on FRED (see NOTION_PAGE.md overlap note); the novel element is the pairing.

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
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
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


def serial_to_ym(v: str) -> Optional[str]:
    """Excel serial date -> 'YYYY-MM' (epoch 1899-12-30)."""
    try:
        n = int(float(v))
    except (ValueError, TypeError):
        return None
    d = dt.date(1899, 12, 30) + dt.timedelta(days=n)
    return f"{d.year:04d}-{d.month:02d}"


def load_series(url: str, cache: Path, ua: str, timeout: int) -> Dict[str, Dict[str, float]]:
    fp = download_cached(url, cache / "mfg_historicaldata.xlsx", ua, timeout)
    rows = read_xlsx(fp.read_bytes())
    hdr = rows[0]
    out: Dict[str, Dict[str, float]] = {}
    for r in rows[1:]:
        if not r or not r[0].strip():
            continue
        ym = serial_to_ym(r[0])
        if not ym:
            continue
        rec = {}
        for i, col in enumerate(hdr):
            if i == 0 or i >= len(r):
                continue
            try:
                rec[col.strip()] = float(r[i])       # "#N/A" / "" raise → skipped
            except (ValueError, TypeError):
                pass
        out[ym] = rec
    return out


# --- narrative extraction (chart-heavy PDF) --------------------------------

def pdftotext(pdf: bytes) -> str:
    p = subprocess.run(["pdftotext", "-", "-"], input=pdf,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.decode("utf-8", "replace")


def extract_richmond(txt: str) -> Optional[str]:
    """Richmond release PDFs are chart-heavy, so `pdftotext` interleaves chart axis labels
    with the prose. Strip the chart tokens (month-year axis labels, "Index, SA" etc., and
    runs of >=3 standalone axis numbers), then take the narrative from its lead sentence
    (which cites the survey) and keep well-formed prose sentences. Best-effort — an
    occasional stray value can still drop out; see README."""
    flat = re.sub(r"\s+", " ", txt)
    flat = re.sub(r"\b[A-Z][a-z]{2}-\d\d\b", " ", flat)                    # month-year axis (Jun-21)
    flat = re.sub(r"\bIndex, SA\b|\bPercent Change, (?:NSA|SA)\b|"
                  r"\b3-month moving average\b|\bMonthly\b|\bQuarterly\b", " ", flat)
    flat = re.sub(r"(?:(?<=\s)-?\d{1,3}(?:\.\d+)?\s+){2,}-?\d{1,3}(?:\.\d+)?(?=\s)", " ", flat)  # axis-number runs
    flat = re.sub(r"\s+", " ", flat).strip()
    for mk in ("Technical Notes", "For more information", "Recent releases", "Next release"):
        j = flat.find(mk)
        if j > 0:
            flat = flat[:j]
    low = flat.lower()
    i = low.find("according to the most recent survey")
    if i < 0:
        i = low.find("according to the latest survey")
    if i < 0:
        return None
    # narrative starts at the lead-sentence marker closest before the survey citation
    cands = [m.start() for m in re.finditer(r"\b(?:According to|Fifth District)\b", flat)
             if m.start() <= i + 2]
    body = flat[max(cands) if cands else 0:]
    sents = re.findall(r"[A-Z][^.]{15,600}?\.(?=\s|$)", body)
    keep = [s.strip() for s in sents
            if re.search(r"\b[a-z]{3,}\b", s)
            and sum(c.isalpha() for c in s) >= 2 * sum(c.isdigit() for c in s)]
    return re.sub(r"\s+", " ", " ".join(keep)).strip() or None


def _nth_tuesday(y: int, m: int, n: int) -> dt.date:
    first = dt.date(y, m, 1)
    off = (1 - first.weekday()) % 7
    return first + dt.timedelta(days=off + 7 * (n - 1))


def _candidate_days(y: int, m: int) -> List[int]:
    days: List[int] = []
    for base_n, offs in ((4, (0, 1, -1, 2, -2, 3)), (3, (0, 1)), (5, (0,))):
        b = _nth_tuesday(y, m, base_n)
        for k in offs:
            d = b + dt.timedelta(days=k)
            if d.month == m and d.day not in days:
                days.append(d.day)
    return days


def fetch_narrative(d: dict, ym: str, cache: Path) -> Optional[str]:
    fp = cache / "releases" / f"{ym}.pdf"
    if fp.exists():
        return extract_richmond(pdftotext(fp.read_bytes()))
    y, m = map(int, ym.split("-"))
    ua, timeout = d["user_agent"], int(d["timeout_s"])
    for dd in _candidate_days(y, m):
        url = d["pdf_dir_template"].format(year=y, mm=f"{m:02d}", dd=f"{dd:02d}", yy=f"{y % 100:02d}")
        try:
            raw = http_get(url, ua, timeout)
        except Exception:
            continue
        if raw.startswith(b"%PDF"):
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(raw)
            time.sleep(float(d.get("request_delay_s", 0.3)))
            return extract_richmond(pdftotext(raw))
    return None


# --- pipeline --------------------------------------------------------------

def build(cfg: Dict[str, Any]) -> Tuple[List[dict], Dict[str, int]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    cache = rp(d["cache_dir"])
    chans = d["channels"]
    maxrec = out_cfg.get("max_records")
    min_year = int(d.get("min_text_year", 2017))
    min_points = int(d.get("min_points", 2))

    series = load_series(d["data_xlsx_url"], cache, d["user_agent"], int(d["timeout_s"]))
    months = sorted(series)
    idx = {ym: i for i, ym in enumerate(months)}

    # Expanding window: pair each release with the FULL history of the indices up to that
    # month (source-native, not a fixed trailing window). Anchor the window at the earliest
    # month where every channel has begun, so all channels stay equal-length; interior gaps
    # become null (SCHEMA allows null for genuine gaps — do not fabricate).
    def _first_idx(col: str) -> Optional[int]:
        for j, m in enumerate(months):
            if series[m].get(col) is not None:
                return j
        return None
    firsts = [_first_idx(c["col"]) for c in chans]
    common_start = None if any(f is None for f in firsts) else max(firsts)

    stat = {"months_with_window": 0, "emitted": 0, "no_text": 0, "short_text": 0,
            "short_window": 0, "invalid": 0}
    records: List[dict] = []

    for ym in reversed(months):                     # newest first
        if maxrec is not None and len(records) >= int(maxrec):
            break
        if int(ym[:4]) < min_year:                  # text archive starts ~2018; skip older
            continue
        i = idx[ym]
        if common_start is None or i - common_start + 1 < min_points:
            stat["short_window"] += 1
            continue
        window_ms = months[common_start: i + 1]     # expanding: common start -> this month
        chan_vals = []
        for c in chans:
            vals = []
            for m in window_ms:
                v = series[m].get(c["col"])
                vals.append(round(v, 3) if v is not None else None)
            chan_vals.append(vals)
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
        start_ym = window_ms[0]
        start_label = f"{_MONTH_NAME[int(start_ym[5:7])]} {start_ym[:4]}"
        intro = t["ts_intro_sentence"].format(
            bank=d["bank"], survey_title=d["survey_title"],
            start=start_label, month=month_label)
        text = f"{narr}\n\n{intro}"
        timeseries = [{"values": chan_vals[j], "unit": chans[j]["name"], "freq": "1M"}
                      for j in range(len(chans))]

        try:
            rec = emit_record(
                text=text,
                timeseries=timeseries,
                alignment="describes",
                license="public-domain-us-gov",
                source=d["source_url"],
                dataset="richmond_manufacturing",
                series_id=f"rich_mfg_{ym}",
                domain="macro_econ",
                region="US",
                period_start=f"{start_ym}-01",
                period_end=f"{ym}-01",
                meta={
                    "bank": d["bank"],
                    "survey": d["survey_title"],
                    "district": d.get("district"),
                    "sector": d["domain"],
                    "release_month": ym,
                    "series_start": start_ym,
                    "n_points": len(window_ms),
                    "channels": [c["name"] for c in chans],
                },
            )
        except ValueError:
            stat["invalid"] += 1
            continue
        if validate(rec):
            stat["invalid"] += 1
            continue
        records.append(rec)
        stat["emitted"] += 1
    return records, stat


def validate(rec: dict) -> List[str]:
    e = []
    if rec["text"].count("<ts></ts>") != 1:
        e.append("ts token count")
    lens = {len(c["values"]) for c in rec["timeseries"]}
    if len(lens) != 1:                         # all channels must share length (same freq)
        e.append(f"channel lengths differ: {sorted(lens)}")
    return e


def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    if not shutil.which("pdftotext"):
        raise SystemExit("pdftotext not found. Install poppler (brew install poppler / "
                         "apt-get install poppler-utils). See requirements.txt.")
    d, out_cfg = cfg["data"], cfg["output"]
    records, stats = build(cfg)
    report = {"survey": d["survey_title"], "bank": d["bank"],
              "window": "expanding (full index history to each release month)",
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
    ap = argparse.ArgumentParser(description="Build Richmond Fifth District Manufacturing → CPT JSONL")
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
