#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from BLS CPI monthly news releases + BLS API.

**One record = (data month x narrative section of that month's release.)**

A CPI news release is natively sectioned and each section is about one group of CPI
indexes, so the release itself supplies the split — `Food`, `Energy`, `All items less
food and energy`, `Not seasonally adjusted CPI measures` from 2009-12 on, and one
paragraph per major expenditure group (food and beverages, housing, transportation,
apparel, medical care, recreation, education and communication, other goods and
services) plus CPI-W and C-CPI-U sections before that. See `cpi_text.py`.

Each section's verbatim prose is paired with a **trailing window of `window_months`
monthly index levels ending on the month that section reports**, covering only the
indexes that section is about (`TOPICS` below). The window ends where the source says
it ends, and its stride is the source's own monthly release cadence.

Run:
  python scripts/build_cpt_jsonl.py                        # full build
  python scripts/build_cpt_jsonl.py --set output.max_records=50
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML is required. Install with: pip install -r requirements.txt") from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT.parent / "schema"))

from cpi_series import SERIES, load_history  # noqa: E402
from cpi_text import (MONTHS, load_text, parse_data_month, sections_of)  # noqa: E402
from emit import emit_record  # noqa: E402

BLS_ARCHIVE = "https://www.bls.gov/news.release/archives/"
BLS_HISTORY = "https://www.bls.gov/news.release/history/"

# ---------------------------------------------------------------------------
# topic -> the CPI indexes that topic's prose is about
# ---------------------------------------------------------------------------
# First entry is the topic anchor: the index the section's opening sentence is about.
# A record is only emitted if its anchor is present for the reported month, so a
# record can never be prose about one index paired with a window of something else.

TOPICS: Dict[str, Dict[str, Any]] = {
    "all_items": {
        "what": "the all items CPI-U",
        "channels": ["all_items_nsa", "all_items_sa", "core_sa", "core_nsa",
                     "food_sa", "energy_sa", "shelter_sa"],
    },
    "cpi_w": {
        "what": "the all items CPI-W",
        "channels": ["cpi_w_nsa", "cpi_w_sa", "all_items_nsa", "all_items_sa"],
    },
    "c_cpi_u": {
        "what": "the chained CPI (C-CPI-U)",
        "channels": ["c_cpi_u_nsa", "all_items_nsa", "all_items_sa"],
    },
    "food": {
        "what": "food prices",
        "channels": ["food_nsa", "food_sa", "food_beverages_sa", "food_at_home_sa",
                     "food_at_home_nsa", "food_away_sa", "food_away_nsa",
                     "cereals_bakery_sa", "meats_eggs_sa", "dairy_sa", "fruits_veg_sa",
                     "nonalc_bev_sa", "other_food_home_sa", "alcoholic_bev_sa",
                     "full_svc_meals_sa"],
    },
    "energy": {
        "what": "energy prices",
        "channels": ["energy_nsa", "energy_sa", "gasoline_sa", "gasoline_nsa",
                     "electricity_sa", "utility_gas_sa", "fuel_oil_sa"],
    },
    "core": {
        "what": "the all items less food and energy index",
        "channels": ["core_nsa", "core_sa", "shelter_sa", "shelter_nsa", "rent_sa",
                     "oer_sa", "lodging_away_sa", "medical_sa", "medical_svcs_sa",
                     "hospital_sa", "apparel_sa", "new_vehicles_sa", "used_cars_sa",
                     "airline_fares_sa", "communication_sa", "recreation_sa",
                     "personal_care_sa", "motor_veh_ins_sa", "household_ops_sa"],
    },
    "nsa": {
        "what": "the not-seasonally-adjusted CPI measures",
        "channels": ["all_items_nsa", "cpi_w_nsa", "c_cpi_u_nsa"],
    },
    "housing": {
        "what": "housing costs",
        "channels": ["housing_nsa", "housing_sa", "shelter_sa", "shelter_nsa", "rent_sa",
                     "oer_sa", "fuels_utilities_sa", "electricity_sa", "utility_gas_sa",
                     "fuel_oil_sa", "household_ops_sa"],
    },
    "transportation": {
        "what": "transportation costs",
        "channels": ["transportation_nsa", "transportation_sa", "new_vehicles_sa",
                     "used_cars_sa", "gasoline_sa", "gasoline_nsa", "public_transp_sa",
                     "airline_fares_sa", "motor_veh_ins_sa"],
    },
    "apparel": {
        "what": "apparel prices",
        "channels": ["apparel_nsa", "apparel_sa"],
    },
    "medical": {
        "what": "medical care costs",
        "channels": ["medical_nsa", "medical_sa", "medical_comm_sa", "medical_svcs_sa",
                     "physicians_sa", "hospital_sa", "prescription_sa"],
    },
    "recreation": {
        "what": "recreation costs",
        "channels": ["recreation_nsa", "recreation_sa"],
    },
    "educ_comm": {
        "what": "education and communication costs",
        "channels": ["educ_comm_nsa", "educ_comm_sa", "education_sa", "communication_sa"],
    },
    "other": {
        "what": "the other goods and services index",
        "channels": ["other_goods_nsa", "other_goods_sa", "personal_care_sa", "tobacco_sa"],
    },
    "annual_review": {
        "what": "the calendar year just ended",
        "channels": ["all_items_nsa", "all_items_sa", "core_nsa", "core_sa", "food_nsa",
                     "energy_nsa", "gasoline_nsa", "shelter_nsa"],
    },
}

EXT_PREF = ("txt", "htm", "pdf")


# ---------------------------------------------------------------------------
# config
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
    low = raw.strip().lower()
    if low in {"true", "yes"}:
        return True
    if low in {"false", "no"}:
        return False
    if low in {"null", "none", "~"}:
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    return raw


def load_config(path: Path, overrides: Sequence[str]) -> Dict[str, Any]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"Invalid --set value (need key=value): {item}")
        key, raw = item.split("=", 1)
        parts, cursor = key.split("."), {}
        node = cursor
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = coerce_value(raw)
        cfg = deep_merge(cfg, cursor)
    return cfg


def resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else ROOT / path


# ---------------------------------------------------------------------------
# months
# ---------------------------------------------------------------------------


def month_window(end_ym: str, n: int) -> List[str]:
    """The n months ending at end_ym, oldest first."""
    y, m = int(end_ym[:4]), int(end_ym[5:7])
    out: List[str] = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def pretty_month(ym: str) -> str:
    return f"{MONTHS[int(ym[5:7]) - 1].capitalize()} {ym[:4]}"


# ---------------------------------------------------------------------------
# recite detection
# ---------------------------------------------------------------------------

# BLS printed index levels to 1 decimal before the 1998 rebasing of many series and to
# 3 decimals after, and the API echoes whichever form was published.
def level_forms(value: float) -> List[str]:
    forms = {f"{value:.3f}", f"{value:.2f}", f"{value:.1f}"}
    return [f for f in forms if f]


def recited_channels(text: str, channels: List[Dict[str, Any]],
                     keys: List[str]) -> List[str]:
    """Channels whose terminal (reported-month) level appears verbatim in the prose."""
    hits = []
    for key, ch in zip(keys, channels):
        end = ch["values"][-1]
        if end is None:
            continue
        if any(re.search(r"(?<![\d.])" + re.escape(f) + r"(?![\d])", text)
               for f in level_forms(end)):
            hits.append(key)
    return hits


# ---------------------------------------------------------------------------
# build one record
# ---------------------------------------------------------------------------


def build_channels(hist, keys: List[str], window: List[str], min_coverage: float
                   ) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Drop a channel unless it covers the reported month and enough of the window.

    Several component indexes begin mid-window (communication 1998-01, personal care
    1999-01, C-CPI-U 1999-12), and an all-but-empty channel is noise rather than a
    series, so it is dropped and the ts-intro names only what survives.
    """
    kept_keys, kept = [], []
    for key in keys:
        series = hist.get(key) or {}
        if series.get(window[-1]) is None:
            continue
        values = [series.get(ym) for ym in window]
        if sum(v is not None for v in values) / len(values) < min_coverage:
            continue
        kept_keys.append(key)
        kept.append({"values": values, "unit": key, "freq": "1M"})
    return kept_keys, kept


def ts_sentence(keys: List[str], window: List[str]) -> str:
    """Name exactly the channels this record carries, in order, with each one's
    adjustment basis — the record mixes seasonally adjusted and not-seasonally-adjusted
    indexes and the distinction is the whole point of several sections."""
    labels = [SERIES[k][1] + (" (not seasonally adjusted)" if k.endswith("_nsa") else "")
              for k in keys]
    joined = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    return (f"Monthly U.S. city average CPI index levels, seasonally adjusted unless "
            f"noted, for {joined} over the {len(window)} months through "
            f"{pretty_month(window[-1])}: <ts></ts>")


def build_record(*, topic: str, prose: str, dm: str, era: str, release_date: str,
                 report_url: str, fetch_url: Optional[str], hist,
                 window_months: int, min_coverage: float) -> Optional[Dict[str, Any]]:
    keys = TOPICS[topic]["channels"]
    window = month_window(dm, window_months)
    kept_keys, channels = build_channels(hist, keys, window, min_coverage)
    if not channels or kept_keys[0] != keys[0]:
        return None                      # anchor index missing -> not this topic's series
    text = f"{prose}\n\n{ts_sentence(kept_keys, window)}"
    recites = recited_channels(prose, channels, kept_keys)
    meta = {
        "data_month": dm,
        "topic": topic,
        "release_format": era,
        "release_date": release_date,
        "window_months": len(window),
        "window_start": window[0],
        "n_channels": len(channels),
        "channels": kept_keys,
        "bls_series_ids": [SERIES[k][0] for k in kept_keys],
        "recited_channels": recites,
        "report_url": report_url,
    }
    if fetch_url:
        meta["fetch_url"] = fetch_url
    return emit_record(
        text=text,
        timeseries=channels,
        alignment="recites" if recites else "describes",
        license="public-domain-us-gov",
        text_source="first_party_official",
        source=report_url,
        dataset="bls_cpi",
        series_id=f"bls_cpi:{dm}:{topic}",
        domain="macro_econ",
        region="US",
        period_start=f"{window[0]}-01",
        period_end=f"{dm}-01",
        meta=meta,
    )


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


TERMINAL_PUNCT = (".", ")", '"', "]", "!", "?")


def ends_cleanly(prose: str) -> bool:
    """A section cut mid-sentence is a damaged capture, not a short section."""
    return prose.rstrip().endswith(TERMINAL_PUNCT)


def choose_document(rel_dir: Path, date: str):
    """Pick the format that parses best, not just the first one on disk.

    Some archived `.txt` captures are themselves truncated mid-paragraph (13 releases,
    e.g. 2002-06-18 stops at "...alcoholic beverages--each increased 0.2 percent,"),
    and the same release's PDF is complete. Scoring the candidates by how many
    sections come out whole recovers those instead of shipping a cut sentence.
    """
    best = None
    for ext in EXT_PREF:
        p = rel_dir / f"{date}.{ext}"
        if not (p.exists() and p.stat().st_size > 0):
            continue
        text = load_text(p)
        dm = parse_data_month(text)
        if not dm:
            continue
        era, sections, _ = sections_of(text)
        if not sections:
            continue
        whole = sum(1 for v in sections.values() if ends_cleanly(v))
        # No character-count term: a longer PDF extraction usually means table text
        # leaked in, not that more narrative was recovered. Ties keep the earlier
        # (more reliable) format because the comparison is strict.
        score = (whole, len(sections))
        if best is None or score > best[0]:
            best = (score, p, text, dm, era, sections)
    return best


def report_url_for(date: str, fetch_log: Dict[str, Any], ext: str) -> str:
    info = (fetch_log.get(date) or {}).get(ext.lstrip("."), {})
    fname = info.get("fname")
    if fname:
        base = BLS_HISTORY if ext == ".txt" else BLS_ARCHIVE
        return f"{base}cpi_{fname}{ext}"
    return f"{BLS_ARCHIVE}cpi_{date}{ext}"


def run_pipeline(cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    dcfg, tcfg, ocfg = cfg["data"], cfg["text"], cfg["output"]
    rel_dir = resolve_path(dcfg["releases_dir"])
    api_dir = resolve_path(dcfg["api_cache_dir"])
    window_months = int(dcfg["window_months"])
    min_coverage = float(dcfg["min_channel_coverage"])
    api_start = int(dcfg["api_start_year"])
    api_end = int(dcfg["api_end_year"])
    min_chars = int(tcfg["min_text_chars"])
    max_records = ocfg.get("max_records")

    print("Loading BLS API history (cached)...", file=sys.stderr)
    hist = load_history(api_dir, api_start, api_end,
                        allow_network=bool(dcfg.get("allow_network", True)))
    empty = [k for k in SERIES if not hist.get(k)]
    if empty:
        raise SystemExit(f"series returned no data: {empty}")

    fetch_log_path = resolve_path(dcfg["fetch_log"])
    fetch_log = json.loads(fetch_log_path.read_text()) if fetch_log_path.exists() else {}

    dates = sorted({p.stem for p in rel_dir.iterdir()
                    if p.suffix in (".txt", ".htm", ".pdf")})
    stats = Counter()
    stats["release_documents"] = len(dates)
    records: List[Dict[str, Any]] = []
    seen_dm: Dict[str, str] = {}
    era_of: Dict[str, str] = {}

    for date in dates:
        if max_records is not None and len(records) >= int(max_records):
            break
        chosen = choose_document(rel_dir, date)
        if chosen is None:
            stats["skip_unparseable_release"] += 1
            print(f"{date}: skipped (no format yielded a data month + sections)",
                  file=sys.stderr)
            continue
        _, path, text, dm, era, sections = chosen
        if dm in seen_dm:
            stats["skip_duplicate_data_month"] += 1
            print(f"{date}: skipped (data month {dm} already from {seen_dm[dm]})",
                  file=sys.stderr)
            continue
        seen_dm[dm] = date
        era_of[dm] = era
        stats[f"releases_{era}"] += 1
        stats[f"source_format_{path.suffix.lstrip('.')}"] += 1
        report_url = report_url_for(date, fetch_log, path.suffix)
        info = (fetch_log.get(date) or {}).get(path.suffix.lstrip("."), {})
        fetch_url = (f"https://web.archive.org/web/{info['ts']}/{info['orig']}"
                     if info.get("ts") else None)

        for topic, prose in sections.items():
            stats["sections_found"] += 1
            if topic not in TOPICS:
                stats["skip_unknown_topic"] += 1
                continue
            if not ends_cleanly(prose):
                stats["skip_truncated_prose"] += 1
                continue
            if len(prose) < min_chars:
                stats["skip_short_text"] += 1
                continue
            try:
                record = build_record(
                    topic=topic, prose=prose, dm=dm, era=era,
                    release_date=date, report_url=report_url, fetch_url=fetch_url,
                    hist=hist, window_months=window_months, min_coverage=min_coverage)
            except ValueError as exc:
                stats["skip_validation"] += 1
                print(f"{dm}/{topic}: validation failed: {exc}", file=sys.stderr)
                continue
            if record is None:
                stats["skip_no_anchor_series"] += 1
                continue
            records.append(record)
            stats["records_emitted"] += 1

    balance = (stats["records_emitted"] + stats["skip_short_text"]
               + stats["skip_unknown_topic"] + stats["skip_no_anchor_series"]
               + stats["skip_validation"] + stats["skip_truncated_prose"])
    if max_records is None and balance != stats["sections_found"]:
        raise SystemExit(f"reconcile failed: {stats['sections_found']} sections found but "
                         f"{balance} accounted for ({dict(stats)})")

    out_path = resolve_path(ocfg["output_path"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    samples = resolve_path(ocfg["samples_path"])
    samples.parent.mkdir(parents=True, exist_ok=True)
    samples.write_text(json.dumps(records[:3], ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")

    n_pts = [len(r["timeseries"][0]["values"]) for r in records]
    n_ch = [len(r["timeseries"]) for r in records]
    report = {
        "run_date": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "record_unit": "one narrative section of one monthly CPI release",
        "window": f"trailing {window_months} months ending on the reported data month",
        "data_months": sorted(seen_dm),
        "data_month_span": [min(seen_dm), max(seen_dm)] if seen_dm else None,
        "n_data_months": len(seen_dm),
        "topics": sorted({r["meta"]["topic"] for r in records}),
        "records_by_topic": dict(Counter(r["meta"]["topic"] for r in records).most_common()),
        "records_by_format": dict(Counter(r["meta"]["release_format"] for r in records)),
        "alignment_tiers": dict(Counter(r["alignment"] for r in records)),
        "timesteps": sum(n_pts),
        "datapoints": sum(len(c["values"]) for r in records for c in r["timeseries"]),
        "channels_min_med_max": [min(n_ch), sorted(n_ch)[len(n_ch) // 2], max(n_ch)] if n_ch else None,
        **dict(stats),
        "config_snapshot": cfg,
    }
    rep_path = resolve_path(ocfg["report_path"])
    rep_path.parent.mkdir(parents=True, exist_ok=True)
    rep_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return report, records


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    report, records = run_pipeline(load_config(args.config, args.set))
    print(f"\nDone: {report['records_emitted']} records from "
          f"{report['n_data_months']} data months "
          f"({report['data_month_span'][0]}..{report['data_month_span'][1]}).",
          file=sys.stderr)
    print(f"  by topic: {report['records_by_topic']}", file=sys.stderr)
    print(f"  tiers: {report['alignment_tiers']}", file=sys.stderr)
    print(f"  timesteps {report['timesteps']:,}  datapoints {report['datapoints']:,}",
          file=sys.stderr)
    print(f"  skips: " + ", ".join(f"{k}={v}" for k, v in report.items()
                                   if k.startswith("skip_")), file=sys.stderr)


if __name__ == "__main__":
    main()
