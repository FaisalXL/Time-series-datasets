#!/usr/bin/env python3
"""Aggregate the harvest shards: one streaming pass that counts, audits and verifies.

Deliberately streaming. `verify_alignment.py` reads every record into a list, which is right for a
capped demo and impossible at harvest scale — 40,000 records of recap prose is several gigabytes.
Everything here is computed incrementally, and the only things held in memory are counters, the
series_id set (needed to prove uniqueness), the text-hash groups, and a strided per-league sample
used for the permutation control.

What it checks, beyond counting:

  * **series_id uniqueness across ALL shards.** NBA/NFL/NHL are three distinct ESPN slugs so they
    cannot collide the way `65`'s FBS and FCS tiers can, but a shard built twice under different
    year cells would show up here. Reported as a hard failure, not a warning.
  * **Series health on every record**, not a sample — non-decreasing, parallel, null-free,
    non-flat, landing on the official final.
  * **The alignment anchors against a permutation control**, on a strided sample per league. The
    sample is strided rather than head-of-file because shards are ordered oldest-first and recap
    style moves with era.
  * **Shared recaps resolved, not just counted.** ESPN serves one recap for two different games in
    a small number of cases, and every id-level check passes those cleanly — only the text hash
    catches them. When two games carry the same recap at most one of them is the game that recap
    describes, and the final-score anchor says which; when the anchor cannot say, the whole group
    is excluded rather than guessed at.
  * **The full source distribution including rejections**, so the AP share is a measured output of
    the harvest rather than a separate estimate.

Usage:
    python scripts/aggregate.py
    python scripts/aggregate.py --sample-per-league 4000 --out output/harvest_summary.json
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(HERE))
from verify_alignment import (pairs_in, anchors, series_health, team_anchor,   # noqa: E402
                             names_in, SHIFTS, poisson_like_z)


def pctile(xs, p):
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--shards", default=str(PKG / "output" / "shards"))
    ap.add_argument("--sample-per-league", type=int, default=3000)
    ap.add_argument("--out", default=str(PKG / "output" / "harvest_summary.json"))
    args = ap.parse_args()

    shard_dir = Path(args.shards)
    shards = sorted(shard_dir.glob("*.jsonl"))
    if not shards:
        print(f"no shards in {shard_dir}")
        return 1

    seen_ids: set[str] = set()
    dupes: list[str] = []
    # Text distinctness matters independently of series_id uniqueness: 45_cricket_report_overseries
    # carried 2.24x text duplication in its old record shape while every id was distinct, because
    # the same report was paired with several windows. Hashes, not the texts themselves -- 150k
    # recaps is about a gigabyte.
    # hash -> [(series_id, states_own_final, shard)] so a shared text can be RESOLVED rather than
    # just counted: when two games carry the same recap, at most one of them can be the game that
    # recap describes, and the final-score anchor says which.
    text_groups: collections.defaultdict = collections.defaultdict(list)
    seen_text: set[str] = set()
    dup_text = 0
    anchor_hits = anchor_total = joint_hits = 0
    n = 0
    by_league = collections.Counter()
    by_league_year = collections.Counter()
    srcs = collections.Counter()
    health = collections.Counter()
    unhealthy_n = 0
    unhealthy_examples: list = []
    plays, chars, fixfrac, clampfrac = [], [], [], []
    clamped_records = 0
    # Strided sample per league for the control test; `keep_every` is set per shard below.
    sample = collections.defaultdict(list)

    for sp in shards:
        # A record count for the shard first, so the stride can be computed without a second pass.
        with sp.open() as fh:
            shard_n = sum(1 for _ in fh)
        league = sp.stem.split("_")[0]
        want = max(1, args.sample_per_league // max(1, len(shards) // 4))
        stride = max(1, shard_n // max(1, want))
        with sp.open() as fh:
            for i, line in enumerate(fh):
                if not line.strip():
                    continue
                r = json.loads(line)
                n += 1
                m = r["meta"]
                sid = r.get("series_id")
                if sid in seen_ids:
                    dupes.append(sid)
                else:
                    seen_ids.add(sid)
                # The final-score anchor is computed for EVERY record, not just the control
                # sample: it is a plain regex over the text, and having it per record is what makes
                # duplicate-text resolution possible and turns the headline alignment rate into a
                # census rather than an estimate. The permutation CONTROL still needs the sample,
                # because it pairs records against each other.
                psets = pairs_in(r["text"])
                fin_i, half_i = anchors(r)
                teams_i = team_anchor(r)
                states_own = bool(fin_i and fin_i in psets)
                anchor_total += 1
                anchor_hits += int(states_own)
                if states_own and names_in(r["text"], teams_i):
                    joint_hits += 1
                th = hashlib.blake2b(r["text"].encode(), digest_size=16).hexdigest()
                text_groups[th].append((sid, states_own, sp.name))
                if th in seen_text:
                    dup_text += 1
                else:
                    seen_text.add(th)
                by_league[m["league"]] += 1
                by_league_year[f"{m['league']}_{sp.stem.split('_')[-1]}"] += 1
                srcs[m["report_source"]] += 1
                plays.append(m["n_plays"])
                chars.append(m["report_chars"])
                fixfrac.append(m["score_fix_plays"] / max(1, m["n_plays"]))
                cl = m.get("score_clamp_plays") or 0
                clamped_records += int(bool(cl))
                clampfrac.append(cl / max(1, m["n_plays"]))
                bad = series_health(r)
                if bad:
                    unhealthy_n += 1
                for b in bad:
                    health[b.split(": ", 1)[-1]] += 1
                if bad and len(unhealthy_examples) < 10:
                    unhealthy_examples.append({"event_id": m["event_id"], "problems": bad})
                if i % stride == 0 and len(sample[m["league"]]) < args.sample_per_league:
                    sample[m["league"]].append((psets, fin_i, half_i, teams_i, r["text"]))
        print(f"  {sp.name}: {shard_n:,} records", flush=True)

    plays.sort(); chars.sort(); fixfrac.sort(); clampfrac.sort()

    # --- alignment on the sample --------------------------------------------------------------
    align = {}
    for league, rows in sorted(sample.items()):
        psets = [x[0] for x in rows]
        fin = [x[1] for x in rows]
        half = [x[2] for x in rows]
        real_f = sum(1 for i in range(len(rows)) if fin[i] and fin[i] in psets[i])
        ctl_f = ctl_n = 0
        for s in SHIFTS:
            if len(rows) <= s:
                continue
            for i in range(len(rows)):
                j = (i + s) % len(rows)
                if fin[j] is None or fin[j] == fin[i]:
                    continue
                ctl_n += 1
                if fin[j] in psets[i]:
                    ctl_f += 1
        nh = sum(1 for h in half if h)
        real_h = sum(1 for i in range(len(rows)) if half[i] and half[i] in psets[i])
        ctl_h = ctl_hn = 0
        for s in SHIFTS:
            if len(rows) <= s:
                continue
            for i in range(len(rows)):
                j = (i + s) % len(rows)
                if not half[j] or half[j] == half[i]:
                    continue
                ctl_hn += 1
                if half[j] in psets[i]:
                    ctl_h += 1
        # --- entity anchor, and the joint test -------------------------------------------------
        # The value anchor above is not equally strong per league: NHL finals occupy 39 distinct
        # pairs, so its control sits near 14% while NBA's is 0.2%. The joint test — this game's
        # final score AND both of this game's team nicknames present in the prose — is what makes
        # the claim decisive in every league, and it is tested against the same permutation
        # control: prose i against game j's score AND game j's teams.
        teams = [x[3] for x in rows]
        texts = [x[4] for x in rows]
        real_t = sum(1 for i in range(len(rows)) if names_in(texts[i], teams[i]))
        real_j = sum(1 for i in range(len(rows))
                     if fin[i] and fin[i] in psets[i] and names_in(texts[i], teams[i]))
        ctl_t = ctl_j = ctl_jn = 0
        for s in SHIFTS:
            if len(rows) <= s:
                continue
            for i in range(len(rows)):
                j = (i + s) % len(rows)
                if not teams[j] or teams[j] == teams[i]:
                    continue
                ctl_jn += 1
                hit_t = names_in(texts[i], teams[j])
                ctl_t += int(hit_t)
                if hit_t and fin[j] is not None and fin[j] in psets[i]:
                    ctl_j += 1

        p_real = real_f / len(rows) if rows else 0
        p_ctl = ctl_f / ctl_n if ctl_n else 0
        pj_real = real_j / len(rows) if rows else 0
        pj_ctl = ctl_j / ctl_jn if ctl_jn else 0
        align[league] = {
            "sampled": len(rows),
            "final_pair_real": round(p_real, 4),
            "final_pair_control": round(p_ctl, 4),
            "final_pair_lift": round(p_real / p_ctl, 1) if p_ctl else None,
            "final_pair_z": round(poisson_like_z(real_f, len(rows), max(p_ctl, 1e-6)), 1),
            "teams_real": round(real_t / len(rows), 4) if rows else None,
            "teams_control": round(ctl_t / ctl_jn, 5) if ctl_jn else None,
            "joint_real": round(pj_real, 4),
            "joint_control": round(pj_ctl, 6),
            "joint_control_trials": ctl_jn,
            # A control that never once fired has no finite lift, and reporting None reads as
            # "not measured" when it is the strongest result available. Report the bound the
            # trial count supports instead.
            "joint_lift": (round(pj_real / pj_ctl, 1) if pj_ctl
                           else f">{int(pj_real * ctl_jn):,} (0 control hits in {ctl_jn:,} trials)"),
            "joint_z": round(poisson_like_z(real_j, len(rows), max(pj_ctl, 1e-6)), 1),
            "period1_real": round(real_h / nh, 4) if nh else None,
            "period1_control": round(ctl_h / ctl_hn, 4) if ctl_hn else None,
        }
        a = align[league]
        print(f"  {league} (n={len(rows)}): score {p_real:.4f} vs ctl {p_ctl:.4f} "
              f"(lift {a['final_pair_lift']}) | teams {a['teams_real']} vs ctl {a['teams_control']}"
              f" | JOINT {pj_real:.4f} vs ctl {pj_ctl:.6f} (lift {a['joint_lift']})", flush=True)

    summary = {
        "shards": len(shards),
        "records": n,
        "series_id_unique": len(seen_ids) == n,
        "duplicate_series_ids": len(dupes),
        "distinct_texts": len(seen_text),
        "duplicate_texts": dup_text,
        "distinct_text_share": round(len(seen_text) / n, 5) if n else None,
        "final_anchor_all_records": round(anchor_hits / anchor_total, 5) if anchor_total else None,
        # The headline claim, over EVERY record rather than the control sample: the prose states
        # this game's final score and names both of this game's teams.
        "joint_anchor_all_records": round(joint_hits / anchor_total, 5) if anchor_total else None,
        "duplicate_examples": dupes[:5],
        "by_league": dict(by_league),
        "by_league_year": dict(sorted(by_league_year.items())),
        "report_sources": dict(srcs),
        "series_healthy": n - unhealthy_n,
        "series_unhealthy": unhealthy_n,
        "health_problems": dict(health),
        "health_examples": unhealthy_examples,
        "n_plays": {"min": plays[0] if plays else None, "p50": pctile(plays, 50),
                    "p99": pctile(plays, 99), "max": plays[-1] if plays else None},
        "report_chars": {"min": chars[0] if chars else None, "p50": pctile(chars, 50),
                         "p99": pctile(chars, 99), "max": chars[-1] if chars else None},
        "score_fix_frac": {"p50": round(pctile(fixfrac, 50) or 0, 4),
                           "p90": round(pctile(fixfrac, 90) or 0, 4),
                           "p99": round(pctile(fixfrac, 99) or 0, 4),
                           "max": round(fixfrac[-1], 4) if fixfrac else None},
        # Plays whose raw score exceeded the official final and were ignored as impossible. This
        # is the correction that rescued the games the official-final check used to drop; the
        # distribution is here so the cut can be judged rather than trusted.
        "records_needing_clamp": clamped_records,
        "score_clamp_frac": {"p50": round(pctile(clampfrac, 50) or 0, 4),
                             "p99": round(pctile(clampfrac, 99) or 0, 4),
                             "max": round(clampfrac[-1], 4) if clampfrac else None},
        "alignment": align,
    }
    # --- resolve shared recaps -----------------------------------------------------------------
    excl, unresolved = [], []
    for th, rows in text_groups.items():
        if len(rows) < 2:
            continue
        verified = [x for x in rows if x[1]]
        if len(verified) == 1:
            keep = verified[0][0]
            excl += [{"series_id": sid, "reason": "shared recap text; final-score anchor "
                      "attributes this recap to another game", "kept_instead": keep}
                     for sid, _, _ in rows if sid != keep]
        else:
            # Nobody verifies, or several do: the anchor cannot say which game the recap belongs
            # to, so exclude the whole group rather than guess.
            excl += [{"series_id": sid, "reason": "shared recap text, unresolvable by anchor"}
                     for sid, _, _ in rows]
            unresolved.append([sid for sid, _, _ in rows])
    summary["shared_text_exclusions"] = len(excl)
    summary["shared_text_unresolvable_groups"] = len(unresolved)
    if excl:
        Path(args.out).with_name("exclusions.json").write_text(json.dumps(excl, indent=1))
        print(f"\n{len(excl)} record(s) excluded for a shared recap -> exclusions.json")
    Path(args.out).write_text(json.dumps(summary, indent=1))
    print("\n" + json.dumps({k: v for k, v in summary.items()
                             if k not in ("by_league_year", "health_examples")}, indent=1))
    if dupes:
        print(f"\n❌ {len(dupes)} DUPLICATE series_id -- de-overlap failed, do not bank this")
        return 2
    if health:
        print(f"\n⚠️  {sum(health.values())} health problems across {n:,} records")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
