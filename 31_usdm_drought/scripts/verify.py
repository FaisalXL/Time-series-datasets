#!/usr/bin/env python3
"""Final-inspection pass for 31_usdm_drought.

Checks the three things the corpus checklist requires -- reconcile, exhaustion,
alignment -- plus the two defects that sank earlier banks (generated text,
short series).
"""
from __future__ import annotations

import collections
import json
import re
import statistics
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]


def load(p: Path):
    return [json.loads(l) for l in p.open(encoding="utf-8")]


def pct(n, d):
    return f"{100*n/d:.2f}%" if d else "n/a"


def main(path: Path):
    recs = load(path)
    n = len(recs)
    print(f"=== {path.name}: {n} records ===\n")

    # -- 1. series length ---------------------------------------------------
    lens = [len(s["values"]) for r in recs for s in r["timeseries"]]
    per = [len(r["timeseries"][0]["values"]) for r in recs]
    nch = collections.Counter(len(r["timeseries"]) for r in recs)
    print("-- series length --")
    print(f"points/record: min={min(per)} med={statistics.median(per):.0f} max={max(per)}")
    print(f"all channels equal length within record: "
          f"{all(len({len(s['values']) for s in r['timeseries']})==1 for r in recs)}")
    print(f"channels/record: {dict(nch)}")
    print(f">=32 points: {pct(sum(1 for x in per if x>=32), n)}   "
          f"datapoints total: {sum(lens):,}")

    # -- 2. one <ts>, no generated text ------------------------------------
    ts_counts = collections.Counter(r["text"].count("<ts></ts>") for r in recs)
    tails = [r["text"].split("<ts></ts>")[0].strip()[-160:] for r in recs]
    print("\n-- text integrity --")
    print(f"<ts> per record: {dict(ts_counts)}")
    print(f"distinct 160-char tails before <ts>: {len(set(tails))}/{n} "
          f"({pct(len(set(tails)), n)})  [1.00 => no script-written intro]")
    bodies = [r["text"].split("<ts></ts>")[0].strip() for r in recs]
    print(f"distinct full texts: {len(set(bodies))}/{n} ({pct(len(set(bodies)), n)})")
    tl = [len(b) for b in bodies]
    print(f"text chars: min={min(tl)} med={statistics.median(tl):.0f} max={max(tl)}")

    # -- 3. identity / duplication -----------------------------------------
    sids = [r["series_id"] for r in recs]
    print("\n-- identity --")
    print(f"duplicate series_id: {n - len(set(sids))}")
    keys = [(r["meta"]["usdm_region"], tuple(r["timeseries"][0]["values"][-8:]))
            for r in recs]
    print(f"distinct (region, last-8-values): {len(set(keys))}/{n} "
          f"({pct(len(set(keys)), n)})")

    # -- 4. structural alignment -------------------------------------------
    print("\n-- alignment: structural --")
    term_ok = sum(1 for r in recs
                  if r["timestamps"][-1] == r["meta"]["valid_week"])
    print(f"window terminal == reported week: {term_ok}/{n} ({pct(term_ok, n)})")
    per_end = sum(1 for r in recs if r["period_end"] == r["meta"]["valid_week"])
    print(f"period_end == reported week:      {per_end}/{n} ({pct(per_end, n)})")
    val_ok = sum(1 for r in recs
                 if abs(r["timeseries"][0]["values"][-1]
                        - r["meta"]["terminal_values"]["d0"]) < 1e-9)
    print(f"terminal D0 == meta terminal:     {val_ok}/{n} ({pct(val_ok, n)})")
    mono = sum(1 for r in recs if all(
        r["meta"]["terminal_values"]["d0"] >= r["meta"]["terminal_values"]["d1"]
        >= r["meta"]["terminal_values"]["d2"] >= r["meta"]["terminal_values"]["d3"]
        >= r["meta"]["terminal_values"]["d4"] - 1e-9 for _ in (0,)))
    print(f"cumulative D0>=D1>=..>=D4:        {mono}/{n} ({pct(mono, n)})")

    # -- 5. semantic alignment: does the prose's direction match the data? --
    WORSE = re.compile(r"\b(degradation|degraded|degrading|deterior\w+|expansion|"
                       r"expanded|intensif\w+|worsen\w+|introduc\w+ of (?:severe|"
                       r"moderate|extreme)|spread)\w*", re.I)
    BETTER = re.compile(r"\b(improvement|improved|improving|reduction|reduced|"
                        r"contract\w+|recovery|recovered|eas\w+ of drought|"
                        r"removal|removed|eliminat\w+)\b", re.I)
    # The prose usually mentions both directions ("improvements in X, degradation
    # in Y"), so lean on the MARGIN: which direction dominates the paragraph.
    buckets = {"all": [0, 0], "|dDSCI|>=10": [0, 0], "|dDSCI|>=25": [0, 0]}
    skipped = flat = 0
    for r in recs:
        body = r["text"].split("<ts></ts>")[0]
        margin = len(WORSE.findall(body)) - len(BETTER.findall(body))
        if abs(margin) < 2:             # no dominant direction -> no signal
            skipped += 1
            continue
        d = (r["meta"]["terminal_values"]["dsci"]
             - r["meta"]["prev_week_values"]["dsci"])
        if d == 0:
            flat += 1
            continue
        hit = (d > 0) == (margin > 0)
        for name, lim in (("all", 0), ("|dDSCI|>=10", 10), ("|dDSCI|>=25", 25)):
            if abs(d) >= lim:
                buckets[name][0 if hit else 1] += 1
    print("\n-- alignment: semantic (dominant prose direction vs week-over-week DSCI) --")
    print(f"no dominant direction: {skipped}   DSCI flat: {flat}")
    for name, (a, dis) in buckets.items():
        print(f"  {name:>12}: agrees {a}/{a+dis} ({pct(a, a+dis)})")

    # -- 5b. does a region's prose actually discuss that region's states? ---
    STATES = {
        "High Plains": ["Colorado", "Kansas", "Nebraska", "North Dakota",
                        "South Dakota", "Wyoming"],
        "Midwest": ["Iowa", "Illinois", "Indiana", "Kentucky", "Michigan",
                    "Minnesota", "Missouri", "Ohio", "Wisconsin"],
        "Northeast": ["Connecticut", "Delaware", "Maine", "Maryland",
                      "Massachusetts", "New Hampshire", "New Jersey", "New York",
                      "Pennsylvania", "Rhode Island", "Vermont", "West Virginia"],
        "South": ["Arkansas", "Louisiana", "Mississippi", "Oklahoma",
                  "Tennessee", "Texas"],
        "Southeast": ["Alabama", "Florida", "Georgia", "North Carolina",
                      "South Carolina", "Virginia", "Puerto Rico"],
        "West": ["Arizona", "California", "Idaho", "Montana", "Nevada",
                 "New Mexico", "Oregon", "Utah", "Washington"],
    }
    tot = hit = 0
    misses = []
    for r in recs:
        reg = r["meta"]["usdm_region"]
        if reg not in STATES:
            continue
        body = r["text"].split("<ts></ts>")[0]
        tot += 1
        if reg.lower() in body.lower() or any(s in body for s in STATES[reg]):
            hit += 1
        else:
            misses.append((r["meta"]["valid_week"], reg))
    print("\n-- alignment: regional relevance --")
    print(f"prose names its own region or one of its states: {hit}/{tot} "
          f"({pct(hit, tot)})")
    if misses:
        print(f"  misses (first 10): {misses[:10]}")

    # -- 6. recited numbers -> justify `describes` vs `recites` -------------
    hits = 0
    for r in recs:
        body = r["text"].split("<ts></ts>")[0]
        nums = {round(float(x), 2) for x in re.findall(r"\d+\.\d+", body)}
        tv = {round(v, 2) for v in r["meta"]["terminal_values"].values()}
        if nums & tv:
            hits += 1
    print(f"\n-- alignment tier evidence --")
    print(f"records reciting a terminal value verbatim: {hits}/{n} ({pct(hits, n)})"
          f"  -> `describes` is the honest tier")

    # -- 7. coverage --------------------------------------------------------
    yrs = collections.Counter(r["meta"]["valid_week"][:4] for r in recs)
    regs = collections.Counter(r["meta"]["usdm_region"] for r in recs)
    print("\n-- coverage --")
    print(f"years: {min(yrs)}..{max(yrs)} ({len(yrs)} distinct)")
    print(f"regions: {dict(sorted(regs.items(), key=lambda kv: -kv[1]))}")
    lic = collections.Counter(r["license"] for r in recs)
    print(f"license: {dict(lic)}")
    aln = collections.Counter(r["alignment"] for r in recs)
    print(f"alignment: {dict(aln)}")


if __name__ == "__main__":
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else PKG / "output/usdm_drought_cpt.jsonl"
    main(p)
