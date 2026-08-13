#!/usr/bin/env python3
"""Records / timesteps / datapoints straight off the built JSONL, for REVIEW_STATUS.md.

Counted from the series themselves, never from meta.n_plays: meta is a claim about the record and
the series is the record. Definitions match the ones REVIEW_STATUS.md states:

  timesteps   time positions in the series, a multi-channel step counted ONCE
  datapoints  individual numeric values (timesteps x channels), the real payload the model reads

Usage:
    python scripts/corpus_totals.py                       # main build
    python scripts/corpus_totals.py --shards output/shards_pre2012
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--shards", default="output/shards")
    args = ap.parse_args()

    recs = ts = dp = 0
    by_league = collections.defaultdict(lambda: [0, 0, 0])
    mismatched = 0
    for sp in sorted(Path(args.shards).glob("*.jsonl")):
        with sp.open() as fh:
            for line in fh:
                if not line.strip():
                    continue
                r = json.loads(line)
                chans = r["timeseries"]
                lens = {len(c["values"]) for c in chans}
                if len(lens) != 1:
                    mismatched += 1          # parallel channels are asserted in aggregate.py
                steps = max(lens)
                recs += 1
                ts += steps
                dp += steps * len(chans)
                row = by_league[r["meta"]["league"]]
                row[0] += 1
                row[1] += steps
                row[2] += steps * len(chans)

    print(f"{'league':8s} {'records':>9s} {'timesteps':>12s} {'datapoints':>13s}")
    for k, v in sorted(by_league.items()):
        print(f"{k:8s} {v[0]:>9,d} {v[1]:>12,d} {v[2]:>13,d}")
    print(f"{'TOTAL':8s} {recs:>9,d} {ts:>12,d} {dp:>13,d}")
    if mismatched:
        print(f"⚠️  {mismatched} records have non-parallel channels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
