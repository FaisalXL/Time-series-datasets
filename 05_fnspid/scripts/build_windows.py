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


_WORD = __import__("re").compile(r"[a-z0-9]+")


# UPSTREAM CORRUPTION IN FNSPID: the digit "1" is sometimes replaced by the literal string
# "Array" -- "20Array5" for 2015, "$Array.64 billion" for $1.64bn, "mid-20Array4" for 2014.
# It looks like an array interpolated into a string somewhere in the original scrape. Only
# DIGIT-ADJACENT occurrences are corruption: "Array Biopharma" is a real company (ARRY), so a
# bare word match would throw away sound articles. Measured at 0.097% of articles.
#
# These articles are DROPPED rather than repaired. Substituting the digit back would be
# editing third-party source text, which is exactly what `text_quality: real` promises we do
# not do -- and a record whose numbers we quietly rewrote could never be audited against its
# source.
_CORRUPT = __import__("re").compile(r"(?:\d\s*Array|Array\s*[.,]?\s*\d)")


def _shingles(text: str, k: int = 8, step: int = 10, cap: int = 6000) -> set:
    """Hashed 8-word shingles, sampled every `step` words over the first `cap` chars.

    Sampled rather than exhaustive, and hashed rather than kept as strings, because this runs
    on every article of every window -- ~650k calls for the 90-day set. Step 10 still catches
    republished wire copy, which shares long verbatim blocks, and cut the pass from >10 min
    to a couple of minutes.
    """
    w = _WORD.findall(text.lower()[:cap])
    return {hash(" ".join(w[i:i + k])) for i in range(0, max(1, len(w) - k), step)}


def dedup_articles(items: List[tuple], thresh: float = 0.5) -> Tuple[List[tuple], int]:
    """Drop near-duplicate articles inside a window, keeping the first of each group.

    The wires republish one story many times: a DSS window held five Benzinga market wraps
    ("Midway through trading Tuesday...", "Following the market opening...", "Toward the end
    of trading...") that were the same report at three times of day. Verbatim extraction then
    faithfully reproduced the same sentence three times. Measured on a 4k-window sample,
    10.7% of 90-day windows contain at least one near-duplicate.

    Jaccard over 8-word shingles, sampled every 3 words -- cheap enough for <=12 articles per
    window and robust to the boilerplate header/footer differences between wire copies.
    """
    keep: List[tuple] = []
    sigs: List[set] = []
    dropped = 0
    for it in items:
        s = _shingles(it[1])
        if any(len(s & p) / (len(s | p) or 1) > thresh for p in sigs):
            dropped += 1
            continue
        keep.append(it)
        sigs.append(s)
    return keep, dropped


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
    ap.add_argument("--min-news-span", type=float, default=0.30,
                    help="news must span at least this fraction of the window, else drop")
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
    len_per: List[int] = []
    span_per: List[float] = []
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
            n_corrupt = sum(1 for it in items if _CORRUPT.search(it[1]))
            if n_corrupt:
                items = [it for it in items if not _CORRUPT.search(it[1])]
                stats["corrupt_articles_dropped"] += n_corrupt
                if not items:
                    stats["window_empty_after_corruption"] += 1
                    continue
            items, n_dup = dedup_articles(items)
            stats["duplicate_articles_dropped"] += n_dup

            # TRIM THE WINDOW TO THE NEWS. Running a fixed W trading days forward from the
            # anchor regardless of whether coverage continues produces records whose series
            # is mostly unexplained: a DSS window carried one news day and then 89 trading
            # days of a -72% collapse that no article mentions. Ending at the last news day
            # (floored at min_window so the series stays usable) makes the window's length a
            # property of the source's reporting rather than of our constant.
            last_news = max(it[0] for it in items)
            li = bisect.bisect_right(cal, last_news) - 1
            ei = min(ei, max(li + 1, si + minW))
            lo, hi = cal[si], cal[ei - 1]
            span = (bisect.bisect_left(cal, last_news) - si) / max(1, (ei - si) - 1)
            if span < args.min_news_span:
                stats["news_span_too_narrow"] += 1
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
                "news_span_frac": round(span, 3),
            }, ensure_ascii=False) + "\n")
            n_win += 1
            arts_per.append(len(items))
            news_per.append(len(block))
            len_per.append(ei - si)
            span_per.append(span)

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
        "window_trading_days_max": W, "min_window": minW, "max_articles": args.max_articles,
        "min_news_span": args.min_news_span,
        "candidate_pairs_in": seen_rows,
        "windows_out": n_win,
        "articles_per_window": {"median": statistics.median(arts_per),
                                "p90": sorted(arts_per)[int(len(arts_per) * .9)],
                                "mean": round(sum(arts_per) / len(arts_per), 2)} if arts_per else {},
        "news_days_per_window": {"median": statistics.median(news_per),
                                 "p90": sorted(news_per)[int(len(news_per) * .9)]} if news_per else {},
        "window_length_actual": {"median": statistics.median(len_per),
                                 "p10": sorted(len_per)[len(len_per) // 10],
                                 "max": max(len_per)} if len_per else {},
        "news_span_frac": {"median": round(statistics.median(span_per), 3),
                           "p10": round(sorted(span_per)[len(span_per) // 10], 3)} if span_per else {},
        "skips": dict(stats),
        "elapsed_s": round(time.time() - t0, 1),
        "out": str(outp), "bytes": outp.stat().st_size,
    }
    if args.report:
        json.dump(report, open(resolve(args.report), "w"), indent=1)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
