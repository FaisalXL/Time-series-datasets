#!/usr/bin/env python3
"""Census ESPN's NBA/NFL/NHL universe: how many finished games exist, and how many carry real prose.

Adapted from `65_espn_college/scripts/census.py`. Two things are deliberately different, both
measured rather than inherited:

  1. **Discovery is a monthly RANGE query, and `&limit=1000` is REQUIRED.** Measured 2026-08-13,
     comparing event-id SETS against a full day-by-day walk of the same month:

       NBA 2024-01 : range bare **100** | range &limit=1000 **231** | day walk **231**
       NFL 2023-11 : range bare  59 | range &limit=1000  59 | day walk  59
       NHL 2024-01 : range bare 208 | range &limit=1000 208 | day walk 208

     With the limit, the range reproduces the day walk EXACTLY — set equality, not just matching
     counts. Without it NBA silently loses 131 of 231 games at HTTP 200. The truncation is at 100
     raw events and it is per league: NBA's default page is 100, NHL returned 209 raw bare. There
     is no `count` / `pageCount` / `pageSize` field in the payload, so nothing in the response
     announces the truncation — the only way to see it is to compare against a walk.

     This is the OPPOSITE of `65`, where `&limit=1000` TRUNCATES a single-date college-football
     query (52 games bare, 25 with the limit). Same endpoint, same param, opposite correct answer.
     Hence the params live in the cache key (see espnfetch.params_tag).

     A range costs one request per month instead of ~30, so the whole 22-year universe is ~790
     requests rather than ~24,000.

  2. **The census cell is (league, CALENDAR YEAR), not (league, season).** `65` walks fixed season
     windows because it walks days and wants to skip the off-season; here a month costs the same
     request whether it holds 0 games or 250, so calendar months tile the era exactly — no gap, no
     overlap, and no window to get wrong. That matters in this era specifically: COVID moved two
     seasons clean out of any fixed window (the 2019-20 NBA season finished 2020-10-11 and the
     2020-21 season began 2020-12-22; the NHL played its 2020-21 season Jan-Jul 2021), and the
     2012-13 NHL lockout season did not start until 2013-01-19. Each record still carries ESPN's
     OWN `season.year` and `season.type`, so season-level slicing survives in the data.

Modes:
  era      cheap: sample months per year per league, report play/recap availability.
  walk     the real census: every month of every requested year. Resumable per (league, year); a
           year is only written once all 12 months answered, so a throttled run leaves a gap
           rather than a wrong low count.
  sources  sample K games per year and count `article.source` — the yield-defining number.

Usage:
    python scripts/census.py --mode walk --years 2005:2026 --out output/census_walk.json
    python scripts/census.py --mode sources --years 2012:2026 -k 40
"""
from __future__ import annotations

import argparse
import calendar
import collections
import concurrent.futures as cf
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from espnfetch import (Fetcher, completed_events, flatten_plays,      # noqa: E402
                       scoreboard_rel, census_cell_rel, find_census_cell)

PKG = HERE.parent


def year_months(year: int) -> list[str]:
    """The 12 monthly range windows for a calendar year, as ESPN `dates=` values."""
    out = []
    for m in range(1, 13):
        last = calendar.monthrange(year, m)[1]
        out.append(f"{year}{m:02d}01-{year}{m:02d}{last}")
    return out


def scoreboard_url(cfg: dict, lg: dict, window: str) -> str:
    return cfg["data"]["scoreboard_url"].format(
        sport=lg["sport"], league=lg["league"], date=window) + lg.get("params", "")


def summary_url(cfg: dict, lg: dict, event_id: str) -> str:
    return cfg["data"]["summary_url"].format(
        sport=lg["sport"], league=lg["league"], event_id=event_id)


def load_cfg(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


def parse_years(spec: str) -> list[int]:
    if ":" in spec:
        a, b = spec.split(":")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in spec.split(",")]


def event_meta(ev: dict) -> tuple:
    """(season_year, season_type) as ESPN labels this event. type 1=pre 2=regular 3=post 4=allstar."""
    s = ev.get("season") or {}
    return s.get("year"), s.get("type")


# --- mode: era --------------------------------------------------------------------------------

def mode_era(cfg, f: Fetcher, years, probe_months: int):
    """Sample months per year; report what the payloads carry.

    Plays and recaps are reported SEPARATELY. Play-by-play reaches back to at least 2002 in all
    three leagues; the wire recaps do not, and it is the recap that gates a record.
    """
    report = {}
    for lg in cfg["data"]["leagues"]:
        label = lg["label"]
        report[label] = {}
        for year in years:
            months = year_months(year)
            step = max(1, len(months) // max(1, probe_months))
            picks = months[::step][:probe_months]
            games = unknown = 0
            srcs = collections.Counter()
            plays_seen = []
            for w in picks:
                sb, ok = f.cached(scoreboard_url(cfg, lg, w), scoreboard_rel(lg, w))
                if not ok:
                    unknown += 1
                    continue
                evs = completed_events(sb)
                games += len(evs)
                if evs:
                    eid = evs[0]["id"]
                    s, ok2 = f.cached(summary_url(cfg, lg, eid),
                                      f"espn/{lg['sport']}/{lg['league']}/{eid}.json")
                    if ok2 and s:
                        art = s.get("article") or {}
                        story = art.get("story") or ""
                        srcs[(art.get("source") or "(blank)") if story else "(no story)"] += 1
                        plays_seen.append(len(flatten_plays(s)))
            report[label][year] = {
                "months_sampled": len(picks) - unknown, "months_unknown": unknown,
                "completed_games": games,
                "plays_median": sorted(plays_seen)[len(plays_seen) // 2] if plays_seen else None,
                "sources": dict(srcs),
            }
            g = report[label][year]
            print(f"  {label} {year}: {g['completed_games']:5d} games in {g['months_sampled']} "
                  f"months  plays~{g['plays_median']}  {dict(srcs)}", flush=True)
    return report


# --- mode: walk -------------------------------------------------------------------------------

def mode_walk(cfg, f: Fetcher, years, workers: int = 1):
    """Full month walk. One cache file per (league, year), written only when the year is whole.

    Refusing the partial write is the point: a year with an unanswered month must not land as a
    number, because nothing downstream can tell a throttled zero from a real one.
    """
    (f.cache / "census" / "seasons").mkdir(parents=True, exist_ok=True)
    totals = {}
    for lg in cfg["data"]["leagues"]:
        label = lg["label"]
        totals[label] = {}
        for year in years:
            fp = f.cache / census_cell_rel(lg, year)
            if fp.exists():
                cell = json.loads(fp.read_text())
                totals[label][year] = cell["completed_games"]
                print(f"  {label} {year}: {cell['completed_games']:5d} (cached)", flush=True)
                continue
            months = year_months(year)
            if workers > 1:
                with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                    list(ex.map(lambda w: f.cached(scoreboard_url(cfg, lg, w),
                                                   scoreboard_rel(lg, w)), months))
            by_month, unknown = {}, []
            seasons = collections.Counter()
            types = collections.Counter()
            for w in months:
                sb, ok = f.cached(scoreboard_url(cfg, lg, w), scoreboard_rel(lg, w))
                if not ok:
                    unknown.append(w)
                    continue
                evs = completed_events(sb)
                if evs:
                    by_month[w[:6]] = [e["id"] for e in evs]
                    for ev in evs:
                        sy, st = event_meta(ev)
                        seasons[str(sy)] += 1
                        types[str(st)] += 1
            total = sum(len(v) for v in by_month.values())
            if unknown:
                print(f"  {label} {year}: INCOMPLETE -- {len(unknown)} months unanswered, "
                      f"not writing (partial total would have been {total})", flush=True)
                totals[label][year] = None
                continue
            cell = {"league": lg["league"], "label": label, "year": year,
                    "months": len(months), "active_months": len(by_month),
                    "completed_games": total,
                    "espn_season_years": dict(sorted(seasons.items())),
                    "espn_season_types": dict(sorted(types.items())),
                    "event_ids": by_month}
            tmp = fp.with_suffix(".part")
            tmp.write_text(json.dumps(cell))
            tmp.replace(fp)
            totals[label][year] = total
            print(f"  {label} {year}: {total:5d} games over {len(by_month)} active months "
                  f"types={dict(types)}", flush=True)
    return totals


# --- mode: sources ----------------------------------------------------------------------------

def mode_sources(cfg, f: Fetcher, years, k: int):
    """Sample k games per (league, year) and count `article.source`.

    Samples are strided across the year's event ids rather than taken from the front: recap
    coverage moves within a year too (a January regular-season game and a June final are covered
    differently), and a head-of-list sample would read that skew as a rate.
    """
    report = {}
    for lg in cfg["data"]["leagues"]:
        label = lg["label"]
        report[label] = {}
        for year in years:
            fp = find_census_cell(f.cache, lg, year)
            if fp is None:
                print(f"  {label} {year}: no census cell yet (run --mode walk first)", flush=True)
                continue
            cell = json.loads(fp.read_text())
            ids = [eid for mo in sorted(cell["event_ids"]) for eid in cell["event_ids"][mo]]
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
            report[label][year] = {
                "sampled": n, "unknown": unknown, "sources": dict(srcs),
                "usable_share": round(usable / n, 3) if n else None,
                "story_chars_median": sorted(chars)[len(chars) // 2] if chars else None,
                "plays_median": sorted(plays)[len(plays) // 2] if plays else None,
                "population": cell["completed_games"],
                "projected_records": round(cell["completed_games"] * usable / n) if n else None,
            }
            r = report[label][year]
            print(f"  {label} {year}: usable {r['usable_share']} of {n} sampled -> "
                  f"~{r['projected_records']} of {r['population']} games  {dict(srcs)}", flush=True)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(PKG / "config.example.yaml"))
    ap.add_argument("--mode", choices=["era", "walk", "sources"], required=True)
    ap.add_argument("--years", default="2005:2026", help="e.g. 2005:2026 or 2012,2013")
    ap.add_argument("-k", type=int, default=40, help="games sampled per year (sources mode)")
    ap.add_argument("--probe-months", type=int, default=4, help="months sampled per year (era)")
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent month fetches (walk mode); the rate limit stays global")
    ap.add_argument("--leagues", default=None, help="comma list of labels, e.g. NBA,NHL")
    ap.add_argument("--out", default=None, help="write the report here (JSON)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg(Path(args.config))
    d = cfg["data"]
    if args.leagues:
        want = set(args.leagues.split(","))
        d["leagues"] = [lg for lg in d["leagues"] if lg["label"] in want]
    f = Fetcher(PKG / d.get("cache_dir", ".cache"), d["user_agent"],
                int(d.get("timeout_s", 60)), float(d.get("request_delay_s", 0.35)),
                verbose=args.verbose)
    years = parse_years(args.years)
    print(f"census mode={args.mode} years={years[0]}..{years[-1]}", flush=True)

    if args.mode == "era":
        rep = mode_era(cfg, f, years, args.probe_months)
    elif args.mode == "walk":
        rep = mode_walk(cfg, f, years, args.workers)
    else:
        rep = mode_sources(cfg, f, years, args.k)

    print("\nfetch stats: " + json.dumps(f.stats), flush=True)
    if args.mode == "walk":
        missing = [(lg, y) for lg, ys in rep.items() for y, v in ys.items() if v is None]
        if missing:
            print(f"⚠️  {len(missing)} year cells incomplete (throttled months) -- rerun to fill: "
                  f"{missing[:8]}", flush=True)
        grand = sum(v for ys in rep.values() for v in ys.values() if v)
        print(f"\nuniverse: {grand:,} finished games across "
              f"{sum(1 for ys in rep.values() for v in ys.values() if v is not None)} cells")
    if args.out:
        op = Path(args.out)
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text(json.dumps({"mode": args.mode, "years": years, "report": rep,
                                  "fetch_stats": f.stats}, indent=1))
        print(f"wrote {op}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
