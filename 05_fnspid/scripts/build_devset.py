#!/usr/bin/env python3
"""Build a small, deliberately stratified FNSPID devset for owner inspection.

This is NOT a random sample. A random 300 of 1.17M records would be ~60% recent, ~60%
single-ticker and almost all truncated, so it would hide exactly the decisions that need
review. The devset instead spreads across:

  * era        -- 4 bins over 2009..2023, because the news CSV is ticker-major/date-DESC and
                  the old caps therefore produced a recency slice, not a sample
  * ticker tier-- 4 quartiles by how much news a ticker has, so mega-caps and thin names are
                  both represented
  * text length-- terciles within each cell, so untruncated and heavily-truncated records both
                  appear

and writes two extra files so the filters can be judged rather than trusted:

  * `<out>_rejects.jsonl`  -- records the `require_symbol_in_text` filter REJECTED, built
                              identically and labelled. Without these you cannot tell whether
                              the filter is dropping noise or dropping data.
  * `<out>_roundups.jsonl` -- records whose article is tagged to >5 tickers. Measured earlier:
                              2.2% of articles, 26.3% of all volume. This is the class the B1
                              LLM judge is supposed to remove, so it needs eyeballing first.

Everything is emitted through `fnspid_emit.make_record`, i.e. the same code path the full run
will use, and gated with `schema/validate.py --strict` at the end.

Usage:
    python3 scripts/build_devset.py --config config.fullscale.yaml --target 320
"""
from __future__ import annotations

import argparse
import bisect
import collections
import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pyarrow as pa
import pyarrow.csv as pv
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fnspid_emit import (CHANNEL_SPEC, make_record, truncate_at_sentence,  # noqa: E402
                         figures_in_series, symbol_named_in_text)

ROOT = Path(__file__).resolve().parents[1]
NEWS_COLS = ["Date", "Article_title", "Stock_symbol", "Article", "Url"]
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


# The symbol test now lives in fnspid_emit so stage 1 of the full build and this devset
# cannot drift apart; `norm_symbol_hit` is kept as the local name used below.
norm_symbol_hit = symbol_named_in_text


class PriceTable:
    def __init__(self, dates: List[str], values: Dict[str, List[Optional[float]]]):
        self.dates, self.values = dates, values

    def window_before(self, news_date: str, n: int):
        idx = bisect.bisect_left(self.dates, news_date)
        start = max(0, idx - n)
        return self.dates[start:idx], {c: v[start:idx] for c, v in self.values.items()}


def load_price_table(prices_dir: Path, ticker: str, csv_cols: List[str]) -> Optional[PriceTable]:
    f = prices_dir / f"{ticker}.csv"
    if not f.exists():
        cands = list(prices_dir.glob(f"{ticker.lower()}.csv")) + list(prices_dir.glob(f"{ticker.upper()}.csv"))
        if not cands:
            return None
        f = cands[0]
    import csv as _csv
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
    ap.add_argument("--target", type=int, default=320)
    ap.add_argument("--rejects", type=int, default=60)
    ap.add_argument("--roundups", type=int, default=30)
    ap.add_argument("--out", default="output/devset.jsonl")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    cfg = yaml.safe_load(open(resolve(args.config), encoding="utf-8"))
    d, f, t = cfg["data"], cfg["filters"], cfg["text"]
    news = resolve(d["news_csv"])
    prices_dir = resolve(d["prices_dir"])
    channels = list(d["channels"])
    csv_cols = [CHANNEL_SPEC[c][0] for c in channels]
    hist, min_hist = int(d["history_days"]), int(d["min_history_days"])
    min_chars = int(f["min_article_chars"])
    max_arts = int(f["max_articles_per_record"])
    cap = int(t["max_chars"])
    have_prices = {p.stem.upper() for p in prices_dir.glob("*.csv")}
    print(f"tickers with prices: {len(have_prices):,}   text cap {cap}   window {hist}/{min_hist}")

    ro = pv.ReadOptions(block_size=64 << 20)
    po = pv.ParseOptions(newlines_in_values=True)
    co = pv.ConvertOptions(include_columns=NEWS_COLS,
                           column_types={c: pa.string() for c in NEWS_COLS})

    # ---------------- pass A: candidate index (no article text kept) ----------------
    # key -> [n_arts, total_chars, named?, first_url, first_title]
    cand: Dict[Tuple[str, str], list] = {}
    url_tickers: Dict[str, set] = collections.defaultdict(set)
    t0 = time.time()
    rows = 0
    for b in pv.open_csv(news, read_options=ro, parse_options=po, convert_options=co):
        D = b.column("Date").to_pylist(); S = b.column("Stock_symbol").to_pylist()
        A = b.column("Article").to_pylist(); T = b.column("Article_title").to_pylist()
        U = b.column("Url").to_pylist()
        rows += len(D)
        for di, si, ai, ti, ui in zip(D, S, A, T, U):
            if not ai or len(ai) < min_chars or not si or not di:
                continue
            sym = si.strip().upper()
            if sym not in have_prices:
                continue
            day = di[:10]
            if ui:
                url_tickers[ui].add(sym)
            k = (sym, day)
            e = cand.get(k)
            if e is None:
                cand[k] = [1, len(ai), norm_symbol_hit(sym, ti or "", ai[:1200]), ui or "", ti or ""]
            else:
                if e[0] < max_arts:
                    e[0] += 1
                    e[1] += len(ai)
                if not e[2]:
                    e[2] = norm_symbol_hit(sym, ti or "", ai[:1200])
        if rows % 4_000_000 < len(D):
            print(f"  passA {rows:,} rows {time.time()-t0:.0f}s cands={len(cand):,}", flush=True)
    print(f"passA done: {rows:,} rows, {len(cand):,} candidate pairs, {time.time()-t0:.0f}s")

    per_ticker = collections.Counter()
    for (sym, _), _e in cand.items():
        per_ticker[sym] += 1
    ranked = [s for s, _ in per_ticker.most_common()]
    tier_of = {}
    q = max(1, len(ranked) // 4)
    for i, s in enumerate(ranked):
        tier_of[s] = min(3, i // q)

    # ---------------- select keys ----------------
    kept_pool: Dict[Tuple[str, int], list] = collections.defaultdict(list)
    reject_pool: List[Tuple[str, str]] = []
    roundup_pool: List[Tuple[str, str]] = []
    for k, e in cand.items():
        sym, day = k
        cell = (era_of(day), tier_of.get(sym, 3))
        if e[2]:
            kept_pool[cell].append((k, e[1]))
        else:
            reject_pool.append(k)
        if e[3] and len(url_tickers.get(e[3], ())) > 5:
            roundup_pool.append(k)

    per_cell = max(1, args.target // max(1, len(kept_pool)))
    chosen: List[Tuple[str, str]] = []
    for cell, items in sorted(kept_pool.items()):
        items.sort(key=lambda x: x[1])                     # by total article chars
        n = len(items)
        # spread across length terciles, oversample 3x -- history/price checks will thin it
        picks = []
        for lo, hi in ((0, n // 3), (n // 3, 2 * n // 3), (2 * n // 3, n)):
            seg = items[lo:hi] or items
            picks += rng.sample(seg, min(len(seg), max(1, per_cell)))
        chosen += [k for k, _ in picks]
    rng.shuffle(chosen)
    rejects = rng.sample(reject_pool, min(len(reject_pool), args.rejects * 3))
    roundups = rng.sample(roundup_pool, min(len(roundup_pool), args.roundups * 3))
    want = set(chosen) | set(rejects) | set(roundups)
    print(f"selected {len(chosen)} kept / {len(rejects)} reject / {len(roundups)} roundup "
          f"candidate keys across {len(kept_pool)} cells (oversampled 3x)")

    # ---------------- pass B: fetch article text for wanted keys only ----------------
    texts: Dict[Tuple[str, str], list] = collections.defaultdict(lambda: [[], [], []])  # bodies, urls, titles
    rows = 0
    t1 = time.time()
    for b in pv.open_csv(news, read_options=ro, parse_options=po, convert_options=co):
        D = b.column("Date").to_pylist(); S = b.column("Stock_symbol").to_pylist()
        A = b.column("Article").to_pylist(); T = b.column("Article_title").to_pylist()
        U = b.column("Url").to_pylist()
        rows += len(D)
        for di, si, ai, ti, ui in zip(D, S, A, T, U):
            if not ai or len(ai) < min_chars or not si or not di:
                continue
            k = (si.strip().upper(), di[:10])
            if k not in want:
                continue
            slot = texts[k]
            if len(slot[0]) >= max_arts:
                continue
            body = " ".join(str(ai).split())
            if body in slot[0]:
                continue
            slot[0].append(body); slot[1].append(ui or ""); slot[2].append(ti or "")
        if rows % 8_000_000 < len(D):
            print(f"  passB {rows:,} rows {time.time()-t1:.0f}s", flush=True)
    print(f"passB done: text for {len(texts):,} keys, {time.time()-t1:.0f}s")

    # ---------------- pass C: pair with prices and emit ----------------
    def emit_group(keys: List[Tuple[str, str]], limit: int, tag: str) -> Tuple[List[dict], Dict[str, int]]:
        out: List[dict] = []
        drops: Dict[str, int] = collections.Counter()
        by_ticker: Dict[str, List[str]] = collections.defaultdict(list)
        for sym, day in keys:
            by_ticker[sym].append(day)
        for sym in sorted(by_ticker):
            if len(out) >= limit:
                break
            pt = load_price_table(prices_dir, sym, csv_cols)
            if pt is None:
                drops["no_price_csv"] += len(by_ticker[sym]); continue
            for day in sorted(by_ticker[sym]):
                if len(out) >= limit:
                    break
                slot = texts.get((sym, day))
                if not slot or not slot[0]:
                    drops["no_text"] += 1; continue
                wd, wv = pt.window_before(day, hist)
                if len(wd) < min_hist:
                    drops["insufficient_history"] += 1; continue
                block = " ".join(slot[0]).strip()
                if len(block) < min_chars:
                    drops["short_text"] += 1; continue
                try:
                    rec = make_record(
                        ticker=sym, news_date=day, article_block=block, channels=channels,
                        win_dates=wd, win_vals=wv, urls=slot[1], titles=slot[2],
                        n_articles_seen=cand[(sym, day)][0], text_cap=cap,
                        extra_meta={"devset_class": tag,
                                    "era": era_of(day),
                                    "ticker_tier": tier_of.get(sym, 3),
                                    "ticker_news_days_total": per_ticker[sym],
                                    "article_ticker_spread": len(url_tickers.get(slot[1][0], ())) if slot[1] else 1,
                                    "symbol_named_in_text": cand[(sym, day)][2]},
                    )
                except ValueError as exc:
                    drops[f"emit_error:{str(exc)[:60]}"] += 1; continue
                out.append(rec)
        return out, dict(drops)

    kept, kd = emit_group(chosen, args.target, "kept")
    rej, rd = emit_group(rejects, args.rejects, "symbol_filter_reject")
    rnd, nd = emit_group(roundups, args.roundups, "wire_roundup")

    outp = resolve(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    for recs, path in ((kept, outp),
                       (rej, outp.with_name(outp.stem + "_rejects.jsonl")),
                       (rnd, outp.with_name(outp.stem + "_roundups.jsonl"))):
        with open(path, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"wrote {len(recs):>4} -> {path}")

    # ---------------- report, incl. the permutation control ----------------
    def stats(recs: List[dict]) -> Dict[str, Any]:
        if not recs:
            return {}
        tc = sorted(r["meta"]["text_chars"] for r in recs)
        hd = sorted(r["meta"]["history_days"] for r in recs)
        fm = [r["meta"]["figures_matching_own_series"] for r in recs]
        # permutation control: same prose against ANOTHER record's close series
        ctrl = []
        for i, r in enumerate(recs):
            other = recs[(i + len(recs) // 2 + 1) % len(recs)]
            cl = next((s["values"] for s in other["timeseries"] if s["unit"] == "close_price_usd"), [])
            body = r["text"].split("\n\n<ts></ts>")[0]
            ctrl.append(figures_in_series(body, cl))
        return {
            "n": len(recs),
            "text_chars": {"median": statistics.median(tc), "p10": tc[len(tc) // 10], "p90": tc[-max(1, len(tc) // 10)]},
            "truncated_pct": round(100 * sum(r["meta"]["text_truncated"] for r in recs) / len(recs), 1),
            "history_days": {"median": statistics.median(hd), "min": hd[0], "max": hd[-1]},
            "era": dict(collections.Counter(r["meta"]["era"] for r in recs)),
            "ticker_tier": dict(collections.Counter(r["meta"]["ticker_tier"] for r in recs)),
            "distinct_tickers": len({r["meta"]["ticker"] for r in recs}),
            "figures_matching_own_series_mean": round(sum(fm) / len(fm), 3),
            "figures_matching_own_series_pct_records_ge1": round(100 * sum(1 for x in fm if x) / len(fm), 1),
            "PERMUTATION_CONTROL_mean": round(sum(ctrl) / len(ctrl), 3),
            "PERMUTATION_CONTROL_pct_records_ge1": round(100 * sum(1 for x in ctrl if x) / len(ctrl), 1),
        }

    report = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": str(resolve(args.config)),
        "universe": {"news_rows_scanned": rows, "candidate_pairs": len(cand),
                     "tickers_with_prices": len(have_prices),
                     "pairs_symbol_named": sum(1 for e in cand.values() if e[2]),
                     "pairs_symbol_named_pct": round(100 * sum(1 for e in cand.values() if e[2]) / max(1, len(cand)), 1)},
        "settings": {"history_days": hist, "min_history_days": min_hist, "text_max_chars": cap,
                     "max_articles_per_record": max_arts, "min_article_chars": min_chars},
        "kept": stats(kept), "rejects": stats(rej), "roundups": stats(rnd),
        "drops": {"kept": kd, "rejects": rd, "roundups": nd},
    }
    rp = outp.with_name(outp.stem + "_report.json")
    json.dump(report, open(rp, "w"), indent=1)
    print(f"wrote report -> {rp}")
    print(json.dumps({k: report[k] for k in ("universe", "kept")}, indent=1))


if __name__ == "__main__":
    main()
