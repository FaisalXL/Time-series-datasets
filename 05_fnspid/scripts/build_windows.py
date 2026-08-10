#!/usr/bin/env python3
"""Form news-anchored windows: the series SPANS the news instead of preceding it.

The v1 record paired an article with the 90 trading days ending the day BEFORE it, so the
prices stopped before the events the text described and the text could not explain the
series. Measured consequence: figures in the prose that also occur in the record's own
series ran 0.89 against a 0.50 permutation control -- barely above coincidence.

Here a record is one `(ticker, window)`: W consecutive trading days of OHLCV, together with
the news published INSIDE that window.

WINDOW PLACEMENT IS SOURCE-DRIVEN, NOT A STRIDE. SCHEMA.md section 7 disqualifies "fixed
stride-1 sliding windows imposed by us rather than by the source's reporting structure", so
windows are anchored on news: start at the first unconsumed news day, run W trading days,
swallow every news day inside, then jump to the next unconsumed news day. Windows never
overlap and none is empty, because each one begins at an actual publication.

TRADE ACCEPTED: text and series now overlap in time, so the v1 no-lookahead guarantee is
deliberately given up. For CPT world-knowledge that is the point -- the text is supposed to
describe the series it ships with -- but `meta.lookahead_safe: false` is stamped on every
record so nobody later builds a forecasting eval on this and leaks.

Cross-ticker text reuse is ALLOWED here (one wire article can back a record under several
tickers). That is a deliberate volume decision, not an oversight; `meta.article_spread`
records it per record so the redundancy stays measurable.

Usage:
    python3 scripts/build_windows.py --window 90 --out .cache/windows_90.jsonl
    python3 scripts/build_windows.py --window 30 --out .cache/windows_30.jsonl
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fnspid_emit import CHANNEL_SPEC  # noqa: E402


def resolve(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (ROOT / q)


def load_calendar(prices_dir: Path, ticker: str, csv_cols: List[str]):
    f = prices_dir / f"{ticker}.csv"
    if not f.exists():
        cands = list(prices_dir.glob(f"{ticker.lower()}.csv")) + \
            list(prices_dir.glob(f"{ticker.upper()}.csv"))
        if not cands:
            return None, None
        f = cands[0]
    dates: List[str] = []
    vals: Dict[str, List[Optional[float]]] = {c: [] for c in csv_cols}
    with open(f, newline="", encoding="utf-8", errors="replace") as fh:
        rd = csv.DictReader(fh)
        lower = {(k or "").strip().lower(): k for k in (rd.fieldnames or [])}
        if "date" not in lower:
            return None, None
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
        return None, None
    order = sorted(range(len(dates)), key=lambda i: dates[i])
    return [dates[i] for i in order], {c: [vals[c][i] for i in order] for c in csv_cols}


def spread_articles(items: List[tuple], cap: int) -> List[tuple]:
    """Keep at most `cap` articles, spread evenly across the window.

    Taking the first `cap` would bias every record toward its opening days, which is exactly
    the window start -- the text would then describe only the beginning of the series it is
    paired with.
    """
    if len(items) <= cap:
        return items
    step = len(items) / cap
    return [items[int(i * step)] for i in range(cap)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.fullscale.yaml")
    ap.add_argument("--candidates", default=".cache/candidates.jsonl")
    ap.add_argument("--spread", default=".cache/article_spread.json")
    ap.add_argument("--window", type=int, default=90, help="trading days per window")
    ap.add_argument("--min-window", type=int, default=0, help="default: config min_history_days")
    ap.add_argument("--max-articles", type=int, default=12)
    ap.add_argument("--out", default=".cache/windows_90.jsonl")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(resolve(args.config), encoding="utf-8"))
    d = cfg["data"]
    prices_dir = resolve(d["prices_dir"])
    channels = list(d["channels"])
    csv_cols = [CHANNEL_SPEC[c][0] for c in channels]
    W = args.window
    # The config floor (32) is a floor on a 90-day target. Clamp it to W, or asking for a
    # 30-day window rejects every window as "too short" and silently yields nothing.
    minW = args.min_window or min(W, int(d["min_history_days"]))

    roundup = set()
    sp = resolve(args.spread)
    if sp.exists():
        roundup = set(json.load(open(sp)))

    outp = resolve(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    fh = open(outp, "w", encoding="utf-8")

    stats: collections.Counter = collections.Counter()
    n_win = 0
    arts_per: List[int] = []
    news_per: List[int] = []
    t0 = time.time()

    def flush(ticker: str, days: Dict[str, list]) -> None:
        """Cut one ticker's news days into news-anchored, non-overlapping windows."""
        nonlocal n_win
        if not days:
            return
        cal, vals = load_calendar(prices_dir, ticker, csv_cols)
        if cal is None:
            stats["no_price_csv"] += 1
            return
        news = sorted(days)
        i = 0
        while i < len(news):
            si = bisect.bisect_left(cal, news[i])
            if si >= len(cal):
                stats["news_after_last_price"] += len(news) - i
                break
            ei = min(si + W, len(cal))
            if ei - si < minW:
                stats["window_too_short"] += 1
                break
            lo, hi = cal[si], cal[ei - 1]
            j = i
            while j < len(news) and news[j] <= hi:
                j += 1
            block = news[i:j]
            i = j

            items: List[tuple] = []      # (day, body, url, title)
            for day in block:
                bodies, urls, titles = days[day]
                for b, u, ti in zip(bodies, urls, titles):
                    items.append((day, b, u, ti))
            if not items:
                stats["empty_window"] += 1
                continue
            items = spread_articles(items, args.max_articles)

            fh.write(json.dumps({
                "t": ticker,
                "w_start": lo, "w_end": hi,
                "dates": cal[si:ei],
                "vals": {c: vals[c][si:ei] for c in csv_cols},
                "news_days": sorted({it[0] for it in items}),
                "n_news_days_in_window": len(block),
                "days": [it[0] for it in items],      # publication date per article, for the prompt
                "bodies": [it[1] for it in items],
                "urls": [it[2] for it in items],
                "titles": [it[3] for it in items],
                "article_spread_gt5": sum(1 for it in items if it[2] in roundup),
            }, ensure_ascii=False) + "\n")
            n_win += 1
            arts_per.append(len(items))
            news_per.append(len(block))

    cur: Optional[str] = None
    days: Dict[str, list] = {}
    seen_rows = 0
    for line in open(resolve(args.candidates), encoding="utf-8"):
        c = json.loads(line)
        seen_rows += 1
        if c["t"] != cur:
            flush(cur, days) if cur else None
            cur, days = c["t"], {}
        days[c["d"]] = [c["bodies"], c["urls"], c["titles"]]
        if seen_rows % 200_000 == 0:
            print(f"  {seen_rows:>9,} pairs  {n_win:>8,} windows  {time.time()-t0:>5.0f}s",
                  flush=True)
    if cur:
        flush(cur, days)
    fh.close()

    report = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_trading_days": W, "min_window": minW, "max_articles": args.max_articles,
        "candidate_pairs_in": seen_rows,
        "windows_out": n_win,
        "articles_per_window": {"median": statistics.median(arts_per),
                                "p90": sorted(arts_per)[int(len(arts_per) * .9)],
                                "mean": round(sum(arts_per) / len(arts_per), 2)} if arts_per else {},
        "news_days_per_window": {"median": statistics.median(news_per),
                                 "p90": sorted(news_per)[int(len(news_per) * .9)]} if news_per else {},
        "skips": dict(stats),
        "elapsed_s": round(time.time() - t0, 1),
        "out": str(outp), "bytes": outp.stat().st_size,
    }
    if args.report:
        json.dump(report, open(resolve(args.report), "w"), indent=1)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
