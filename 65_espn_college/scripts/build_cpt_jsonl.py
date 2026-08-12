#!/usr/bin/env python3
"""ESPN college sports (FBS football + D-I men's/women's basketball) → CPT.

One record = one FINISHED GAME: the real wire-service recap (served via ESPN's API) paired with
the game's play-by-play RUNNING SCORE (away/home cumulative, one point per play). The recap
*describes* the shape of the game the series quantifies → `alignment: describes`.

Sibling of `51_espn_us_majors`, which covers NBA/NFL/NHL. The play-extraction logic is that
package's, unchanged in substance: `plays` for basketball, `drives.previous[].plays` for football,
score taken at every play (not per period — period level gives 3-4 points for football), and a
game is dropped if the play-by-play final disagrees with the official final.

Three things are NOT inherited from `51`, each measured on 2026-08-12:

  1. **`&groups=50` is mandatory for college basketball.** Without it the scoreboard returns 21
     games for 2024-02-10; with it, 155 — a 7x undercount at HTTP 200 with no error. Same class as
     the `&limit=1000` truncation `51` documents, and the reason scoreboard params are per league.
  2. **College basketball rejects date RANGES** (404 on every monthly range tried) while single
     dates work, so there is no range-query census shortcut here: discovery is a day walk.
  3. **A source allowlist is on by default.** 26 of 40 sampled 2023-25 men's recaps are
     "Data Skrive" — automated content, median 1,653 chars — which `SCHEMA.md` §7 disqualifies as
     boilerplate/template text and which would otherwise need `text_quality: generated` plus
     sign-off. Historical seasons are almost entirely AP (39 of 40 sampled across 2016-19), so the
     allowlist keeps the real journalism and drops the machine copy. It filters on
     `article.source`; it does not guess from the prose.

Records are built through `schema/emit.py`, so they are born strict-clean.

⚠️ LICENSE: the recaps are Associated Press wire copy served via ESPN's API — the same
rightsholder and the same open question as `51`, and a different (likely stricter) chain than
`45_cricket_report_overseries`, which is ESPNcricinfo's own staff journalism. Every record is
tagged `proprietary-review`. Do not scale or publish until redistribution is cleared.

Usage:
    python scripts/build_cpt_jsonl.py --dry-run
    python scripts/build_cpt_jsonl.py --set output.max_records=50
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import yaml

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG.parent / "schema"))
from emit import emit_record                                              # noqa: E402


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


# --- fetch -----------------------------------------------------------------------------------

_last = [0.0]


def http_json(url: str, ua: str, timeout: int, delay: float, tries: int = 4):
    """Paced GET returning parsed JSON, or None. A 404 is a real answer; 429/5xx are retried."""
    for attempt in range(tries):
        wait = delay - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(delay * (attempt + 2))
        except Exception:
            time.sleep(delay * (attempt + 2))
    return None


def cached_json(url: str, fp: Path, ua: str, timeout: int, delay: float):
    """Cache only real payloads. A failed fetch must not be cached as an empty answer."""
    if fp.exists():
        try:
            return json.loads(fp.read_text())
        except Exception:
            pass
    data = http_json(url, ua, timeout, delay)
    if data is not None:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(data))
    return data


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


def discover_events(lg: dict, d: dict, cache: Path) -> list[str]:
    """Finished event ids for one league, by walking the scoreboard day by day.

    Day-walk rather than range queries because college basketball 404s on ranges. `params` is
    per league so `&groups=50` reaches basketball (mandatory: 7x undercount without it) without
    being sent where it does not belong.
    """
    sport, league = lg["sport"], lg["league"]
    ids: list[str] = []
    dates = date_range(d["discovery"]["start_date"], d["discovery"]["end_date"],
                       int(d["discovery"].get("step_days", 1)))
    for date in dates:
        url = d["scoreboard_url"].format(sport=sport, league=league, date=date)
        url += lg.get("params", "")
        fp = cache / "scoreboard" / sport / league / f"{date}.json"
        data = cached_json(url, fp, d["user_agent"], int(d["timeout_s"]),
                           float(d.get("request_delay_s", 0.4)))
        if not data:
            continue
        for ev in data.get("events") or []:
            st = ((ev.get("status") or {}).get("type") or {})
            if st.get("name") == "STATUS_FINAL" or st.get("completed"):
                ids.append(ev["id"])
    return sorted(set(ids), key=lambda s: (len(s), s))


# --- summary parsing (logic inherited from 51_espn_us_majors) ---------------------------------

def strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s or "")
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</p>", "\n\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
          .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", s).strip()


def flatten_plays(summary: dict) -> list[dict]:
    """Basketball exposes a flat `plays` list; football nests plays under `drives.previous[]`."""
    plays = summary.get("plays") or []
    if plays:
        return plays
    out = []
    for drv in ((summary.get("drives") or {}).get("previous")) or []:
        out.extend(drv.get("plays") or [])
    return out


def play_scores(plays: list[dict]):
    """Running (away, home) score at EVERY play, in order, plus play and period counts.

    Every play carries a point, not only scoring plays: the flat shape of a possession that
    ends without points is part of what the recap describes.
    """
    away, home, periods = [], [], set()
    a = h = 0
    for p in plays:
        try:
            a = int(p.get("awayScore", a))
            h = int(p.get("homeScore", h))
        except (TypeError, ValueError):
            pass
        per = ((p.get("period") or {}).get("number"))
        if per is not None:
            periods.add(per)
        away.append(a)
        home.append(h)
    return away, home, len(plays), len(periods)


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


# --- record ----------------------------------------------------------------------------------

def build_record(event_id: str, lg: dict, cfg: dict, cache: Path):
    """-> (record | None, skip_reason). skip_reason is '' on success."""
    d, t = cfg["data"], cfg["text"]
    sport, league, label = lg["sport"], lg["league"], lg["label"]

    url = d["summary_url"].format(sport=sport, league=league, event_id=event_id)
    fp = cache / "espn" / sport / league / f"{event_id}.json"
    summary = cached_json(url, fp, d["user_agent"], int(d["timeout_s"]),
                          float(d.get("request_delay_s", 0.4)))
    if not summary:
        return None, "fetch_failed"

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
    away, home, n_plays, n_periods = play_scores(plays)
    if n_periods < int(d.get("min_periods", 2)) or n_plays < int(d.get("min_plays", 20)):
        return None, "short_game"

    off_away, off_home = official_scores(summary)
    if (off_away is not None and away and away[-1] != off_away) or \
       (off_home is not None and home and home[-1] != off_home):
        # The play-by-play series must land on the official final, or the pairing is wrong.
        return None, "score_mismatch"

    away_name, home_name = team_names(summary)
    gdate = game_date(summary)
    gdate_short = gdate[:10] if gdate else None
    page = "boxscore" if sport == "football" else "game"
    report_url = f"https://www.espn.com/{sport}/{page}/_/gameId/{event_id}"

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
            "away_team": away_name,
            "home_team": home_name,
            "game_date": gdate,
            "n_plays": n_plays,
            "n_periods": n_periods,
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
    args = ap.parse_args()

    cfg = load_config(Path(args.config), args.set)
    d, o = cfg["data"], cfg["output"]
    cache = PKG / d.get("cache_dir", ".cache")
    cap = o.get("max_records")

    # Per-league cap as well as a global one: leagues are walked in order, so a single global cap
    # fills entirely from the first league and the committed demo would show only football --
    # leaving the basketball path (and its mandatory &groups=50) unexercised.
    lcap = o.get("max_records_per_league")
    recs, skips = [], collections.Counter()
    per_league = {}
    for lg in d["leagues"]:
        ids = discover_events(lg, d, cache)
        kept = 0
        print(f"[{lg['label']}] discovered {len(ids)} finished games", flush=True)
        for eid in ids:
            if lcap and kept >= int(lcap):
                break
            rec, why = build_record(eid, lg, cfg, cache)
            if rec is None:
                skips[why.split(":")[0]] += 1
                skips[why] += 1 if ":" in why else 0
                continue
            recs.append(rec)
            kept += 1
            if cap and len(recs) >= int(cap):
                break
        per_league[lg["label"]] = {"discovered": len(ids), "records": kept}
        print(f"[{lg['label']}] kept {kept}", flush=True)
        if cap and len(recs) >= int(cap):
            break

    srcs = collections.Counter(r["meta"]["report_source"] for r in recs)
    plays = sorted(r["meta"]["n_plays"] for r in recs)
    chars = sorted(r["meta"]["report_chars"] for r in recs)
    stats = {
        "records": len(recs),
        "per_league": per_league,
        "skips": dict(skips),
        "report_sources": dict(srcs),
        "n_plays_median": plays[len(plays) // 2] if plays else None,
        "n_plays_min": plays[0] if plays else None,
        "report_chars_median": chars[len(chars) // 2] if chars else None,
        "alignment": "describes",
        "license": "proprietary-review",
    }
    print(json.dumps(stats, indent=2)[:2500])
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
