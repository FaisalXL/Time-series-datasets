#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from the raw FNSPID HuggingFace files.

One record = one ``(ticker, news_date)``: the real financial-news article(s)
published that day, paired with the ticker's trailing daily **OHLCV** window
ending the trading day *before* the news date (no lookahead).

Design notes (see CHALLENGES.md for the full write-up):
  * **Multivariate** — emits open/high/low/close/adj_close/volume channels, not
    just close (configurable via ``data.channels``).
  * **Irregular series** — the market is closed on weekends/holidays, so the
    daily series is irregular on the calendar. We do NOT impute synthetic days;
    instead each record carries an explicit ``trading_dates`` array aligned with
    the values, so the gaps are represented honestly (freq stays ``1d`` on
    trading days).
  * **One ticker per record** — the text describes a single stock, so the
    cross-sectional "some stocks aren't trading at time t" panel-alignment
    problem does not arise here (see CHALLENGES.md §A3).

The raw inputs live OUTSIDE the git repo (downloaded by scripts/download_fnspid.py).
The 21.6 GB news CSV is streamed in chunks — never loaded whole.

Examples:
  python scripts/build_cpt_from_hf.py --set output.max_records=20 \
      --set data.tickers_filter=[AAL,AAPL] --set data.max_news_rows=2000000
  python scripts/build_cpt_from_hf.py
  python scripts/build_cpt_from_hf.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import random
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import pandas as pd
except ImportError as exc:
    raise SystemExit(
        "pandas is required. Install with: pip install -r requirements.txt"
    ) from exc

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required. Install with: pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

# (config name, price-CSV column, unit label, cast)
CHANNEL_SPEC: Dict[str, Tuple[str, str, Any]] = {
    "open":      ("open",      "open_price_usd",      float),
    "high":      ("high",      "high_price_usd",      float),
    "low":       ("low",       "low_price_usd",       float),
    "close":     ("close",     "close_price_usd",     float),
    "adj_close": ("adj close", "adj_close_price_usd", float),
    "volume":    ("volume",    "volume_shares",       lambda v: int(float(v))),
}

NEWS_COLS = ["Date", "Article_title", "Stock_symbol", "Article"]


# ---------------------------------------------------------------------------
# Config helpers (borrowed boilerplate: --config + --set dotted.key=value)
# ---------------------------------------------------------------------------


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def coerce_value(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    if re.fullmatch(r"-?\d+", raw.strip()):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw.strip()):
        return float(raw)
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [coerce_value(p.strip()) for p in inner.split(",")]
    return raw


def parse_set_args(set_args: Sequence[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for item in set_args:
        if "=" not in item:
            raise ValueError(f"Invalid --set value (need key=value): {item}")
        key, raw = item.split("=", 1)
        parts = key.split(".")
        cursor = result
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = coerce_value(raw)
    return result


def load_config(config_path: Path, set_overrides: Sequence[str]) -> Dict[str, Any]:
    with config_path.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if set_overrides:
        cfg = deep_merge(cfg, parse_set_args(set_overrides))
    return cfg


def resolve_path(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else ROOT / p


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def normalize_for_dedup(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def clean_article(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip())


def symbol_mentioned(symbol: str, title: str, article: str) -> bool:
    """Heuristic: does the title/body actually name this ticker?"""
    hay = f"{title}\n{article}"
    sym = re.escape(symbol)
    patterns = [
        rf"\(\s*Symbol:\s*{sym}\s*\)",
        rf"\((?:NYSE|NASDAQ|NYSEARCA|AMEX|OTC)[:\s]+{sym}\s*\)",
        rf"\${sym}\b",
        rf"\bticker\s+{sym}\b",
    ]
    if any(re.search(p, hay, re.IGNORECASE) for p in patterns):
        return True
    # Bare standalone symbol (only trustworthy for multi-char tickers).
    if len(symbol) >= 3 and re.search(rf"\b{sym}\b", hay):
        return True
    return False


# ---------------------------------------------------------------------------
# Price loading
# ---------------------------------------------------------------------------


class PriceTable:
    """Ascending trading dates + per-date channel values for one ticker."""

    def __init__(self, dates: List[str], values: Dict[str, List[float]]):
        self.dates = dates
        self.values = values  # csv_col -> list aligned with dates

    def window_before(self, news_date: str, n: int) -> Tuple[List[str], Dict[str, List[Any]]]:
        # ISO date strings sort lexicographically; dates[:idx] are strictly < news_date.
        idx = bisect.bisect_left(self.dates, news_date)
        start = max(0, idx - n)
        win_dates = self.dates[start:idx]
        win_vals = {col: vals[start:idx] for col, vals in self.values.items()}
        return win_dates, win_vals


def load_price_table(prices_dir: Path, ticker: str, csv_cols: List[str]) -> Optional[PriceTable]:
    csv_path = prices_dir / f"{ticker}.csv"
    if not csv_path.exists():
        return None
    rows: List[Tuple[str, Dict[str, float]]] = []
    with csv_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            d = (row.get("date") or "").strip()[:10]
            if not d:
                continue
            try:
                vals = {col: float(row[col]) for col in csv_cols}
            except (KeyError, ValueError, TypeError):
                continue
            rows.append((d, vals))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])  # ascending by date
    dates = [d for d, _ in rows]
    values = {col: [v[col] for _, v in rows] for col in csv_cols}
    return PriceTable(dates, values)


# ---------------------------------------------------------------------------
# Pass 1: stream news -> grouped (ticker, date) article blocks
# ---------------------------------------------------------------------------


class NewsGroup:
    __slots__ = ("texts", "hashes", "chars", "n_seen", "title")

    def __init__(self) -> None:
        self.texts: List[str] = []
        self.hashes: set = set()
        self.chars: int = 0
        self.n_seen: int = 0
        self.title: str = ""


def stream_news_groups(
    cfg: Dict[str, Any], drops: Dict[str, int]
) -> Dict[Tuple[str, str], NewsGroup]:
    dcfg, fcfg, scfg, tcfg = cfg["data"], cfg["filters"], cfg["sampling"], cfg["text"]
    news_csv = resolve_path(dcfg["news_csv"])
    if not news_csv.exists():
        raise SystemExit(
            f"News CSV not found: {news_csv}\nRun scripts/download_fnspid.py first."
        )

    universe = {t.strip().upper() for t in dcfg.get("tickers_filter", []) if t}
    date_min = dcfg.get("date_min")
    date_max = dcfg.get("date_max")
    max_rows = dcfg.get("max_news_rows")
    max_per_ticker = scfg.get("max_per_ticker")
    max_per_month = scfg.get("max_per_ticker_month")
    require_symbol = bool(fcfg.get("require_symbol_in_text", False))
    max_articles = int(fcfg.get("max_articles_per_record", 5))
    text_cap = int(tcfg.get("max_chars", 3000))

    groups: Dict[Tuple[str, str], NewsGroup] = {}
    ticker_dates: Dict[str, set] = defaultdict(set)
    ticker_month: Dict[Tuple[str, str], int] = defaultdict(int)

    seen_rows = 0
    chunksize = 200_000
    reader = pd.read_csv(
        news_csv, usecols=NEWS_COLS, dtype=str, chunksize=chunksize, on_bad_lines="skip"
    )
    for chunk in reader:
        for date_raw, title, sym, article in zip(
            chunk["Date"], chunk["Article_title"], chunk["Stock_symbol"], chunk["Article"]
        ):
            seen_rows += 1
            if not isinstance(article, str) or not article.strip():
                drops["empty_article"] += 1
                continue
            if not isinstance(sym, str) or not sym.strip():
                drops["no_symbol"] += 1
                continue
            if not isinstance(date_raw, str) or len(date_raw) < 10:
                drops["bad_date"] += 1
                continue

            ticker = sym.strip().upper()
            news_date = date_raw[:10]

            if universe and ticker not in universe:
                drops["not_in_universe"] += 1
                continue
            if date_min and news_date < date_min:
                drops["before_date_min"] += 1
                continue
            if date_max and news_date > date_max:
                drops["after_date_max"] += 1
                continue
            title_s = title if isinstance(title, str) else ""
            if require_symbol and not symbol_mentioned(ticker, title_s, article):
                drops["symbol_not_in_text"] += 1
                continue

            key = (ticker, news_date)
            grp = groups.get(key)
            if grp is None:
                # New (ticker, date): enforce sampling caps.
                if max_per_ticker and len(ticker_dates[ticker]) >= int(max_per_ticker):
                    drops["cap_per_ticker"] += 1
                    continue
                ym = news_date[:7]
                if max_per_month and ticker_month[(ticker, ym)] >= int(max_per_month):
                    drops["cap_per_ticker_month"] += 1
                    continue
                grp = NewsGroup()
                groups[key] = grp
                ticker_dates[ticker].add(news_date)
                ticker_month[(ticker, ym)] += 1

            grp.n_seen += 1
            if len(grp.texts) >= max_articles or grp.chars >= text_cap:
                continue
            cleaned = clean_article(article)
            h = hash(normalize_for_dedup(cleaned))
            if h in grp.hashes:
                continue
            grp.hashes.add(h)
            if not grp.title:
                grp.title = title_s
            grp.texts.append(cleaned)
            grp.chars += len(cleaned)

        if max_rows and seen_rows >= int(max_rows):
            print(f"[pass1] stopped after {seen_rows:,} rows (max_news_rows)", file=sys.stderr)
            break

    drops["_news_rows_scanned"] = seen_rows
    return groups


# ---------------------------------------------------------------------------
# Pass 2: pair groups with prices -> records
# ---------------------------------------------------------------------------


def build_text(ticker: str, news_date: str, article_block: str, n: int,
               last_date: str, ts_sentence: str) -> str:
    ts = ts_sentence.format(ticker=ticker, n=n, last_date=last_date)
    return f"{ticker}, {news_date}. {article_block} {ts}"


def build_timeseries(channels: List[str], csv_cols: List[str],
                     win_vals: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    series = []
    for name in channels:
        csv_col, unit, cast = CHANNEL_SPEC[name]
        raw = win_vals[csv_col]
        if name == "volume":
            vals: List[Any] = [int(round(v)) for v in raw]
        else:
            vals = [round(float(v), 4) for v in raw]
        series.append({"values": vals, "unit": unit, "freq": "1d"})
    return series


def validate_record(rec: Dict[str, Any], min_history: int) -> List[str]:
    errs: List[str] = []
    if rec["text"].count("<ts></ts>") != 1:
        errs.append("text must contain exactly one <ts></ts>")
    if rec.get("task_type") != "world_knowledge":
        errs.append("task_type must be world_knowledge")
    if rec.get("text_quality") != "real":
        errs.append("text_quality must be real")
    ts = rec.get("timeseries", [])
    if not ts:
        errs.append("timeseries empty")
    n_dates = len(rec.get("trading_dates", []))
    for ch in ts:
        if not ch.get("values"):
            errs.append(f"channel {ch.get('unit')} has no values")
        if ch.get("freq") != "1d":
            errs.append(f"channel {ch.get('unit')} freq must be 1d")
        if not ch.get("unit"):
            errs.append("channel missing unit")
        if len(ch.get("values", [])) < min_history:
            errs.append(f"channel {ch.get('unit')} shorter than min_history_days")
        if len(ch.get("values", [])) != n_dates:
            errs.append(f"channel {ch.get('unit')} length != trading_dates")
    # No lookahead: last trading date strictly before the news date.
    if rec.get("trading_dates") and rec["trading_dates"][-1] >= rec["news_date"]:
        errs.append("lookahead: trading window not strictly before news_date")
    return errs


def pair_groups_to_records(
    groups: Dict[Tuple[str, str], NewsGroup], cfg: Dict[str, Any], drops: Dict[str, int]
) -> List[Dict[str, Any]]:
    dcfg, fcfg, tcfg = cfg["data"], cfg["filters"], cfg["text"]
    prices_dir = resolve_path(dcfg["prices_dir"])
    channels = list(dcfg["channels"])
    csv_cols = [CHANNEL_SPEC[c][0] for c in channels]
    history_days = int(dcfg["history_days"])
    min_history = int(dcfg["min_history_days"])
    min_article_chars = int(fcfg.get("min_article_chars", 200))
    text_cap = int(tcfg.get("max_chars", 3000))
    ts_sentence = tcfg["ts_intro_sentence"]

    # Group keys by ticker so each price CSV is read once.
    by_ticker: Dict[str, List[str]] = defaultdict(list)
    for ticker, news_date in groups:
        by_ticker[ticker].append(news_date)

    records: List[Dict[str, Any]] = []
    for ticker in sorted(by_ticker):
        ptable = load_price_table(prices_dir, ticker, csv_cols)
        if ptable is None:
            drops["no_price_csv"] += len(by_ticker[ticker])
            continue
        for news_date in sorted(by_ticker[ticker]):
            grp = groups[(ticker, news_date)]
            article_block = " ".join(grp.texts).strip()
            if len(article_block) > text_cap:
                article_block = article_block[:text_cap].rstrip()
            if len(article_block) < min_article_chars:
                drops["short_text"] += 1
                continue

            win_dates, win_vals = ptable.window_before(news_date, history_days)
            if len(win_dates) < min_history:
                drops["insufficient_history"] += 1
                continue

            n_shown = len(grp.texts)
            rec = {
                "text": build_text(ticker, news_date, article_block,
                                   len(win_dates), win_dates[-1], ts_sentence),
                "timeseries": build_timeseries(channels, csv_cols, win_vals),
                "task_type": "world_knowledge",
                "text_quality": "real",
                "ticker": ticker,
                "news_date": news_date,
                "history_days": len(win_dates),
                "trading_dates": win_dates,
                "channels": channels,
                "n_articles": grp.n_seen,
                "n_articles_shown": n_shown,
                "dataset": "fnspid",
                "source": "Zihan1004/FNSPID",
                "series_id": f"fnspid_{ticker}_{news_date}",
                "license": "CC BY-NC 4.0",
            }
            errs = validate_record(rec, min_history)
            if errs:
                drops["validation_error"] += 1
                continue
            records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_jsonl(records: List[Dict[str, Any]], path: Path, indent: Optional[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if indent is None:
        with path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    else:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=int(indent))
            fh.write("\n")


def run_pipeline(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out_cfg = cfg["output"]
    scfg = cfg["sampling"]
    max_records = out_cfg.get("max_records")
    if max_records is not None:
        max_records = int(max_records)

    drops: Dict[str, int] = defaultdict(int)

    print("[pass1] streaming news CSV ...", file=sys.stderr)
    groups = stream_news_groups(cfg, drops)
    print(f"[pass1] {len(groups):,} (ticker, date) groups retained", file=sys.stderr)

    print("[pass2] pairing with prices ...", file=sys.stderr)
    candidates = pair_groups_to_records(groups, cfg, drops)
    print(f"[pass2] {len(candidates):,} valid candidate records", file=sys.stderr)

    sampled_down = False
    if max_records is not None and len(candidates) > max_records:
        rng = random.Random(int(scfg.get("random_seed", 42)))
        candidates = rng.sample(candidates, max_records)
        sampled_down = True
    candidates.sort(key=lambda r: r["series_id"])

    write_jsonl(candidates, resolve_path(out_cfg["output_path"]), out_cfg.get("indent"))
    write_jsonl(candidates[:50], resolve_path(out_cfg["samples_path"]), indent=2)

    tickers = sorted({r["ticker"] for r in candidates})
    dates = [r["news_date"] for r in candidates]
    avg_hist = sum(r["history_days"] for r in candidates) / len(candidates) if candidates else 0.0
    avg_art = sum(r["n_articles"] for r in candidates) / len(candidates) if candidates else 0.0

    news_scanned = drops.pop("_news_rows_scanned", 0)
    report = {
        "run_date": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "news_rows_scanned": news_scanned,
        "groups_retained": len(groups),
        "candidate_records": len(candidates) if not sampled_down else None,
        "records_written": len(candidates),
        "down_sampled_to_max_records": sampled_down,
        "unique_tickers": len(tickers),
        "date_range": {"min": min(dates) if dates else None, "max": max(dates) if dates else None},
        "avg_history_days": round(avg_hist, 2),
        "avg_articles_per_record": round(avg_art, 2),
        "channels": list(cfg["data"]["channels"]),
        "drops": dict(sorted(drops.items())),
        "license": "CC BY-NC 4.0 (research use only)",
        "config_snapshot": cfg,
    }
    report_path = resolve_path(out_cfg["report_path"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build CPT JSONL from raw FNSPID HuggingFace files (streamed).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"YAML config path (default: {DEFAULT_CONFIG.name})")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="Override a config key (dotted path). Repeatable.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config, args.set)
    report = run_pipeline(cfg)
    print(
        f"\nDone: wrote {report['records_written']} records "
        f"({report['unique_tickers']} tickers, "
        f"{report['news_rows_scanned']:,} news rows scanned). "
        f"Output: {cfg['output']['output_path']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
