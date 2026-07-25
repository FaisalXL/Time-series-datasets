#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from the US Drought Monitor (USDM).

One record = **one region section of one weekly USDM release**:
  - text: that section's own prose from the official National Drought Summary
          (the week's "Northeast" paragraphs, "West" paragraphs, ...), taken
          from the structured XML the USDM publishes for every week since 2000.
  - timeseries: **only that region's** drought-category area coverage (D0-D4,
          cumulative % of the region's land area) plus its DSCI, over a
          **trailing window of N weeks ending on the week the section reports**.

This replaces two defects in the retired build:
  1. It paired ONE national narrative with an EXPANDING window running back to
     2000 -- 1,114-1,385 points of mostly-unrelated history per record.
  2. It scraped /data/narrativepdf/, which is under a `Disallow: /data/` prefix
     in the site's robots.txt. Narratives now come from
     /services/data/summary/xml/, which is outside every disallowed prefix.

Alignment is structural: the section's region == the series' region, and the
week the section reports == the window's terminal point, in 100% of records.

Licensing is per-record, on the week's byline: only weeks authored entirely by
federal employees (NOAA/NWS/CPC/USDA/...) go to output.jsonl as
`public-domain-us-gov`. Weeks with any NDMC/DRI/university author are written
to a separate quarantine file as `proprietary-review`.

Examples:
  python scripts/build_cpt_jsonl.py --dry-run
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required. pip install -r requirements.txt") from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402
from usdm_parse import (  # noqa: E402
    REGION_AOI, XML_DIR, parse_week, week_license,
)

CATS = [
    ("d0", "pct_area_d0_abnormally_dry"),
    ("d1", "pct_area_d1_moderate_drought"),
    ("d2", "pct_area_d2_severe_drought"),
    ("d3", "pct_area_d3_extreme_drought"),
    ("d4", "pct_area_d4_exceptional_drought"),
]
DSCI_UNIT = "dsci_drought_severity_coverage_index"
XML_URL = "https://droughtmonitor.unl.edu/services/data/summary/xml/usdm_summary_{d}.xml"


# ----------------------------------------------------------------- config ---
def coerce_value(raw: str) -> Any:
    low = raw.strip().lower()
    if low in ("null", "none", "~"):
        return None
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def deep_merge(base: Dict, over: Dict) -> Dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_set_args(items: Sequence[str]) -> Dict:
    result: Dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid --set value (need key=value): {item}")
        key, raw = item.split("=", 1)
        cursor = result
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = coerce_value(raw)
    return result


def load_config(path: Path, overrides: Sequence[str]) -> Dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    return deep_merge(cfg, parse_set_args(overrides)) if overrides else cfg


def resolve_path(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else ROOT / q


# ------------------------------------------------------------------ series ---
def _mapdate(row: Dict) -> str:
    return (row.get("mapDate") or "")[:10]


def load_series(cache_dir: Path) -> Dict[str, Dict[str, Dict[str, float]]]:
    """{aoi_key: {'YYYY-MM-DD': {d0..d4, dsci}}} for the 6 regions + 'us'."""
    out: Dict[str, Dict[str, Dict[str, float]]] = {}
    for key in list(REGION_AOI.values()) + ["us"]:
        by_date: Dict[str, Dict[str, float]] = {}
        for row in json.loads((cache_dir / f"area_{key}.json").read_text()):
            # The national endpoint returns CONUS and Total; keep CONUS, which
            # is the footprint the narrative covers.
            if key == "us" and row.get("areaOfInterest") != "CONUS":
                continue
            d = by_date.setdefault(_mapdate(row), {})
            for src, _unit in CATS:
                if row.get(src) is not None:
                    d[src] = float(row[src])
        for row in json.loads((cache_dir / f"dsci_{key}.json").read_text()):
            if row.get("dsci") is not None:
                by_date.setdefault(_mapdate(row), {})["dsci"] = float(row["dsci"])
        out[key] = by_date
    return out


def build_window(series: Dict[str, Dict[str, float]], weeks: List[str],
                 end_idx: int, n: int):
    """Trailing n weekly points ending at weeks[end_idx]. None if incomplete."""
    if end_idx + 1 < n:
        return None
    dates = weeks[end_idx + 1 - n: end_idx + 1]
    channels: Dict[str, List[float]] = {src: [] for src, _ in CATS}
    channels["dsci"] = []
    for d in dates:
        row = series.get(d)
        if not row:
            return None
        for src in channels:
            if row.get(src) is None:
                return None
            channels[src].append(row[src])
    return dates, channels


# ------------------------------------------------------------------- build ---
def run_pipeline(cfg: Dict, dry_run: bool = False) -> Dict:
    dcfg, tcfg, ocfg = cfg["data"], cfg["text"], cfg["output"]
    window = int(dcfg["window_weeks"])
    min_chars = int(tcfg["min_text_chars"])
    emit_national = bool(tcfg.get("emit_national_summary", True))
    cache_dir = resolve_path(dcfg["api_cache_dir"])

    series = load_series(cache_dir)
    # Master week axis: the national CONUS series, which every region shares.
    weeks = sorted(series["us"].keys())
    week_index = {d: i for i, d in enumerate(weeks)}

    xml_weeks = sorted(p.stem for p in XML_DIR.glob("*.xml"))
    stats = Counter()
    stats["weeks_in_series"] = len(weeks)
    stats["narratives_cached"] = len(xml_weeks)

    shippable: List[Dict] = []
    quarantine: List[Dict] = []
    max_records = ocfg.get("max_records")

    for d8 in xml_weeks:
        iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        if iso not in week_index:
            stats["skip_week_not_in_series"] += 1
            continue
        idx = week_index[iso]
        if idx + 1 < window:
            stats["skip_window_too_early"] += 1
            continue
        parsed = parse_week(d8)
        if parsed is None:
            stats["skip_parse_failed"] += 1
            continue

        lic, why = week_license(parsed["authors"])
        stats[f"byline_{why}"] += 1
        is_ship = lic == "public-domain-us-gov"
        bucket = shippable if is_ship else quarantine
        emit_lic = lic if is_ship else "proprietary-review"

        # Units for this week: the national summary section, then every region
        # section whose label is an unambiguous official USDM region.
        units = []
        if emit_national and parsed["intro"]:
            units.append(("US", "us", "National Summary", parsed["intro"]))
        elif emit_national:
            stats["week_no_intro"] += 1
        for label, mapped, text in parsed["regions"]:
            if mapped:
                units.append((mapped, REGION_AOI[mapped], label, text))
            else:
                stats["section_unmapped"] += 1

        # A few weeks publish the SAME paragraph under two different <region>
        # elements (e.g. 2020-01-28 repeats the South's prose under Midwest).
        # We cannot tell which region it was written for, so drop every copy --
        # keeping one would pair a region's series with another region's text.
        dup_texts = {t for t, c in Counter(u[3] for u in units).items() if c > 1}

        stats["units_considered"] += len(units)
        seen_regions = set()
        for region_name, aoi, label, text in units:
            if text in dup_texts:
                stats["skip_text_duplicated_across_regions"] += 1
                continue
            if region_name in seen_regions:
                stats["skip_duplicate_region_section"] += 1
                continue
            if len(text) < min_chars:
                stats["skip_short_text"] += 1
                continue
            win = build_window(series[aoi], weeks, idx, window)
            if win is None:
                stats["skip_incomplete_window"] += 1
                continue
            seen_regions.add(region_name)
            dates, channels = win

            ts = [{"values": channels[src], "unit": unit, "freq": "1w"}
                  for src, unit in CATS]
            ts.append({"values": channels["dsci"], "unit": DSCI_UNIT, "freq": "1w"})

            slug = region_name.lower().replace(" ", "_")
            terminal = {src: channels[src][-1] for src in channels}
            prev = {src: channels[src][-2] for src in channels}

            rec = emit_record(
                text=text + "\n\n<ts></ts>",
                timeseries=ts,
                timestamps=dates,
                alignment="describes",
                license=emit_lic,
                text_source="first_party_official",
                text_quality="real",
                source=XML_URL.format(d=d8),
                dataset="usdm_drought",
                series_id=f"usdm_{slug}_{d8}",
                domain="climate",
                region="US",
                period_start=dates[0],
                period_end=dates[-1],
                meta={
                    "usdm_region": region_name,
                    "section_label": label,
                    "aoi_code": aoi,
                    "valid_week": iso,
                    "release_date": parsed["release_date"],
                    "window_weeks": window,
                    "n_points": len(dates),
                    "statistics_type": "cumulative",
                    "terminal_values": terminal,
                    "prev_week_values": prev,
                    "authors": [{"name": n, "affiliation": a}
                                for n, a in parsed["authors"]],
                    "byline_class": why,
                    "license_note": (
                        "first (lead) author is a federal employee; the lead "
                        "byline is taken as authoritative for the whole release"
                        if is_ship else
                        "first (lead) author is non-federal (NDMC/DRI); held "
                        "pending NDMC permission"),
                },
            )
            bucket.append(rec)
            stats["records_shippable" if is_ship else "records_quarantine"] += 1
            if max_records and len(shippable) >= max_records:
                break
        if max_records and len(shippable) >= max_records:
            break

    stats["records_emitted"] = len(shippable)

    # Reconcile: every unit the build looked at is either emitted or dropped for
    # exactly one stated reason. A mismatch means a silent drop.
    drops = ("skip_short_text", "skip_incomplete_window",
             "skip_duplicate_region_section", "skip_text_duplicated_across_regions")
    accounted = len(shippable) + len(quarantine) + sum(stats[k] for k in drops)
    reconcile = {
        "narratives_available": len(xml_weeks),
        "weeks_before_full_window": stats["skip_window_too_early"],
        "weeks_eligible": len(xml_weeks) - stats["skip_window_too_early"],
        "units_considered": stats["units_considered"],
        "emitted_shippable": len(shippable),
        "emitted_quarantine": len(quarantine),
        **{k: stats[k] for k in drops},
        "accounted_for": accounted,
        "balances": accounted == stats["units_considered"],
    }
    if not reconcile["balances"]:
        raise SystemExit(f"reconcile failed: {reconcile}")

    if not dry_run:
        out_path = resolve_path(ocfg["output_path"])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for r in shippable:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        q_path = resolve_path(ocfg["quarantine_path"])
        q_path.parent.mkdir(parents=True, exist_ok=True)
        with q_path.open("w", encoding="utf-8") as fh:
            for r in quarantine:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    lens = [len(r["timeseries"][0]["values"]) for r in shippable]
    tlens = [len(r["text"]) for r in shippable]
    report = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "record_unit": "one region section of one weekly USDM release",
        "window": f"trailing {window} weeks ending on the reported week",
        "stats": dict(sorted(stats.items())),
        "reconcile": reconcile,
        "series_points": {
            "min": min(lens) if lens else 0,
            "median": statistics.median(lens) if lens else 0,
            "max": max(lens) if lens else 0,
        },
        "text_chars": {
            "min": min(tlens) if tlens else 0,
            "median": statistics.median(tlens) if tlens else 0,
            "max": max(tlens) if tlens else 0,
        },
        "regions": dict(Counter(r["meta"]["usdm_region"] for r in shippable)),
        "years": dict(sorted(Counter(r["meta"]["valid_week"][:4]
                                     for r in shippable).items())),
        "config_snapshot": cfg,
        "dry_run": dry_run,
    }
    if not dry_run:
        rp = resolve_path(ocfg["report_path"])
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Build USDM → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    report = run_pipeline(load_config(args.config, args.set), dry_run=args.dry_run)
    print(json.dumps({k: report[k] for k in
                      ("reconcile", "series_points", "text_chars", "regions")},
                     indent=2))


if __name__ == "__main__":
    main()
