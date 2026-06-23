#!/usr/bin/env python3
"""Fast advisory coverage checker for NHC HURDAT2.

NHC archive has two URL structures depending on era:
  - 2005 and earlier: advisories live in /archive/{year}/pub/ (one listing per year)
  - 2006 and later:   advisories live in /archive/{year}/{slug}/ (one dir per storm)

Strategy:
  - Pre-2006 years: fetch the single /pub/ directory listing per year (cheap: 6 requests
    for a 2000-2023 window), then match all storm IDs found there.
  - 2006+ storms: HEAD-check each storm's /archive/{year}/{slug}/ directory in parallel.

Usage:
  python scripts/advisory_coverage_check.py
  python scripts/advisory_coverage_check.py --season-start 1990 --season-end 2023
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
NHC_ARCHIVE_BASE = "https://www.nhc.noaa.gov/archive"
QUALIFYING_STATUSES = frozenset({"TD", "TS", "HU", "SS", "SD"})

# Pre-2006, advisories are in a shared /pub/ directory per year.
# From 2006 onward, each storm gets its own /archive/{year}/{slug}/ directory.
SLUG_DIR_ERA_START = 2006


# ---------------------------------------------------------------------------
# Minimal HURDAT2 parser
# ---------------------------------------------------------------------------


@dataclass
class Storm:
    storm_id: str
    name: str
    year: int
    basin: str
    qualifying_obs: int = 0

    @property
    def storm_id_lower(self) -> str:
        return self.storm_id.lower()

    @property
    def slug(self) -> str:
        """e.g. AL092021 → al09"""
        return self.storm_id[:2].lower() + self.storm_id[2:4]


def _parse_header(line: str) -> Optional[Tuple[str, str, int, str]]:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) >= 6:
        return None
    storm_id = parts[0].strip()
    if not re.fullmatch(r"[A-Z]{2}\d{6}", storm_id):
        return None
    try:
        num_records = int(parts[2])
    except (ValueError, IndexError):
        return None
    name = parts[1].strip().title()
    basin = storm_id[:2].upper()
    return storm_id, name, num_records, basin


def parse_hurdat2(
    text: str, season_start: int, season_end: int, min_obs: int
) -> Tuple[List[Storm], int]:
    """Parse HURDAT2 text; return (qualifying_storms, total_storms_seen)."""
    storms_seen = 0
    qualifying: List[Storm] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    i = 0
    while i < len(lines):
        header = _parse_header(lines[i])
        if header is None:
            i += 1
            continue
        storm_id, name, num_records, basin = header
        year = int(storm_id[-4:])
        storms_seen += 1
        i += 1
        qual_obs = 0
        for _ in range(num_records):
            if i >= len(lines):
                break
            parts = [p.strip() for p in lines[i].split(",")]
            if len(parts) >= 8 and parts[3].strip() in QUALIFYING_STATUSES:
                qual_obs += 1
            i += 1

        if season_start <= year <= season_end and qual_obs >= min_obs:
            qualifying.append(
                Storm(storm_id=storm_id, name=name, year=year, basin=basin,
                      qualifying_obs=qual_obs)
            )

    return qualifying, storms_seen


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def get_url(url: str, timeout: int) -> Optional[str]:
    """GET a URL; return body text or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CPT-coverage-check/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def head_url(url: str, timeout: int) -> bool:
    """Return True if the URL responds with 2xx."""
    try:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "CPT-coverage-check/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Coverage checks — two strategies
# ---------------------------------------------------------------------------


def storms_with_pub_index(
    storms: List[Storm], year: int, timeout: int
) -> Tuple[Set[str], Set[str]]:
    """Pre-2006 strategy: fetch /pub/ directory listing once, return (found, missing) IDs."""
    url = f"{NHC_ARCHIVE_BASE}/{year}/pub/"
    html = get_url(url, timeout)
    if html is None:
        return set(), {s.storm_id for s in storms}

    found: Set[str] = set()
    missing: Set[str] = set()
    for storm in storms:
        pattern = re.compile(
            rf"{re.escape(storm.storm_id_lower)}\.public\.\d+\.shtml", re.I
        )
        count = len(set(pattern.findall(html)))
        if count > 0:
            found.add(storm.storm_id)
        else:
            missing.add(storm.storm_id)
    return found, missing


def check_storm_slug_dir(storm: Storm, timeout: int) -> Tuple[Storm, bool]:
    """2006+ strategy: HEAD the per-storm directory."""
    url = f"{NHC_ARCHIVE_BASE}/{storm.year}/{storm.slug}/"
    ok = head_url(url, timeout)
    return storm, ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check NHC advisory archive coverage for HURDAT2 storms."
    )
    parser.add_argument(
        "--hurdat",
        default=str(ROOT / ".cache/hurdat2/hurdat2-1851-2023-051124.txt"),
    )
    parser.add_argument("--season-start", type=int, default=2000)
    parser.add_argument("--season-end", type=int, default=2023)
    parser.add_argument("--min-obs", type=int, default=8)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument(
        "--out",
        default=str(ROOT / "output/advisory_coverage.json"),
    )
    args = parser.parse_args()

    hurdat_path = Path(args.hurdat)
    if not hurdat_path.exists():
        sys.exit(f"HURDAT2 file not found: {hurdat_path}")

    print(f"Parsing HURDAT2 ...", file=sys.stderr)
    text = hurdat_path.read_text(encoding="utf-8", errors="replace")
    qualifying, storms_seen = parse_hurdat2(
        text, args.season_start, args.season_end, args.min_obs
    )

    old_era = [s for s in qualifying if s.year < SLUG_DIR_ERA_START]
    new_era = [s for s in qualifying if s.year >= SLUG_DIR_ERA_START]

    print(f"  {storms_seen:,} storms seen in full HURDAT2", file=sys.stderr)
    print(
        f"  {len(qualifying)} qualifying "
        f"(season {args.season_start}-{args.season_end}, min_obs={args.min_obs})",
        file=sys.stderr,
    )
    print(
        f"  {len(old_era)} in pre-{SLUG_DIR_ERA_START} era (shared /pub/ dir) | "
        f"{len(new_era)} in {SLUG_DIR_ERA_START}+ era (per-storm dir)",
        file=sys.stderr,
    )

    found_ids: Set[str] = set()
    missing_ids: Set[str] = set()

    # ---- Pre-2006: one request per year ----
    if old_era:
        print(
            f"Checking pre-{SLUG_DIR_ERA_START} storms via /pub/ directory ...",
            file=sys.stderr,
        )
        by_year: Dict[int, List[Storm]] = {}
        for s in old_era:
            by_year.setdefault(s.year, []).append(s)

        with ThreadPoolExecutor(max_workers=min(args.workers, len(by_year))) as pool:
            futures = {
                pool.submit(storms_with_pub_index, storms, yr, args.timeout): yr
                for yr, storms in by_year.items()
            }
            for future in as_completed(futures):
                yr = futures[future]
                f, m = future.result()
                found_ids |= f
                missing_ids |= m
                print(
                    f"  {yr}: {len(f)} found, {len(m)} missing", file=sys.stderr
                )

    # ---- 2006+: one HEAD request per storm ----
    if new_era:
        print(
            f"Checking {SLUG_DIR_ERA_START}+ storms via per-storm directories "
            f"({len(new_era)} storms, {args.workers} workers) ...",
            file=sys.stderr,
        )
        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures2 = {
                pool.submit(check_storm_slug_dir, s, args.timeout): s for s in new_era
            }
            for future in as_completed(futures2):
                storm, ok = future.result()
                done += 1
                if ok:
                    found_ids.add(storm.storm_id)
                else:
                    missing_ids.add(storm.storm_id)
                if done % 50 == 0 or done == len(new_era):
                    print(f"  {done}/{len(new_era)} checked ...", file=sys.stderr)

    # ---- Build report ----
    storms_with = len(found_ids)
    storms_without = len(missing_ids)
    coverage_pct = (
        round(100.0 * storms_with / len(qualifying), 1) if qualifying else 0.0
    )

    by_year_report: Dict[str, dict] = {}
    for s in qualifying:
        yr = str(s.year)
        by_year_report.setdefault(yr, {"qualifying": 0, "with_advisory": 0, "without_advisory": 0})
        by_year_report[yr]["qualifying"] += 1
        if s.storm_id in found_ids:
            by_year_report[yr]["with_advisory"] += 1
        else:
            by_year_report[yr]["without_advisory"] += 1

    storms_without_list = [
        {"storm_id": s.storm_id, "name": s.name, "year": s.year}
        for s in qualifying
        if s.storm_id in missing_ids
    ]
    storms_without_list.sort(key=lambda x: (x["year"], x["storm_id"]))

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": {
            "season_start": args.season_start,
            "season_end": args.season_end,
            "min_qualifying_obs": args.min_obs,
        },
        "summary": {
            "storms_seen_in_hurdat2": storms_seen,
            "storms_qualifying": len(qualifying),
            "storms_with_advisory_archive": storms_with,
            "storms_without_advisory_archive": storms_without,
            "coverage_pct": coverage_pct,
        },
        "by_year": {yr: by_year_report[yr] for yr in sorted(by_year_report)},
        "storms_without_advisory": storms_without_list,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(f"\nResults:", file=sys.stderr)
    print(
        f"  {storms_with}/{len(qualifying)} qualifying storms have advisory archives "
        f"({coverage_pct}%)",
        file=sys.stderr,
    )
    print(f"  Report written to {out_path}", file=sys.stderr)

    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
