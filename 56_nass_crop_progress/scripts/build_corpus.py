#!/usr/bin/env python3
"""Stage 3: pair cached narratives with cached series into CPT records, and measure the pairing.

Purely local (reads `.cache/series_index.pkl` + `.cache/text/*.jsonl`), so a full 48-state build
is a couple of minutes and every design question below is answerable by measurement rather than
argument.

Two window strategies, selectable, because the package inherited one without a measurement:

  season   -- expanding within-season: season's first reported week -> this report's week. The
              design the corpus's windowed-series policy rejected on `11_eia`. Bounded here by a
              ~30-45 week season rather than 25 years, so far less pathological, but it still
              duplicates the early-season weeks into every later record of the same season, and
              it forces a `min_window_weeks` floor that throws away every early-season report.
  trailing -- the last N *reported* weeks ending at this report's week, crossing season
              boundaries. This is the shipped `11_eia` pattern, whose window length was chosen
              from what the prose actually references: NASS weekly narratives anchor on
              week-over-week, season-to-date, and same-week-last-year, so a window that reaches
              back through the previous season grounds all three. Off-season weeks simply don't
              exist in the source, so the window is irregular in clock time and carries an
              explicit `timestamps` array (SCHEMA.md §3.3).

`--measure` reports, per strategy: record count, window-length and null-density distributions,
series duplication (unique vs emitted points), and a **recitation-by-lag profile** -- for each
lag k behind the report week, the share of that week's values that appear verbatim in the prose,
against a permutation control. That profile is the direct evidence for how much of the window the
text actually accounts for.

Usage:
    python scripts/build_corpus.py --measure --states IA,KS,TX
    python scripts/build_corpus.py --window trailing --trailing-weeks 52 --out output/...jsonl
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pickle
import random
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import channels as ch  # noqa: E402
import state_sources as ss  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent
CACHE = PKG_ROOT / ".cache"
TEXT_DIR = CACHE / "text"

# ---------------------------------------------------------------- loading


def load_series() -> dict[str, dict[str, dict[dt.date, float]]]:
    idx = pickle.load(open(CACHE / "series_index.pkl", "rb"))
    by_state: dict[str, dict[str, dict]] = collections.defaultdict(dict)
    for (st, sd), per_date in idx.items():
        by_state[st][sd] = per_date
    return by_state


def load_text(alpha: str) -> dict[dt.date, dict]:
    """date -> best narrative row for that date, read from the state's *text pool*.

    A state normally reads its own archive; the pool indirection exists for states whose reports
    are published by a shared office (see `StateConfig.text_pool`).

    A date can have several captures (re-crawls, corrected reissues). Keep the *longest clean
    narrative* rather than the first seen: extraction quality varies between captures of the same
    report, and "first wins" silently prefers whichever crawl CDX happened to list first.
    """
    cfg = ss.STATE_CONFIGS.get(alpha)
    p = TEXT_DIR / f"{cfg.pool if cfg else alpha}.jsonl"
    if not p.exists():
        return {}
    best: dict[dt.date, dict] = {}
    with open(p) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("reject") or not r.get("narrative") or not r.get("date"):
                continue
            d = dt.date.fromisoformat(r["date"])
            cur = best.get(d)
            if cur is None or len(r["narrative"]) > len(cur["narrative"]):
                best[d] = r
    return best


# ---------------------------------------------------------------- numbers in prose

_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d)?)(?![\w])")


def text_numbers(text: str) -> set[float]:
    """Numeric tokens in the prose, as floats.

    Deliberately narrow: 1-3 digits with at most one decimal place, which is the shape of every
    quantity these reports state (percentages 0-100 and days-suitable 0.0-7.0). Wider patterns
    pull in years, phone fragments and table row ids, which inflates any recitation measurement.
    """
    return {float(m.group(1)) for m in _NUM_RE.finditer(text)}


def week_values(state_series: dict, chans: list[ch.Channel], d: dt.date) -> set[float]:
    out = set()
    for c in chans:
        v = state_series.get(c.short_desc, {}).get(d)
        if v is not None:
            out.add(float(v))
    return out


def recite_frac(nums: set[float], vals: set[float]) -> float | None:
    if not vals:
        return None
    return len(vals & nums) / len(vals)


# ---------------------------------------------------------------- ordered-group recitation
#
# Loose "does this number appear anywhere" matching is nearly useless on its own here: the values
# are 0-100 percentages and a report prints dozens of numbers, so an unrelated report's prose
# already matches ~50% of any week's values (measured control, Iowa: 0.5035). Alignment therefore
# has to be tested on something a coincidence cannot produce.
#
# NASS recites each rating group as an ordered sentence -- "Topsoil moisture levels rated 8 percent
# very short, 28 percent short, 60 percent adequate, and 4 percent surplus" -- so the test is
# whether a group's 4-5 values for the report's own week appear *in that order, close together*.
# Chance agreement on an ordered 4-tuple is negligible, which is what makes the resulting
# `recites` tag mean something.

_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("topsoil_moisture", ("topsoil_moisture_pct_very_short", "topsoil_moisture_pct_short",
                          "topsoil_moisture_pct_adequate", "topsoil_moisture_pct_surplus")),
    ("subsoil_moisture", ("subsoil_moisture_pct_very_short", "subsoil_moisture_pct_short",
                          "subsoil_moisture_pct_adequate", "subsoil_moisture_pct_surplus")),
]
_COND_SUFFIXES = ("_condition_pct_very_poor", "_condition_pct_poor", "_condition_pct_fair",
                  "_condition_pct_good", "_condition_pct_excellent")


def _condition_groups(units: set[str]) -> list[tuple[str, tuple[str, ...]]]:
    bases = {u[: -len("_condition_pct_very_poor")] for u in units
             if u.endswith("_condition_pct_very_poor")}
    return [(f"{b}_condition", tuple(f"{b}{s}" for s in _COND_SUFFIXES)) for b in sorted(bases)]


def ordered_groups_recited(text: str, terminal: dict[str, float], units: set[str],
                           span: int = 260) -> list[str]:
    """Names of the rating groups whose terminal-week values appear in order in the prose."""
    hits = []
    # position list per value, so "in order and close together" is checkable directly
    positions: dict[str, list[int]] = {}
    for m in _NUM_RE.finditer(text):
        positions.setdefault(m.group(1), []).append(m.start())

    def key(v: float) -> str:
        return f"{int(v)}" if float(v).is_integer() else f"{v:.1f}"

    for name, group in _GROUPS + _condition_groups(units):
        vals = [terminal.get(u) for u in group]
        if any(v is None for v in vals):
            continue
        cur = -1
        start = None
        ok = True
        for v in vals:                      # type: ignore[union-attr]
            nxt = next((p for p in positions.get(key(v), []) if p > cur), None)
            if nxt is None:
                ok = False
                break
            if start is None:
                start = nxt
            cur = nxt
        if ok and start is not None and cur - start <= span:
            hits.append(name)
    return hits


# The weather narrative in many states' reports is contributed by the *State* Climatologist or a
# state department of agriculture, inside a federal NASS publication. State government works are
# not covered by 17 U.S.C. §105, so this is the same shape as the USDM non-federal lead-byline
# hold and #55's `nonfederal_affiliation` flag: detected and flagged per record for a licensing
# call, not silently included or silently dropped.
_NONFEDERAL_RE = re.compile(
    r"(State Climatologist|Department of Agriculture and Land Stewardship|"
    r"State Climatology|Office of the State Climatologist|"
    r"[A-Z][a-z]+ Department of Agriculture)", re.IGNORECASE,
)


# ---------------------------------------------------------------- window construction


def reported_weeks(state_series: dict, chans: list[ch.Channel]) -> list[dt.date]:
    ds: set[dt.date] = set()
    for c in chans:
        ds |= set(state_series.get(c.short_desc, {}))
    return sorted(ds)


def season_window(weeks: list[dt.date], i: int) -> list[dt.date]:
    """All reported weeks of the same calendar year up to and including weeks[i]."""
    y = weeks[i].year
    return [d for d in weeks[: i + 1] if d.year == y]


def trailing_window(weeks: list[dt.date], i: int, n: int) -> list[dt.date]:
    lo = max(0, i + 1 - n)
    return weeks[lo: i + 1]


# ---------------------------------------------------------------- build


def build_state(alpha: str, state_series: dict, texts: dict, cfg: dict,
                stats: dict) -> list[dict]:
    chans, commodities = ch.select_channels(state_series, min_weeks=cfg["min_commodity_weeks"],
                                            max_commodities=cfg["max_commodities"])
    if not chans:
        return []
    weeks = reported_weeks(state_series, chans)
    if not weeks:
        return []
    wk_index = {d: i for i, d in enumerate(weeks)}

    recs: list[dict] = []
    for d, trow in sorted(texts.items()):
        i = wk_index.get(d)
        if i is None:
            stats["skip_no_series_week"] += 1
            continue
        if cfg["window"] == "season":
            win = season_window(weeks, i)
            if len(win) < cfg["min_window_weeks"]:
                stats["skip_short_window"] += 1
                continue
        else:
            win = trailing_window(weeks, i, cfg["trailing_weeks"])
            if len(win) < cfg["trailing_weeks"]:
                stats["skip_short_window"] += 1
                continue

        narrative = trow["narrative"].strip()
        if len(narrative) < cfg["min_text_chars"]:
            stats["skip_short_text"] += 1
            continue
        # Upper gate catches documents that are not a single weekly report. Measured: exactly 12 of
        # 19,058 harvested reports exceed 20k chars (0.06%), and every one is a whole-season
        # compilation (Wisconsin `cw2002.pdf`, `cwan2001.pdf`) or a monthly summary (Idaho
        # `Monthly_Feb_2016.pdf`). They state a valid week-ending date, so nothing upstream rejects
        # them, but their text spans a season while their window ends at one week -- a genuine
        # alignment defect. The distribution leaves no ambiguity: p99.9 is 13,918 chars.
        if len(narrative) > cfg["max_text_chars"]:
            stats["skip_long_text"] += 1
            continue

        timeseries = []
        n_null = n_tot = 0
        for c in chans:
            per = state_series.get(c.short_desc, {})
            vals = [per.get(w) for w in win]
            if all(v is None for v in vals):
                continue
            timeseries.append({"values": vals, "unit": c.unit, "freq": "1w"})
            n_null += sum(1 for v in vals if v is None)
            n_tot += len(vals)
        if not timeseries:
            stats["skip_no_channels"] += 1
            continue
        null_frac = n_null / max(n_tot, 1)
        if null_frac > cfg["max_null_frac"]:
            stats["skip_too_sparse"] += 1
            continue

        # Measured, per-record alignment tier (the pattern #41 and #55 settled on): tag from what
        # this record's own prose demonstrably recites, rather than asserting one tier for the
        # whole package.
        units = {t["unit"] for t in timeseries}
        terminal = {t["unit"]: t["values"][-1] for t in timeseries if t["values"][-1] is not None}
        groups = ordered_groups_recited(narrative, terminal, units)
        alignment = "recites" if groups else "describes"
        stats["_align"][alignment] += 1
        for g in groups:
            stats["_groups"][g] += 1
        nonfed = bool(_NONFEDERAL_RE.search(narrative))
        if nonfed:
            stats["nonfederal_narrative"] += 1

        rec = {
            "text": narrative + "\n\n<ts></ts>",
            "timeseries": timeseries,
            "task_type": "world_knowledge",
            "text_quality": "real",
            "timestamps": [w.isoformat() for w in win],
            "series_id": f"nass_crop_progress:{alpha}:{d.isoformat()}",
            "dataset": "nass_crop_progress",
            "source": trow["url"],
            "license": "public-domain-us-gov",
            "text_source": "first_party_official",
            "alignment": alignment,
            "domain": "agriculture",
            "region": f"US-{alpha}",
            "period_start": win[0].isoformat(),
            "period_end": d.isoformat(),
            "meta": {
                "state": alpha,
                "commodities": [c.lower() for c in commodities],
                "season_year": d.year,
                "week_ending": d.isoformat(),
                "window_weeks": len(win),
                "n_channels": len(timeseries),
                "null_frac": round(null_frac, 4),
                "recited_groups": groups,
                "nonfederal_narrative": nonfed,
            },
        }
        recs.append(rec)
        stats["emitted"] += 1
        stats["_win_lens"].append(len(win))
        stats["_null_fracs"].append(n_null / max(n_tot, 1))
        stats["_n_chans"].append(len(timeseries))
        stats["_text_chars"].append(len(narrative))
        for w in win:
            stats["_unique_points"].add((alpha, w))
        stats["_emitted_points"] += n_tot - n_null
        stats["_unique_obs"].update((alpha, w, t["unit"]) for t in timeseries for w in win)
    return recs


# ---------------------------------------------------------------- measurement


def measure_alignment(alpha: str, state_series: dict, texts: dict, recs: list[dict],
                      cfg: dict, prof: dict, rng: random.Random) -> None:
    """Recitation-by-lag profile + permutation control.

    For each record, walk back from the report week and ask what share of that week's real values
    appear as numbers in the prose. Lag 0 is the report's own week. The control re-runs lag 0
    against a *different* record's prose from the same state, which fixes the coincidence floor:
    these are 0-100 percentages and a page holds dozens of numbers, so some overlap is guaranteed
    even with unrelated text, and any claim about alignment is meaningless without that baseline.
    """
    chans, _ = ch.select_channels(state_series, min_weeks=cfg["min_commodity_weeks"],
                                  max_commodities=cfg["max_commodities"])
    if len(recs) < 3:
        return
    all_texts = [r["text"] for r in recs]
    for r in recs:
        nums = text_numbers(r["text"])
        win = [dt.date.fromisoformat(s) for s in r["timestamps"]]
        for lag, w in enumerate(reversed(win)):
            if lag > 60:
                break
            f = recite_frac(nums, week_values(state_series, chans, w))
            if f is not None:
                prof["by_lag"][lag].append(f)
        # control: this record's window, someone else's prose
        other = rng.choice(all_texts)
        while other is r["text"] and len(all_texts) > 1:
            other = rng.choice(all_texts)
        cnums = text_numbers(other)
        f0 = recite_frac(cnums, week_values(state_series, chans, win[-1]))
        if f0 is not None:
            prof["control_lag0"].append(f0)
        f_true = recite_frac(nums, week_values(state_series, chans, win[-1]))
        if f_true is not None:
            prof["true_lag0"].append(f_true)

        # The same permutation control applied to the ordered-group test, which is what actually
        # decides each record's alignment tier. This is the number that says whether `recites`
        # means anything: the loose test's control sits at ~50%, so without this the tier would be
        # indistinguishable from chance.
        units = {t["unit"] for t in r["timeseries"]}
        terminal = {t["unit"]: t["values"][-1] for t in r["timeseries"]
                    if t["values"][-1] is not None}
        prof["grp_true"].append(1 if ordered_groups_recited(r["text"], terminal, units) else 0)
        prof["grp_ctrl"].append(1 if ordered_groups_recited(other, terminal, units) else 0)


def new_stats() -> dict:
    return {
        "emitted": 0, "skip_no_series_week": 0, "skip_short_window": 0,
        "skip_short_text": 0, "skip_long_text": 0, "skip_no_channels": 0, "skip_too_sparse": 0,
        "nonfederal_narrative": 0,
        "_win_lens": [], "_null_fracs": [], "_n_chans": [], "_text_chars": [],
        "_unique_points": set(), "_emitted_points": 0, "_unique_obs": set(),
        "_align": collections.Counter(), "_groups": collections.Counter(),
    }


def summarize(stats: dict, prof: dict | None = None) -> dict:
    wl = stats["_win_lens"]
    out = {k: v for k, v in stats.items() if not k.startswith("_")}
    if wl:
        out["window_weeks"] = {
            "min": min(wl), "median": statistics.median(wl), "max": max(wl),
            "mean": round(statistics.mean(wl), 1),
        }
        out["null_frac"] = {
            "median": round(statistics.median(stats["_null_fracs"]), 4),
            "mean": round(statistics.mean(stats["_null_fracs"]), 4),
        }
        out["channels_per_record"] = {
            "min": min(stats["_n_chans"]), "median": statistics.median(stats["_n_chans"]),
            "max": max(stats["_n_chans"]),
        }
        out["text_chars"] = {
            "min": min(stats["_text_chars"]),
            "median": statistics.median(stats["_text_chars"]),
            "max": max(stats["_text_chars"]),
        }
        uo = len(stats["_unique_obs"])
        out["duplication"] = {
            "emitted_real_points": stats["_emitted_points"],
            "unique_state_week_channel": uo,
            "ratio": round(stats["_emitted_points"] / max(uo, 1), 2),
        }
        out["alignment_tier"] = dict(stats["_align"])
        out["recited_groups"] = dict(stats["_groups"].most_common(12))
    if prof and prof.get("grp_true"):
        out["ordered_group_alignment"] = {
            "true": round(statistics.mean(prof["grp_true"]), 4),
            "control": round(statistics.mean(prof["grp_ctrl"]), 4),
            "lift_pp": round(100 * (statistics.mean(prof["grp_true"])
                                    - statistics.mean(prof["grp_ctrl"])), 1),
            "n": len(prof["grp_true"]),
        }
    if prof and prof["true_lag0"]:
        lags = {}
        for lag in sorted(prof["by_lag"]):
            v = prof["by_lag"][lag]
            if len(v) >= 20:
                lags[lag] = round(statistics.mean(v), 4)
        out["recitation"] = {
            "true_lag0": round(statistics.mean(prof["true_lag0"]), 4),
            "control_lag0": round(statistics.mean(prof["control_lag0"]), 4),
            "lift_pp": round(100 * (statistics.mean(prof["true_lag0"])
                                    - statistics.mean(prof["control_lag0"])), 1),
            "by_lag": lags,
        }
    return out


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, help="YAML config (see config.example.yaml)")
    ap.add_argument("--states", default=None)
    ap.add_argument("--window", choices=["season", "trailing"], default="trailing")
    ap.add_argument("--trailing-weeks", type=int, default=52)
    ap.add_argument("--min-window-weeks", type=int, default=20)
    ap.add_argument("--min-text-chars", type=int, default=200)
    ap.add_argument("--min-commodity-weeks", type=int, default=150)
    ap.add_argument("--max-commodities", type=int, default=8,
                    help="cap on commodities contributing channels per state")
    ap.add_argument("--max-text-chars", type=int, default=20000,
                    help="reject season compilations / monthly summaries masquerading as reports")
    ap.add_argument("--max-null-frac", type=float, default=0.75,
                    help="drop records whose window is mostly gaps (health gate)")
    ap.add_argument("--measure", action="store_true",
                    help="run BOTH window strategies and print a comparison, emit nothing")
    ap.add_argument("--out", default=None)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    # A config file supplies defaults; anything given explicitly on the command line wins, so the
    # documented config is the real source of truth for a build rather than decoration.
    if args.config:
        import yaml
        cfg_file = yaml.safe_load(open(args.config)) or {}
        given = {a.lstrip("-").replace("-", "_") for a in sys.argv[1:] if a.startswith("--")}
        for k, v in cfg_file.items():
            key = k.replace("-", "_")
            if key in vars(args) and key not in given and v is not None:
                setattr(args, key, v)

    by_state = load_series()
    if args.states:
        states = args.states.split(",")
    else:
        # Only states that emit; the New England pool is a text source with no series it aligns to.
        states = sorted(a for a, c in ss.STATE_CONFIGS.items() if c.emits_records)

    base_cfg = {
        "min_window_weeks": args.min_window_weeks,
        "min_text_chars": args.min_text_chars,
        "min_commodity_weeks": args.min_commodity_weeks,
        "trailing_weeks": args.trailing_weeks,
        "max_null_frac": args.max_null_frac,
        "max_text_chars": args.max_text_chars,
        "max_commodities": args.max_commodities,
    }

    variants = ([("season", dict(base_cfg, window="season")),
                 ("trailing", dict(base_cfg, window="trailing"))] if args.measure
                else [(args.window, dict(base_cfg, window=args.window))])

    results = {}
    for name, cfg in variants:
        stats = new_stats()
        prof = {"by_lag": collections.defaultdict(list), "true_lag0": [], "control_lag0": [],
                "grp_true": [], "grp_ctrl": []}
        rng = random.Random(20260730)
        out_recs: list[dict] = []
        for alpha in states:
            if alpha not in by_state:
                continue
            texts = load_text(alpha)
            if not texts:
                continue
            recs = build_state(alpha, by_state[alpha], texts, cfg, stats)
            if args.measure:
                measure_alignment(alpha, by_state[alpha], texts, recs, cfg, prof, rng)
            out_recs.extend(recs)
            print(f"  {alpha}: {len(recs)} records ({name})", file=sys.stderr, flush=True)
        results[name] = summarize(stats, prof if args.measure else None)
        if args.out and not args.measure:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            with open(args.out, "w") as f:
                for r in out_recs:
                    f.write(json.dumps(r) + "\n")
            print(f"wrote {len(out_recs)} records -> {args.out}", file=sys.stderr)

    print(json.dumps(results, indent=2, default=str))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        with open(args.report, "w") as f:
            json.dump({"config": vars(args), "results": results}, f, indent=2, default=str)


if __name__ == "__main__":
    main()
