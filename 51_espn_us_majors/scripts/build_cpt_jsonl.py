#!/usr/bin/env python3
"""ESPN US majors (NBA + NFL + NHL) → CPT.

One record = one FINISHED GAME: the real AP wire recap (served via ESPN's API) paired with the
game's play-by-play RUNNING SCORE (away/home cumulative, one point per play). The recap *describes*
the shape of the game the series quantifies → `alignment: describes`.

Sibling of `65_espn_college`, whose pipeline this is. Records are built through `schema/emit.py`,
so they are born strict-clean, and `build_record` here is the single definition of a record for
both the demo path (`main` below) and the sharded full harvest (`harvest.py`).

**Two defects this file was rewritten to fix**, both dating from before schema v1:

  1. **The old builder emitted a pretty-printed JSON ARRAY into samples/.** `validate.py --strict`
     reads JSONL, so it saw 2,868 malformed "records" and failed every one of them — a package
     that reported itself as built was failing the gate 100%. Everything is emitted through
     `emit_record` now, one JSON object per line.
  2. **The old builder accumulated every record in a list before writing.** At this universe's
     scale that is roughly a gigabyte resident and a single monolithic file that has to restart
     from zero if anything interrupts. `harvest.py` shards instead; this file keeps only the capped
     demo path.

**Cumulative scores are monotonised, and the count is recorded.** ESPN populates `awayScore` /
`homeScore` on every play but not always correctly: administrative plays can carry a stale
pre-scoring value, and some carry a value from an unrelated point in the game. Raw, that produced a
DECREASING cumulative score on 9 of the first 50 college records. A cumulative score cannot
decrease, so each channel carries a running max and `meta.score_fix_plays` records how many plays
needed it. The official-final check still runs afterwards and is the real guard: a spuriously HIGH
value propagates to the last point and fails there, dropping the game.

⚠️ LICENSE: the recaps are Associated Press wire copy served via ESPN's API. Every record is tagged
`proprietary-review`. Do not scale or publish until redistribution is cleared.

Usage:
    python scripts/build_cpt_jsonl.py --dry-run
    python scripts/build_cpt_jsonl.py --set output.max_records=60
"""
from __future__ import annotations

import argparse
import collections
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
from espnfetch import Fetcher, flatten_plays, find_census_cell                # noqa: E402
from emit import emit_record                                                 # noqa: E402


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
    cfg = yaml.safe_load(Path(path).read_text())
    for s in sets or []:
        k, _, v = s.partition("=")
        deep_set(cfg, k.strip(), v.strip())
    return cfg


def parse_years(spec) -> list[int]:
    s = str(spec)
    if ":" in s:
        a, b = s.split(":")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in s.split(",")]


# --- discovery -------------------------------------------------------------------------------

def _sorted_ids(ids) -> list[str]:
    # Length-then-lexical so 9-digit modern ids sort after shorter historical ones, i.e. roughly
    # chronologically. A capped run therefore takes OLDEST first — deliberate, because recap
    # coverage is era-skewed, so a capped build is NOT a random sample of the universe.
    return sorted(set(ids), key=lambda s: (len(s), s))


def discover_from_census(lg: dict, years, cache: Path) -> tuple[list[str], list[int]]:
    """Event ids from cached `census.py --mode walk` cells. No requests at all.

    Only whole year cells exist on disk (the census refuses to write a year with an unanswered
    month), so this cannot silently inherit a throttled undercount.
    """
    ids, missing = [], []
    for year in years:
        fp = find_census_cell(cache, lg, year)
        if fp is None:
            missing.append(year)
            continue
        cell = json.loads(fp.read_text())
        for mo in sorted(cell["event_ids"]):
            ids.extend(cell["event_ids"][mo])
    return _sorted_ids(ids), missing


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


def play_scores(plays: list[dict], off_away: Optional[int] = None, off_home: Optional[int] = None):
    """Running (away, home) score at EVERY play, cleaned, plus counts and period boundaries.

    Every play carries a point, not only scoring plays: the flat shape of a possession that ends
    without points is part of what the recap describes. Period-level was the original design here
    and is far too short — 3-4 points per game, and for NFL/NHL where scoring events are rare it
    throws away the entire shape the recap narrates.

    ESPN's per-play `awayScore`/`homeScore` are unreliable on administrative plays, and the errors
    go in BOTH directions. Traced on NFL event 401123243:

        178  P4  a=14  Passing Touchdown ... extra point is GOOD     <- true score, 14
        179  P4  a=16  Kickoff                                       <- spurious HIGH
        180  P4  a=14  Penalty (False Start) - No Play               <- back to 14

    so two hard facts about what a cumulative score IS are applied, in this order:

      1. **It cannot exceed the game's final score.** A raw value above the official final is
         impossible, so it is ignored rather than carried. Counted as `clamps`.
      2. **It cannot decrease.** Each channel then carries a running max, so a stale low value on
         an administrative play (play 179 above reads a=6 right after a 7-point touchdown in the
         first quarter of the same game) does not pull the series backwards. Counted as `fixes`.

    Neither is a heuristic fitted to this feed; both are properties of a cumulative total. Rule 1
    is what rescues the games that rule 1's absence used to send to `score_mismatch` — measured on
    NFL 2019, it recovers 11 of 11 mismatching games and moves the shard's yield from 0.946 to
    0.979 — and it does NOT weaken the guard, because the official-final check still runs
    afterwards: clamping can only cap an overshoot, never raise a series that fell short.
    """
    away, home, period_end_idx = [], [], []
    a = h = 0
    fixes = clamps = 0
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
        # (1) impossible-high values are not evidence about the score at this play
        if (off_away is not None and raw_a > off_away) or \
           (off_home is not None and raw_h > off_home):
            clamps += 1
            if off_away is not None and raw_a > off_away:
                raw_a = a
            if off_home is not None and raw_h > off_home:
                raw_h = h
        # (2) a cumulative score cannot decrease
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
    return away, home, len(plays), len(periods), fixes, clamps, period_end_idx


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
    """-> (away, home), each a dict of the team's names, or None.

    The NICKNAME (`team.name`: "Wild", "Maple Leafs") is kept alongside the display name because it
    is what the prose actually uses — AP writes "the Wild", not "the Minnesota Wild" — and the team
    anchor in verify_alignment.py needs to match against the prose. It cannot be derived from the
    display name after the fact: "Minnesota Wild" splits into a nickname correctly but "New York
    Rangers" does not, because the location is two words.
    """
    comps = ((summary.get("header") or {}).get("competitions") or [])
    if not comps:
        return None, None
    away = home = None
    for c in comps[0].get("competitors") or []:
        t = c.get("team") or {}
        rec = {"display": t.get("displayName") or t.get("name") or t.get("abbreviation"),
               "name": t.get("name"), "location": t.get("location"),
               "abbrev": t.get("abbreviation")}
        if c.get("homeAway") == "home":
            home = rec
        elif c.get("homeAway") == "away":
            away = rec
    return away, home


def game_date(summary: dict) -> Optional[str]:
    comps = ((summary.get("header") or {}).get("competitions") or [])
    if comps and comps[0].get("date"):
        return comps[0]["date"]
    return None


def season_info(summary: dict):
    """ESPN's own (season year, season type). type 1=preseason 2=regular 3=post 4/5=all-star etc."""
    s = ((summary.get("header") or {}).get("season") or {})
    try:
        y = int(s.get("year"))
    except (TypeError, ValueError):
        y = None
    return y, s.get("type")


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

    # Recap presence is checked BEFORE the source filter so the two stay separable in the skip
    # counts: "no recap exists" is a coverage gap and "this recap is not AP" is a licence/quality
    # rejection, and those are the two numbers this package is judged on. Counted over the whole
    # cached universe the split is 2,904 games with no `article` key at all vs 173 non-AP stories.
    story = strip_html(art.get("story") or "")
    if len(story) < int(t.get("min_report_chars", 400)):
        return None, "no_report"

    allow = t.get("source_allowlist")
    if allow is not None and src not in set(allow):
        return None, f"source_not_allowed:{src or '(empty)'}"

    # The official final is read BEFORE the series is built, because it bounds it: see play_scores.
    off_away, off_home = official_scores(summary)
    plays = flatten_plays(summary)
    away, home, n_plays, n_periods, fixes, clamps, period_end_idx = play_scores(
        plays, off_away, off_home)
    if n_periods < int(d.get("min_periods", 3)) or n_plays < int(d.get("min_plays", 20)):
        return None, "short_game"

    play_cap = (d.get("max_plays_by_sport") or {}).get(sport)
    if play_cap and n_plays > int(play_cap):
        return None, "implausible_play_count"

    max_frac = d.get("max_score_fix_frac")
    if max_frac is not None and n_plays and fixes / n_plays > float(max_frac):
        return None, "unreliable_scores"

    # A feed that needed the impossible-value clamp on a large share of its plays is not a score
    # feed. The official-final check CANNOT catch these -- the endpoint still lands correctly; what
    # is wrong is the middle of the path, where several scores collapse into one step. See the
    # config for the two inspected examples on each side of the cut.
    max_clamp = d.get("max_score_clamp_frac")
    if max_clamp is not None and n_plays and clamps / n_plays > float(max_clamp):
        return None, "unreliable_score_path"

    if (off_away is not None and away and away[-1] != off_away) or \
       (off_home is not None and home and home[-1] != off_home):
        # The play-by-play series must LAND ON the official final, or the pairing is wrong. After
        # the clamp this can only fail short — a feed whose plays never reach the score the game
        # actually finished on is a feed with plays missing, and its shape is not the game's.
        return None, "score_mismatch"

    away_t, home_t = team_names(summary)
    away_name = (away_t or {}).get("display")
    home_name = (home_t or {}).get("display")
    gdate = game_date(summary)
    gdate_short = gdate[:10] if gdate else None
    syear, stype = season_info(summary)
    # The LEAGUE SLUG, not the sport. Measured 2026-08-13: the sport form 404s
    # (www.espn.com/basketball/game/_/gameId/401810333 -> 404) while the league form resolves
    # (www.espn.com/nba/game/_/gameId/401810333 -> 200), for all three leagues.
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
        dataset="espn_us_majors",
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
            # ESPN's OWN season fields, kept under the source's name so nothing silently
            # reinterprets them. The census cell is a CALENDAR YEAR here (months tile the calendar;
            # COVID moved two seasons out of any fixed window), so this is where season-level
            # slicing actually lives. type: 1 preseason, 2 regular, 3 postseason, 4/5 all-star.
            "espn_season_year": syear,
            "espn_season_type": stype,
            "away_team": away_name,
            "home_team": home_name,
            # Nickname/abbreviation as the source gives them. These are what the prose uses, and
            # they are what the team anchor in verify_alignment.py matches on.
            "away_team_name": (away_t or {}).get("name"),
            "home_team_name": (home_t or {}).get("name"),
            "away_team_abbrev": (away_t or {}).get("abbrev"),
            "home_team_abbrev": (home_t or {}).get("abbrev"),
            "game_date": gdate,
            "n_plays": n_plays,
            "n_periods": n_periods,
            # Last index of each period: lets a consumer slice the series by period, and is what
            # makes the period score recoverable as an independent alignment anchor.
            "period_end_idx": period_end_idx,
            # Audit trail for the two cleanups in play_scores(), 0 for a clean feed:
            #   score_fix_plays    raw value BELOW the carried score, corrected upward (stale)
            #   score_clamp_plays  raw value ABOVE the official final, ignored (impossible)
            # A game with many of either is a game whose feed should be distrusted, which is why
            # they are per-record rather than a run-level total.
            "score_fix_plays": fixes,
            "score_clamp_plays": clamps,
            "final_away_score": away[-1] if away else None,
            "final_home_score": home[-1] if home else None,
            "report_headline": art.get("headline"),
            "report_source": src,
            "report_published": art.get("published"),
            "report_chars": len(story),
            "license_note": ("Associated Press wire recap served via ESPN's API; article.source is "
                             "recorded per record. Same rightsholder chain as 65_espn_college. "
                             "Tagged proprietary-review -- not cleared for distribution."),
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
                float(d.get("request_delay_s", 0.35)), verbose=args.verbose)
    cap = o.get("max_records")
    lcap = o.get("max_records_per_league")
    years = parse_years(d["discovery"]["years"])

    recs, skips = [], collections.Counter()
    per_league = {}
    for lg in d["leagues"]:
        ids, missing = discover_from_census(lg, years, cache)
        if missing:
            print(f"  ⚠️  {lg['label']}: no census cell for {missing} — "
                  f"run census.py --mode walk for those years", flush=True)
        kept = 0
        print(f"[{lg['label']}] discovered {len(ids)} finished games", flush=True)
        for eid in ids:
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
            if cap and len(recs) >= int(cap):
                break
        per_league[lg["label"]] = {"discovered": len(ids), "records": kept}
        print(f"[{lg['label']}] kept {kept}", flush=True)
        if cap and len(recs) >= int(cap):
            break

    plays = sorted(r["meta"]["n_plays"] for r in recs)
    chars = sorted(r["meta"]["report_chars"] for r in recs)
    stats = {
        "records": len(recs),
        "per_league": per_league,
        "skips": dict(skips),
        "report_sources": dict(collections.Counter(r["meta"]["report_source"] for r in recs)),
        "n_plays_median": plays[len(plays) // 2] if plays else None,
        "report_chars_median": chars[len(chars) // 2] if chars else None,
        "records_needing_score_fix": sum(1 for r in recs if r["meta"]["score_fix_plays"]),
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
    (PKG / o["run_report"]).write_text(json.dumps(
        {"dataset": "espn_us_majors", "stats": stats, "config_snapshot": cfg},
        indent=1, default=str))
    sp = PKG / o["samples_path"]
    sp.parent.mkdir(parents=True, exist_ok=True)
    # JSONL, one object per line. The old builder wrote a pretty-printed ARRAY here, which
    # validate.py --strict read as 2,868 malformed records.
    with sp.open("w") as fh:
        for r in recs[:5]:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(recs)} records -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
