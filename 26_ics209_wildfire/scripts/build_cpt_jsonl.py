#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from ICS-209-PLUS wildfire situation reports.

One record = one SITUATION REPORT: that report's own free-text narrative paired with the
TRAILING window of daily incident metrics — acres burned, percent contained, total
personnel — ENDING at that report. The narrative *describes* the fire state the series
quantifies → "describes".

Source: ICS-209-PLUS (St. Denis et al. 2023, Scientific Data), figshare 19858927,
        CC BY 4.0. Keyless; the sitrep CSV lives inside the wildfire zip. The CSV is
        contiguous by INCIDENT_ID, so we stream and process one incident at a time.

Design (trailing window — same pattern as 11_eia_petroleum_weekly):
  - daily points: one report per calendar day (the last of that day), kept only if ALL
    channels are numeric (no imputation); dropped days => explicit gaps in `report_dates`.
  - one record per report that has >= min_window_reports of trailing history and a
    narrative >= text.min_text_chars. The series is the last `window_reports` reporting
    days up to and including that report, so the report's OWN metrics are always the
    series' terminal point (alignment is structural, not incidental).
  - a fixed trailing window (not the whole arc) keeps every record's series long enough
    for a patch-based model while avoiding the expanding-window redundancy that copies a
    fire's full history into every record.
  - text_quality "real"; nothing is generated — `<ts></ts>` is appended directly to the
    real situation-report prose (no templated sentence describing the series).

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
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required. pip install -r requirements.txt") from exc

csv.field_size_limit(10 * 1024 * 1024)  # narrative fields can be long

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

# shared v1-compliant record builder (self-validates against schema/validate.py --strict)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

# Canonical URL for the ICS-209-PLUS wildfire bundle (St. Denis et al. 2023, CC BY 4.0).
SOURCE_URL = (
    "https://figshare.com/articles/dataset/"
    "All-hazards_dataset_mined_from_the_US_National_Incident_Management_System_"
    "1999-2020/19858927"
)
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


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


# --- helpers ---------------------------------------------------------------

def to_float(s: str) -> Optional[float]:
    if s is None:
        return None
    s = s.strip()
    if s == "" or s.lower() in {"na", "n/a", "null", "none"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def combine_narrative(row: Dict[str, str], fields: Sequence[str]) -> str:
    parts = []
    for f in fields:
        v = (row.get(f) or "").strip()
        if v and v.lower() not in {"na", "n/a", "none", "null"}:
            parts.append(v)
    text = " ".join(parts)
    return re.sub(r"\s+", " ", text).strip()


# --- per-incident record construction --------------------------------------

# windows rejected by the physical-validity check, reported in run_report stats
_dropped: Dict[str, int] = {"acres_drop": 0}


def build_records(rows: List[Dict[str, str]], cfg) -> Tuple[List[dict], Optional[str]]:
    """One record per qualifying situation report (trailing window ending at that report)."""
    d, t = cfg["data"], cfg["text"]
    chans = d["channels"]
    win_n = int(d["window_reports"])
    min_win = int(d["min_window_reports"])
    min_text = int(t.get("min_text_chars", 300))

    # 1 report per calendar day (the last of that day), keep only all-channels-present
    by_day: Dict[str, dict] = {}
    for r in rows:
        rtd = (r.get("REPORT_TO_DATE") or "").strip()
        if len(rtd) < 10:
            continue
        day = rtd[:10]
        vals = {c["name"]: to_float(r.get(c["col"])) for c in chans}
        if any(v is None for v in vals.values()):
            continue
        # later timestamp on the same day wins (most complete)
        prev = by_day.get(day)
        if prev is None or rtd >= prev["_rtd"]:
            by_day[day] = {"_rtd": rtd, "date": day, "vals": vals,
                           "narr": combine_narrative(r, d["narrative_fields"]), "row": r}

    points = [by_day[k] for k in sorted(by_day)]
    if len(points) < min_win:
        return [], "few_points"

    # Every report with enough trailing history AND a usable narrative becomes a record.
    idxs = [i for i in range(len(points))
            if i + 1 >= min_win and len(points[i]["narr"]) >= min_text]
    if not idxs:
        return [], "short_text"
    cap = d.get("max_records_per_incident")
    if cap and len(idxs) > int(cap):          # spread the picks evenly across the fire
        step = len(idxs) / int(cap)
        idxs = [idxs[int(k * step)] for k in range(int(cap))]

    # Burned area cannot physically shrink. Sub-fire re-scoping inside a "complex" can make
    # reported ACRES collapse mid-incident (seen as far as -100%), which would teach wrong
    # dynamics; such windows are dropped. Small decreases (remapping/GIS corrections) are
    # normal and kept. None disables the check.
    max_drop = d.get("max_acres_drop_frac")

    def acres_corrupt(window: List[dict]) -> bool:
        if max_drop is None:
            return False
        vals = [p["vals"]["acres_burned"] for p in window]
        return any(a > 0 and b < a and (a - b) / a > float(max_drop)
                   for a, b in zip(vals, vals[1:]))

    cause_map = {"H": "Human", "L": "Natural (lightning)", "U": "Undetermined", "O": "Other"}
    out: List[dict] = []
    for i in idxs:
        window = points[max(0, i + 1 - win_n): i + 1]
        if len(window) < min_win:
            continue
        if acres_corrupt(window):
            _dropped["acres_drop"] += 1
            continue
        report = points[i]
        timeseries = [{"values": [round(p["vals"][c["name"]], 3) for p in window],
                       "unit": c["unit"], "freq": "1d"} for c in chans]
        report_dates = [p["date"] for p in window]

        a = report["row"]
        name = (a.get("INCIDENT_NAME") or "Unnamed").strip().title()
        state = (a.get("POO_STATE") or "").strip()
        yr = (a.get("START_YEAR") or "").strip()
        yr = yr[:-2] if yr.endswith(".0") else yr
        cause = (a.get("CAUSE") or "").strip()
        cause = cause_map.get(cause, cause) or None
        # Nothing generated: <ts></ts> is appended directly to the real narrative.
        text = f"{report['narr']}\n\n<ts></ts>"

        out.append(emit_record(
            text=text,
            timeseries=timeseries,
            timestamps=report_dates,
            alignment="describes",
            license="cc-by-4.0",
            text_source="first_party_official",
            source=SOURCE_URL,
            dataset="ics209_wildfire",
            series_id=f"ics209_{a.get('INCIDENT_ID')}_{report['date']}",
            domain="wildfire",
            region=f"US-{state}" if state else "US",
            period_start=report_dates[0],
            period_end=report_dates[-1],
            meta={
                "report_dates": report_dates,
                "incident_id": a.get("INCIDENT_ID"),
                "incident_name": name,
                "poo_state": state,
                "start_year": yr or None,
                "cause": cause,
                "discovery_date": (a.get("DISCOVERY_DATE") or "")[:10] or None,
                # this report IS the series' terminal point (structural alignment)
                "report_date": report["date"],
                "report_acres": round(report["vals"]["acres_burned"], 3),
                "report_pct_contained": round(report["vals"]["percent_contained"], 3),
                "report_total_personnel": round(report["vals"]["total_personnel"], 3),
                "final_acres": to_float(a.get("EVENT_FINAL_ACRES")),
                "n_reports": len(window),
                "incident_reporting_days": len(points),
                "attribution": (
                    "St. Denis et al. 2023, ICS-209-PLUS, CC BY 4.0 "
                    "(figshare article 19858927)"
                ),
            },
        ))
    return out, None


# Per-record validation now lives in emit_record(): each record is self-checked against
# schema/validate.py --strict at construction time, raising ValueError on any violation.


# --- pipeline --------------------------------------------------------------

def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    d, out_cfg = cfg["data"], cfg["output"]
    cache = rp(d["cache_dir"])
    maxrec = out_cfg.get("max_records")

    zp = download_cached(d["wildfire_zip_url"], cache / "ics209plus-wildfire.zip",
                         d["user_agent"], int(d["timeout_s"]))
    z = zipfile.ZipFile(zp)

    stats = {"incidents": 0, "incidents_used": 0, "emitted": 0, "few_points": 0,
             "short_text": 0, "acres_drop": 0, "invalid": 0}
    records: List[dict] = []
    _dropped["acres_drop"] = 0

    def flush(rows: List[Dict[str, str]]) -> bool:
        """Process one incident's rows; return True if we should stop (hit max)."""
        stats["incidents"] += 1
        try:
            recs, err = build_records(rows, cfg)
        except ValueError:
            # emit_record rejected an assembled record (strict schema violation).
            stats["invalid"] += 1
            return False
        if not recs:
            if err:            # None => every window was rejected by a validity check
                stats[err] += 1
            return False
        stats["incidents_used"] += 1
        for rec in recs:
            records.append(rec)
            stats["emitted"] += 1
            if maxrec is not None and len(records) >= int(maxrec):
                return True
        return False

    with z.open(d["sitreps_csv_name"]) as fh:
        rd = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"))
        cur_id = None
        buf: List[Dict[str, str]] = []
        for row in rd:
            iid = row.get("INCIDENT_ID")
            if iid != cur_id and buf:
                if flush(buf):
                    buf = []
                    break
                buf = []
            cur_id = iid
            buf.append(row)
        if buf and (maxrec is None or len(records) < int(maxrec)):
            flush(buf)

    stats["acres_drop"] = _dropped["acres_drop"]
    report = {
        "wildfire_zip_url": d["wildfire_zip_url"],
        "window_reports": int(d["window_reports"]),
        "max_acres_drop_frac": d.get("max_acres_drop_frac"),
        "min_window_reports": int(d["min_window_reports"]),
        "max_records_per_incident": d.get("max_records_per_incident"),
        "channels": [c["name"] for c in d["channels"]],
        "min_text_chars": cfg["text"].get("min_text_chars"),
        "stats": stats,
        "config_snapshot": cfg,
        "dry_run": dry,
    }

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
    ap = argparse.ArgumentParser(description="Build ICS-209-PLUS wildfire → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s['emitted']} records from {s['incidents_used']}/{s['incidents']} "
          f"incidents (few_points={s['few_points']}, short_text={s['short_text']}, "
          f"invalid={s['invalid']}).", file=sys.stderr)


if __name__ == "__main__":
    main()
