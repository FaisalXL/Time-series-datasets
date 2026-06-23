#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from AliMaatouk/TelecomTS (Hugging Face).

Produces separate normal and anomaly records with 5 KPI time series each.
Anomaly troubleshooting tickets are GPT-generated — always tagged accordingly.
No synthetic text fallback.

Example:
  python scripts/build_cpt_jsonl.py --config config.example.yaml
  python scripts/build_cpt_jsonl.py --dry-run
  python scripts/build_cpt_jsonl.py --set output.max_records=10
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required. Install with: pip install pyyaml\n"
        "Or: pip install -r requirements.txt"
    ) from exc

try:
    from datasets import load_dataset
except ImportError as exc:
    raise SystemExit(
        "datasets is required. Install with: pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

# Output unit labels and human-readable channel names for text templates.
CHANNEL_META: Dict[str, Dict[str, str]] = {
    "RX_Bytes": {
        "unit": "DL_Throughput_Mbps",
        "label": "downlink throughput (Mbps)",
    },
    "TX_Bytes": {
        "unit": "UL_Throughput_Mbps",
        "label": "uplink throughput (Mbps)",
    },
    "DL_BLER": {
        "unit": "DL_BLER_pct",
        "label": "downlink BLER (%)",
    },
    "UL_BLER": {
        "unit": "UL_BLER_pct",
        "label": "uplink BLER (%)",
    },
    "RSRP": {
        "unit": "RSRP_dBm",
        "label": "RSRP (dBm)",
    },
}

BYTES_TO_MBPS = 8e-5  # bytes per 100 ms sample → Mbps


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
# Dataset loading + transforms
# ---------------------------------------------------------------------------


def stream_dataset(cfg: Dict[str, Any]) -> Iterable[Mapping[str, Any]]:
    data_cfg = cfg["data"]
    cache_dir = resolve_path(data_cfg.get("cache_dir", ".cache/telecom_ts"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return load_dataset(
        data_cfg["hf_dataset_id"],
        split=data_cfg.get("split", "train"),
        streaming=True,
        cache_dir=str(cache_dir),
    )


def normalize_kpi_value(channel: str, raw: float) -> float:
    if channel in {"RX_Bytes", "TX_Bytes"}:
        return round(float(raw) * BYTES_TO_MBPS, 6)
    if channel in {"DL_BLER", "UL_BLER"}:
        return round(float(raw) * 100.0, 6)
    return round(float(raw), 6)


def build_timeseries(
    row: Mapping[str, Any], channels: Sequence[str], freq: str
) -> List[Dict[str, Any]]:
    kpis = row["KPIs"]
    series: List[Dict[str, Any]] = []
    for channel in channels:
        if channel not in kpis:
            raise KeyError(f"Missing KPI channel: {channel}")
        values = [normalize_kpi_value(channel, v) for v in kpis[channel]]
        meta = CHANNEL_META.get(channel, {"unit": channel, "label": channel})
        series.append({"values": values, "unit": meta["unit"], "freq": freq})
    return series


def channel_list_text(channels: Sequence[str]) -> str:
    labels = [CHANNEL_META.get(c, {"label": c})["label"] for c in channels]
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def map_traffic_type(application: str) -> str:
    mapping = {
        "File": "FileDownload",
        "Youtube": "YouTube",
        "Twitch": "Twitch",
    }
    return mapping.get(application, application)


def map_mobility(value: str) -> str:
    return "mobile" if str(value).lower() in {"yes", "true", "1"} else "static"


def map_congestion(value: str) -> bool:
    return str(value).lower() in {"yes", "true", "1"}


def is_anomaly_row(row: Mapping[str, Any]) -> bool:
    return bool(row.get("anomalies", {}).get("exists"))


def passes_anomaly_filter(row: Mapping[str, Any], filters: Sequence[str]) -> bool:
    if not filters:
        return True
    atype = row.get("anomalies", {}).get("type")
    return atype in filters


def build_normal_text(description: str, channels: Sequence[str]) -> str:
    desc = " ".join(description.split())
    return (
        f"{desc} Six-hourly KPI observations — {channel_list_text(channels)}: <ts></ts>."
    )


def build_normal_text(description: str, channels: Sequence[str]) -> str:
    desc = " ".join(description.split())
    return (
        f"{desc} 100ms-interval KPI observations — {channel_list_text(channels)}: <ts></ts>."
    )


def build_anomaly_text(
    description: str, ticket: str, channels: Sequence[str]
) -> str:
    desc = " ".join(description.split())
    ticket_clean = " ".join(str(ticket).split())
    return (
        f"{desc} [Anomaly event] {ticket_clean} KPI observations during the anomaly "
        f"window — {channel_list_text(channels)}: <ts></ts>."
    )


def row_to_record(
    row: Mapping[str, Any],
    index: int,
    record_type: str,
    channels: Sequence[str],
    freq: str,
) -> Dict[str, Any]:
    labels = row["labels"]
    anomalies = row.get("anomalies", {})
    description = str(row.get("description", "")).strip()

    if record_type == "normal":
        text = build_normal_text(description, channels)
        text_source = "dataset_description"
        text_quality = "real"
        anomaly_type = None
    else:
        ticket = anomalies.get("troubleshooting_tickets", "")
        text = build_anomaly_text(description, ticket, channels)
        text_source = "generated_gpt4"
        text_quality = "generated"
        anomaly_type = anomalies.get("type")

    return {
        "text": text,
        "timeseries": build_timeseries(row, channels, freq),
        "record_type": record_type,
        "anomaly_type": anomaly_type,
        "zone": labels.get("zone"),
        "traffic_type": map_traffic_type(str(labels.get("application", ""))),
        "mobility": map_mobility(str(labels.get("mobility", "No"))),
        "congestion": map_congestion(str(labels.get("congestion", "No"))),
        "dataset": "telecom_ts",
        "source": "AliMaatouk/TelecomTS",
        "series_id": f"sample_{index}",
        "task_type": "world_knowledge",
        "text_source": text_source,
        "text_quality": text_quality,
    }


def validate_record(record: Dict[str, Any], window_length: int) -> List[str]:
    errors: List[str] = []
    required = [
        "text",
        "timeseries",
        "record_type",
        "anomaly_type",
        "zone",
        "traffic_type",
        "mobility",
        "congestion",
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

    rtype = record.get("record_type")
    if rtype == "normal":
        if record.get("text_source") != "dataset_description":
            errors.append("normal record must have text_source=dataset_description")
        if record.get("text_quality") != "real":
            errors.append("normal record must have text_quality=real")
        if record.get("anomaly_type") is not None:
            errors.append("normal record must have anomaly_type=null")
    elif rtype == "anomaly":
        if record.get("text_source") != "generated_gpt4":
            errors.append("anomaly record must have text_source=generated_gpt4")
        if record.get("text_quality") != "generated":
            errors.append("anomaly record must have text_quality=generated")
        if not record.get("anomaly_type"):
            errors.append("anomaly record must have anomaly_type set")

    ts_list = record.get("timeseries", [])
    if not ts_list:
        errors.append("timeseries must not be empty")
    else:
        lengths = {len(obj.get("values", [])) for obj in ts_list}
        if len(lengths) != 1:
            errors.append("timeseries value arrays have mismatched lengths")
        elif window_length not in lengths:
            errors.append(f"expected window length {window_length}")

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
        return
    report_path = resolve_path(cfg["output"]["report_path"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def quota_for_types(cfg: Dict[str, Any]) -> Dict[str, Optional[int]]:
    out_cfg = cfg["output"]
    record_types = list(cfg["data"].get("record_types", ["normal", "anomaly"]))
    max_records = out_cfg.get("max_records")

    if max_records is None:
        return {t: None for t in record_types}

    max_records = int(max_records)
    if out_cfg.get("balance_types", True) and len(record_types) > 1:
        per = max_records // len(record_types)
        remainder = max_records % len(record_types)
        quotas = {t: per for t in record_types}
        for i, t in enumerate(record_types[:remainder]):
            quotas[t] += 1
        return quotas

    return {record_types[0]: max_records}


def run_pipeline(cfg: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    data_cfg = cfg["data"]
    channels = list(data_cfg.get("channels", []))
    record_types = set(data_cfg.get("record_types", ["normal", "anomaly"]))
    anomaly_filters = list(data_cfg.get("anomaly_types_filter", []))
    window_length = int(data_cfg.get("window_length", 128))
    freq = str(data_cfg.get("freq", "100ms"))
    quotas = quota_for_types(cfg)

    records: List[Dict[str, Any]] = []
    counts = Counter({"normal": 0, "anomaly": 0})
    anomaly_type_breakdown: Counter = Counter()
    skipped = Counter()
    validation_errors: List[str] = []
    rows_seen = 0

    want_normal = "normal" in record_types and quotas.get("normal", 0) != 0
    want_anomaly = "anomaly" in record_types and quotas.get("anomaly", 0) != 0

    def quota_full(rtype: str) -> bool:
        q = quotas.get(rtype)
        return q is not None and counts[rtype] >= q

    for index, row in enumerate(stream_dataset(cfg)):
        rows_seen += 1
        is_anom = is_anomaly_row(row)

        if is_anom:
            if "anomaly" not in record_types:
                continue
            if not passes_anomaly_filter(row, anomaly_filters):
                skipped["anomaly_type_filter"] += 1
                continue
            if quota_full("anomaly"):
                if quota_full("normal") or not want_normal:
                    break
                continue
            rtype = "anomaly"
        else:
            if "normal" not in record_types:
                continue
            if quota_full("normal"):
                if quota_full("anomaly") or not want_anomaly:
                    break
                continue
            rtype = "normal"

        description = str(row.get("description", "")).strip()
        if not description:
            skipped["missing_description"] += 1
            continue

        if rtype == "anomaly":
            ticket = str(row.get("anomalies", {}).get("troubleshooting_tickets", "")).strip()
            if not ticket:
                skipped["missing_ticket"] += 1
                continue

        try:
            record = row_to_record(row, index, rtype, channels, freq)
        except KeyError as exc:
            skipped["missing_kpi"] += 1
            validation_errors.append(f"sample_{index}: {exc}")
            continue

        errors = validate_record(record, window_length)
        if errors:
            skipped["validation_error"] += 1
            validation_errors.extend(f"sample_{index}: {e}" for e in errors)
            continue

        records.append(record)
        counts[rtype] += 1
        if rtype == "anomaly" and record.get("anomaly_type"):
            anomaly_type_breakdown[record["anomaly_type"]] += 1

        if dry_run and records:
            break

        if quotas.get("normal") is not None and quotas.get("anomaly") is not None:
            if counts["normal"] >= quotas["normal"] and counts["anomaly"] >= quotas["anomaly"]:
                break

    report = {
        "rows_seen": rows_seen,
        "records_written": len(records),
        "records_normal": counts["normal"],
        "records_anomaly": counts["anomaly"],
        "anomaly_type_breakdown": dict(sorted(anomaly_type_breakdown.items())),
        "channel_list": channels,
        "skipped": dict(sorted(skipped.items())),
        "validation_errors": validation_errors[:20],
        "config_snapshot": cfg,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "dry_run": dry_run,
    }

    if dry_run:
        if records:
            print(json.dumps(records[0], indent=2, ensure_ascii=False))
        else:
            print(json.dumps({"error": "no record produced", "report": report}, indent=2))
        return report

    write_output(records, cfg, dry_run=False)
    write_report(report, cfg, dry_run=False)
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build CPT JSONL from TelecomTS (Hugging Face).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print one example record to stdout; do not write files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config, args.set)
    report = run_pipeline(cfg, dry_run=args.dry_run)
    if not args.dry_run:
        print(
            f"Wrote {report['records_written']} records "
            f"({report['records_normal']} normal, {report['records_anomaly']} anomaly).",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
