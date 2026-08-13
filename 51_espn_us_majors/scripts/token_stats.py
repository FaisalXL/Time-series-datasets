#!/usr/bin/env python3
"""How long are these recaps in tokens, and does truncating them cost the alignment evidence?

`65_espn_college` had to answer this when a reviewer asked whether the recaps blow a 500-token
budget. The answer there was that AP writes the score into the lede, so a 500-token cut loses zero
anchors. The pro-league recaps are LONGER than the college ones, so the question has to be asked
again rather than inherited — this script is the measurement.

Two things are reported:

  * the token distribution per league (cl100k_base, the same encoding 65 used, so the two packages
    are comparable);
  * what a truncation would COST: the share of records whose final-score anchor still appears in
    the first N tokens. That is the number that decides whether truncation is safe here, and it is
    the same question `61_ons_statistical_bulletins` answered the other way — there a token cap
    orphaned recited values in 92% of records and forced a split-don't-cut rule.

Usage:
    python scripts/token_stats.py
    python scripts/token_stats.py --sample 4000 --budgets 512,1024
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import tiktoken

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(HERE))
from verify_alignment import pairs_in, anchors                     # noqa: E402


def pct(xs, p):
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))] if xs else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--shards", default=str(PKG / "output" / "shards"))
    ap.add_argument("--sample", type=int, default=3000, help="records per league")
    ap.add_argument("--budgets", default="500,1024,2048")
    ap.add_argument("--out", default=str(PKG / "output" / "token_stats.json"))
    args = ap.parse_args()

    budgets = [int(x) for x in args.budgets.split(",")]
    enc = tiktoken.get_encoding("cl100k_base")
    shards = sorted(Path(args.shards).glob("*.jsonl"))
    if not shards:
        print(f"no shards in {args.shards}")
        return 1

    toks = collections.defaultdict(list)
    # anchor retention: league -> budget -> hits
    kept = collections.defaultdict(lambda: collections.Counter())
    have_anchor = collections.Counter()
    lede = collections.Counter()
    n_seen = collections.Counter()

    for sp in shards:
        with sp.open() as fh:
            shard_n = sum(1 for _ in fh)
        # Strided so the sample spans the whole shard, not its head: shards are ordered
        # oldest-first and recap length moves with era.
        stride = max(1, shard_n // max(1, args.sample // max(1, len(shards) // 3)))
        with sp.open() as fh:
            for i, line in enumerate(fh):
                if i % stride or not line.strip():
                    continue
                r = json.loads(line)
                lg = r["meta"]["league"]
                if len(toks[lg]) >= args.sample:
                    continue
                text = r["text"]
                ids = enc.encode(text)
                toks[lg].append(len(ids))
                n_seen[lg] += 1
                fin, _ = anchors(r)
                if not fin:
                    continue
                have_anchor[lg] += 1
                for b in budgets:
                    head = enc.decode(ids[:b])
                    if fin in pairs_in(head):
                        kept[lg][b] += 1
                # "lede" = the first paragraph of the recap
                if fin in pairs_in(text.split("\n\n")[0]):
                    lede[lg] += 1
        print(f"  {sp.name}: sampled {n_seen}", flush=True)

    report = {}
    allt = []
    for lg, xs in sorted(toks.items()):
        xs = sorted(xs)
        allt += xs
        row = {
            "sampled": len(xs), "median": pct(xs, 50),
            "p25": pct(xs, 25), "p75": pct(xs, 75),
            "p90": pct(xs, 90), "p99": pct(xs, 99), "max": xs[-1] if xs else None,
        }
        for b in budgets:
            row[f"share_over_{b}"] = round(sum(1 for x in xs if x > b) / len(xs), 4) if xs else None
        row["anchor_available"] = have_anchor[lg]
        for b in budgets:
            row[f"anchor_kept_at_{b}"] = (round(kept[lg][b] / have_anchor[lg], 4)
                                          if have_anchor[lg] else None)
            row[f"anchor_lost_at_{b}"] = have_anchor[lg] - kept[lg][b]
        row["anchor_in_first_paragraph"] = (round(lede[lg] / have_anchor[lg], 4)
                                            if have_anchor[lg] else None)
        report[lg] = row
        print(f"\n{lg}: median {row['median']} tokens  p90 {row['p90']}  max {row['max']}")
        for b in budgets:
            print(f"   over {b}: {row[f'share_over_{b}']:.1%}   anchor kept at {b}: "
                  f"{row[f'anchor_kept_at_{b}']}  (lost {row[f'anchor_lost_at_{b}']})")
        print(f"   anchor already in first paragraph: {row['anchor_in_first_paragraph']}")

    allt.sort()
    report["ALL"] = {"sampled": len(allt), "median": pct(allt, 50), "p90": pct(allt, 90),
                     "p99": pct(allt, 99), "max": allt[-1] if allt else None,
                     **{f"share_over_{b}": round(sum(1 for x in allt if x > b) / len(allt), 4)
                        for b in budgets}}
    print(f"\nALL: median {report['ALL']['median']}  p90 {report['ALL']['p90']}  "
          f"max {report['ALL']['max']}")
    Path(args.out).write_text(json.dumps(report, indent=1))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
