#!/usr/bin/env python3
"""Stage 1 of the full-scale FNSPID build: scan the news wire into candidate pairs.

One streaming pass over the 23 GB news CSV produces one JSONL line per `(ticker, day)`
pair, carrying the article bodies and nothing else. No prices, no LLM, no truncation.

Why a separate stage. The full build has three costs that fail differently: a 23 GB disk
scan, a ~20 h GPU pass, and a cheap join. Fusing them means a crash in hour 19 of the GPU
pass re-reads 23 GB, and a change of keep-policy re-runs the GPU. Split, each stage is
restartable and stage 3 can be re-run for free.

Memory is O(one ticker), not O(corpus). The wire is sorted ticker-major (verified: runs of
`A`x379, `AA`x879, date DESC within each), so this accumulates one ticker's rows and flushes
them when the symbol changes. A ticker that reappears out of order is counted and reported
rather than silently split.

Article boundaries are PRESERVED. `bodies` is a list, never a pre-joined block, because
stage 3 needs to know which article each selected sentence came from -- the old builder
joined first and then reported `n_articles_used: 5` for text that the 2,240-char truncation
had cut back to article 1.

Usage:
    python3 scripts/build_scan.py --config config.fullscale.yaml
    python3 scripts/build_scan.py --limit-rows 2000000 --out .cache/candidates_smoke.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pyarrow as pa
import pyarrow.csv as pv
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fnspid_emit import symbol_named_in_text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NEWS_COLS = ["Date", "Article_title", "Stock_symbol", "Article", "Url"]

# Bound on stored article text. The prompt is lede-first anyway (news is inverted-pyramid)
# and a 40k-char wire dump would dominate the candidates file for no gain.
BODY_CAP = 20_000

# Spread tracking: we only care whether an article is tagged to more than this many
# tickers, so each URL's ticker set stops growing once it passes -- bounds the map at
# ~6 entries per article instead of the full 2.4M taggings.
SPREAD_WATERMARK = 6


def resolve(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (ROOT / q)


def rss_gb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 ** 2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.fullscale.yaml")
    ap.add_argument("--out", default=".cache/candidates.jsonl")
    ap.add_argument("--spread-out", default=".cache/article_spread.json")
    ap.add_argument("--report", default="output/scan_report.json")
    ap.add_argument("--limit-rows", type=int, default=0, help="stop after N rows (smoke test)")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(resolve(args.config), encoding="utf-8"))
    d, f = cfg["data"], cfg["filters"]
    news = resolve(d["news_csv"])
    prices_dir = resolve(d["prices_dir"])
    min_chars = int(f["min_article_chars"])
    max_arts = int(f["max_articles_per_record"])

    have_prices = {p.stem.upper() for p in prices_dir.glob("*.csv")}
    if not have_prices:
        sys.exit(f"no price CSVs under {prices_dir}")
    print(f"news   {news}\nprices {prices_dir}  ({len(have_prices):,} tickers)\n"
          f"min_article_chars {min_chars}  max_articles_per_record {max_arts}", flush=True)

    outp = resolve(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    fh_out = open(outp, "w", encoding="utf-8")

    # url -> set of tickers it is tagged to (capped at the watermark)
    spread: Dict[str, Any] = {}

    # current ticker block: day -> [bodies, urls, titles, n_seen, named]
    cur_sym: Optional[str] = None
    block: Dict[str, list] = {}
    flushed: set = set()

    stats = collections.Counter()
    n_pairs = 0
    t0 = time.time()
    rows = 0

    def flush() -> None:
        nonlocal n_pairs
        if cur_sym is None:
            return
        if cur_sym in flushed:
            stats["ticker_reappeared"] += 1
        flushed.add(cur_sym)
        for day in sorted(block):
            bodies, urls, titles, n_seen, named = block[day]
            if not bodies:
                continue
            fh_out.write(json.dumps({
                "t": cur_sym, "d": day, "bodies": bodies, "urls": urls, "titles": titles,
                "n_seen": n_seen, "named": named,
            }, ensure_ascii=False) + "\n")
            n_pairs += 1

    ro = pv.ReadOptions(block_size=64 << 20)
    po = pv.ParseOptions(newlines_in_values=True)
    co = pv.ConvertOptions(include_columns=NEWS_COLS,
                           column_types={c: pa.string() for c in NEWS_COLS})

    for b in pv.open_csv(news, read_options=ro, parse_options=po, convert_options=co):
        D = b.column("Date").to_pylist()
        S = b.column("Stock_symbol").to_pylist()
        A = b.column("Article").to_pylist()
        T = b.column("Article_title").to_pylist()
        U = b.column("Url").to_pylist()
        rows += len(D)
        for di, si, ai, ti, ui in zip(D, S, A, T, U):
            if not si or not di:
                stats["drop_no_symbol_or_date"] += 1
                continue
            sym = si.strip().upper()
            if not ai or len(ai) < min_chars:
                stats["drop_short_or_missing_body"] += 1
                continue
            if sym not in have_prices:
                stats["drop_no_price_csv"] += 1
                continue

            # article -> ticker spread, tracked globally (an article recurs under many
            # tickers scattered across the file, so this cannot be done per block)
            if ui:
                s = spread.get(ui)
                if s is None:
                    spread[ui] = {sym}
                elif isinstance(s, set) and sym not in s:
                    s.add(sym)
                    if len(s) >= SPREAD_WATERMARK:
                        spread[ui] = SPREAD_WATERMARK   # stop growing; we only need ">5"

            if sym != cur_sym:
                flush()
                cur_sym, block = sym, {}

            day = di[:10]
            slot = block.get(day)
            if slot is None:
                slot = block[day] = [[], [], [], 0, False]
            slot[3] += 1                                    # n_seen counts every article
            if not slot[4]:
                slot[4] = symbol_named_in_text(sym, ti or "", ai[:1200])
            if len(slot[0]) >= max_arts:
                continue
            body = " ".join(str(ai).split())
            if len(body) > BODY_CAP:
                body = body[:BODY_CAP]
            if body in slot[0]:
                stats["drop_duplicate_body"] += 1
                continue
            slot[0].append(body)
            slot[1].append(ui or "")
            slot[2].append(ti or "")

        if rows % 2_000_000 < len(D):
            print(f"  {rows:>12,} rows  {time.time()-t0:>5.0f}s  pairs {n_pairs:>9,}  "
                  f"urls {len(spread):>9,}  rss {rss_gb():.1f}G", flush=True)
        if args.limit_rows and rows >= args.limit_rows:
            print(f"  stopping at --limit-rows {args.limit_rows:,}")
            break

    flush()
    fh_out.close()
    el = time.time() - t0

    roundups = sorted(u for u, s in spread.items()
                      if s == SPREAD_WATERMARK or (isinstance(s, set) and len(s) > 5))
    sp = resolve(args.spread_out)
    sp.parent.mkdir(parents=True, exist_ok=True)
    json.dump(roundups, open(sp, "w"))

    report = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": str(resolve(args.config)),
        "news_csv": str(news),
        "rows_scanned": rows,
        "candidate_pairs": n_pairs,
        "distinct_tickers": len(flushed),
        "distinct_articles": len(spread),
        "roundup_articles_gt5_tickers": len(roundups),
        "drops": dict(stats),
        "elapsed_s": round(el, 1),
        "peak_rss_gb": round(rss_gb(), 2),
        "candidates_path": str(outp),
        "candidates_bytes": outp.stat().st_size,
    }
    rp = resolve(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    json.dump(report, open(rp, "w"), indent=1)
    print(json.dumps(report, indent=1))
    if stats.get("ticker_reappeared"):
        print(f"\n⚠ {stats['ticker_reappeared']} ticker blocks reappeared out of order — the "
              f"wire is not strictly ticker-major and those pairs are split across lines. "
              f"Merge on (t, d) in stage 3.")


if __name__ == "__main__":
    main()
