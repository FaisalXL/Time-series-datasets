#!/usr/bin/env python3
"""ESPN college sports (FBS football + D-I men's/women's basketball) → CPT.

One record = one FINISHED GAME: the real wire-service recap (served via ESPN's API) paired with
the game's play-by-play RUNNING SCORE (away/home cumulative, one point per play). The recap
*describes* the shape of the game the series quantifies → `alignment: describes`.

Sibling of `51_espn_us_majors`, which covers NBA/NFL/NHL. The play-extraction logic is that
package's, unchanged in substance: `plays` for basketball, `drives.previous[].plays` for football,
score taken at every play (not per period — period level gives 3-4 points for football), and a
game is dropped if the play-by-play final disagrees with the official final.

Four things are NOT inherited from `51`, each measured:

  1. **`&groups=50` is mandatory for college basketball.** Without it the scoreboard returns 21
     games for 2024-02-10; with it, 155 — a 7x undercount at HTTP 200 with no error. Women's is
     worse: 7 vs 116, a 16x undercount.
  2. **`&limit=1000` must NOT be sent.** It is the fix for `51`'s monthly range queries, but on a
     single-date college-football query it TRUNCATES: 2024-10-26 returns 52 completed games bare
     and 25 with the limit. It was hardcoded in the shared scoreboard_url, so every CFB discovery
     call this package originally made lost half the slate.
  3. **College basketball rejects date RANGES** (404 on every monthly range tried) while single
     dates work, so discovery is a day walk. Use `census.py --mode walk` once and then feed its
     cached event ids back in via `discovery.census_seasons`; the walk is the expensive half.
  4. **A source allowlist is on by default.** "Data Skrive" is automated content, which
     `SCHEMA.md` §7 disqualifies as boilerplate/template text and which would otherwise need
     `text_quality: generated` plus sign-off. It filters on `article.source`; it does not guess
     from the prose.

**Cumulative scores are monotonised, and the count is recorded.** ESPN populates `awayScore` /
`homeScore` on every play, but not always correctly: administrative plays (timeouts, steals,
rebounds) can carry a stale pre-scoring-play score, and some carry a value from an unrelated point
in the game (event 401706896 reports a=57 h=86 in a 68/92 neighbourhood). Raw, that produced a
DECREASING cumulative score on 9 of the first 50 records. A cumulative score cannot decrease, so
the channel carries a running max and `meta.score_fix_plays` records how many plays needed it. The
official-final check still runs afterwards and is the real guard: a spuriously HIGH value would
propagate to the last point and fail it, dropping the game.

Records are built through `schema/emit.py`, so they are born strict-clean.

⚠️ LICENSE: the recaps are Associated Press wire copy served via ESPN's API — the same
rightsholder and the same open question as `51`, and a different (likely stricter) chain than
`45_cricket_report_overseries`, which is ESPNcricinfo's own staff journalism. Every record is
tagged `proprietary-review`. Do not scale or publish until redistribution is cleared.

Usage:
    python scripts/build_cpt_jsonl.py --dry-run
    python scripts/build_cpt_jsonl.py --set output.max_records=50
    python scripts/build_cpt_jsonl.py --set data.discovery.census_seasons=[2013,2014] \
                                      --set output.max_records=null
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PKG.parent / "schema"))
from espnfetch import (Fetcher, completed_events, flatten_plays,            # noqa: E402
                       scoreboard_rel, find_census_cell)
from emit import emit_record                                               # noqa: E402


# --- config ----------------------------------------------------------------------------------

def coerce(raw: str) -> Any:
    return yaml.safe_load(raw)


def deep_set(d: dict, dotted: str, raw: str) -> None:
    cur = d
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = coerce(raw)


def load_config(path: Path, sets) -> dict:
    cfg = yaml.safe_load(path.read_text())
    for s in sets or []:
        k, _, v = s.partition("=")
        deep_set(cfg, k.strip(), v.strip())
    return cfg


# --- discovery -------------------------------------------------------------------------------

def _as_date(v) -> dt.date:
    """Accept a date or a string. YAML parses an unquoted 2024-11-08 into a date object, so a
    `--set data.discovery.start_date=2024-11-08` override arrives typed while the same value
    quoted in the config file arrives as a string."""
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))


def date_range(start, end, step_days: int = 1):
    a, b = _as_date(start), _as_date(end)
    out = []
    while a <= b:
        out.append(a.strftime("%Y%m%d"))
        a += dt.timedelta(days=step_days)
    return out


def _sorted_ids(ids) -> list[str]:
    # Length-then-lexical so 9-digit modern ids sort after 6-digit historical ones, i.e. roughly
    # chronologically. A capped run therefore takes OLDEST first — deliberate, because recap
    # coverage is era-skewed and the old seasons are the AP-heavy ones.
    return sorted(set(ids), key=lambda s: (len(s), s))


def discover_from_census(lg: dict, seasons, cache: Path) -> tuple[list[str], list[int]]:
    """Event ids from cached `census.py --mode walk` cells. No requests at all.

    Only whole season cells exist on disk (the census refuses to write a season with unanswered
    days), so this cannot silently inherit a throttled undercount.
    """
    ids, missing = [], []
    for season in seasons:
        fp = find_census_cell(cache, lg, season)
        if fp is None:
            missing.append(season)
            continue
        cell = json.loads(fp.read_text())
        for day in sorted(cell["event_ids"]):
            ids.extend(cell["event_ids"][day])
    return _sorted_ids(ids), missing


def discover_events(lg: dict, d: dict, f: Fetcher) -> list[str]:
    """Finished event ids for one league, by walking the scoreboard day by day.

    Day-walk rather than range queries because college basketball 404s on ranges. `params` is
    per league so `&groups=50` reaches basketball (mandatory: 7-16x undercount without it) without
    being sent where it does not belong, and so no `&limit=` reaches football (which truncates).
    """
    ids: list[str] = []
    dates = date_range(d["discovery"]["start_date"], d["discovery"]["end_date"],
                       int(d["discovery"].get("step_days", 1)))
    unanswered = 0
    for date in dates:
        url = d["scoreboard_url"].format(sport=lg["sport"], league=lg["league"], date=date)
        url += lg.get("params", "")
        data, ok = f.cached(url, scoreboard_rel(lg, date))
        if not ok:
            unanswered += 1
            continue
        ids.extend(e["id"] for e in completed_events(data))
    if unanswered:
        print(f"  ⚠️  {lg['label']}: {unanswered} of {len(dates)} days never answered — "
              f"discovery is an undercount, rerun to fill", flush=True)
    return _sorted_ids(ids)


# --- summary parsing --------------------------------------------------------------------------

def strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s or "")
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</p>", "\n\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
          .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", s).strip()


def play_scores(plays: list[dict]):
    """Running (away, home) score at EVERY play, monotonised, plus counts and period boundaries.

    Every play carries a point, not only scoring plays: the flat shape of a possession that ends
    without points is part of what the recap describes.

    `awayScore`/`homeScore` are taken as the score AFTER the play, but the feed is not reliable
    about it (see the module docstring), so each channel carries a running max. `fixes` counts the
    plays whose raw value was below the carried score — the audit trail for that correction, and a
    number worth watching: a game with many is a game whose feed should be distrusted.

    `period_end_idx` is the index of the last play of each period, which is what lets the halftime
    score be recovered from the series afterwards (verify_alignment.py uses it as a second,
    independent anchor).
    """
    away, home, period_end_idx = [], [], []
    a = h = 0
    fixes = 0
    periods = set()
    last_period = None
    for p in plays:
        raw_a, raw_h = a, h
        try:
            raw_a = int(p.get("awayScore", a))
        except (TypeError, ValueError):
            pass
        try:
            raw_h = int(p.get("homeScore", h))
        except (TypeError, ValueError):
            pass
        if raw_a < a or raw_h < h:
            fixes += 1
        a, h = max(a, raw_a), max(h, raw_h)

        per = ((p.get("period") or {}).get("number"))
        if per is not None:
            periods.add(per)
            if last_period is not None and per != last_period and away:
                period_end_idx.append(len(away) - 1)
            last_period = per
        away.append(a)
        home.append(h)
    if away:
        period_end_idx.append(len(away) - 1)
    return away, home, len(plays), len(periods), fixes, period_end_idx


def official_scores(summary: dict):
    comps = ((summary.get("header") or {}).get("competitions") or [])
    if not comps:
        return None, None
    away = home = None
    for c in comps[0].get("competitors") or []:
        try:
            v = int(c.get("score"))
        except (TypeError, ValueError):
            continue
        if c.get("homeAway") == "home":
            home = v
        elif c.get("homeAway") == "away":
            away = v
    return away, home


def team_names(summary: dict):
    comps = ((summary.get("header") or {}).get("competitions") or [])
    if not comps:
        return None, None
    away = home = None
    for c in comps[0].get("competitors") or []:
        t = c.get("team") or {}
        name = t.get("displayName") or t.get("name") or t.get("abbreviation")
        if c.get("homeAway") == "home":
            home = name
        elif c.get("homeAway") == "away":
            away = name
    return away, home


def game_date(summary: dict) -> Optional[str]:
    comps = ((summary.get("header") or {}).get("competitions") or [])
    if comps and comps[0].get("date"):
        return comps[0]["date"]
    return None


def season_of(summary: dict) -> Optional[int]:
    y = ((summary.get("header") or {}).get("season") or {}).get("year")
    try:
        return int(y)
    except (TypeError, ValueError):
        return None


# --- record ----------------------------------------------------------------------------------

def build_record(event_id: str, lg: dict, cfg: dict, f: Fetcher):
    """-> (record | None, skip_reason). skip_reason is '' on success."""
    d, t = cfg["data"], cfg["text"]
    sport, league, label = lg["sport"], lg["league"], lg["label"]

    url = d["summary_url"].format(sport=sport, league=league, event_id=event_id)
    summary, ok = f.cached(url, f"espn/{sport}/{league}/{event_id}.json")
    if not ok:
        return None, "fetch_failed"          # unknown, NOT an empty answer — see espnfetch
    if not summary:
        return None, "no_summary"

    art = summary.get("article") or {}
    src = (art.get("source") or "").strip()

    # Recap presence is checked BEFORE the source filter, so the two are separable in the skip
    # counts. Filtering first attributed 56 games with no `article` key at all to
    # "source_not_allowed:(empty)", which reads as a licence/quality rejection when it is really
    # a coverage gap -- and coverage vs filtering are the two numbers this package is judged on.
    story = strip_html(art.get("story") or "")
    if len(story) < int(t.get("min_report_chars", 400)):
        return None, "no_report"

    allow = t.get("source_allowlist")
    if allow is not None and src not in set(allow):
        # Automated content (notably "Data Skrive") is filtered HERE, on the source field the
        # publisher sets -- never inferred from the prose. See the module docstring.
        return None, f"source_not_allowed:{src or '(empty)'}"

    plays = flatten_plays(summary)
    away, home, n_plays, n_periods, fixes, period_end_idx = play_scores(plays)
    if n_periods < int(d.get("min_periods", 2)) or n_plays < int(d.get("min_plays", 20)):
        return None, "short_game"

    # Outlier guard on a corrupt play list. See the config: football p99.9 is 267 plays and one
    # payload in 5,241 carries 1,493, with 1,346 of them stamped period 3. Such a game still passes
    # the official-final check, so nothing else catches it.
    play_cap = (d.get("max_plays_by_sport") or {}).get(sport)
    if play_cap and n_plays > int(play_cap):
        return None, "implausible_play_count"

    # A feed that needed the monotonic correction on a large share of its plays is not a feed to
    # trust for the shape of the game, which is the entire content of the `describes` claim.
    max_frac = d.get("max_score_fix_frac")
    if max_frac is not None and n_plays and fixes / n_plays > float(max_frac):
        return None, "unreliable_scores"

    off_away, off_home = official_scores(summary)
    if (off_away is not None and away and away[-1] != off_away) or \
       (off_home is not None and home and home[-1] != off_home):
        # The play-by-play series must land on the official final, or the pairing is wrong. This
        # also catches a monotonised channel that was dragged too high by a bogus play value.
        return None, "score_mismatch"

    away_name, home_name = team_names(summary)
    gdate = game_date(summary)
    gdate_short = gdate[:10] if gdate else None
    # The LEAGUE SLUG, not the sport. Measured 2026-08-13: the sport form does not resolve --
    # www.espn.com/football/boxscore/_/gameId/322430238 and www.espn.com/basketball/game/... both
    # 404, which is what every record built before that date carries. The league form returns 200,
    # verified across all four tiers at both ends of the era (CFB/FCS 2012+2025,
    # MCB/WCB 2012+2025). `/game/` for every tier, matching 51_espn_us_majors -- `/boxscore/`
    # also resolves under the league slug, but there is no reason for the siblings to differ.
    report_url = f"https://www.espn.com/{league}/game/_/gameId/{event_id}"

    rec = emit_record(
        text=f"{story}\n\n<ts></ts>",
        timeseries=[
            {"values": away, "unit": "away_score_cumulative", "freq": "1play"},
            {"values": home, "unit": "home_score_cumulative", "freq": "1play"},
        ],
        alignment="describes",
        license="proprietary-review",
        text_source="third_party",
        source=report_url,
        dataset="espn_college",
        series_id=f"espn_{league}_{event_id}",
        domain="sports",
        region="US",
        period_start=gdate_short,
        period_end=gdate_short,
        meta={
            "league": label,
            "sport": sport,
            "espn_league_slug": league,
            "event_id": event_id,
            # ESPN's OWN season field, not this package's census label, and the two differ:
            # ESPN labels a basketball season by its ENDING year (a 2024-12 game is season 2025)
            # while census.py labels seasons by start year (that game is in season 2024). Football
            # agrees in both. Kept under the source's name so nothing silently reinterprets it.
            "espn_season_year": season_of(summary),
            "away_team": away_name,
            "home_team": home_name,
            "game_date": gdate,
            "n_plays": n_plays,
            "n_periods": n_periods,
            # Last index of each period: lets a consumer slice the series by half/quarter, and is
            # what makes the halftime score recoverable as an independent alignment anchor.
            "period_end_idx": period_end_idx,
            # Plays whose raw score field was below the carried cumulative score and were corrected
            # upward. 0 for a clean feed. See play_scores().
            "score_fix_plays": fixes,
            "final_away_score": away[-1] if away else None,
            "final_home_score": home[-1] if home else None,
            "report_headline": art.get("headline"),
            "report_source": src,
            "report_published": art.get("published"),
            "report_chars": len(story),
            "license_note": ("Wire-service recap prose served via ESPN's API; "
                             "article.source is recorded per record. Same rightsholder chain as "
                             "51_espn_us_majors (AP). Tagged proprietary-review -- not cleared "
                             "for distribution."),
            "source_allowlist": list(allow) if allow is not None else None,
        },
    )
    return rec, ""


# --- main ------------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(PKG / "config.example.yaml"))
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true", help="discover + report, write nothing")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config), args.set)
    d, o = cfg["data"], cfg["output"]
    cache = PKG / d.get("cache_dir", ".cache")
    f = Fetcher(cache, d["user_agent"], int(d.get("timeout_s", 60)),
                float(d.get("request_delay_s", 0.45)), verbose=args.verbose)
    cap = o.get("max_records")

    # Per-league cap as well as a global one: leagues are walked in order, so a single global cap
    # fills entirely from the first league and the committed demo would show only football --
    # leaving the basketball path (and its mandatory &groups=50) unexercised.
    lcap = o.get("max_records_per_league")
    seasons = d["discovery"].get("census_seasons")
    recs, skips = [], collections.Counter()
    per_league = {}
    for lg in d["leagues"]:
        if seasons:
            ids, missing = discover_from_census(lg, seasons, cache)
            if missing:
                print(f"  ⚠️  {lg['label']}: no census cell for {missing} — "
                      f"run census.py --mode walk for those seasons", flush=True)
        else:
            ids = discover_events(lg, d, f)
        kept = 0
        print(f"[{lg['label']}] discovered {len(ids)} finished games", flush=True)
        for n, eid in enumerate(ids):
            if lcap and kept >= int(lcap):
                break
            rec, why = build_record(eid, lg, cfg, f)
            if rec is None:
                skips[why.split(":")[0]] += 1
                if ":" in why:
                    skips[why] += 1
                continue
            recs.append(rec)
            kept += 1
            if args.verbose and kept % 250 == 0:
                print(f"    [{lg['label']}] {kept} kept / {n + 1} walked", flush=True)
            if cap and len(recs) >= int(cap):
                break
        per_league[lg["label"]] = {"discovered": len(ids), "records": kept}
        print(f"[{lg['label']}] kept {kept}", flush=True)
        if cap and len(recs) >= int(cap):
            break

    srcs = collections.Counter(r["meta"]["report_source"] for r in recs)
    by_season = collections.Counter(r["meta"]["espn_season_year"] for r in recs)
    plays = sorted(r["meta"]["n_plays"] for r in recs)
    chars = sorted(r["meta"]["report_chars"] for r in recs)
    fixed = [r["meta"]["score_fix_plays"] for r in recs]
    stats = {
        "records": len(recs),
        "per_league": per_league,
        "skips": dict(skips),
        "report_sources": dict(srcs),
        "records_by_season": {str(k): v for k, v in sorted(by_season.items(), key=lambda x: str(x[0]))},
        "n_plays_median": plays[len(plays) // 2] if plays else None,
        "n_plays_min": plays[0] if plays else None,
        "report_chars_median": chars[len(chars) // 2] if chars else None,
        "records_needing_score_fix": sum(1 for x in fixed if x),
        "score_fix_plays_max": max(fixed) if fixed else 0,
        # The distribution `max_score_fix_frac` should be set from, rather than guessed at. If
        # there is no heavy tail here, there is no broken-feed population to filter out and the
        # official-final check is doing the whole job on its own.
        "score_fix_frac_pctiles": (lambda xs: {p: round(xs[min(len(xs) - 1, int(len(xs) * p / 100))], 4)
                                               for p in (50, 90, 99, 100)} if xs else {})(
            sorted(r["meta"]["score_fix_plays"] / max(1, r["meta"]["n_plays"]) for r in recs)),
        "alignment": "describes",
        "license": "proprietary-review",
        "fetch_stats": f.stats,
    }
    print(json.dumps(stats, indent=2)[:3000])
    if args.dry_run:
        print("(dry run -- nothing written)")
        return 0

    out = PKG / o["path"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    # default=str because a --set date override arrives as a date object, not a string
    (PKG / o["run_report"]).write_text(json.dumps(
        {"dataset": "espn_college", "stats": stats, "config_snapshot": cfg},
        indent=1, default=str))
    sp = PKG / o["samples_path"]
    sp.parent.mkdir(parents=True, exist_ok=True)
    with sp.open("w") as fh:
        for r in recs[:5]:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(recs)} records -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
