#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from ESPN US-majors (NBA/NFL/NHL) game recaps
+ period-by-period running-score series.

One record = one FINISHED GAME: the away/home cumulative score at the end of each
period, paired with the real recap that narrates the game's shape (comeback,
blowout, lead held throughout). The recap *describes* the progression the series
quantifies -> "describes". text_quality is always "real"; a game with no usable
recap is dropped (no synthetic fallback).

Series: extracted directly from ESPN's own play-by-play (`plays` for NBA/NHL;
        `drives.previous[].plays` for NFL) -- take the score at the last play of
        each period. Stdlib only.
Text  : recap prose, fetched via the same ESPN parent API Cricket already uses
        (site.api.espn.com) -- `article.story`. NOTE: `article.source == "AP"`
        on every game checked across all 3 leagues -- this is Associated Press
        wire copy, not ESPN staff writing (a different, likely stricter chain
        than ESPNcricinfo's own journalists in the Cricket package).

No bulk archive exists for this source (unlike Cricsheet for cricket), so event
discovery walks a date range via the scoreboard endpoint per league.

LICENSE: see config.example.yaml header and README.md. The committed output/ +
samples/ are a capped 50-record demo for the lead to inspect (internal review,
not distribution). Keep output.max_records small; do not run/commit a full
build or publish until redistribution is cleared with Charon.

Examples:
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=5
  python scripts/build_cpt_jsonl.py
"""

from __future__ import annotations

import argparse
import html as _html
import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required. pip install -r requirements.txt") from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


# --- config helpers (same conventions as the other packages) ---------------

def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    m = dict(base)
    for k, v in over.items():
        m[k] = deep_merge(m[k], v) if k in m and isinstance(m[k], dict) and isinstance(v, dict) else v
    return m


def coerce(raw: str) -> Any:
    low = raw.strip().lower()
    if low in {"true", "yes"}: return True
    if low in {"false", "no"}: return False
    if low in {"null", "none", "~"}: return None
    if re.fullmatch(r"-?\d+", raw): return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw): return float(raw)
    return raw


def parse_sets(sets: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for it in sets:
        k, v = it.split("=", 1)
        cur = out
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = coerce(v)
    return out


def load_config(path: Path, sets: Sequence[str]) -> Dict[str, Any]:
    cfg = yaml.safe_load(path.read_text())
    return deep_merge(cfg, parse_sets(sets)) if sets else cfg


def rp(s: str) -> Path:
    p = Path(s)
    return p if p.is_absolute() else ROOT / p


# --- HTTP --------------------------------------------------------------------

def http_get_json(url: str, ua: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    raw = urllib.request.urlopen(req, timeout=timeout, context=_SSL).read()
    return json.loads(raw)


def cached_get_json(url: str, cache_fp: Path, ua: str, timeout: int, delay_s: float) -> Optional[dict]:
    if cache_fp.exists():
        try:
            return json.loads(cache_fp.read_text())
        except Exception:
            pass
    try:
        data = http_get_json(url, ua, timeout)
    except Exception:
        return None
    cache_fp.parent.mkdir(parents=True, exist_ok=True)
    cache_fp.write_text(json.dumps(data))
    time.sleep(delay_s)
    return data


# --- event discovery ---------------------------------------------------------

def date_range(start: str, end: str, step_days: int) -> List[str]:
    s = datetime.strptime(start, "%Y%m%d")
    e = datetime.strptime(end, "%Y%m%d")
    out, cur = [], s
    while cur <= e:
        out.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=step_days)
    return out


def discover_events(league_cfg: dict, d: dict, cache: Path) -> List[str]:
    """Walk the configured date range for one league's scoreboard; return finished event ids."""
    sport, league = league_cfg["sport"], league_cfg["league"]
    ids: List[str] = []
    for date in date_range(d["discovery"]["start_date"], d["discovery"]["end_date"], int(d["discovery"]["step_days"])):
        url = d["scoreboard_url"].format(sport=sport, league=league, date=date)
        fp = cache / "scoreboard" / sport / league / f"{date}.json"
        data = cached_get_json(url, fp, d["user_agent"], int(d["timeout_s"]), float(d.get("request_delay_s", 0.4)))
        if not data:
            continue
        for ev in data.get("events", []):
            status = (ev.get("status") or {}).get("type", {}).get("name")
            if status == "STATUS_FINAL":
                ids.append(ev["id"])
    return ids


# --- summary parsing ----------------------------------------------------------

def _strip_html(s: str) -> str:
    """ESPN `article.story` is HTML with inline <a> links and paragraph tags."""
    s = re.sub(r"<(?:video|photo|inline)\d*[^>]*>", "", s)
    s = re.sub(r"</(?:p|h[1-6]|li|div|ul|ol)>", "\n\n", s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _flatten_plays(summary: dict, sport: str) -> List[dict]:
    """NBA/NHL expose a flat `plays` list; NFL nests plays under `drives.previous[]`."""
    plays = summary.get("plays") or []
    if plays:
        return plays
    drives = ((summary.get("drives") or {}).get("previous")) or []
    out: List[dict] = []
    for drv in drives:
        out.extend(drv.get("plays") or [])
    return out


def play_scores(plays: List[dict]) -> Tuple[List[int], List[int], int, int]:
    """Running score at EVERY play, in order. Returns (away, home, n_plays, n_periods).

    One point per play -- the source's actual finest native granularity (more
    native than period boundaries, which are a broadcast convention we would
    otherwise be imposing on top of the raw feed). Score is held constant across
    non-scoring plays (a rebound, a timeout, a penalty) -- this is the real
    signal, not filler: it is exactly how NOAA SWPC's quiet-vs-active days are
    kept in that package, and the *contrast* between flat stretches and jumps is
    what the model has to learn from a play-by-play trace.

    Uses a running MAX rather than each play's literal score field: ESPN's own
    feed trails the true final scoring play with non-scoring administrative
    events ("End of the 4th Quarter", "End of Game") that can carry a STALE
    snapshot (observed live: last scoring play = 113, but the subsequent
    "End of Game" marker still read 112 -- one free throw behind). A running
    max self-corrects this at every position, not just the final one.
    """
    away, home, periods_seen = [], [], set()
    running_away, running_home = 0, 0
    for p in plays:
        aw, hm = p.get("awayScore"), p.get("homeScore")
        if aw is None or hm is None:
            if away:
                away.append(running_away); home.append(running_home)
            continue
        running_away = max(running_away, int(aw))
        running_home = max(running_home, int(hm))
        away.append(running_away)
        home.append(running_home)
        pn = (p.get("period") or {}).get("number")
        if pn is not None:
            periods_seen.add(int(pn))
    n_periods = max(periods_seen) if periods_seen else 0
    return away, home, len(away), n_periods


def official_scores(summary: dict) -> Tuple[Optional[int], Optional[int]]:
    """(away_score, home_score) from the header's official boxscore -- the source of
    truth for the final score. Used as a safety net: NHL shootouts award the deciding
    goal as a +1 to the final tally WITHOUT it appearing in the play-by-play's
    awayScore/homeScore fields (those stay frozen at the tied regulation/OT score
    through the whole shootout period) -- so a shootout game's extracted series would
    silently disagree with the text's own stated final score. Any game where the
    extracted series final doesn't match this official score is dropped rather than
    shipped with a mismatched pairing.
    """
    header = summary.get("header") or {}
    comps = (header.get("competitions") or [{}])[0]
    away = home = None
    for c in comps.get("competitors", []):
        score = c.get("score")
        try:
            score = int(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        if c.get("homeAway") == "home":
            home = score
        elif c.get("homeAway") == "away":
            away = score
    return away, home


def team_names(summary: dict) -> Tuple[Optional[str], Optional[str]]:
    """(away_name, home_name) from header.competitions[0].competitors."""
    header = summary.get("header") or {}
    comps = (header.get("competitions") or [{}])[0]
    away = home = None
    for c in comps.get("competitors", []):
        name = (c.get("team") or {}).get("displayName")
        if c.get("homeAway") == "home":
            home = name
        elif c.get("homeAway") == "away":
            away = name
    return away, home


def game_date(summary: dict) -> Optional[str]:
    header = summary.get("header") or {}
    comps = (header.get("competitions") or [{}])[0]
    return comps.get("date")


# --- record construction ------------------------------------------------------

def build_record_for_event(event_id: str, league_cfg: dict, cfg: Dict[str, Any],
                            cache: Path) -> Tuple[Optional[dict], str]:
    """Returns (record | None, skip_reason). skip_reason is '' on success."""
    d, t = cfg["data"], cfg["text"]
    sport, league, label = league_cfg["sport"], league_cfg["league"], league_cfg["label"]

    url = d["summary_url"].format(sport=sport, league=league, event_id=event_id)
    fp = cache / "espn" / sport / league / f"{event_id}.json"
    summary = cached_get_json(url, fp, d["user_agent"], int(d["timeout_s"]), float(d.get("request_delay_s", 0.4)))
    if not summary:
        return None, "fetch_failed"

    art = summary.get("article") or {}
    story = _strip_html(art.get("story") or "")
    min_chars = int(t.get("min_report_chars", 400))
    if not story or len(story) < min_chars:
        return None, "no_report"

    plays = _flatten_plays(summary, sport)
    away_scores, home_scores, n_plays, n_periods = play_scores(plays)
    if n_periods < int(d.get("min_periods", 3)) or n_plays < int(d.get("min_plays", 20)):
        return None, "short_game"

    off_away, off_home = official_scores(summary)
    if (off_away is not None and away_scores and away_scores[-1] != off_away) or \
       (off_home is not None and home_scores and home_scores[-1] != off_home):
        # Catches NHL shootouts (deciding goal not reflected in play-by-play score
        # fields) and any other case where the play-by-play series disagrees with
        # the official final -- drop rather than ship a mismatched pairing.
        return None, "score_mismatch"

    away_name, home_name = team_names(summary)
    intro = t["ts_intro_sentence"].format(away=away_name or "the away team", home=home_name or "the home team")
    text = f"{story}\n\n{intro}"

    timeseries = [
        {"values": away_scores, "unit": "away_score_cumulative", "freq": "1play"},
        {"values": home_scores, "unit": "home_score_cumulative", "freq": "1play"},
    ]

    gdate = game_date(summary)
    gdate_short = gdate[:10] if gdate else None
    report_url = f"https://www.espn.com/{sport}/{('game' if sport != 'football' else 'boxscore')}/_/gameId/{event_id}"

    rec = {
        "text": text,
        "timeseries": timeseries,
        "task_type": "world_knowledge",
        "text_quality": "real",
        "event_id": event_id,
        "league": label,
        "away_team": away_name,
        "home_team": home_name,
        "game_date": gdate,
        "n_periods": n_periods,
        "n_plays": n_plays,
        "final_away_score": away_scores[-1] if away_scores else None,
        "final_home_score": home_scores[-1] if home_scores else None,
        "report_url": report_url,
        "report_headline": art.get("headline"),
        "report_source": art.get("source"),
        "report_published": art.get("published"),
        "dataset": "espn_us_majors",
        "source": report_url,
        "series_id": f"espn_{league}_{event_id}",
        # v1 schema optional fields (SCHEMA.md SS5-7) -- added from the start per
        # SS10 migration notes ("new packages should add alignment/license from day one").
        "license": "proprietary-review",   # AP wire copy served via ESPN's API -- see README
        "text_source": "third_party",      # independent AP journalism, not team/league official text
        "alignment": "describes",          # recap narrates the game's shape the score series quantifies
        "domain": "sports",
        "region": "US",
        "period_start": gdate_short,
        "period_end": gdate_short,
    }
    return rec, ""


def validate(rec: dict) -> List[str]:
    e = []
    if rec["text"].count("<ts></ts>") != 1:
        e.append("ts token count")
    ts = rec.get("timeseries", [])
    lens = {len(c["values"]) for c in ts}
    if len(lens) != 1:
        e.append(f"channel length mismatch {sorted(lens)}")
    if lens and next(iter(lens)) != rec["n_plays"]:
        e.append("length != n_plays")
    return e


# --- pipeline ------------------------------------------------------------------

def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    d, out_cfg = cfg["data"], cfg["output"]
    cache = rp(d["cache_dir"])
    maxrec = out_cfg.get("max_records")
    # Split the cap evenly across leagues so a demo run actually exercises all of
    # them, instead of one league (whichever discovers first) eating the whole cap.
    per_league_cap = None if maxrec is None else max(1, -(-int(maxrec) // len(d["leagues"])))

    stats = {"events_discovered": 0, "fetch_failed": 0, "no_report": 0,
             "short_game": 0, "invalid": 0, "emitted": 0, "by_league": {}}
    records: List[dict] = []

    for league_cfg in d["leagues"]:
        label = league_cfg["label"]
        ids = discover_events(league_cfg, d, cache)
        stats["events_discovered"] += len(ids)
        stats["by_league"][label] = {"discovered": len(ids), "emitted": 0}
        league_count = 0

        for eid in ids:
            rec, skip = build_record_for_event(eid, league_cfg, cfg, cache)
            if skip:
                stats[skip] = stats.get(skip, 0) + 1
                continue
            verr = validate(rec)
            if verr:
                stats["invalid"] += 1
                continue
            records.append(rec)
            stats["emitted"] += 1
            league_count += 1
            stats["by_league"][label]["emitted"] += 1
            if per_league_cap is not None and league_count >= per_league_cap:
                break
            if maxrec is not None and len(records) >= int(maxrec):
                break
        if maxrec is not None and len(records) >= int(maxrec):
            break

    report = {
        "leagues": [lc["label"] for lc in d["leagues"]],
        "discovery_window": [d["discovery"]["start_date"], d["discovery"]["end_date"]],
        "report_source": "site.api.espn.com (article.source == AP on every game checked)",
        "stats": stats,
        "config_snapshot": cfg,
        "dry_run": dry,
    }

    if dry:
        if records:
            print("\n--- sample record ---")
            print(json.dumps(records[0], ensure_ascii=False, indent=2)[:2400])
        print("\n" + json.dumps(stats, indent=2))
        return report

    op = rp(out_cfg["output_path"]); op.parent.mkdir(parents=True, exist_ok=True)
    with op.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if records and out_cfg.get("samples_path"):
        sp = rp(out_cfg["samples_path"]); sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("w", encoding="utf-8") as fh:
            json.dump(records[:3], fh, ensure_ascii=False, indent=2); fh.write("\n")
    rpath = rp(out_cfg["report_path"]); rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Build ESPN US-majors recap + period-score CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s['emitted']} records "
          f"(discovered {s['events_discovered']}, no_report={s.get('no_report',0)}, "
          f"short_game={s.get('short_game',0)}, fetch_failed={s.get('fetch_failed',0)}, "
          f"invalid={s['invalid']}). By league: {s['by_league']}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
