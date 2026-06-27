#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from NHC HURDAT2 + public advisory text.

One record per tropical cyclone with real NHC public advisory prose and five
6-hourly time series over the storm's qualifying tropical/subtropical life.
Storms without retrievable advisory text are dropped — no synthetic fallback.

Example:
  python scripts/build_cpt_jsonl.py --config config.example.yaml
  python scripts/build_cpt_jsonl.py --set data.source=local --set data.local_path=.cache/hurdat2/hurdat2-1851-2023-051124.txt
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required. Install with: pip install pyyaml\n"
        "Or: pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
HURDAT_INDEX_URL = "https://www.nhc.noaa.gov/data/hurdat/"
NHC_ARCHIVE_BASE = "https://www.nhc.noaa.gov/archive"

QUALIFYING_STATUSES = frozenset({"TD", "TS", "HU", "SS", "SD"})

_SAFFIR_SIMPSON = (
    (137, 5),
    (113, 4),
    (96, 3),
    (83, 2),
    (64, 1),
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
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TrackPoint:
    timestamp: datetime
    record_id: str
    status: str
    lat: float
    lon: float
    max_wind_kt: int
    min_pressure_mb: int
    wind_radii: List[int] = field(default_factory=lambda: [-999] * 12)


@dataclass
class Storm:
    storm_id: str
    name: str
    year: int
    basin: str
    track: List[TrackPoint] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HURDAT2 parsing
# ---------------------------------------------------------------------------


def _parse_lat(raw: str) -> float:
    raw = raw.strip()
    sign = -1.0 if raw.endswith("S") else 1.0
    return round(sign * float(raw[:-1]), 4)


def _parse_lon(raw: str) -> float:
    raw = raw.strip()
    sign = -1.0 if raw.endswith("W") else 1.0
    return round(sign * float(raw[:-1]), 4)


def _safe_int(raw: str, default: int = -999) -> int:
    raw = raw.strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_header(line: str) -> Optional[Tuple[str, str, int, str]]:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) >= 6:
        return None
    storm_id = parts[0].strip()
    if not re.fullmatch(r"[A-Z]{2}\d{6}", storm_id):
        return None
    try:
        num_records = int(parts[2])
    except (ValueError, IndexError):
        return None
    name = parts[1].strip().title()
    basin = storm_id[:2].upper()
    return storm_id, name, num_records, basin


def _parse_data_line(line: str) -> Optional[TrackPoint]:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 8:
        return None
    try:
        date_str = parts[0].strip()
        time_str = parts[1].strip().zfill(4)
        ts = datetime.strptime(date_str + time_str, "%Y%m%d%H%M")
    except ValueError:
        return None

    record_id = parts[2].strip()
    status = parts[3].strip()
    try:
        lat = _parse_lat(parts[4])
        lon = _parse_lon(parts[5])
    except (ValueError, IndexError):
        return None

    max_wind = _safe_int(parts[6])
    min_pres = _safe_int(parts[7])
    radii = [_safe_int(parts[i]) for i in range(8, min(8 + 12, len(parts)))]
    while len(radii) < 12:
        radii.append(-999)

    return TrackPoint(
        timestamp=ts,
        record_id=record_id,
        status=status,
        lat=lat,
        lon=lon,
        max_wind_kt=max_wind,
        min_pressure_mb=min_pres,
        wind_radii=radii,
    )


def parse_hurdat2_text(text: str) -> List[Storm]:
    storms: List[Storm] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    i = 0
    while i < len(lines):
        header = _parse_header(lines[i])
        if header is None:
            i += 1
            continue
        storm_id, name, num_records, basin = header
        year = int(storm_id[-4:])
        storm = Storm(storm_id=storm_id, name=name, year=year, basin=basin)
        i += 1
        for _ in range(num_records):
            if i >= len(lines):
                break
            point = _parse_data_line(lines[i])
            if point is not None:
                storm.track.append(point)
            i += 1
        storms.append(storm)
    return storms


def download_hurdat2(url: str, cache_dir: Path) -> str:
    cache_dir.mkdir(parents=True, exist_ok=True)
    filename = url.rsplit("/", 1)[-1]
    dest = cache_dir / filename
    if dest.exists():
        return dest.read_text(encoding="utf-8", errors="replace")
    print(f"Downloading {url} ...", file=sys.stderr)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Download failed ({exc.code}) for {url}.\n"
            f"Check the current filename at {HURDAT_INDEX_URL}"
        ) from exc
    dest.write_text(text, encoding="utf-8")
    return text


def load_storms(cfg: Dict[str, Any]) -> List[Storm]:
    data_cfg = cfg["data"]
    source = data_cfg.get("source", "download")
    basin = str(data_cfg.get("basin", "atlantic")).lower()
    cache_dir = ROOT / ".cache" / "hurdat2"

    if source == "local":
        local_path = data_cfg.get("local_path")
        if not local_path:
            raise SystemExit("data.local_path is required when data.source=local")
        text = resolve_path(local_path).read_text(encoding="utf-8", errors="replace")
        return parse_hurdat2_text(text)

    if source != "download":
        raise SystemExit(f"Unknown data.source: {source}")

    urls: List[str] = []
    if basin in {"atlantic", "both"}:
        urls.append(data_cfg["atlantic_url"])
    if basin in {"east_pacific", "both"}:
        urls.append(data_cfg["east_pacific_url"])
    if not urls:
        raise SystemExit(f"Unknown data.basin: {basin}")

    storms: List[Storm] = []
    for url in urls:
        text = download_hurdat2(url, cache_dir)
        storms.extend(parse_hurdat2_text(text))
    return storms


# ---------------------------------------------------------------------------
# Storm filtering + track helpers
# ---------------------------------------------------------------------------


def qualifying_track(storm: Storm) -> List[TrackPoint]:
    return [p for p in storm.track if p.status in QUALIFYING_STATUSES]


def saffir_simpson_category(wind_kt: int, status: str = "HU") -> int:
    for threshold, category in _SAFFIR_SIMPSON:
        if wind_kt >= threshold:
            return category
    if wind_kt >= 34 and status == "TS":
        return 0
    return -1


def format_datetime_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def should_skip_storm(storm: Storm, cfg: Dict[str, Any]) -> Optional[str]:
    data_cfg = cfg["data"]
    season_start = int(data_cfg.get("season_start", 0))
    season_end = int(data_cfg.get("season_end", 9999))
    min_obs = int(data_cfg.get("min_qualifying_obs", 1))

    if storm.year < season_start or storm.year > season_end:
        return "season_filter"

    storm_filter = [s.strip().upper() for s in data_cfg.get("storm_filter", []) if s]
    if storm_filter:
        name_key = storm.name.upper()
        id_key = storm.storm_id.upper()
        if name_key not in storm_filter and id_key not in storm_filter:
            return "storm_filter"

    if len(qualifying_track(storm)) < min_obs:
        return "min_qualifying_obs"

    return None


def landfall_points(storm: Storm) -> List[TrackPoint]:
    return [p for p in storm.track if p.record_id.upper() == "L"]


def peak_observation(qtrack: List[TrackPoint]) -> TrackPoint:
    return max(qtrack, key=lambda p: p.max_wind_kt)


def r34_max_nm(point: TrackPoint) -> Optional[int]:
    """Max 34-kt wind radius across quadrants; null if all missing."""
    radii = point.wind_radii[:4]
    valid = [r for r in radii if r != -999]
    if not valid:
        return None
    return max(valid)


def build_timeseries(qtrack: List[TrackPoint]) -> List[Dict[str, Any]]:
    return [
        {"values": [p.max_wind_kt for p in qtrack], "unit": "max_wind_kt", "freq": "6h"},
        {
            "values": [
                None if p.min_pressure_mb == -999 else p.min_pressure_mb for p in qtrack
            ],
            "unit": "min_pressure_mb",
            "freq": "6h",
        },
        {"values": [p.lat for p in qtrack], "unit": "lat", "freq": "6h"},
        {"values": [p.lon for p in qtrack], "unit": "lon", "freq": "6h"},
        {"values": [r34_max_nm(p) for p in qtrack], "unit": "r34_max_nm", "freq": "6h"},
    ]


# ---------------------------------------------------------------------------
# NHC advisory acquisition
# ---------------------------------------------------------------------------


def storm_archive_path(storm_id: str) -> Tuple[str, str]:
    """Return (year, archive slug) e.g. ('2021', 'al09') for AL092021."""
    year = storm_id[-4:]
    basin = storm_id[:2].lower()
    number = storm_id[2:4]
    return year, f"{basin}{number}"


def fetch_url(url: str, timeout: int) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CPT-dataset-builder/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def list_public_advisory_numbers(index_html: str, storm_id_lower: str) -> List[int]:
    pattern = re.compile(rf"{re.escape(storm_id_lower)}\.public\.(\d+)\.shtml", re.I)
    numbers = sorted({int(m.group(1)) for m in pattern.finditer(index_html)})
    return numbers


def select_advisory_numbers(
    numbers: List[int], qtrack: List[TrackPoint], max_per_storm: int
) -> List[int]:
    """Pick first, last, and (if >4 advisories) one closest to peak intensity."""
    if not numbers:
        return []
    selected = {numbers[0], numbers[-1]}
    if len(numbers) > 4 and qtrack:
        peak = peak_observation(qtrack)
        peak_idx = qtrack.index(peak)
        fraction = peak_idx / max(len(qtrack) - 1, 1)
        target = round(fraction * (numbers[-1] - numbers[0])) + numbers[0]
        closest = min(numbers, key=lambda n: abs(n - target))
        selected.add(closest)
    ordered = sorted(selected)
    return ordered[:max_per_storm]


def strip_advisory_html(page_html: str) -> str:
    # Prefer the <pre> product block when present.
    pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", page_html, re.I | re.S)
    if pre_match:
        text = pre_match.group(1)
    else:
        text = re.sub(r"(?is)<script.*?>.*?</script>", " ", page_html)
        text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^ZCZC\b", stripped):
            continue
        if stripped == "NNNN":
            continue
        if re.match(r"^TTAA\d+\s", stripped):
            continue
        lines.append(line.rstrip())
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def truncate_advisory(text: str, limit: int) -> str:
    text = " ".join(text.split()) if limit < 200 else text
    if len(text) <= limit:
        return text
    clipped = text[: limit - 3].rsplit("\n", 1)[0] if "\n" in text[:limit] else text[: limit - 3]
    if len(clipped) >= limit:
        clipped = text[: limit - 3].rsplit(" ", 1)[0]
    return clipped.rstrip() + "..."


def fetch_advisory_text(
    storm: Storm, qtrack: List[TrackPoint], cfg: Dict[str, Any]
) -> Optional[str]:
    adv_cfg = cfg.get("advisories", {})
    if not adv_cfg.get("enabled", True):
        return None

    timeout = int(adv_cfg.get("timeout_seconds", 10))
    char_limit = int(adv_cfg.get("char_limit_per_advisory", 1500))
    max_per_storm = int(adv_cfg.get("max_per_storm", 3))
    ts_intro = cfg["text"]["ts_intro_sentence"]

    year, slug = storm_archive_path(storm.storm_id)
    index_url = f"{NHC_ARCHIVE_BASE}/{year}/{slug}/"
    index_html = fetch_url(index_url, timeout)
    if not index_html:
        return None

    storm_id_lower = storm.storm_id.lower()
    numbers = list_public_advisory_numbers(index_html, storm_id_lower)
    if not numbers:
        return None

    selected = select_advisory_numbers(numbers, qtrack, max_per_storm)
    chunks: List[str] = []
    for num in selected:
        filename = f"{storm_id_lower}.public.{num:03d}.shtml"
        page = fetch_url(f"{index_url}{filename}", timeout)
        if not page:
            continue
        cleaned = strip_advisory_html(page)
        if cleaned:
            chunks.append(truncate_advisory(cleaned, char_limit))

    if not chunks:
        return None

    body = "\n\n---\n\n".join(chunks)
    if not body.strip():
        return None
    return f"{body}\n\n{ts_intro}"


# ---------------------------------------------------------------------------
# Record building + validation
# ---------------------------------------------------------------------------


def storm_to_record(storm: Storm, advisory_text: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    qtrack = qualifying_track(storm)
    peak = peak_observation(qtrack)
    peak_cat = saffir_simpson_category(peak.max_wind_kt, peak.status)

    return {
        "text": advisory_text,
        "timeseries": build_timeseries(qtrack),
        "track_date_range": [
            format_datetime_iso(qtrack[0].timestamp),
            format_datetime_iso(qtrack[-1].timestamp),
        ],
        "storm_name": storm.name.upper(),
        "storm_id": storm.storm_id,
        "basin": storm.basin,
        "season": storm.year,
        "peak_wind_kt": peak.max_wind_kt,
        "peak_category": peak_cat,
        "made_landfall": bool(landfall_points(storm)),
        "dataset": "nhc_hurdat2",
        "source": "nhc_hurdat2_best_track",
        "series_id": storm.storm_id,
        "task_type": "world_knowledge",
        "text_quality": "real",
        "text_source": "nhc_advisory",
    }


def validate_record(record: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    required = [
        "text",
        "timeseries",
        "track_date_range",
        "storm_name",
        "storm_id",
        "basin",
        "season",
        "peak_wind_kt",
        "peak_category",
        "made_landfall",
        "dataset",
        "source",
        "series_id",
        "task_type",
        "text_quality",
        "text_source",
    ]
    for key in required:
        if key not in record:
            errors.append(f"missing field: {key}")

    text = record.get("text", "")
    if text.count("<ts></ts>") != 1:
        errors.append("text must contain exactly one <ts></ts>")

    auto_phrases = (
        "it developed as a",
        "was an atlantic tropical cyclone active from",
        "was an eastern pacific tropical cyclone active from",
        "the storm dissipated on",
        "the storm reached peak intensity of",
    )
    lower = text.lower()
    for phrase in auto_phrases:
        if phrase in lower:
            errors.append(f"text appears auto-generated: {phrase!r}")

    if record.get("text_source") != "nhc_advisory":
        errors.append("text_source must be nhc_advisory")

    ts_list = record.get("timeseries", [])
    expected_units = ["max_wind_kt", "min_pressure_mb", "lat", "lon", "r34_max_nm"]
    if len(ts_list) != 5:
        errors.append("timeseries must have exactly 5 objects")
    else:
        lengths = {len(obj.get("values", [])) for obj in ts_list}
        if len(lengths) != 1:
            errors.append("timeseries value arrays have mismatched lengths")
        for obj, unit in zip(ts_list, expected_units):
            if obj.get("unit") != unit:
                errors.append(f"timeseries unit mismatch: expected {unit}")

    return errors


def write_output(records: List[Dict[str, Any]], cfg: Dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    out_cfg = cfg["output"]
    output_path = resolve_path(out_cfg["output_path"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    indent = out_cfg.get("indent")
    with output_path.open("w", encoding="utf-8") as fh:
        for i, record in enumerate(records):
            if i > 0 and indent is not None:
                fh.write("\n")
            if indent is None:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            else:
                fh.write(json.dumps(record, ensure_ascii=False, indent=int(indent)))
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
    storms = load_storms(cfg)
    skipped: Dict[str, int] = {}
    records: List[Dict[str, Any]] = []
    validation_errors: List[str] = []
    storms_with_advisory = 0
    max_records = cfg["output"].get("max_records")

    if not cfg.get("advisories", {}).get("enabled", True):
        skipped["advisories_disabled"] = len(storms)

    for storm in storms:
        reason = should_skip_storm(storm, cfg)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue

        if not cfg.get("advisories", {}).get("enabled", True):
            continue

        qtrack = qualifying_track(storm)
        advisory_text = fetch_advisory_text(storm, qtrack, cfg)
        if not advisory_text:
            skipped["no_advisory_text"] = skipped.get("no_advisory_text", 0) + 1
            continue

        storms_with_advisory += 1
        record = storm_to_record(storm, advisory_text, cfg)
        errors = validate_record(record)
        if errors:
            skipped["validation_error"] = skipped.get("validation_error", 0) + 1
            validation_errors.extend(f"{storm.storm_id}: {e}" for e in errors)
            continue

        records.append(record)
        if max_records is not None and len(records) >= int(max_records):
            break

    qualifying_count = sum(
        1 for storm in storms if should_skip_storm(storm, cfg) is None
    )

    report = {
        "storms_seen": len(storms),
        "storms_qualifying": qualifying_count,
        "storms_with_advisory": storms_with_advisory,
        "storms_skipped": dict(sorted(skipped.items())),
        "records_written": len(records),
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
        description="Build CPT JSONL from HURDAT2 + NHC public advisories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config, args.set)
    report = run_pipeline(cfg, dry_run=args.dry_run)
    if not args.dry_run:
        print(
            f"Wrote {report['records_written']} records "
            f"({report['storms_seen']} storms seen, "
            f"{report['storms_qualifying']} qualifying, "
            f"{report['storms_with_advisory']} with advisory, "
            f"{sum(report['storms_skipped'].values())} skipped).",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
