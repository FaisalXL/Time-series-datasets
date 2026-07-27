#!/usr/bin/env python3
"""Build CPT world-knowledge records for USDA/NASS state-level "Crop Progress and Condition"
weekly reports.

One record = one state's one real weekly report: the report's own narrative text (state
Agricultural Summary + weather-narrative paragraphs; per-station raw weather tables and NASS
release boilerplate stripped) paired with an expanding within-season window of that state's
primary-commodity progress/condition/fieldwork channels, from the season's first real reported
week through the current week.

Series data: USDA NASS Quick Stats bulk flat file (`qs.crops_*.txt.gz`, free, no API key) --
see README for the download URL. Filtered once to state-level weekly PROGRESS/CONDITION/
DAYS SUITABLE rows and cached locally.

Text data: fetched live + via the Wayback Machine per state (see scripts/state_sources.py --
every state has its own idiosyncratic archive; there is no universal per-state URL template).

Usage:
    python scripts/build_cpt_jsonl.py --config config.example.yaml
    python scripts/build_cpt_jsonl.py --set output.max_records=5   # smoke test
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import state_sources as ss  # noqa: E402

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent
CACHE_DIR = PKG_ROOT / ".cache"
BULK_URL = "https://www.nass.usda.gov/datasets/qs.crops_current.txt.gz"
# NASS names the file by the date it was generated; there is no stable "latest" alias, so we
# accept any qs.crops_*.txt.gz already cached and only hit the datasets page to discover the
# current exact filename when nothing is cached yet.
BULK_INDEX_URL = "https://www.nass.usda.gov/datasets/"


def load_config(path: str | None, overrides: list[str]) -> dict:
    cfg = {}
    if path:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    for kv in overrides:
        key, _, val = kv.partition("=")
        node = cfg
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        try:
            val_parsed = json.loads(val)
        except Exception:
            val_parsed = None if val == "null" else val
        node[parts[-1]] = val_parsed
    return cfg


def _discover_bulk_filename() -> str:
    req = urllib.request.Request(BULK_INDEX_URL, headers=ss.UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    m = re.search(r"qs\.crops_(\d{8})\.txt\.gz", html)
    if not m:
        raise RuntimeError("Could not find qs.crops_*.txt.gz filename on NASS datasets page")
    return f"qs.crops_{m.group(1)}.txt.gz"


def ensure_bulk_file() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(CACHE_DIR.glob("qs.crops_*.txt.gz"))
    if existing:
        return existing[-1]
    fname = _discover_bulk_filename()
    dest = CACHE_DIR / fname
    print(f"Downloading {fname} (~1GB, one-time)...", file=sys.stderr)
    req = urllib.request.Request(f"https://www.nass.usda.gov/datasets/{fname}", headers=ss.UA)
    with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    return dest


def ensure_filtered_series(bulk_path: Path) -> Path:
    filtered = CACHE_DIR / "progress_condition_state_weekly.tsv"
    if filtered.exists():
        return filtered
    print("Filtering bulk file to state-level weekly PROGRESS/CONDITION/DAYS SUITABLE rows "
          "(one-time, several minutes)...", file=sys.stderr)
    wanted_cats = {"PROGRESS", "CONDITION", "DAYS SUITABLE"}
    with gzip.open(bulk_path, "rt", encoding="utf-8", errors="replace") as inp, \
            open(filtered, "w", newline="") as out:
        reader = csv.reader(inp, delimiter="\t")
        writer = csv.writer(out, delimiter="\t")
        header = next(reader)
        writer.writerow(header)
        idx = {name: i for i, name in enumerate(header)}
        for row in reader:
            if (row[idx["AGG_LEVEL_DESC"]] == "STATE"
                    and row[idx["FREQ_DESC"]] == "WEEKLY"
                    and row[idx["STATISTICCAT_DESC"]] in wanted_cats):
                writer.writerow(row)
    return filtered


def load_series_index(filtered_path: Path, state_alpha: str, short_descs: set[str]):
    """Return {short_desc: {date: value}} for one state, only the requested SHORT_DESC keys."""
    idx: dict[str, dict[dt.date, float]] = defaultdict(dict)
    with open(filtered_path) as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            if row["STATE_ALPHA"] != state_alpha:
                continue
            sd = row["SHORT_DESC"]
            if sd not in short_descs:
                continue
            we = row["WEEK_ENDING"]
            if not we:
                continue
            try:
                d = dt.date.fromisoformat(we)
                v = float(row["VALUE"].replace(",", ""))
            except ValueError:
                continue
            idx[sd][d] = v
    return idx


def season_dates_for_year(series_idx: dict, year: int) -> list[dt.date]:
    dates = set()
    for per_date in series_idx.values():
        for d in per_date:
            if d.year == year:
                dates.add(d)
    return sorted(dates)


def fetch_and_parse_pdf(source: tuple[str, str]) -> tuple[dt.date | None, str]:
    ts, url = source
    raw = ss.fetch_wayback_raw(ts, url)
    if not raw or not raw.lstrip().startswith(b"%PDF"):
        return None, ""
    pages = ss.pdf_to_pages_text(raw)
    if not pages:
        return None, ""
    full_text = "\n".join(pages)
    report_date = ss.parse_week_ending(full_text)
    narrative = ss.clean_narrative(pages)
    return report_date, narrative


def build_state(state_cfg: ss.StateConfig, cfg: dict, run_report: dict) -> list[dict]:
    filtered_path = Path(cfg["_filtered_series_path"])
    short_descs = {c.short_desc for c in state_cfg.channels}
    series_idx = load_series_index(filtered_path, state_cfg.alpha, short_descs)

    start_year = max(state_cfg.clean_text_start_year, cfg.get("start_year") or 0)
    end_year = cfg.get("end_year") or dt.date.today().year
    min_window = cfg.get("min_window_weeks", 8)
    min_text_chars = cfg.get("min_text_chars", 200)

    records = []
    for year in range(start_year, end_year + 1):
        season_dates = season_dates_for_year(series_idx, year)
        if not season_dates:
            run_report["skipped_no_series"] += 1
            continue

        candidates = state_cfg.discover_year(year)
        run_report["cdx_candidates_seen"] += len(candidates)
        print(f"  {state_cfg.alpha} {year}: {len(candidates)} candidate PDFs, "
              f"{len(season_dates)} real season weeks", file=sys.stderr)
        date_to_text: dict[dt.date, tuple[str, str]] = {}
        for ci, (ts, url) in enumerate(candidates):
            report_date, narrative = fetch_and_parse_pdf((ts, url))
            print(f"    [{ci+1}/{len(candidates)}] {url.rsplit('/', 1)[-1]} -> "
                  f"{report_date} ({len(narrative)} chars)", file=sys.stderr)
            if report_date is None or report_date not in season_dates:
                continue
            if len(narrative) < min_text_chars:
                run_report["skipped_short_text"] += 1
                continue
            # Prefer the first successfully-parsed candidate for a given date.
            date_to_text.setdefault(report_date, (narrative, url))

        # Process latest-in-season weeks first: with a capped max_records, chronological order
        # would fill the cap with the shortest (just-past-min_window) trailing windows every
        # time. Reversing means a capped run instead keeps the longest, richest windows the
        # season actually offers.
        indexed_dates = list(enumerate(season_dates))
        for i, week_date in reversed(indexed_dates):
            if week_date not in date_to_text:
                continue
            window_dates = [d for d in season_dates if d <= week_date]
            if len(window_dates) < min_window:
                run_report["skipped_short_window"] += 1
                continue
            narrative, source_url = date_to_text[week_date]

            timeseries = []
            ok = True
            for chan in state_cfg.channels:
                per_date = series_idx.get(chan.short_desc, {})
                vals = [per_date.get(d) for d in window_dates]
                if all(v is None for v in vals):
                    continue
                timeseries.append({"values": vals, "unit": chan.unit, "freq": "1w"})
            if not timeseries:
                ok = False
            if not ok:
                run_report["skipped_no_series"] += 1
                continue

            text = narrative.strip() + "\n\n<ts></ts>"
            season_start = window_dates[0]
            rec = {
                "text": text,
                "timeseries": timeseries,
                "task_type": "world_knowledge",
                "text_quality": "real",
                "series_id": f"nass_crop_progress:{state_cfg.alpha}:{year}:w{i+1:02d}",
                "dataset": "nass_crop_progress",
                "source": source_url,
                "license": "public-domain-us-gov",
                "text_source": "first_party_official",
                "alignment": "recites",
                "domain": "agriculture",
                "region": f"US-{state_cfg.alpha}",
                "period_start": season_start.isoformat(),
                "period_end": week_date.isoformat(),
                "meta": {
                    "state": state_cfg.name,
                    "commodity": state_cfg.commodity_label,
                    "season_year": year,
                    "week_index": i + 1,
                    "window_weeks": len(window_dates),
                },
            }
            records.append(rec)
            run_report["emitted"] += 1
            max_records = cfg.get("max_records")
            if max_records and len(records) >= max_records:
                return records
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--set", action="append", default=[], dest="overrides")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config, args.overrides)
    output_cfg = cfg.get("output", {})
    states_wanted = cfg.get("states") or list(ss.STATE_CONFIGS.keys())

    bulk_path = ensure_bulk_file()
    filtered_path = ensure_filtered_series(bulk_path)

    run_report = {
        "emitted": 0, "skipped_no_series": 0, "skipped_short_text": 0,
        "skipped_short_window": 0, "cdx_candidates_seen": 0,
        "config": cfg,
    }

    all_records = []
    per_state_cfg = dict(output_cfg)
    per_state_cfg["_filtered_series_path"] = str(filtered_path)
    per_state_cfg["start_year"] = cfg.get("start_year")
    per_state_cfg["end_year"] = cfg.get("end_year")
    total_max = output_cfg.get("max_records")
    # Split the cap evenly across states so a combined demo actually demonstrates every state
    # (a shared pool would let the first state alone consume the whole cap).
    per_state_max = (
        max(1, -(-total_max // len(states_wanted))) if total_max is not None else None
    )
    for state_alpha in states_wanted:
        state_cfg = ss.STATE_CONFIGS[state_alpha]
        this_cfg = dict(per_state_cfg)
        if per_state_max is not None:
            this_cfg["max_records"] = per_state_max
        print(f"Building {state_cfg.name} ({state_alpha})...", file=sys.stderr)
        recs = build_state(state_cfg, this_cfg, run_report)
        all_records.extend(recs)

    if args.dry_run:
        print(json.dumps(run_report, indent=2, default=str))
        return

    out_dir = PKG_ROOT / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "nass_crop_progress_cpt.jsonl"
    with open(out_path, "w") as f:
        for rec in all_records:
            f.write(json.dumps(rec) + "\n")

    run_report["total_records"] = len(all_records)
    with open(out_dir / "run_report.json", "w") as f:
        json.dump(run_report, f, indent=2, default=str)

    print(f"Wrote {len(all_records)} records to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
