#!/usr/bin/env python3
"""Survey cached USDM narratives: section labels by era, region mapping, bylines."""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from usdm_parse import (  # noqa: E402
    XML_DIR, parse_week, map_region, is_non_conus, norm_label,
    classify_affiliation, week_license,
)

weeks = sorted(p.stem for p in XML_DIR.glob("*.xml"))
print(f"cached weeks: {len(weeks)}  {weeks[0]}..{weeks[-1]}\n")

parsed, bad = [], []
for w in weeks:
    d = parse_week(w)
    (parsed if d else bad).append(d or w)
print(f"parsed OK: {len(parsed)}   parse-failed: {len(bad)}")
if bad:
    print("  failures:", bad[:20])

# --- section labels by era -----------------------------------------------
by_year_mapped = collections.defaultdict(list)
label_counts = collections.Counter()
unmapped = collections.Counter()
for d in parsed:
    yr = int(d["date8"][:4])
    n_mapped = 0
    for label, mapped, text in d["regions"]:
        label_counts[norm_label(label)] += 1
        if mapped:
            n_mapped += 1
        elif not is_non_conus(label):
            unmapped[norm_label(label)] += 1
    by_year_mapped[yr].append(n_mapped)

print("\n=== mapped official regions per week, by year ===")
print(f"{'yr':>5} {'wks':>4} {'mean':>6} {'min':>4} {'max':>4}  {'==6':>5}")
for yr in sorted(by_year_mapped):
    v = by_year_mapped[yr]
    full = sum(1 for x in v if x == 6)
    print(f"{yr:>5} {len(v):>4} {sum(v)/len(v):>6.2f} {min(v):>4} {max(v):>4}  "
          f"{full:>4}/{len(v)}")

print("\n=== top unmapped CONUS section labels ===")
for lab, n in unmapped.most_common(25):
    print(f"{n:>5}  {lab}")

# --- bylines --------------------------------------------------------------
aff_counts = collections.Counter()
lic_counts = collections.Counter()
lic_by_era = collections.defaultdict(collections.Counter)
nauth = collections.Counter()
for d in parsed:
    nauth[len(d["authors"])] += 1
    for _, aff in d["authors"]:
        aff_counts[(aff, classify_affiliation(aff))] += 1
    lic, why = week_license(d["authors"])
    lic_counts[(lic, why)] += 1
    lic_by_era[int(d["date8"][:4])][lic] += 1

print("\n=== authors per week ===", dict(sorted(nauth.items())))
print("\n=== affiliations (top 30) ===")
for (aff, kind), n in aff_counts.most_common(30):
    print(f"{n:>5}  [{kind:>10}]  {aff[:70]}")
print("\n=== week license verdict ===")
for (lic, why), n in lic_counts.most_common():
    print(f"{n:>5}  {lic:<22} {why}")

# --- text lengths ---------------------------------------------------------
import statistics  # noqa: E402
reg_lens = [len(t) for d in parsed for _, m, t in d["regions"] if m]
intro_lens = [len(d["intro"]) for d in parsed if d["intro"]]
if reg_lens:
    print(f"\nregional section chars: n={len(reg_lens)} min={min(reg_lens)} "
          f"med={statistics.median(reg_lens):.0f} max={max(reg_lens)} "
          f"pct<200={100*sum(1 for x in reg_lens if x<200)/len(reg_lens):.1f}%")
if intro_lens:
    print(f"intro chars:            n={len(intro_lens)} min={min(intro_lens)} "
          f"med={statistics.median(intro_lens):.0f} max={max(intro_lens)}")
