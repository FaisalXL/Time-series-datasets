#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from US Drought Monitor (USDM) data.

One record = one weekly USDM release (Tuesday "valid" date):
  - text: the official weekly narrative PDF (national summary + regional breakdown
          + "Looking Ahead" outlook), extracted with pdfplumber.
  - timeseries: the FULL weekly history of drought-category area percentages
          (D0-D4, % of CONUS land area) from the USDM statistics API, from the
          series' common start (the earliest week where every D0-D4 channel has a
          value, ~2000) through the release week. This is an EXPANDING window: it
          grows one week per release, so recent releases carry ~1,300 weekly points.

Text and TS are independent USDM products keyed on the same valid week, so the
alignment is source-native (the narrative discusses the same period/categories the
percentages quantify).

The full CONUS history is fetched once (a single API call) and cached, then each
release slices its expanding window in-memory (common_start -> release week).

Examples:
  python scripts/build_cpt_jsonl.py --config config.example.yaml
  python scripts/build_cpt_jsonl.py --dry-run
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit("requests is required. pip install -r requirements.txt") from exc

try:
    import pdfplumber
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pdfplumber is required. pip install -r requirements.txt") from exc

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required. pip install -r requirements.txt") from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

# shared v1-compliant record builder (self-validates against schema/validate.py --strict)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

# D0-D4 cumulative drought categories → channel unit names.
CATEGORY_UNITS = [
    ("d0", "pct_area_d0_abnormally_dry"),
    ("d1", "pct_area_d1_moderate_drought"),
    ("d2", "pct_area_d2_severe_drought"),
    ("d3", "pct_area_d3_extreme_drought"),
    ("d4", "pct_area_d4_exceptional_drought"),
]


# ---------------------------------------------------------------------------
# Config helpers (same conventions as the other dataset packages)
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
    return raw


def parse_set_args(set_args: Sequence[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for item in set_args:
        if "=" not in item:
            raise ValueError(f"Invalid --set value (need key=value): {item}")
        key, raw = item.split("=", 1)
        cursor = result
        parts = key.split(".")
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
# Date enumeration
# ---------------------------------------------------------------------------


def parse_iso(d: str) -> date:
    return date.fromisoformat(d)


def tuesdays(start: date, end: date) -> List[date]:
    """All Tuesdays in [start, end] inclusive (USDM valid dates)."""
    cur = start + timedelta(days=(1 - start.weekday()) % 7)  # advance to a Tuesday
    out: List[date] = []
    while cur <= end:
        out.append(cur)
        cur += timedelta(weeks=1)
    return out


# ---------------------------------------------------------------------------
# Text: narrative PDF
# ---------------------------------------------------------------------------


def fetch_pdf(session: requests.Session, url: str, cache_file: Path, timeout: int):
    """Return (pdf_bytes, from_cache). pdf_bytes is None on 404/error."""
    if cache_file.exists():
        return cache_file.read_bytes(), True
    try:
        resp = session.get(url, timeout=timeout)
    except requests.RequestException:
        return None, False
    if resp.status_code != 200 or not resp.content:
        return None, False
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(resp.content)
    return resp.content, False


def extract_narrative(pdf_path: Path, strip_authors: bool) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    text = text.replace("\r", "")
    if strip_authors:
        # Drop the trailing "Author(s)\n<names>" credits block.
        text = re.split(r"\n\s*Author\(s\)\s*\n", text)[0]
    # Collapse runs of blank lines, trim trailing whitespace.
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


# ---------------------------------------------------------------------------
# Time series: USDM area-percent API (full weekly history, sliced per release)
# ---------------------------------------------------------------------------


def fetch_full_history(
    session: requests.Session,
    template: str,
    start: date,
    end: date,
    stype: int,
    area: str,
    cache_file: Path,
    timeout: int,
) -> Dict[date, Dict[str, float]]:
    """Fetch the ENTIRE weekly history for `area` in one API call and return a
    {valid_date -> {d0..d4}} map. Cached to a single file so re-runs are instant."""
    if cache_file.exists():
        payload = json.loads(cache_file.read_text())
    else:
        url = template.format(start=start.isoformat(), end=end.isoformat(), stype=stype)
        resp = session.get(url, timeout=timeout, headers={"Accept": "application/json"})
        resp.raise_for_status()
        payload = resp.json()
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(payload))
    hist: Dict[date, Dict[str, float]] = {}
    for r in payload:
        if r.get("areaOfInterest") != area:
            continue
        vd = date.fromisoformat(r["mapDate"][:10])
        hist[vd] = {key: float(r[key]) for key, _ in CATEGORY_UNITS if r.get(key) is not None}
    return hist


def common_start_week(hist: Dict[date, Dict[str, float]]) -> Optional[date]:
    """Earliest week where EVERY D0-D4 channel already has a value."""
    keys = [key for key, _ in CATEGORY_UNITS]
    for wk in sorted(hist):
        if all(k in hist[wk] for k in keys):
            return wk
    return None


def expanding_window(
    hist: Dict[date, Dict[str, float]], common_start: date, valid_date: date
) -> List[date]:
    """All weekly (Tuesday) periods from common_start through valid_date, inclusive.

    Weeks with no USDM row at all are still emitted (as null across every channel)
    so the window is the full contiguous weekly history, not just observed rows."""
    weeks: List[date] = []
    cur = common_start
    while cur <= valid_date:
        weeks.append(cur)
        cur += timedelta(weeks=1)
    return weeks


def build_timeseries(
    hist: Dict[date, Dict[str, float]], weeks: List[date]
) -> List[Dict[str, Any]]:
    """One channel per D0-D4 category, each the value at every week in `weeks`
    (missing -> None). All channels are equal length by construction."""
    series: List[Dict[str, Any]] = []
    for key, unit in CATEGORY_UNITS:
        values = [
            round(hist[wk][key], 2) if wk in hist and key in hist[wk] else None
            for wk in weeks
        ]
        series.append({"values": values, "unit": unit, "freq": "1w"})
    return series


# ---------------------------------------------------------------------------
# Record construction + validation
# ---------------------------------------------------------------------------


def build_record(
    valid_date: date,
    narrative: str,
    hist: Dict[date, Dict[str, float]],
    common_start: date,
    weeks: List[date],
    pdf_url: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    stype = int(cfg["data"]["statistics_type"])
    intro = cfg["text"]["ts_intro_sentence"].format(
        start=common_start.isoformat(), date=valid_date.isoformat()
    )
    text = f"{narrative}\n\n{intro}"
    timeseries = build_timeseries(hist, weeks)
    lengths = {len(ch["values"]) for ch in timeseries}
    assert len(lengths) == 1, f"channel lengths differ: {sorted(lengths)}"
    return emit_record(
        text=text,
        timeseries=timeseries,
        alignment="describes",
        license="unknown",
        text_source="first_party_official",
        source=pdf_url,
        dataset="usdm_drought",
        series_id=f"usdm_{valid_date.isoformat()}",
        domain="climate",
        region="US",
        period_start=common_start.isoformat(),
        period_end=valid_date.isoformat(),
        meta={
            "data_week": valid_date.isoformat(),
            "release_date": valid_date.isoformat(),
            "series_start": common_start.isoformat(),
            "n_points": len(weeks),
            "statistics_type": "cumulative" if stype == 1 else "marginal",
            "area_of_interest": cfg["data"]["area_of_interest"],
            "report_url": pdf_url,
        },
    )


# Per-record validation now lives in emit_record(): each record is self-checked against
# schema/validate.py --strict at construction time, raising ValueError on any violation.


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def write_jsonl(records: List[Dict[str, Any]], path: Path, indent: Optional[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        if indent is None:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        else:
            json.dump(records, fh, ensure_ascii=False, indent=int(indent))
            fh.write("\n")


def run_pipeline(cfg: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    data_cfg = cfg["data"]
    text_cfg = cfg["text"]
    out_cfg = cfg["output"]

    start = parse_iso(data_cfg["start_date"])
    end = parse_iso(data_cfg["end_date"]) if data_cfg.get("end_date") else date.today()
    # Lower bound for the full-history TS fetch (series begins ~2000). The
    # expanding window is anchored at the common_start derived from the fetched
    # history, not at this bound; it only needs to reach back before the series.
    hist_start = parse_iso(data_cfg.get("history_start_date", "2000-01-01"))
    min_points = int(data_cfg.get("min_points", 2))
    stype = int(data_cfg["statistics_type"])
    area = data_cfg["area_of_interest"]
    min_chars = int(text_cfg.get("min_text_chars", 200))
    strip_authors = bool(text_cfg.get("strip_authors", True))
    delay = float(data_cfg.get("request_delay_s", 1.0))
    timeout = int(data_cfg.get("timeout_s", 30))
    max_records = out_cfg.get("max_records")

    pdf_cache = resolve_path(data_cfg["pdf_cache_dir"])
    api_cache = resolve_path(data_cfg["api_cache_dir"])

    session = requests.Session()
    session.headers.update({"User-Agent": "CPTDatasetBuilder/1.0 (+research)"})

    # Fetch the FULL weekly history once (single API call, cached) and anchor the
    # expanding window at the earliest week where every D0-D4 channel has a value.
    hist = fetch_full_history(
        session, data_cfg["ts_api_template"], hist_start, end, stype, area,
        api_cache / f"full_history_{area}.json", timeout,
    )
    common_start = common_start_week(hist)
    if common_start is None:
        raise SystemExit("No week has all D0-D4 channels present; cannot anchor window.")
    print(f"Series common start: {common_start.isoformat()} ({len(hist)} weekly rows)",
          file=sys.stderr)

    dates = tuesdays(start, end)
    stats = {
        "weeks_attempted": 0,
        "records_emitted": 0,
        "skipped_no_pdf": 0,
        "skipped_short_text": 0,
        "skipped_short_window": 0,
        "skipped_validation": 0,
    }
    records: List[Dict[str, Any]] = []
    validation_errors: List[str] = []

    for vd in dates:
        stats["weeks_attempted"] += 1
        ymd = vd.strftime("%Y%m%d")
        label = vd.isoformat()
        pdf_url = data_cfg["narrative_url_template"].format(date=ymd)

        pdf_bytes, pdf_cached = fetch_pdf(
            session, pdf_url, pdf_cache / f"{ymd}.pdf", timeout
        )
        if pdf_bytes is None:
            stats["skipped_no_pdf"] += 1
            if not pdf_cached:
                time.sleep(delay)
            continue

        narrative = extract_narrative(pdf_cache / f"{ymd}.pdf", strip_authors)
        if len(narrative) < min_chars:
            stats["skipped_short_text"] += 1
            print(f"{label}: skipped (short text {len(narrative)} chars)")
            continue

        if vd < common_start:
            stats["skipped_short_window"] += 1
            print(f"{label}: skipped (release before series common start)")
            continue
        weeks = expanding_window(hist, common_start, vd)
        if len(weeks) < min_points:
            stats["skipped_short_window"] += 1
            print(f"{label}: skipped (window {len(weeks)} < min_points {min_points})")
            continue

        try:
            record = build_record(vd, narrative, hist, common_start, weeks, pdf_url, cfg)
        except (ValueError, AssertionError) as exc:
            stats["skipped_validation"] += 1
            validation_errors.append(f"{label}: {exc}")
            continue

        records.append(record)
        stats["records_emitted"] += 1
        print(f"{label}: emitted ({'cached' if pdf_cached else 'fetched'} pdf, {len(weeks)}w TS)")

        if not pdf_cached:
            time.sleep(delay)
        if max_records is not None and len(records) >= int(max_records):
            break

    report = {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "window": "expanding (full weekly D0-D4 history to each release week)",
        "series_start": common_start.isoformat(),
        "statistics_type": "cumulative" if stype == 1 else "marginal",
        "area_of_interest": area,
        "stats": stats,
        "validation_errors": validation_errors[:20],
        "config_snapshot": cfg,
        "dry_run": dry_run,
    }

    if dry_run:
        if records:
            print("\n--- sample record ---")
            print(json.dumps(records[0], ensure_ascii=False, indent=2)[:2000])
        print("\n" + json.dumps({k: report[k] for k in ("stats",)}, indent=2))
        return report

    write_jsonl(records, resolve_path(out_cfg["output_path"]), out_cfg.get("indent"))
    if records and out_cfg.get("samples_path"):
        write_jsonl(records[:5], resolve_path(out_cfg["samples_path"]), 2)
    report_path = resolve_path(out_cfg["report_path"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Build USDM → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[],
                    help="Override config: --set dotted.key=value")
    ap.add_argument("--dry-run", action="store_true")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config, args.set)
    report = run_pipeline(cfg, dry_run=args.dry_run)
    s = report["stats"]
    print(
        f"\nDone: {s['records_emitted']} records "
        f"(no_pdf={s['skipped_no_pdf']}, short_text={s['skipped_short_text']}, "
        f"short_window={s['skipped_short_window']}, invalid={s['skipped_validation']}).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
