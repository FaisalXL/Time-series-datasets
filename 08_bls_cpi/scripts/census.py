#!/usr/bin/env python3
"""Read-only: how does the section split behave across all 389 releases?"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cpi_text import load_text, parse_data_month, sections_of  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REL = ROOT / ".cache/releases"
EXT_PREF = ("txt", "htm", "pdf")


def pick(date: str):
    for ext in EXT_PREF:
        p = REL / f"{date}.{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def main() -> None:
    dates = sorted({p.stem for p in REL.iterdir() if p.suffix in (".txt", ".htm", ".pdf")})
    era_by_dm, rows = {}, []
    era_count = Counter()
    sec_by_era = defaultdict(Counter)
    chars = defaultdict(list)
    problems = []
    dm_seen = defaultdict(list)

    for date in dates:
        p = pick(date)
        text = load_text(p)
        dm = parse_data_month(text)
        era, sec, diag = sections_of(text)
        era_count[era] += 1
        for k, v in sec.items():
            sec_by_era[era][k] += 1
            chars[(era, k)].append(len(v))
        if dm:
            dm_seen[dm].append(date)
        else:
            problems.append((date, p.suffix, "no_data_month", diag))
        if era == "unknown" or not sec:
            problems.append((date, p.suffix, f"era={era} nsec={len(sec)}", diag))
        era_by_dm[dm or date] = era
        rows.append({"date": date, "ext": p.suffix, "dm": dm, "era": era,
                     "sections": {k: len(v) for k, v in sec.items()}, **diag})

    print(f"releases: {len(dates)}")
    print("era:", dict(era_count))
    print(f"distinct data months: {len(dm_seen)}  dups: "
          f"{ {k: v for k, v in dm_seen.items() if len(v) > 1} }")
    for era in ("group", "heading"):
        print(f"\n--- era {era} ({era_count[era]} releases) ---")
        for k, n in sec_by_era[era].most_common():
            cs = sorted(chars[(era, k)])
            print(f"  {k:16} present in {n:4}/{era_count[era]:4} "
                  f"({100*n/max(1,era_count[era]):5.1f}%)  chars min={cs[0]:5} "
                  f"med={cs[len(cs)//2]:5} max={cs[-1]:6}")
        tot = sum(sec_by_era[era].values())
        print(f"  => {tot} section-records ({tot/max(1,era_count[era]):.2f} per release)")

    print(f"\nproblems: {len(problems)}")
    for row in problems[:40]:
        print("  ", row)

    # era boundary
    print("\nera by year:")
    byyear = defaultdict(Counter)
    for r in rows:
        byyear[(r["dm"] or r["date"])[:4]][r["era"]] += 1
    for y in sorted(byyear):
        print(f"  {y}: {dict(byyear[y])}")

    (ROOT / ".cache/probe/census.json").write_text(json.dumps(rows, indent=0))


if __name__ == "__main__":
    main()
