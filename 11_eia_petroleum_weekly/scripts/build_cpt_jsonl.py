#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from EIA Weekly Petroleum Status Report Highlights.

One record = ONE TOPIC from one weekly report: the sentence(s) of the WPSR "Highlights"
narrative describing a single national supply series (e.g. crude-oil inventories) paired
with a TRAILING WINDOW of exactly that series up to the report's data week. Each report
yields up to 6 records (crude/gasoline/distillate stocks, refinery inputs, utilization,
crude imports), each with text that tightly describes the one series attached to it.
The prose *describes* the series → "describes". text_quality = "real".

Why per-topic + trailing window (not one expanding record per report): the narrative talks
only about the latest week and its recent context ("past four weeks", "five-year average"),
so pairing it with the full 1990→date history was ~99% irrelevant and hugely redundant
(the same early history copied into every record). Splitting by topic and bounding the
window to the horizon the text references makes each record a tight, non-redundant pair.

Text : WPSR Highlights PDF, one per release date from the archive index (~779 back to
       Aug 2011). PDF -> text via the `pdftotext` CLI (poppler). The narrative is split
       into sentences and each sentence assigned to a topic by keyword (config `channels`).
Series: EIA bulk PET.zip (keyless, public domain), national weekly channels. Each record's
        series = the most-recent `trailing_window_weeks` weekly points of its one channel,
        ending at the report's data week (single dense channel → no nulls).
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


def clean_highlights(raw: str, release_date: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Return (narrative_prose, data_week_ending 'YYYYMMDD') from Highlights PDF text.
    release_date ('YYYY_MM_DD') lets us recover the year for older reports that omit it."""
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

    # Newer reports say "week ending July 10, 2026" (with year); pre-2013 reports say
    # "week ending July 29," with NO year — infer it from the release date (the data week
    # is a few days before release; handle the Dec→Jan boundary).
    week = None
    m = re.search(r"week ending ([A-Z][a-z]+) (\d{1,2})(?:,\s*(\d{4}))?", prose)
    if m:
        mon, day, yr = _MONTHS.get(m.group(1)), int(m.group(2)), m.group(3)
        year = None
        if mon and yr:
            year = int(yr)
        elif mon and release_date and re.match(r"\d{4}_\d\d_\d\d", release_date):
            ry, rm, rd = int(release_date[:4]), int(release_date[5:7]), int(release_date[8:10])
            year = ry
            try:
                if dt.date(ry, mon, day) > dt.date(ry, rm, rd):
                    year -= 1     # Jan release referencing a late-December data week
            except ValueError:
                pass
        if mon and year:
            week = f"{year:04d}{mon:02d}{day:02d}"
    return (prose or None), week


# --- record construction (per topic) --------------------------------------

# Protect abbreviations whose "." must not end a sentence (mainly "U.S.", "No. 2").
_ABBR_SENTINEL = ""


def split_sentences(prose: str) -> List[str]:
    """Split narrative prose into sentences, guarding common abbreviations and decimals.
    Decimals like $3.855 / 96.2% are safe (no space after the dot); we only guard "U.S."
    and "No./no." which are followed by a space."""
    prot = prose.replace("U.S.", "U" + _ABBR_SENTINEL + "S" + _ABBR_SENTINEL)
    prot = re.sub(r"\b([Nn]o)\.(\s)", r"\1" + _ABBR_SENTINEL + r"\2", prot)
    parts = re.split(r"(?<=[.!?])\s+", prot)
    return [p.replace(_ABBR_SENTINEL, ".").strip() for p in parts if p.strip()]


def sentence_matches(sentence: str, groups: Sequence[Sequence[str]]) -> bool:
    """True if the sentence contains ALL words of ANY one keyword group (case-insensitive)."""
    sl = sentence.lower()
    return any(all(sub.lower() in sl for sub in grp) for grp in groups)


def trailing_window(series: Dict[str, float], week_end: str, n: int) -> List[str]:
    """The n most-recent week keys (<= week_end) for a single channel, oldest→newest."""
    weeks = sorted(w for w in series if w <= week_end)
    return weeks[-n:]


def build_records(date: str, text_raw: str, series: Dict[str, Dict[str, float]],
                  cfg) -> Tuple[List[dict], Dict[str, int]]:
    """One weekly report → up to len(channels) per-topic records. Returns (records, tallies)."""
    d, t = cfg["data"], cfg["text"]
    tally = {"no_week": 0, "no_series": 0, "vintage_skip": 0, "no_snippet": 0,
             "short_snippet": 0, "short_window": 0, "invalid": 0}
    prose, week_end = clean_highlights(text_raw, date)
    if not prose or not week_end:
        tally["no_week"] += 1
        return [], tally

    sentences = split_sentences(prose)
    n_weeks = int(d.get("trailing_window_weeks", 260))
    min_points = int(d.get("min_points", 2))
    min_snip = int(t.get("min_snippet_chars", 40))
    wk_iso = f"{week_end[:4]}-{week_end[4:6]}-{week_end[6:8]}"
    report_date = f"{date[:4]}-{date[5:7]}-{date[8:10]}"
    report_url = d["highlights_url_template"].format(year=date[:4], date=date)
    framing = t["framing_template"].format(week=wk_iso)

    out: List[dict] = []
    for c in d["channels"]:
        sid = c["series_id"]
        smap = series.get(sid)
        if not smap:
            tally["no_series"] += 1
            continue
        # vintage cutoff: skip topics whose stated level no longer matches the revised
        # series before a rebenchmark year (e.g. crude stocks pre-2017; see config).
        min_year = c.get("min_data_year")
        if min_year and int(week_end[:4]) < int(min_year):
            tally["vintage_skip"] += 1
            continue
        # the narrative sentence(s) that describe THIS series
        snippet = " ".join(s for s in sentences if sentence_matches(s, c["match"]))
        if not snippet:
            tally["no_snippet"] += 1
            continue
        if len(snippet) < min_snip:
            tally["short_snippet"] += 1
            continue
        weeks = trailing_window(smap, week_end, n_weeks)
        if len(weeks) < min_points:
            tally["short_window"] += 1
            continue
        values = [round(smap[w], 3) for w in weeks]
        w0 = weeks[0]
        start_iso = f"{w0[:4]}-{w0[4:6]}-{w0[6:8]}"
        intro = t["ts_intro_template"].format(label=c["label"], unit_h=c["unit"],
                                              n_weeks=len(weeks), week=wk_iso)
        desc = (c.get("description") or "").strip()
        lead = f"{desc} " if desc else ""
        text = f"{lead}{framing}{snippet}\n\n{intro}"
        try:
            rec = emit_record(
                text=text,
                timeseries=[{"values": values, "unit": c["unit"], "freq": "1W"}],
                alignment="describes",
                license="public-domain-us-gov",
                text_source="first_party_official",
                source=report_url,
                dataset="eia_petroleum_weekly",
                series_id=f"eiapet_{week_end}_{c['tag']}",
                domain="energy",
                region="US",
                period_start=start_iso,
                period_end=wk_iso,
                meta={
                    "topic": c["tag"],
                    "eia_series_id": sid,
                    "report_date": report_date,
                    "data_week_ending": wk_iso,
                    "window_start": start_iso,
                    "n_points": len(weeks),
                    "report_url": report_url,
                },
            )
        except ValueError:
            tally["invalid"] += 1
            continue
        out.append(rec)
    return out, tally


# --- pipeline --------------------------------------------------------------

def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    d, out_cfg = cfg["data"], cfg["output"]
    if not shutil.which("pdftotext"):
        raise SystemExit("pdftotext not found. Install poppler (brew install poppler / "
                         "apt-get install poppler-utils). See requirements.txt.")
    cache = rp(d["cache_dir"])
    maxrec = out_cfg.get("max_records")

    series = load_series(cfg, cache)
    dates = archive_dates(cfg, cache)

    stats = {"dates": len(dates), "scanned": 0, "reports_used": 0, "emitted": 0,
             "no_pdf": 0, "no_week": 0, "no_series": 0, "no_snippet": 0,
             "short_snippet": 0, "short_window": 0, "invalid": 0}
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
        recs, tally = build_records(date, text_raw, series, cfg)
        for k, v in tally.items():
            stats[k] = stats.get(k, 0) + v
        if recs:
            stats["reports_used"] += 1
            records.extend(recs)
            stats["emitted"] += len(recs)

    if maxrec is not None:           # each report emits up to 6 records; trim overshoot
        records = records[:int(maxrec)]
        stats["emitted"] = len(records)

    report = {
        "archive_index_url": d["archive_index_url"],
        "bulk_pet_url": d["bulk_pet_url"],
        "window": f"per-topic; trailing {int(d.get('trailing_window_weeks', 260))} weeks per record",
        "topics": [c["tag"] for c in d["channels"]],
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
    print(f"\nDone: {s['emitted']} records from {s['reports_used']} reports "
          f"(scanned {s['scanned']}/{s['dates']} dates; no_pdf={s['no_pdf']}, "
          f"no_snippet={s['no_snippet']}, short_window={s['short_window']}, "
          f"invalid={s['invalid']}).", file=sys.stderr)


if __name__ == "__main__":
    main()
