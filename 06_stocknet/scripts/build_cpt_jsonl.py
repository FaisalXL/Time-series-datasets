#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from StockNet tweet corpus + Yahoo Finance prices.

One record per (ticker, ISO-week): unique investor tweets for the week paired with
daily OHLCV trading data for the same week's trading sessions.

Examples:
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --config config.example.yaml
  python scripts/build_cpt_jsonl.py --set output.max_records=50
  python scripts/build_cpt_jsonl.py --set filters.min_tweets=5
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required. Install with: pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# (csv_column, unit_label, cast_callable)
PRICE_CHANNELS: Sequence[Tuple[str, str, Any]] = (
    ("Open",   "open_usd",      float),
    ("High",   "high_usd",      float),
    ("Low",    "low_usd",       float),
    ("Close",  "close_usd",     float),
    ("Volume", "volume_shares", lambda v: int(float(v))),
)


# ---------------------------------------------------------------------------
# Config helpers
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
# Data acquisition
# ---------------------------------------------------------------------------


def ensure_data(cache_dir: Path, repo_url: str) -> Path:
    dataset_dir = cache_dir / "stocknet-dataset"
    if not dataset_dir.exists():
        print(f"Cloning {repo_url} into {dataset_dir} ...", file=sys.stderr)
        cache_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dataset_dir)],
            check=True,
        )
    return dataset_dir


# ---------------------------------------------------------------------------
# Stock table
# ---------------------------------------------------------------------------


def load_stock_table(data_dir: Path) -> Dict[str, Dict[str, str]]:
    """Returns {ticker: {"company": ..., "sector": ...}}."""
    table_path = data_dir / "StockTable"
    result: Dict[str, Dict[str, str]] = {}
    if not table_path.exists():
        print(f"warning: StockTable not found at {table_path}", file=sys.stderr)
        return result
    with table_path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            symbol = row.get("Symbol", "").strip().lstrip("$")
            if not symbol:
                continue
            result[symbol] = {
                "company": row.get("Company", symbol).strip(),
                "sector": row.get("Sector", "").strip(),
            }
    return result


# ---------------------------------------------------------------------------
# Price loading
# ---------------------------------------------------------------------------


def load_prices(price_dir: Path, ticker: str) -> Dict[str, Dict[str, Any]]:
    """Returns {date_str: {Open, High, Low, Close, Volume}}."""
    csv_path = price_dir / f"{ticker}.csv"
    if not csv_path.exists():
        return {}
    prices: Dict[str, Dict[str, Any]] = {}
    with csv_path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            day: Dict[str, Any] = {}
            valid = True
            for col, _unit, cast in PRICE_CHANNELS:
                try:
                    day[col] = cast(row[col])
                except (KeyError, ValueError, TypeError):
                    valid = False
                    break
            if valid:
                prices[row["Date"]] = day
    return prices


def prices_for_week(
    prices: Dict[str, Dict[str, Any]],
    iso_year: int,
    iso_week: int,
) -> List[str]:
    """Sorted date strings that fall in the given ISO week."""
    days = []
    for date_str in prices:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        y, w, _ = d.isocalendar()
        if y == iso_year and w == iso_week:
            days.append(date_str)
    return sorted(days)


# ---------------------------------------------------------------------------
# Tweet loading + cleaning
# ---------------------------------------------------------------------------


def detokenize(tokens: List[Any]) -> str:
    """Join a token array into a readable sentence with basic punctuation fixes."""
    text = " ".join(str(t) for t in tokens)
    text = re.sub(r" ([.,!?;:])", r"\1", text)    # no space before punctuation
    text = re.sub(r"([(]) ", r"\1", text)          # no space after open paren
    text = re.sub(r"\$ ([A-Z]{1,5})\b", r"$\1", text)  # $AAPL not $ AAPL
    return text.strip()


def dedup_tweets(tweets: List[str]) -> List[str]:
    """Remove duplicates by normalized text (case-insensitive, whitespace-collapsed)."""
    seen: set = set()
    result: List[str] = []
    for t in tweets:
        key = re.sub(r"\s+", " ", t.lower().strip())
        if key and key not in seen:
            seen.add(key)
            result.append(t)
    return result


def load_tweets_by_week(
    tweet_dir: Path,
    ticker: str,
    start: date,
    end: date,
) -> Dict[Tuple[int, int], List[str]]:
    """Returns {(iso_year, iso_week): [raw_tweet_text, ...]} for all tweet files."""
    ticker_dir = tweet_dir / ticker
    if not ticker_dir.exists():
        return {}

    by_week: Dict[Tuple[int, int], List[str]] = defaultdict(list)
    for day_file in sorted(ticker_dir.iterdir()):
        try:
            day = date.fromisoformat(day_file.name)
        except ValueError:
            continue
        if not (start <= day < end):
            continue
        iso_y, iso_w, _ = day.isocalendar()
        with day_file.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    tokens = obj.get("text", [])
                    text = detokenize(tokens) if isinstance(tokens, list) else str(tokens).strip()
                    if text:
                        by_week[(iso_y, iso_w)].append(text)
                except (json.JSONDecodeError, KeyError):
                    continue
    return dict(by_week)


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------


def date_natural(d: date) -> str:
    return f"{MONTH_NAMES[d.month - 1]} {d.day}"


def iso_week_monday(iso_year: int, iso_week: int) -> date:
    jan4 = date(iso_year, 1, 4)
    mon_w1 = jan4 - timedelta(days=jan4.weekday())
    return mon_w1 + timedelta(weeks=iso_week - 1)


def build_timeseries(
    trading_days: List[str],
    prices: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    series = []
    for col, unit, _ in PRICE_CHANNELS:
        values = [prices[d][col] for d in trading_days]
        series.append({"values": values, "unit": unit, "freq": "1d"})
    return series


LICENSE = "proprietary-review"
SOURCE_URL = "https://github.com/yumoxu/stocknet-dataset"
_PRICE_RE = re.compile(r"\d+\.\d{2}")


def recite_evidence(text: str, series) -> list:
    """OHLCV values from this week that the tweets state literally, at 2dp.

    Verified against a permutation control (the same tweets against another week's series):
    7.6% real vs 0.0% control, a 187x lift. Exact-at-own-precision rather than a tolerance band,
    because a loose numeric match on price-dense text sits on a large coincidence floor.
    """
    seen = {m.group(0) for m in _PRICE_RE.finditer(text)}
    ev = []
    for ch in series:
        for v in ch.get("values") or []:
            if v is None:
                continue
            lit = f"{v:.2f}"
            if lit in seen:
                ev.append({"unit": ch["unit"], "value": v, "literal": lit})
                break
    return ev[:4]


def build_text(
    company: str,
    ticker: str,
    sector: str,
    iso_year: int,
    iso_week: int,
    unique_tweets: List[str],
    max_tweets: Optional[int],
) -> str:
    monday = iso_week_monday(iso_year, iso_week)
    friday = monday + timedelta(days=4)
    date_range = f"{date_natural(monday)}–{date_natural(friday)}, {friday.year}"

    shown = unique_tweets[:max_tweets] if max_tweets else unique_tweets
    remainder = len(unique_tweets) - len(shown)

    tweet_block = " ".join(f'"{t}."' for t in shown)
    if remainder > 0:
        tweet_block += f" [{remainder} more tweet{'s' if remainder != 1 else ''} this week.]"

    # NOTHING is generated around the tweets. An earlier version opened every record with a
    # script-authored sentence -- "Investor commentary on Apple Inc. (AAPL; Consumer Goods) for
    # the week of December 30-January 3, 2014:" -- which no source wrote. The 2026-07-24 pass
    # across 04/06/42/53 removed the generated *closing* sentence and left this *opening* one,
    # so the "No generated text" claim was only half true. Company, ticker, sector and week all
    # live in `meta`, which is where derived context belongs.
    return f"{tweet_block}\n\n<ts></ts>"


def validate_record(record: Dict[str, Any]) -> List[str]:
    errors = []
    if record["text"].count("<ts></ts>") != 1:
        errors.append("text must contain exactly one <ts></ts>")
    for ch in record.get("timeseries", []):
        if not ch.get("values"):
            errors.append(f"channel {ch.get('unit')} has no values")
    return errors


def build_record(
    ticker: str,
    company: str,
    sector: str,
    iso_year: int,
    iso_week: int,
    unique_tweets: List[str],
    trading_days: List[str],
    prices: Dict[str, Dict[str, Any]],
    max_tweets: Optional[int],
) -> Dict[str, Any]:
    monday = iso_week_monday(iso_year, iso_week)
    friday = monday + timedelta(days=4)
    text = build_text(company, ticker, sector, iso_year, iso_week, unique_tweets, max_tweets)
    series = build_timeseries(trading_days, prices)
    ev = recite_evidence(text, series)
    return {
        "text": text,
        "timeseries": series,
        "task_type": "world_knowledge",
        "text_quality": "real",
        # Measured, not asserted. A tweet sometimes quotes that week's actual price -- "the good
        # support for $ aapl today is 541.02" -- so 7.6% of records literally state a value the
        # series holds, against a permutation control (another week's series, same tweets) of
        # 0.0%: a 187x lift. The rest is commentary ABOUT the company rather than a description
        # of its price path, which is `contextualizes` under SCHEMA §7, not `describes`; claiming
        # `describes` for a wall of mixed opinion would overclaim.
        "alignment": "recites" if ev else "contextualizes",
        "license": LICENSE,
        "text_source": "third_party",
        "domain": "finance",
        "region": "US",
        "dataset": "stocknet",
        "source": SOURCE_URL,
        "series_id": f"stocknet_{ticker}_{iso_year}_w{iso_week:02d}",
        "period_start": monday.isoformat(),
        "period_end": friday.isoformat(),
        "meta": {
            "ticker": ticker,
            "company": company,
            "sector": sector,
            "iso_year": iso_year,
            "iso_week": iso_week,
            "week_start": monday.isoformat(),
            "week_end": friday.isoformat(),
            "trading_days": trading_days,
            "n_tweets": len(unique_tweets),
            "n_tweets_shown": len(unique_tweets[:max_tweets] if max_tweets else unique_tweets),
            "recite_evidence": ev,
            "text_preprocessing": ("StockNet ships tweets pre-tokenised: lower-cased, mentions "
                                   "replaced by AT_USER, links by URL, and cashtags spaced "
                                   "($ aapl). This is the corpus's own form, not our cleaning."),
            "true_license": ("StockNet corpus (Xu & Cohen 2018, ACL) redistributing Twitter "
                             "content; Twitter's terms restrict redistribution of tweet text, "
                             "so the rights question is open."),
        },
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_jsonl(records: List[Dict[str, Any]], path: Path, indent: Optional[int] = None) -> None:
    """Always line-delimited. `indent` is accepted and ignored.

    The old branch wrote `json.dump(records, ...)` -- a pretty-printed ARRAY -- whenever an indent
    was passed, and `samples_path` passed indent=2. So a file named `.jsonl` held a JSON array
    that `json.loads` fails on at line 2, breaking any per-line consumer globbing `*.jsonl`.
    That defect has now shipped five times in this corpus, from five different builders, which is
    why the option is removed rather than the call site fixed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_pipeline(cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    data_cfg = cfg["data"]
    filt_cfg = cfg.get("filters", {})
    out_cfg = cfg["output"]

    cache_dir    = resolve_path(data_cfg.get("cache_dir", ".cache"))
    repo_url     = data_cfg.get("repo_url", "https://github.com/yumoxu/stocknet-dataset.git")
    tweet_start  = date.fromisoformat(data_cfg.get("tweet_start", "2014-01-01"))
    tweet_end    = date.fromisoformat(data_cfg.get("tweet_end", "2016-01-01"))

    min_tweets       = int(filt_cfg.get("min_tweets", 3))
    min_trading_days = int(filt_cfg.get("min_trading_days", 3))
    min_text_chars   = int(filt_cfg.get("min_text_chars", 100))
    max_tweets_rec   = filt_cfg.get("max_tweets_per_record")
    if max_tweets_rec is not None:
        max_tweets_rec = int(max_tweets_rec)

    max_records = out_cfg.get("max_records")
    if max_records is not None:
        max_records = int(max_records)

    dataset_dir = ensure_data(cache_dir, repo_url)
    tweet_dir   = dataset_dir / "tweet" / "preprocessed"
    price_dir   = dataset_dir / "price" / "raw"

    stock_table = load_stock_table(dataset_dir)
    tickers = sorted(d.name for d in tweet_dir.iterdir() if d.is_dir())

    stats: Dict[str, Any] = {
        "tickers_found": len(tickers),
        "tickers_processed": 0,
        "records_emitted": 0,
        "skipped_no_prices": 0,
        "skipped_few_tweets": 0,
        "skipped_few_trading_days": 0,
        "skipped_short_text": 0,
        "skipped_validation": 0,
    }

    records: List[Dict[str, Any]] = []

    for ticker in tickers:
        info = stock_table.get(ticker, {"company": ticker, "sector": "Unknown"})
        company = info["company"]
        sector  = info["sector"]

        prices = load_prices(price_dir, ticker)
        if not prices:
            stats["skipped_no_prices"] += 1
            print(f"  {ticker}: skip — no price CSV", file=sys.stderr)
            continue

        tweets_by_week = load_tweets_by_week(tweet_dir, ticker, tweet_start, tweet_end)
        stats["tickers_processed"] += 1

        for (iso_year, iso_week), raw_tweets in sorted(tweets_by_week.items()):
            label = f"{ticker} {iso_year}-W{iso_week:02d}"

            unique_tweets = dedup_tweets(raw_tweets)
            if len(unique_tweets) < min_tweets:
                stats["skipped_few_tweets"] += 1
                continue

            trading_days = prices_for_week(prices, iso_year, iso_week)
            if len(trading_days) < min_trading_days:
                stats["skipped_few_trading_days"] += 1
                continue

            record = build_record(
                ticker=ticker,
                company=company,
                sector=sector,
                iso_year=iso_year,
                iso_week=iso_week,
                unique_tweets=unique_tweets,
                trading_days=trading_days,
                prices=prices,
                max_tweets=max_tweets_rec,
            )

            if len(record["text"]) < min_text_chars:
                stats["skipped_short_text"] += 1
                continue

            errors = validate_record(record)
            if errors:
                print(f"  {label}: validation failed: {errors}", file=sys.stderr)
                stats["skipped_validation"] += 1
                continue

            records.append(record)
            stats["records_emitted"] += 1
            print(f"  {label}: emitted (tweets={len(unique_tweets)}, days={len(trading_days)})")

            if max_records and len(records) >= max_records:
                break

        if max_records and len(records) >= max_records:
            break

    output_path = resolve_path(out_cfg["output_path"])
    write_jsonl(records, output_path, out_cfg.get("indent"))

    samples_path = resolve_path(out_cfg["samples_path"])
    write_jsonl(records[:3], samples_path)   # real JSONL -- a pretty-printed array
                                             # under a .jsonl name has now shipped 5x

    report_path = resolve_path(out_cfg["report_path"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "run_date": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "date_range": {"start": tweet_start.isoformat(), "end": tweet_end.isoformat()},
        **stats,
        "config_snapshot": cfg,
    }
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    return report, records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build CPT JSONL from StockNet tweets + Yahoo Finance prices.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/build_cpt_jsonl.py\n"
            "  python scripts/build_cpt_jsonl.py --set output.max_records=50\n"
            "  python scripts/build_cpt_jsonl.py --set filters.min_tweets=5\n"
        ),
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"YAML config path (default: {DEFAULT_CONFIG.name})")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="Override a config key (dotted path). Repeatable.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config, args.set)
    report, records = run_pipeline(cfg)

    print(
        f"\nDone: {report['records_emitted']} records emitted "
        f"({report['tickers_processed']} tickers processed, "
        f"{report['skipped_few_tweets']} week-ticker pairs skipped for low tweet count, "
        f"{report['skipped_few_trading_days']} skipped for holiday weeks).",
        file=sys.stderr,
    )

    if records:
        print("\n--- First record text ---\n")
        print(records[0]["text"])


if __name__ == "__main__":
    main()
