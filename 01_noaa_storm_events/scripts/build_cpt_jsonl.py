#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from NOAA NCEI Storm Events CSV.

Groups event rows by (EPISODE_ID, STATE), aggregates three daily metrics
(injuries, property damage, event count), and pairs them with official NOAA
episode/event narratives. Output is one natural-text record per episode with a
<ts></ts> placeholder — not Alpaca instruction format.

Example:
  python scripts/build_cpt_jsonl.py --config config.example.yaml
  python scripts/build_cpt_jsonl.py --set data.years=[2023] --set output.max_records=10
  python scripts/build_cpt_jsonl.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required. Install with: pip install pyyaml\n"
        "Or: pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
NOAA_INDEX_URL = (
    "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"
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
# Parsing helpers
# ---------------------------------------------------------------------------


_DATE_FORMATS = (
    "%d-%b-%y %H:%M:%S",
    "%d-%b-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H",
    "%d-%b-%y",
    "%Y-%m-%d",
)


def parse_begin_date(raw: str) -> Optional[date]:
    """Parse NOAA BEGIN_DATE_TIME to a calendar date."""
    text = (raw or "").strip()
    if not text:
        return None
    # Try full string first, then common truncated prefixes.
    for chunk in (text, text[:19], text[:16], text[:11], text[:10]):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(chunk, fmt).date()
            except ValueError:
                continue
    return None


def parse_damage_usd(raw: str) -> int:
    """Convert DAMAGE_PROPERTY strings like '50K', '1.5M', '200' to integer USD."""
    text = (raw or "").strip().upper().replace(",", "").replace("$", "")
    if not text or text in {"0", "0.0", "0.00"}:
        return 0
    multiplier = 1
    if text.endswith("K"):
        multiplier = 1_000
        text = text[:-1]
    elif text.endswith("M"):
        multiplier = 1_000_000
        text = text[:-1]
    try:
        return int(round(float(text) * multiplier))
    except ValueError:
        return 0


def safe_int(raw: str) -> int:
    text = (raw or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def slug_event_type(event_type: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", event_type.lower()).strip("_") or "unknown"


def normalize_row(row: Mapping[str, str]) -> Dict[str, str]:
    return {k.strip().lower(): (v or "").strip() for k, v in row.items()}


def iter_date_range(first: date, last: date) -> List[date]:
    days = (last - first).days + 1
    return [first + timedelta(days=i) for i in range(days)]


def truncate_text(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    clipped = text[: limit - 3].rsplit(" ", 1)[0]
    return clipped + "..."


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class EventRow:
    episode_id: str
    state: str
    event_type: str
    event_date: date
    injuries: int
    damage_usd: int
    episode_narrative: str
    event_narrative: str


@dataclass
class EpisodeGroup:
    episode_id: str
    state: str
    rows: List[EventRow] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def download_year_csv(url_template: str, year: int, cache_dir: Path) -> Path:
    url = url_template.format(year=year)
    cache_dir.mkdir(parents=True, exist_ok=True)
    filename = url.rsplit("/", 1)[-1]
    dest = cache_dir / filename
    if dest.exists():
        return dest
    print(f"Downloading {url} ...", file=sys.stderr)
    try:
        urllib.request.urlretrieve(url, dest)
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Download failed ({exc.code}) for {url}.\n"
            f"Check the NOAA index for the current filename: {NOAA_INDEX_URL}"
        ) from exc
    return dest


def open_csv_source(path: Path) -> Iterable[Dict[str, str]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            yield normalize_row(row)


def load_rows(cfg: Dict[str, Any]) -> List[EventRow]:
    data_cfg = cfg["data"]
    source = data_cfg.get("source", "download")
    paths: List[Path] = []

    if source == "local":
        local_path = data_cfg.get("local_path")
        if not local_path:
            raise SystemExit("data.local_path is required when data.source=local")
        paths.append(resolve_path(local_path))
    elif source == "download":
        template = data_cfg["download_url_template"]
        cache_dir = ROOT / ".cache" / "noaa_storm_events"
        for year in data_cfg.get("years", []):
            paths.append(download_year_csv(template, int(year), cache_dir))
    else:
        raise SystemExit(f"Unknown data.source: {source}")

    rows: List[EventRow] = []
    for path in paths:
        for raw in open_csv_source(path):
            event_date = parse_begin_date(raw.get("begin_date_time", ""))
            if event_date is None:
                continue
            episode_id = raw.get("episode_id", "").strip()
            state = raw.get("state", "").strip().upper()
            if not state:
                continue
            rows.append(
                EventRow(
                    episode_id=episode_id,
                    state=state,
                    event_type=raw.get("event_type", "").strip(),
                    event_date=event_date,
                    injuries=safe_int(raw.get("injuries_direct", ""))
                    + safe_int(raw.get("injuries_indirect", "")),
                    damage_usd=parse_damage_usd(raw.get("damage_property", "")),
                    episode_narrative=raw.get("episode_narrative", "").strip(),
                    event_narrative=raw.get("event_narrative", "").strip(),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Episode processing
# ---------------------------------------------------------------------------


def group_episodes(rows: Iterable[EventRow]) -> List[EpisodeGroup]:
    buckets: DefaultDict[Tuple[str, str], List[EventRow]] = defaultdict(list)
    for row in rows:
        key = (row.episode_id or f"__no_id__{row.event_date.isoformat()}", row.state)
        buckets[key].append(row)
    return [
        EpisodeGroup(episode_id=key[0], state=key[1], rows=group_rows)
        for key, group_rows in buckets.items()
    ]


def filter_episode_rows(rows: List[EventRow], cfg: Dict[str, Any]) -> List[EventRow]:
    data_cfg = cfg["data"]
    event_filter = [e.strip() for e in data_cfg.get("event_type_filter", []) if e]
    if not event_filter:
        return rows
    allowed = {e.lower() for e in event_filter}
    return [r for r in rows if r.event_type.lower() in allowed]


def build_daily_arrays(
    rows: List[EventRow], first_date: date, last_date: date
) -> Tuple[List[int], List[int], List[int]]:
    day_count = (last_date - first_date).days + 1
    injuries = [0] * day_count
    damage = [0] * day_count
    events = [0] * day_count
    for row in rows:
        idx = (row.event_date - first_date).days
        if 0 <= idx < day_count:
            injuries[idx] += row.injuries
            damage[idx] += row.damage_usd
            events[idx] += 1
    return injuries, damage, events


def assemble_text(rows: List[EventRow], cfg: Dict[str, Any]) -> str:
    text_cfg = cfg["text"]
    max_event_narratives = int(text_cfg.get("max_event_narratives", 3))
    event_limit = int(text_cfg.get("event_narrative_char_limit", 400))
    episode_limit = int(text_cfg.get("episode_narrative_char_limit", 1200))
    ts_intro = text_cfg.get(
        "ts_intro_sentence", "Daily impact metrics for this episode: <ts></ts>."
    )

    # Episode narrative is usually identical across rows — keep unique non-empty texts.
    episode_parts: List[str] = []
    seen_episode: set[str] = set()
    for row in rows:
        if row.episode_narrative and row.episode_narrative not in seen_episode:
            seen_episode.add(row.episode_narrative)
            episode_parts.append(row.episode_narrative)

    body = " ".join(episode_parts)
    if body:
        body = truncate_text(body, episode_limit)

    # Append up to N distinct event narratives (prefer rows with non-empty text).
    event_parts: List[str] = []
    seen_event: set[str] = set()
    for row in rows:
        if not row.event_narrative or row.event_narrative in seen_event:
            continue
        seen_event.add(row.event_narrative)
        event_parts.append(truncate_text(row.event_narrative, event_limit))
        if len(event_parts) >= max_event_narratives:
            break

    segments = [part for part in [body, *event_parts] if part]
    prose = " ".join(segments)
    if prose and not prose.endswith((".", "!", "?")):
        prose += "."
    if prose:
        prose += " "
    prose += ts_intro
    return prose


def make_series_id(episode_id: str, state: str, rows: List[EventRow]) -> str:
    if episode_id and not episode_id.startswith("__no_id__"):
        return f"{episode_id}_{state}"
    first_date = min(r.event_date for r in rows)
    first_type = rows[0].event_type if rows else "unknown"
    return f"{first_date.isoformat()}_{state}_{slug_event_type(first_type)}"


def episode_to_record(group: EpisodeGroup, cfg: Dict[str, Any]) -> Dict[str, Any]:
    rows = filter_episode_rows(group.rows, cfg)
    first_date = min(r.event_date for r in rows)
    last_date = max(r.event_date for r in rows)
    injuries, damage, events = build_daily_arrays(rows, first_date, last_date)

    event_types = sorted({r.event_type for r in rows if r.event_type})
    text = assemble_text(rows, cfg)

    return {
        "text": text,
        "timeseries": [
            {"values": injuries, "unit": "injuries/day", "freq": "daily"},
            {"values": damage, "unit": "USD/day", "freq": "daily"},
            {"values": events, "unit": "events/day", "freq": "daily"},
        ],
        "episode_date_range": [first_date.isoformat(), last_date.isoformat()],
        "geography": group.state,
        "event_types": event_types,
        "dataset": "noaa_storm_events",
        "source": "ncei_storm_events_db",
        "series_id": make_series_id(group.episode_id, group.state, rows),
        "task_type": "world_knowledge",
    }


def should_skip_episode(
    group: EpisodeGroup, cfg: Dict[str, Any]
) -> Optional[str]:
    data_cfg = cfg["data"]
    state_filter = [s.strip().upper() for s in data_cfg.get("state_filter", []) if s]
    if state_filter and group.state not in state_filter:
        return "state_filter"

    rows = filter_episode_rows(group.rows, cfg)
    if not rows:
        return "event_type_filter"

    min_events = int(data_cfg.get("min_episode_events", 1))
    if len(rows) < min_events:
        return "min_episode_events"

    first_date = min(r.event_date for r in rows)
    last_date = max(r.event_date for r in rows)
    episode_days = (last_date - first_date).days + 1
    min_days = int(data_cfg.get("min_episode_days", 1))
    if episode_days < min_days:
        return "min_episode_days"

    if data_cfg.get("require_episode_narrative", True):
        has_narrative = any(r.episode_narrative for r in rows)
        if not has_narrative:
            return "missing_episode_narrative"

    return None


# ---------------------------------------------------------------------------
# Output + validation
# ---------------------------------------------------------------------------


def validate_record(record: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    required = [
        "text",
        "timeseries",
        "episode_date_range",
        "geography",
        "event_types",
        "dataset",
        "source",
        "series_id",
        "task_type",
    ]
    for key in required:
        if key not in record:
            errors.append(f"missing field: {key}")

    text = record.get("text", "")
    if text.count("<ts></ts>") != 1:
        errors.append("text must contain exactly one <ts></ts>")

    ts_list = record.get("timeseries", [])
    if len(ts_list) != 3:
        errors.append("timeseries must have exactly 3 objects")
    else:
        lengths = {len(obj.get("values", [])) for obj in ts_list}
        if len(lengths) != 1:
            errors.append("timeseries value arrays have mismatched lengths")

        start, end = record.get("episode_date_range", ["", ""])
        try:
            expected_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
            if lengths and next(iter(lengths)) != expected_days:
                errors.append("timeseries length does not match episode_date_range")
        except ValueError:
            errors.append("invalid episode_date_range")

    return errors


def write_output(
    records: List[Dict[str, Any]], cfg: Dict[str, Any], dry_run: bool
) -> None:
    if dry_run:
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


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_pipeline(cfg: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    rows = load_rows(cfg)
    groups = group_episodes(rows)

    skipped: DefaultDict[str, int] = defaultdict(int)
    records: List[Dict[str, Any]] = []
    max_records = cfg["output"].get("max_records")
    validation_errors: List[str] = []

    for group in groups:
        reason = should_skip_episode(group, cfg)
        if reason:
            skipped[reason] += 1
            continue

        record = episode_to_record(group, cfg)
        errors = validate_record(record)
        if errors:
            skipped["validation_error"] += 1
            validation_errors.extend(
                f"{group.episode_id}/{group.state}: {err}" for err in errors
            )
            continue

        records.append(record)
        if max_records is not None and len(records) >= int(max_records):
            break

    report = {
        "episodes_seen": len(groups),
        "episodes_skipped": dict(sorted(skipped.items())),
        "records_written": len(records),
        "rows_loaded": len(rows),
        "validation_errors": validation_errors[:20],
        "config_snapshot": cfg,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
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
        description="Build CPT JSONL from NOAA Storm Events CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/build_cpt_jsonl.py --config config.example.yaml\n"
            "  python scripts/build_cpt_jsonl.py --set data.state_filter=[OKLAHOMA] --dry-run\n"
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
        help="Compute stats and print report; do not write output files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config, args.set)
    report = run_pipeline(cfg, dry_run=args.dry_run)
    if not args.dry_run:
        print(
            f"Wrote {report['records_written']} records "
            f"({report['episodes_seen']} episodes seen, "
            f"{sum(report['episodes_skipped'].values())} skipped).",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
