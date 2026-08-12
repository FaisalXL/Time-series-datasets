#!/usr/bin/env python3
"""Verify that each record's prose actually belongs to the series it is paired with, and that the
series itself is healthy.

`alignment: describes` is a weak claim on its face — a recap "describing the shape of the game"
is unfalsifiable if nothing is checked. But these recaps contain a hard, checkable anchor: they
state the final score, and the final score is literally the last value of both channels. So the
pairing is testable at the VALUE level even though the tag stays `describes` (only the terminal
point is quoted, not the 160-point path, so `recites` would overclaim — see SCHEMA.md §7).

Two anchors are tested, each against a permutation control:

  final_pair     the record's (away_final, home_final) appears as a score pair in its own prose
  halftime_pair  the score at the end of period 1 appears as a pair in its own prose
                 (basketball recaps routinely give the halftime score; football's is rarer)

The control is the lesson from #61: a match rate means nothing without a coincidence floor. Here
the control pairs record i's PROSE with a DIFFERENT GAME's series values — same league, same
season, same house style, different game — and asks the same question. Scores are small integers
and recaps are full of them, so the floor is not zero and has to be measured rather than assumed.
Shifts are several distinct offsets so the floor is not itself a coincidence.

Series health is checked separately and is not statistical: cumulative score channels must be
non-decreasing, parallel in length, free of nulls, non-flat, and must land on the official final.

Usage:
    python scripts/verify_alignment.py --input output/espn_college_cpt.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import re
from pathlib import Path

# "78-72", "78 - 72", en/em dashes. Bounded to 1-3 digits: a 4-digit run is a year, not a score.
PAIR_RE = re.compile(r"\b(\d{1,3})\s*[-–—]\s*(\d{1,3})\b")
SHIFTS = (1, 7, 23, 101)


def pairs_in(text: str) -> set[tuple[int, int]]:
    """Unordered score pairs mentioned in the prose. Unordered because a recap writes the winner
    first ("won 78-72") regardless of which team is the home side in the series."""
    out = set()
    for a, b in PAIR_RE.findall(text or ""):
        x, y = int(a), int(b)
        out.add((min(x, y), max(x, y)))
    return out


def norm(a, b) -> tuple[int, int]:
    return (min(a, b), max(a, b))


def series_health(rec: dict) -> list[str]:
    """-> list of problems. Empty means healthy."""
    bad = []
    ts = rec.get("timeseries") or []
    if len(ts) != 2:
        bad.append(f"expected 2 channels, got {len(ts)}")
        return bad
    lens = {len(c.get("values") or []) for c in ts}
    if len(lens) != 1:
        bad.append(f"channels not parallel: lengths {sorted(lens)}")
    for c in ts:
        vals = c.get("values") or []
        unit = c.get("unit")
        if not vals:
            bad.append(f"{unit}: empty")
            continue
        if any(v is None for v in vals):
            bad.append(f"{unit}: contains null")
            continue
        if any(vals[i + 1] < vals[i] for i in range(len(vals) - 1)):
            bad.append(f"{unit}: cumulative score decreases")
        if len(set(vals)) == 1:
            # A flat channel passes min_plays but carries no information: the play feed had no
            # score fields at all, and the "series" is a constant.
            bad.append(f"{unit}: flat ({vals[0]} for all {len(vals)} plays)")
    meta = rec.get("meta") or {}
    away, home = ts[0]["values"], ts[1]["values"]
    if away and meta.get("final_away_score") is not None and away[-1] != meta["final_away_score"]:
        bad.append("away channel does not end on the recorded final")
    if home and meta.get("final_home_score") is not None and home[-1] != meta["final_home_score"]:
        bad.append("home channel does not end on the recorded final")
    return bad


def anchors(rec: dict):
    """-> (final_pair, halftime_pair | None) taken from the SERIES, not from meta text fields."""
    ts = rec["timeseries"]
    away, home = ts[0]["values"], ts[1]["values"]
    final = norm(away[-1], home[-1]) if away and home else None
    half = None
    bounds = (rec.get("meta") or {}).get("period_end_idx") or []
    if bounds and len(bounds) >= 2:
        i = bounds[0]
        if 0 <= i < len(away):
            half = norm(away[i], home[i])
    return final, half


def poisson_like_z(hit: int, n: int, p0: float) -> float:
    """Normal-approx z of `hit` successes in `n` trials against the control rate p0. Reported as a
    sanity magnitude only — the headline numbers are the two rates themselves."""
    if n == 0 or p0 <= 0:
        return float("inf") if hit else 0.0
    mu = n * p0
    sd = math.sqrt(n * p0 * (1 - p0)) or 1e-9
    return (hit - mu) / sd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", default="output/espn_college_cpt.jsonl")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    recs = [json.loads(l) for l in Path(args.input).read_text().splitlines() if l.strip()]
    print(f"{len(recs)} records from {args.input}\n")

    # --- series health -----------------------------------------------------------------------
    problems = collections.Counter()
    unhealthy = []
    for r in recs:
        bad = series_health(r)
        for b in bad:
            problems[re.sub(r"^\w+_score_cumulative: ", "", b)] += 1
        if bad:
            unhealthy.append((r["meta"]["event_id"], bad))
    print("== series health ==")
    print(f"  healthy: {len(recs) - len(unhealthy)}/{len(recs)}")
    for k, v in problems.most_common():
        print(f"    {v:5d}  {k}")
    if unhealthy[:3]:
        for eid, bad in unhealthy[:3]:
            print(f"    e.g. {eid}: {bad}")

    lens = sorted(len(r["timeseries"][0]["values"]) for r in recs)
    if lens:
        print(f"  series length: min {lens[0]}  p25 {lens[len(lens)//4]}  "
              f"median {lens[len(lens)//2]}  max {lens[-1]}")

    # --- alignment: real vs permutation control ----------------------------------------------
    by_league = collections.defaultdict(list)
    for r in recs:
        by_league[r["meta"]["league"]].append(r)

    print("\n== alignment (final-score anchor, vs permutation control) ==")
    summary = {}
    for league, rs in sorted(by_league.items()):
        texts = [r["text"] for r in rs]
        pairsets = [pairs_in(t) for t in texts]
        fin, half = zip(*(anchors(r) for r in rs)) if rs else ((), ())

        real_f = sum(1 for i in range(len(rs)) if fin[i] and fin[i] in pairsets[i])
        # control: prose i vs a different game's finals, several offsets
        ctl_f = ctl_n = 0
        for s in SHIFTS:
            if len(rs) <= s:
                continue
            for i in range(len(rs)):
                j = (i + s) % len(rs)
                if fin[j] is None or fin[j] == fin[i]:
                    continue                     # identical scoreline: not a usable control trial
                ctl_n += 1
                if fin[j] in pairsets[i]:
                    ctl_f += 1
        p_real = real_f / len(rs) if rs else 0.0
        p_ctl = ctl_f / ctl_n if ctl_n else 0.0
        z = poisson_like_z(real_f, len(rs), max(p_ctl, 1e-6))

        n_half = sum(1 for h in half if h)
        real_h = sum(1 for i in range(len(rs)) if half[i] and half[i] in pairsets[i])
        ctl_h = ctl_hn = 0
        for s in SHIFTS:
            if len(rs) <= s:
                continue
            for i in range(len(rs)):
                j = (i + s) % len(rs)
                if not half[j] or half[j] == half[i]:
                    continue
                ctl_hn += 1
                if half[j] in pairsets[i]:
                    ctl_h += 1
        ph_real = real_h / n_half if n_half else None
        ph_ctl = ctl_h / ctl_hn if ctl_hn else None

        summary[league] = {
            "records": len(rs),
            "final_pair_real": round(p_real, 3),
            "final_pair_control": round(p_ctl, 3),
            "final_pair_lift": round(p_real / p_ctl, 1) if p_ctl else None,
            "final_pair_z": round(z, 1),
            "halftime_anchor_available": n_half,
            "halftime_pair_real": round(ph_real, 3) if ph_real is not None else None,
            "halftime_pair_control": round(ph_ctl, 3) if ph_ctl is not None else None,
        }
        print(f"  {league}: n={len(rs)}")
        print(f"     final score in own prose      : {real_f}/{len(rs)} = {p_real:.3f}")
        print(f"     other game's final in prose   : {ctl_f}/{ctl_n} = {p_ctl:.3f}  "
              f"(lift {p_real/p_ctl:.1f}x, z={z:.1f})" if ctl_n and p_ctl else
              f"     other game's final in prose   : {ctl_f}/{ctl_n}")
        if n_half:
            print(f"     halftime in own prose         : {real_h}/{n_half} = {ph_real:.3f}"
                  + (f"   control {ph_ctl:.3f}" if ph_ctl is not None else ""))
        else:
            print("     halftime anchor               : unavailable "
                  "(no meta.period_end_idx — rebuild to enable)")

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"records": len(recs), "unhealthy": len(unhealthy),
             "health_problems": dict(problems), "alignment": summary}, indent=1))
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
