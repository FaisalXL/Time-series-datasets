#!/usr/bin/env python3
"""Stage 3 of the full-scale FNSPID build: assemble verdicts + prices into the corpus.

Joins stage 1's candidates with stage 2's verdicts, attaches each pair's trailing price
window, and emits schema-v1 records through `fnspid_emit.make_record`.

Cheap and re-runnable. Every keep-policy knob lives here -- which roles enter the corpus, the
character floor, the staleness guard -- so changing your mind costs one CPU pass over local
files and zero GPU. That is the whole reason the stages are split.

Text is assembled VERBATIM from the model's sentence indices. Nothing is rewritten, so
`text_quality` stays `real` and no `llm_summarized` sign-off is needed. Truncation only
applies if the selected sentences still exceed the token cap, and then at a sentence
boundary.

`n_articles_used` finally means what it says: the number of source articles that actually
contributed a selected sentence, counted after extraction rather than before truncation.

Usage:
    python3 scripts/build_assemble.py --config config.fullscale.yaml
    python3 scripts/build_assemble.py --roles primary,secondary --floor 300
"""
from __future__ import annotations

import argparse
import bisect
import collections
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_extract import numbered_sentences  # noqa: E402
from fnspid_emit import (CHANNEL_SPEC, figures_in_series, make_record,  # noqa: E402
                         truncate_at_sentence)

ROOT = Path(__file__).resolve().parents[1]
ERAS = [("2009-2012", "0000", "2013-01-01"), ("2013-2016", "2013-01-01", "2017-01-01"),
        ("2017-2020", "2017-01-01", "2021-01-01"), ("2021-2023", "2021-01-01", "9999")]


def era_of(d: str) -> str:
    for name, lo, hi in ERAS:
        if lo <= d < hi:
            return name
    return "other"


def resolve(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (ROOT / q)


def days_between(a: str, b: str) -> int:
    from datetime import date
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


class PriceTable:
    __slots__ = ("dates", "values")

    def __init__(self, dates: List[str], values: Dict[str, List[Optional[float]]]):
        self.dates, self.values = dates, values

    def window_before(self, news_date: str, n: int):
        """The n trading rows immediately BEFORE news_date.

        bisect_left, not bisect_right: if the market traded on the news date itself that row
        is excluded, so the window can never contain a price from the day the article was
        published or later. Verified 0/2400 lookahead on the 3k sample.
        """
        idx = bisect.bisect_left(self.dates, news_date)
        start = max(0, idx - n)
        return self.dates[start:idx], {c: v[start:idx] for c, v in self.values.items()}


def load_price_table(prices_dir: Path, ticker: str, csv_cols: List[str]) -> Optional[PriceTable]:
    import csv as _csv
    f = prices_dir / f"{ticker}.csv"
    if not f.exists():
        cands = list(prices_dir.glob(f"{ticker.lower()}.csv")) + \
            list(prices_dir.glob(f"{ticker.upper()}.csv"))
        if not cands:
            return None
        f = cands[0]
    dates: List[str] = []
    vals: Dict[str, List[Optional[float]]] = {c: [] for c in csv_cols}
    with open(f, newline="", encoding="utf-8", errors="replace") as fh:
        rd = _csv.DictReader(fh)
        lower = {(k or "").strip().lower(): k for k in (rd.fieldnames or [])}
        if "date" not in lower:
            return None
        for row in rd:
            d = (row.get(lower["date"]) or "").strip()[:10]
            if len(d) < 10:
                continue
            dates.append(d)
            for c in csv_cols:
                key = lower.get(c)
                raw = (row.get(key) or "").strip() if key else ""
                try:
                    vals[c].append(float(raw))
                except ValueError:
                    vals[c].append(None)
    if not dates:
        return None
    order = sorted(range(len(dates)), key=lambda i: dates[i])
    return PriceTable([dates[i] for i in order], {c: [vals[c][i] for i in order] for c in csv_cols})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.fullscale.yaml")
    ap.add_argument("--candidates", default=".cache/candidates.jsonl")
    ap.add_argument("--verdicts", default=".cache/verdicts.jsonl")
    ap.add_argument("--spread", default=".cache/article_spread.json")
    ap.add_argument("--out", default="output/fnspid_cpt_v1.jsonl")
    ap.add_argument("--report", default="output/assemble_report.json")
    ap.add_argument("--roles", default="primary,secondary",
                    help="roles that enter the corpus (comma-separated)")
    ap.add_argument("--floor", type=int, default=300,
                    help="min assembled chars; a second guard, not the gate")
    ap.add_argument("--max-staleness-days", type=int, default=30,
                    help="drop if the last price predates the news date by more than this")
    ap.add_argument("--char-cap", type=int, default=12000, help="must match stage 2")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(resolve(args.config), encoding="utf-8"))
    d, t = cfg["data"], cfg["text"]
    prices_dir = resolve(d["prices_dir"])
    channels = list(d["channels"])
    csv_cols = [CHANNEL_SPEC[c][0] for c in channels]
    hist, min_hist = int(d["history_days"]), int(d["min_history_days"])
    cap = int(t["max_chars"])
    keep_roles = {r.strip() for r in args.roles.split(",") if r.strip()}
    print(f"roles kept {sorted(keep_roles)}  floor {args.floor}  text cap {cap}  "
          f"window {hist}/{min_hist}  staleness<={args.max_staleness_days}d", flush=True)

    # verdicts are small (~200 B/pair); hold them in memory and stream the big candidates file
    verdicts: Dict[str, dict] = {}
    vpath = resolve(args.verdicts)
    for line in open(vpath, encoding="utf-8"):
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            continue
        verdicts[f"{v['t']}|{v['d']}"] = v
    print(f"verdicts loaded: {len(verdicts):,}", flush=True)

    roundup = set()
    sp = resolve(args.spread)
    if sp.exists():
        roundup = set(json.load(open(sp)))
    print(f"round-up articles (>5 tickers): {len(roundup):,}", flush=True)

    outp = resolve(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    fh = open(outp, "w", encoding="utf-8")

    drops: collections.Counter = collections.Counter()
    roles_seen: collections.Counter = collections.Counter()
    kept = 0
    text_chars: List[int] = []
    fig: List[int] = []
    ctrl_pool: List[List[float]] = []
    eras: collections.Counter = collections.Counter()
    n_arts: collections.Counter = collections.Counter()
    tickers: set = set()
    price_cache: Tuple[Optional[str], Optional[PriceTable]] = (None, None)
    t0 = time.time()
    seen = 0

    for line in open(resolve(args.candidates), encoding="utf-8"):
        c = json.loads(line)
        seen += 1
        key = f"{c['t']}|{c['d']}"
        v = verdicts.get(key)
        if v is None:
            drops["no_verdict"] += 1
            continue
        if v.get("status") != "ok":
            drops["verdict_" + str(v.get("status"))] += 1
            continue
        role = v.get("role")
        roles_seen[role] += 1
        if role not in keep_roles:
            drops["role_" + str(role)] += 1
            continue
        idxs = v.get("sentences") or []
        if not idxs:
            drops["no_sentences_selected"] += 1
            continue

        # Re-derive the exact numbering stage 2 showed the model. Same function, same cap,
        # so index i means the same sentence it did at judging time.
        sents, art_of, _ = numbered_sentences(c["bodies"], args.char_cap)
        picks = [i for i in idxs if 1 <= i <= len(sents)]
        if not picks:
            drops["all_indices_invalid"] += 1
            continue
        body = " ".join(sents[i - 1] for i in picks)
        if len(body) < args.floor:
            drops["below_floor"] += 1
            continue
        used_articles = sorted({art_of[i - 1] for i in picks})

        # prices: candidates are ticker-major, so a one-entry cache covers the whole block
        if price_cache[0] != c["t"]:
            price_cache = (c["t"], load_price_table(prices_dir, c["t"], csv_cols))
        pt = price_cache[1]
        if pt is None:
            drops["no_price_csv"] += 1
            continue
        wd, wv = pt.window_before(c["d"], hist)
        if len(wd) < min_hist:
            drops["insufficient_history"] += 1
            continue
        stale = days_between(wd[-1], c["d"])
        if stale > args.max_staleness_days:
            # Measured on the 3k sample: 9 records (0.4%) paired news with prices up to 1,070
            # days old, because those tickers' price CSVs simply stop. `freq: 1d` would be a
            # false claim about the gap.
            drops["stale_prices"] += 1
            continue

        try:
            rec = make_record(
                ticker=c["t"], news_date=c["d"], article_block=body, channels=channels,
                win_dates=wd, win_vals=wv,
                urls=[c["urls"][i] for i in used_articles if i < len(c["urls"])],
                titles=[c["titles"][i] for i in used_articles if i < len(c["titles"])],
                n_articles_seen=c["n_seen"], text_cap=cap,
                extra_meta={
                    "era": era_of(c["d"]),
                    "symbol_named_in_text": c["named"],
                    "extraction": {
                        "model": v.get("model") or "Qwen/Qwen3.6-35B-A3B",
                        "role": role,
                        "relation": v.get("relation"),
                        "confidence": v.get("confidence"),
                        "sentences": picks,
                        "n_sentences_available": len(sents),
                        "input_capped": bool(v.get("capped")),
                        "invalid_idx_dropped": v.get("invalid_idx", 0),
                    },
                    "n_articles_available": len(c["bodies"]),
                    "article_ticker_spread_gt5": any(u in roundup for u in c["urls"] if u),
                    "price_staleness_days": stale,
                },
            )
        except ValueError as exc:
            drops[f"emit_error:{str(exc)[:50]}"] += 1
            continue

        # n_articles_used is set by make_record from len(urls) -- which is now the count of
        # articles that actually contributed a sentence, not the count that was fetched.
        rec["meta"]["n_articles_used"] = len(used_articles)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        kept += 1
        text_chars.append(rec["meta"]["text_chars"])
        fig.append(rec["meta"]["figures_matching_own_series"])
        eras[rec["meta"]["era"]] += 1
        n_arts[len(used_articles)] += 1
        tickers.add(c["t"])
        if len(ctrl_pool) < 4000:
            close = next((s["values"] for s in rec["timeseries"]
                          if s["unit"] == "close_price_usd"), [])
            ctrl_pool.append([body, close])
        if kept % 25000 == 0:
            fh.flush()
            print(f"  kept {kept:,} / seen {seen:,}  {time.time()-t0:.0f}s", flush=True)
        if args.limit and kept >= args.limit:
            break
    fh.close()
    el = time.time() - t0

    # permutation control: the same prose against ANOTHER record's close series. A bare
    # figure-match count has a high coincidence floor, so it means nothing without this.
    ctrl = []
    for i, (bd, _cl) in enumerate(ctrl_pool):
        other = ctrl_pool[(i + len(ctrl_pool) // 2 + 1) % len(ctrl_pool)][1]
        ctrl.append(figures_in_series(bd, other))

    report = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": {"roles_kept": sorted(keep_roles), "floor_chars": args.floor,
                   "text_cap": cap, "history_days": hist, "min_history_days": min_hist,
                   "max_staleness_days": args.max_staleness_days},
        "candidates_seen": seen, "records_kept": kept,
        "keep_rate_pct": round(100 * kept / max(1, seen), 2),
        "distinct_tickers": len(tickers),
        "roles_observed": dict(roles_seen.most_common()),
        "drops": dict(drops.most_common()),
        "era": dict(eras.most_common()),
        "n_articles_used": dict(sorted(n_arts.items())),
        "text_chars": {"median": statistics.median(text_chars),
                       "p10": sorted(text_chars)[len(text_chars) // 10],
                       "p90": sorted(text_chars)[-max(1, len(text_chars) // 10)]} if text_chars else {},
        "figures_matching_own_series_mean": round(sum(fig) / len(fig), 3) if fig else None,
        "PERMUTATION_CONTROL_mean": round(sum(ctrl) / len(ctrl), 3) if ctrl else None,
        "elapsed_s": round(el, 1),
        "output_path": str(outp),
    }
    rp = resolve(args.report)
    json.dump(report, open(rp, "w"), indent=1)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
