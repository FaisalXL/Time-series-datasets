#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from local FNSPID Alpaca JSON files.

Converts instruction/input/output Alpaca records to CPT format: natural financial
news text + historical daily close prices. Future prices in the Alpaca output
field are discarded (no lookahead).

Local demo files (FNSPID_train.json + FNSPID_val.json) cover 2 tickers (AAL, A)
with ~400 records. For full-scale CPT (15.7M news records, 4,775 tickers),
download the complete dataset from HuggingFace — see README.

Example:
  python scripts/build_cpt_jsonl.py --config config.example.yaml
  python scripts/build_cpt_jsonl.py --dry-run
  python scripts/build_cpt_jsonl.py --set data.tickers_filter=[AAL] --set output.max_records=10
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required. Install with: pip install pyyaml\n"
        "Or: pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

PRICES_RE = re.compile(
    r"The historical stock close data is:\s*([\d\.,\-]+)", re.IGNORECASE
)
TICKER_RE = re.compile(r"The ticker is (\S+)\.", re.IGNORECASE)
START_DATE_RE = re.compile(
    r"The start date of historical data was on (\d{4}-\d{2}-\d{2})\.", re.IGNORECASE
)
PREDICTION_DATE_RE = re.compile(
    r"The date of prediction is on (\d{4}-\d{2}-\d{2})\.", re.IGNORECASE
)
NEWS_BLOCK_RE = re.compile(
    r"On (\d{4}-\d{2}-\d{2})(?:\s+\d{2}:\d{2}:\d{2}\+\d{2}:\d{2})?,\s*the news was:\s*'\.?\s*(.*?)(?=(?:'\s*)?On \d{4}-\d{2}-\d{2}|$)",
    re.DOTALL,
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
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [coerce_value(part.strip()) for part in inner.split(",")]
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
    path = Path(path_str)
    return path if path.is_absolute() else ROOT / path


# ---------------------------------------------------------------------------
# Alpaca parsing
# ---------------------------------------------------------------------------


def parse_prices(instruction: str) -> List[float]:
    match = PRICES_RE.search(instruction)
    if not match:
        raise ValueError("missing historical stock close data in instruction")
    values: List[float] = []
    for part in match.group(1).split(","):
        part = part.strip()
        if part:
            values.append(float(part))
    return values


def parse_ticker(input_text: str) -> str:
    match = TICKER_RE.search(input_text)
    if not match:
        raise ValueError("missing ticker in input")
    return match.group(1)


def parse_start_date(input_text: str) -> date:
    match = START_DATE_RE.search(input_text)
    if not match:
        raise ValueError("missing start date in input")
    return date.fromisoformat(match.group(1))


def parse_prediction_date(input_text: str) -> date:
    match = PREDICTION_DATE_RE.search(input_text)
    if not match:
        raise ValueError("missing prediction date in input")
    return date.fromisoformat(match.group(1))


def extract_news_articles(input_text: str) -> List[str]:
    if "No relevant news available" in input_text:
        return []
    articles: List[str] = []
    seen: set[str] = set()
    for match in NEWS_BLOCK_RE.finditer(input_text):
        text = match.group(2).strip()
        if text and text not in seen:
            seen.add(text)
            articles.append(text)
    return articles


def truncate_news(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


def build_text(
    ticker: str,
    news_date: str,
    news_text: str,
) -> str:
    return (
        f"{ticker}, {news_date}. {news_text} "
        f"Historical daily closing prices (USD) for {ticker}: <ts></ts>."
    )


def alpaca_to_record(row: Mapping[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    data_cfg = cfg["data"]
    text_cfg = cfg["text"]

    prices = parse_prices(row["instruction"])
    input_text = row["input"]
    ticker = parse_ticker(input_text)
    history_start = parse_start_date(input_text)
    prediction_date = parse_prediction_date(input_text)
    news_date = prediction_date.isoformat()
    history_days = len(prices)

    articles = extract_news_articles(input_text)
    news_text = truncate_news("\n".join(articles), int(text_cfg.get("max_chars", 2000)))

    record = {
        "text": build_text(ticker, news_date, news_text),
        "timeseries": [
            {
                "values": prices,
                "unit": "close_price_usd",
                "freq": "daily",
            }
        ],
        "ticker": ticker,
        "news_date": news_date,
        "history_start": history_start.isoformat(),
        "history_days": history_days,
        "news_count": len(articles),
        "dataset": "fnspid",
        "source": "Zihan1004/FNSPID",
        "series_id": f"{ticker}_{news_date}",
        "task_type": "world_knowledge",
        "text_source": "financial_news_wire",
        "text_quality": "real",
    }
    return record


def should_skip(row: Mapping[str, Any], cfg: Dict[str, Any]) -> Optional[str]:
    data_cfg = cfg["data"]
    min_history = int(data_cfg.get("min_history_days", 5))
    min_news_chars = int(data_cfg.get("min_news_chars", 50))
    tickers_filter = [t.strip().upper() for t in data_cfg.get("tickers_filter", []) if t]

    try:
        input_text = row["input"]
        if "No relevant news available" in input_text:
            return "no_news"

        ticker = parse_ticker(input_text)
        if tickers_filter and ticker.upper() not in tickers_filter:
            return "tickers_filter"

        prices = parse_prices(row["instruction"])
        if len(prices) < min_history:
            return "min_history_days"

        articles = extract_news_articles(input_text)
        combined = "\n".join(articles)
        if len(combined) < min_news_chars:
            return "min_news_chars"

    except (ValueError, KeyError):
        return "parse_error"

    return None


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_alpaca_records(cfg: Dict[str, Any]) -> List[Mapping[str, Any]]:
    data_cfg = cfg["data"]
    split = str(data_cfg.get("split", "both")).lower()
    records: List[Mapping[str, Any]] = []

    if split in {"both", "train"}:
        train_path = resolve_path(data_cfg["train_path"])
        with train_path.open(encoding="utf-8") as fh:
            records.extend(json.load(fh))

    if split in {"both", "val"}:
        val_path = resolve_path(data_cfg["val_path"])
        with val_path.open(encoding="utf-8") as fh:
            records.extend(json.load(fh))

    return records


# ---------------------------------------------------------------------------
# Output + validation
# ---------------------------------------------------------------------------


def validate_record(record: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    required = [
        "text",
        "timeseries",
        "ticker",
        "news_date",
        "history_start",
        "history_days",
        "news_count",
        "dataset",
        "source",
        "series_id",
        "task_type",
        "text_source",
        "text_quality",
    ]
    for key in required:
        if key not in record:
            errors.append(f"missing field: {key}")

    text = record.get("text", "")
    if text.count("<ts></ts>") != 1:
        errors.append("text must contain exactly one <ts></ts>")

    ts_list = record.get("timeseries", [])
    if len(ts_list) != 1:
        errors.append("timeseries must have exactly 1 object")
    elif not ts_list[0].get("values"):
        errors.append("timeseries values must be non-empty")

    if record.get("text_quality") != "real":
        errors.append("text_quality must be real")

    return errors


def write_output(
    records: List[Dict[str, Any]], cfg: Dict[str, Any], dry_run: bool
) -> None:
    if dry_run:
        if records:
            print(json.dumps(records[0], ensure_ascii=False, indent=2))
        return

    out_cfg = cfg["output"]
    output_path = resolve_path(out_cfg["output_path"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    indent = out_cfg.get("indent")

    if indent is None:
        with output_path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    else:
        with output_path.open("w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=int(indent))
            fh.write("\n")


def write_report(report: Dict[str, Any], cfg: Dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    report_path = resolve_path(cfg["output"]["report_path"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def compute_report_stats(
    records: List[Dict[str, Any]],
) -> Tuple[Dict[str, int], List[str], Optional[str], Optional[str], float, float]:
    tickers_seen = sorted({r["ticker"] for r in records})
    news_dates = [r["news_date"] for r in records]
    date_min = min(news_dates) if news_dates else None
    date_max = max(news_dates) if news_dates else None
    avg_history = (
        sum(r["history_days"] for r in records) / len(records) if records else 0.0
    )
    avg_news = (
        sum(r["news_count"] for r in records) / len(records) if records else 0.0
    )
    return tickers_seen, news_dates, date_min, date_max, avg_history, avg_news


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_pipeline(cfg: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    rows = load_alpaca_records(cfg)
    skipped: DefaultDict[str, int] = defaultdict(int)
    records: List[Dict[str, Any]] = []
    max_records = cfg["output"].get("max_records")
    validation_errors: List[str] = []
    seen_series: set[str] = set()

    for row in rows:
        reason = should_skip(row, cfg)
        if reason:
            skipped[reason] += 1
            continue

        try:
            record = alpaca_to_record(row, cfg)
        except (ValueError, KeyError) as exc:
            skipped["parse_error"] += 1
            validation_errors.append(str(exc))
            continue

        if record["series_id"] in seen_series:
            skipped["duplicate_series_id"] += 1
            continue

        errors = validate_record(record)
        if errors:
            skipped["validation_error"] += 1
            validation_errors.extend(f"{record.get('series_id', '?')}: {err}" for err in errors)
            continue

        seen_series.add(record["series_id"])
        records.append(record)
        if max_records is not None and len(records) >= int(max_records):
            break

    tickers_seen, _, date_min, date_max, avg_history, avg_news = compute_report_stats(
        records
    )

    report = {
        "records_seen": len(rows),
        "records_written": len(records),
        "records_skipped": dict(sorted(skipped.items())),
        "tickers_seen": tickers_seen,
        "date_range": {"min": date_min, "max": date_max},
        "avg_history_days": round(avg_history, 2),
        "avg_news_count": round(avg_news, 2),
        "validation_errors": validation_errors[:20],
        "config_snapshot": cfg,
        "generated_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "dry_run": dry_run,
    }

    write_output(records, cfg, dry_run=dry_run)
    write_report(report, cfg, dry_run=dry_run)
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build CPT JSONL from local FNSPID Alpaca JSON files "
            "(demo slice: 2 tickers, ~400 records)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Local demo files cover AAL and A (2019 only). For full-scale CPT with "
            "4,775 S&P500 tickers and 15.7M news records, download from HuggingFace:\n"
            "  https://huggingface.co/datasets/Zihan1004/FNSPID\n"
            "See README for wget/csv download steps.\n\n"
            "Examples:\n"
            "  python scripts/build_cpt_jsonl.py --config config.example.yaml\n"
            "  python scripts/build_cpt_jsonl.py --dry-run\n"
            "  python scripts/build_cpt_jsonl.py --set data.tickers_filter=[AAL]\n"
            "  python scripts/build_cpt_jsonl.py --set output.max_records=null\n"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"YAML config path (default: {DEFAULT_CONFIG.name})",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override a config key (dotted path). Repeatable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print one example record and report; do not write output files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config, args.set)
    report = run_pipeline(cfg, dry_run=args.dry_run)
    if not args.dry_run:
        skipped_total = sum(report["records_skipped"].values())
        print(
            f"Wrote {report['records_written']} records "
            f"({report['records_seen']} seen, {skipped_total} skipped).",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
