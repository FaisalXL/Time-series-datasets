#!/usr/bin/env python3
"""Census ESPN's college universe: how many finished games exist, and how many carry real prose.

This exists because the package's first scale numbers were wrong in both directions, and both
errors came from trusting a scouting probe instead of a walk:

  * **CFB was under-counted 2x by a param that looks like a fix.** `&limit=1000` — correct for the
    monthly RANGE queries `51_espn_us_majors` uses — TRUNCATES single-date college-football
    queries: 2024-10-26 returns 52 completed games bare and 25 with `&limit=1000`, HTTP 200 both
    times. The "exactly 25 games on an October Saturday" that read as suspicious was this.
  * **Basketball was under-counted 7-16x without `&groups=50`.** 2024-02-10 returns 21 (men) / 7
    (women) bare, and 155 / 116 with the Division I group filter.

So scoreboard params are per league and are asserted here rather than assumed. Counting games and
reading `article.source` redistributes no prose, which is why this can run before the AP licence
question is settled — and it is what turns that ask from "some college games" into a number.

Modes:
  era      cheap: sample a few days per season per league, report play/recap availability by season.
           Answers "which seasons are even usable" before spending a full walk on them.
  walk     the real census: every day of every requested season, counting completed games.
           Resumable per (league, season) under .cache/census/; a season is only written once every
           one of its days answered, so a throttled run leaves a gap rather than a wrong low count.
  sources  sample K games per season and count `article.source`. This is the yield-defining number:
           "Data Skrive" is automated content the schema disqualifies, so the AP share by season is
           the usable fraction.

Usage:
    python scripts/census.py --mode era
    python scripts/census.py --mode walk --seasons 2013:2025
    python scripts/census.py --mode sources --seasons 2013:2025 -k 40
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import datetime as dt
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from espnfetch import (Fetcher, completed_events, flatten_plays,      # noqa: E402
                       scoreboard_rel, census_cell_rel, find_census_cell)

PKG = HERE.parent

# Season windows, generous at both ends so a walk cannot clip a title game or an early kickoff.
# A season is labelled by the calendar year it STARTS in: the 2024 basketball season runs
# 2024-11-01 -> 2025-04-15.
SEASON_WINDOWS = {
    "football":   ((8, 15), (1, 20)),
    "basketball": ((11, 1), (4, 15)),
}


def season_days(sport: str, season: int) -> list[str]:
    (m0, d0), (m1, d1) = SEASON_WINDOWS[sport]
    start = dt.date(season, m0, d0)
    end = dt.date(season + 1, m1, d1)
    out, cur = [], start
    while cur <= end:
        out.append(cur.strftime("%Y%m%d"))
        cur += dt.timedelta(days=1)
    return out


def scoreboard_url(cfg: dict, lg: dict, date: str) -> str:
    return cfg["data"]["scoreboard_url"].format(
        sport=lg["sport"], league=lg["league"], date=date) + lg.get("params", "")


def summary_url(cfg: dict, lg: dict, event_id: str) -> str:
    return cfg["data"]["summary_url"].format(
        sport=lg["sport"], league=lg["league"], event_id=event_id)


def load_cfg(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


def parse_seasons(spec: str) -> list[int]:
    if ":" in spec:
        a, b = spec.split(":")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in spec.split(",")]


# --- mode: era --------------------------------------------------------------------------------

def mode_era(cfg, f: Fetcher, seasons, probe_days: int):
    """Sample `probe_days` days spread through each season; report what the payloads carry.

    Reports plays and recaps SEPARATELY: college play-by-play reaches back years further than the
    wire recaps do, and it is the recap that gates a record. A season with plays and no prose is
    worth zero records here.
    """
    report = {}
    for lg in cfg["data"]["leagues"]:
        label = lg["label"]
        report[label] = {}
        for season in seasons:
            days = season_days(lg["sport"], season)
            # Evenly spaced samples from the dense middle of the season, skipping the thin edges.
            lo, hi = int(len(days) * 0.15), int(len(days) * 0.85)
            step = max(1, (hi - lo) // max(1, probe_days))
            picks = days[lo:hi:step][:probe_days]
            games = 0
            unknown_days = 0
            srcs = collections.Counter()
            plays_seen = []
            for date in picks:
                sb, ok = f.cached(scoreboard_url(cfg, lg, date),
                                  scoreboard_rel(lg, date))
                if not ok:
                    unknown_days += 1
                    continue
                evs = completed_events(sb)
                games += len(evs)
                if evs:                      # one summary per sampled day keeps this cheap
                    eid = evs[0]["id"]
                    s, ok2 = f.cached(summary_url(cfg, lg, eid),
                                      f"espn/{lg['sport']}/{lg['league']}/{eid}.json")
                    if ok2 and s:
                        art = s.get("article") or {}
                        story = art.get("story") or ""
                        srcs[(art.get("source") or "(none)") if story else "(no story)"] += 1
                        plays_seen.append(len(flatten_plays(s)))
            n = len(picks) - unknown_days
            report[label][season] = {
                "days_sampled": n,
                "days_unknown": unknown_days,
                "completed_games": games,
                "games_per_day": round(games / n, 1) if n else None,
                "plays_median": sorted(plays_seen)[len(plays_seen) // 2] if plays_seen else None,
                "sources": dict(srcs),
            }
            g = report[label][season]
            print(f"  {label} {season}: {g['completed_games']:5d} games in {n} days "
                  f"({g['games_per_day']}/day) plays~{g['plays_median']} {dict(srcs)}", flush=True)
    return report


# --- mode: walk -------------------------------------------------------------------------------

def mode_walk(cfg, f: Fetcher, seasons, workers: int = 1):
    """Full day walk. One cache file per (league, season), written only when the season is whole.

    Refusing the partial write is the point: a season with 30 unanswered days must not land as a
    number, because nothing downstream can tell a throttled zero from a real one.

    With `workers > 1` each season's days are fetched through a thread pool first and then read
    back out of the cache serially, so the counting stays deterministic while the waiting overlaps.
    Worth doing here: scoreboard payloads for a 155-game basketball Saturday are large, and latency
    rather than the configured gap was setting the pace (~1 req/s against a 0.45s gap). The pool
    shares one Fetcher, so the rate limit stays global.
    """
    outdir = f.cache / "census" / "seasons"
    outdir.mkdir(parents=True, exist_ok=True)
    totals = {}
    for lg in cfg["data"]["leagues"]:
        label = lg["label"]
        totals[label] = {}
        for season in seasons:
            fp = f.cache / census_cell_rel(lg, season)
            if fp.exists():
                cell = json.loads(fp.read_text())
                totals[label][season] = cell["completed_games"]
                print(f"  {label} {season}: {cell['completed_games']:5d} (cached)", flush=True)
                continue
            days = season_days(lg["sport"], season)
            if workers > 1:
                # Warm the cache first; the counting pass below then reads it back deterministically.
                with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                    list(ex.map(lambda dd: f.cached(scoreboard_url(cfg, lg, dd),
                                                    scoreboard_rel(lg, dd)), days))
            by_day, unknown = {}, []
            for date in days:
                sb, ok = f.cached(scoreboard_url(cfg, lg, date),
                                  scoreboard_rel(lg, date))
                if not ok:
                    unknown.append(date)
                    continue
                evs = completed_events(sb)
                if evs:
                    by_day[date] = [e["id"] for e in evs]
            total = sum(len(v) for v in by_day.values())
            if unknown:
                print(f"  {label} {season}: INCOMPLETE -- {len(unknown)} days unanswered, "
                      f"not writing (partial total would have been {total})", flush=True)
                totals[label][season] = None
                continue
            cell = {"league": lg["league"], "season": season, "days": len(days),
                    "active_days": len(by_day), "completed_games": total, "event_ids": by_day}
            tmp = fp.with_suffix(".part")
            tmp.write_text(json.dumps(cell))
            tmp.replace(fp)
            totals[label][season] = total
            print(f"  {label} {season}: {total:5d} games over {len(by_day)} active days",
                  flush=True)
    return totals


# --- mode: sources ----------------------------------------------------------------------------

def mode_sources(cfg, f: Fetcher, seasons, k: int):
    """Sample k games per season and count `article.source`.

    Samples are drawn evenly across the season's event ids, not from the front: recap coverage is
    era-skewed WITHIN a season too (bowl games and tournament games are covered differently from a
    Tuesday in December), and a head-of-list sample would read that skew as a rate.
    """
    seasondir = f.cache / "census" / "seasons"
    report = {}
    for lg in cfg["data"]["leagues"]:
        label = lg["label"]
        report[label] = {}
        for season in seasons:
            fp = find_census_cell(f.cache, lg, season)
            if fp is None:
                print(f"  {label} {season}: no census cell yet (run --mode walk first)", flush=True)
                continue
            cell = json.loads(fp.read_text())
            ids = [eid for day in sorted(cell["event_ids"]) for eid in cell["event_ids"][day]]
            if not ids:
                continue
            step = max(1, len(ids) // k)
            picks = ids[::step][:k]
            srcs, chars, plays = collections.Counter(), [], []
            unknown = 0
            for eid in picks:
                s, ok = f.cached(summary_url(cfg, lg, eid),
                                 f"espn/{lg['sport']}/{lg['league']}/{eid}.json")
                if not ok:
                    unknown += 1
                    continue
                art = (s or {}).get("article") or {}
                story = art.get("story") or ""
                if len(story) < int(cfg["text"].get("min_report_chars", 400)):
                    srcs["(no story)"] += 1
                    continue
                srcs[art.get("source") or "(blank)"] += 1
                chars.append(len(story))
                plays.append(len(flatten_plays(s)))
            n = len(picks) - unknown
            allow = set(cfg["text"].get("source_allowlist") or [])
            usable = sum(v for kk, v in srcs.items() if kk in allow) if allow else \
                sum(v for kk, v in srcs.items() if kk != "(no story)")
            report[label][season] = {
                "sampled": n,
                "unknown": unknown,
                "sources": dict(srcs),
                "usable_share": round(usable / n, 3) if n else None,
                "story_chars_median": sorted(chars)[len(chars) // 2] if chars else None,
                "plays_median": sorted(plays)[len(plays) // 2] if plays else None,
                "population": cell["completed_games"],
                "projected_records": round(cell["completed_games"] * usable / n) if n else None,
            }
            r = report[label][season]
            print(f"  {label} {season}: usable {r['usable_share']} of {n} sampled -> "
                  f"~{r['projected_records']} of {r['population']} games  {dict(srcs)}", flush=True)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(PKG / "config.example.yaml"))
    ap.add_argument("--mode", choices=["era", "walk", "sources"], required=True)
    ap.add_argument("--seasons", default="2013:2025", help="e.g. 2013:2025 or 2019,2023")
    ap.add_argument("-k", type=int, default=40, help="games sampled per season (sources mode)")
    ap.add_argument("--probe-days", type=int, default=6, help="days sampled per season (era mode)")
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent day fetches (walk mode); the rate limit stays global")
    ap.add_argument("--out", default=None, help="write the report here (JSON)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg(Path(args.config))
    d = cfg["data"]
    f = Fetcher(PKG / d.get("cache_dir", ".cache"), d["user_agent"],
                int(d.get("timeout_s", 60)), float(d.get("request_delay_s", 0.45)),
                verbose=args.verbose)
    seasons = parse_seasons(args.seasons)
    print(f"census mode={args.mode} seasons={seasons[0]}..{seasons[-1]}", flush=True)

    if args.mode == "era":
        rep = mode_era(cfg, f, seasons, args.probe_days)
    elif args.mode == "walk":
        rep = mode_walk(cfg, f, seasons, args.workers)
    else:
        rep = mode_sources(cfg, f, seasons, args.k)

    print("\nfetch stats: " + json.dumps(f.stats), flush=True)
    if args.mode == "walk":
        missing = [(lg, s) for lg, ss in rep.items() for s, v in ss.items() if v is None]
        if missing:
            print(f"⚠️  {len(missing)} season cells incomplete (throttled days) -- rerun to fill: "
                  f"{missing[:8]}", flush=True)
    if args.out:
        op = Path(args.out)
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text(json.dumps({"mode": args.mode, "seasons": seasons, "report": rep,
                                  "fetch_stats": f.stats}, indent=1))
        print(f"wrote {op}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
