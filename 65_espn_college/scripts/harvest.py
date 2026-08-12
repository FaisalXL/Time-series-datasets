#!/usr/bin/env python3
"""Full-scale harvest: one JSONL shard per (league, season), resumable, concurrency-limited.

`build_cpt_jsonl.py` is the right tool for a demo and the wrong one for this universe. The census
measures ~180,000 finished games across 2012-2025, and that builder accumulates every record in a
list before writing — roughly a gigabyte of resident memory at this scale — then writes one
monolithic file that has to be rebuilt from scratch if anything interrupts it.

So the shape here is #61's: shard, then aggregate.

  * **One shard per (league, season).** Bounded memory (a season is ~6,000 records), and a
    natural resume unit: a shard with a report file beside it is done and is skipped.
  * **Record logic is imported, not reimplemented.** `build_record` from the builder stays the
    single definition of what a record is, so the demo and the harvest cannot drift.
  * **Two phases per shard.** Phase 1 fetches summaries through a thread pool into the cache;
    phase 2 builds records serially straight out of the cache. Separating them means the
    concurrency only ever touches I/O, and record construction stays deterministic and ordered.
  * **The source distribution of EVERY game is recorded**, including games the allowlist rejects,
    because `build_record` returns `source_not_allowed:<source>` as its skip reason. The AP share
    per season therefore falls out of the harvest itself rather than needing a separate sample.

Concurrency is a global rate limit, not N independent ones: all workers share one `Fetcher`, whose
inter-request gap is held under a lock. Workers hide per-request latency (~0.5s), which is worth
about 2x on its own; going faster than 1/gap means lowering the gap, and the AIMD backoff widens it
again the moment ESPN answers with a 502.

⚠️ LICENSE: this writes real Associated Press wire prose to disk at scale. output/shards/ is
gitignored and must stay that way. Nothing here is cleared for distribution — see the README.

Usage:
    python scripts/harvest.py --seasons 2012:2025 --workers 8 --delay 0.15
    python scripts/harvest.py --seasons 2012:2013 --leagues CFB --dry-run
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(HERE))
from espnfetch import Fetcher, find_census_cell                          # noqa: E402
from build_cpt_jsonl import build_record, load_config                    # noqa: E402
from census import parse_seasons                                         # noqa: E402


def census_ids(lg: dict, season: int, cache: Path):
    """Event ids for one (league, season) out of the census walk. -> (ids, ok).

    ok is False when the cell does not exist. Only whole cells are ever written, so a cell that
    exists cannot be a throttled undercount.
    """
    fp = find_census_cell(cache, lg, season)
    if fp is None:
        return [], False
    cell = json.loads(fp.read_text())
    ids = [eid for day in sorted(cell["event_ids"]) for eid in cell["event_ids"][day]]
    return ids, True


def drop_overlap(lg: dict, season: int, ids: list[str], cfg: dict, cache: Path):
    """Remove event ids that belong to another tier of the same ESPN league. -> (ids, dropped).

    FBS and FCS football are both `college-football`, separated only by `&groups=81`, and their
    slates overlap (5 of 60 on the day this was measured). Because series_id is
    `espn_<league_slug>_<event_id>`, it does not encode the tier — so a shared game harvested under
    both labels is not a duplicate row to reconcile downstream, it is two records with the SAME
    series_id. Declared per league via `exclude_overlap_with`.
    """
    other_label = lg.get("exclude_overlap_with")
    if not other_label:
        return ids, 0
    other = next((x for x in cfg["data"]["leagues"] if x["label"] == other_label), None)
    if other is None:
        return ids, 0
    other_ids, ok = census_ids(other, season, cache)
    if not ok:
        # No cell for the other tier: refuse to guess. Harvesting anyway would risk the duplicate
        # series_id this function exists to prevent.
        print(f"    ⚠️  {lg['label']} {season}: cannot de-overlap against {other_label} "
              f"(no census cell) -- skipping this shard", flush=True)
        return [], -1
    keep = [i for i in ids if i not in set(other_ids)]
    return keep, len(ids) - len(keep)


def prefetch(ids, lg, cfg, f: Fetcher, workers: int, label: str):
    """Warm the cache for these event ids through a thread pool. Returns seconds elapsed."""
    d = cfg["data"]
    t0 = time.time()

    def one(eid):
        url = d["summary_url"].format(sport=lg["sport"], league=lg["league"], event_id=eid)
        f.cached(url, f"espn/{lg['sport']}/{lg['league']}/{eid}.json")

    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in ex.map(one, ids):
            done += 1
            if done % 500 == 0:
                el = time.time() - t0
                rate = done / el if el else 0
                print(f"    {label}: fetched {done}/{len(ids)}  {rate:.1f} req/s  "
                      f"gap={f.gap:.2f}s throttled={f.stats['throttled']}", flush=True)
    return time.time() - t0


def harvest_cell(lg, season, ids, cfg, f, shard_dir: Path, workers: int):
    """Build one shard. -> stats dict."""
    label = f"{lg['label']} {season}"
    fetched_s = prefetch(ids, lg, cfg, f, workers, label)

    recs, skips = [], collections.Counter()
    for eid in ids:
        rec, why = build_record(eid, lg, cfg, f)
        if rec is None:
            skips[why.split(":")[0]] += 1
            if ":" in why:
                skips[why] += 1
            continue
        recs.append(rec)

    shard = shard_dir / f"{lg['label']}_{season}.jsonl"
    tmp = shard.with_suffix(".jsonl.part")
    with tmp.open("w") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(shard)                       # the shard appears complete or not at all

    fixed = [r["meta"]["score_fix_plays"] for r in recs]
    fracs = sorted(r["meta"]["score_fix_plays"] / max(1, r["meta"]["n_plays"]) for r in recs)
    plays = sorted(r["meta"]["n_plays"] for r in recs)
    chars = sorted(r["meta"]["report_chars"] for r in recs)
    stats = {
        "league": lg["label"], "espn_league_slug": lg["league"], "season": season,
        "games": len(ids), "records": len(recs),
        "yield": round(len(recs) / len(ids), 4) if ids else None,
        "skips": dict(skips),
        "report_sources": dict(collections.Counter(r["meta"]["report_source"] for r in recs)),
        "n_plays_median": plays[len(plays) // 2] if plays else None,
        "report_chars_median": chars[len(chars) // 2] if chars else None,
        "records_needing_score_fix": sum(1 for x in fixed if x),
        "score_fix_frac_p99": round(fracs[int(len(fracs) * 0.99)], 4) if fracs else None,
        "score_fix_frac_max": round(fracs[-1], 4) if fracs else None,
        "fetch_seconds": round(fetched_s, 1),
    }
    # The report is written LAST and is the completion marker: a killed run leaves no report, so
    # the cell is redone rather than silently treated as finished.
    (shard_dir / f"{lg['label']}_{season}.report.json").write_text(json.dumps(stats, indent=1))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(PKG / "config.example.yaml"))
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--seasons", default="2012:2025")
    ap.add_argument("--leagues", default=None, help="comma list of labels, e.g. CFB,MCB")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--delay", type=float, default=None, help="override request_delay_s")
    ap.add_argument("--shard-dir", default=None)
    ap.add_argument("--dry-run", action="store_true", help="report the plan, fetch nothing")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config), args.set)
    d = cfg["data"]
    cache = PKG / d.get("cache_dir", ".cache")
    shard_dir = Path(args.shard_dir) if args.shard_dir else PKG / "output" / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)

    seasons = parse_seasons(args.seasons)
    want = set(args.leagues.split(",")) if args.leagues else None
    leagues = [lg for lg in d["leagues"] if not want or lg["label"] in want]

    f = Fetcher(cache, d["user_agent"], int(d.get("timeout_s", 60)),
                float(args.delay if args.delay is not None else d.get("request_delay_s", 0.45)),
                verbose=args.verbose)

    # --- plan ---------------------------------------------------------------------------------
    todo, done, missing = [], [], []
    for lg in leagues:
        for season in seasons:
            ids, ok = census_ids(lg, season, cache)
            if not ok:
                missing.append(f"{lg['label']} {season}")
                continue
            # Either naming counts as done: shards written before the FCS tier existed are
            # keyed on the ESPN slug, which FBS and FCS share.
            if any((shard_dir / f"{key}_{season}.report.json").exists()
                   for key in (lg["label"], lg["league"])):
                done.append((lg["label"], season, len(ids)))
                continue
            ids, dropped = drop_overlap(lg, season, ids, cfg, cache)
            if dropped < 0:
                continue                     # could not de-overlap: shard deliberately skipped
            if dropped:
                print(f"    {lg['label']} {season}: dropped {dropped} games also in "
                      f"{lg['exclude_overlap_with']}", flush=True)
            todo.append((lg, season, ids))
    planned = sum(len(x[2]) for x in todo)
    print(f"plan: {len(todo)} shards to build ({planned:,} games), {len(done)} already done, "
          f"{len(missing)} census cells missing", flush=True)
    if missing:
        print(f"  ⚠️  no census cell for: {', '.join(missing[:12])}"
              f"{' ...' if len(missing) > 12 else ''}\n"
              f"      run: python scripts/census.py --mode walk --seasons {args.seasons}",
              flush=True)
    if args.dry_run:
        for lg, season, ids in todo:
            print(f"    {lg['label']} {season}: {len(ids):,} games")
        est = planned * float(args.delay if args.delay is not None
                              else d.get("request_delay_s", 0.45))
        print(f"  estimated fetch time at the configured gap: {est / 3600:.1f} h")
        return 0

    # --- build --------------------------------------------------------------------------------
    t0 = time.time()
    allstats = []
    for lg, season, ids in todo:
        if not ids:
            continue
        st = harvest_cell(lg, season, ids, cfg, f, shard_dir, args.workers)
        allstats.append(st)
        el = time.time() - t0
        got = sum(s["records"] for s in allstats)
        walked = sum(s["games"] for s in allstats)
        print(f"  {st['league']} {season}: {st['records']:,} records from {st['games']:,} games "
              f"(yield {st['yield']}) | cumulative {got:,} records / {walked:,} games "
              f"| {el / 60:.1f} min elapsed", flush=True)

    # --- aggregate ----------------------------------------------------------------------------
    reports = sorted(shard_dir.glob("*.report.json"))
    cells = [json.loads(p.read_text()) for p in reports]
    by_league = collections.defaultdict(lambda: {"games": 0, "records": 0})
    srcs = collections.Counter()
    skips = collections.Counter()
    for c in cells:
        by_league[c["league"]]["games"] += c["games"]
        by_league[c["league"]]["records"] += c["records"]
        srcs.update(c["report_sources"])
        skips.update(c["skips"])
    total = {
        "shards": len(cells),
        "games_walked": sum(c["games"] for c in cells),
        "records": sum(c["records"] for c in cells),
        "by_league": {k: v for k, v in sorted(by_league.items())},
        "by_season": {f"{c['league']}_{c['season']}": c["records"] for c in cells},
        "report_sources": dict(srcs),
        "skips": dict(skips),
        "fetch_stats": f.stats,
        "elapsed_minutes": round((time.time() - t0) / 60, 1),
    }
    (PKG / "output" / "harvest_report.json").write_text(json.dumps(total, indent=1))
    print("\n" + json.dumps({k: v for k, v in total.items() if k != "by_season"}, indent=1))
    print(f"\n{total['records']:,} records in {len(cells)} shards -> {shard_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
