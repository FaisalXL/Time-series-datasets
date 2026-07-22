#!/usr/bin/env python3
"""
build_daily_cpt.py  —  NOAA SWPC SGAS + DGD + DSD  →  CPT world-knowledge JSONL

One record per calendar day where all three sources align:
  Text  : Joint USAF/NOAA Solar and Geophysical Activity Summary (SGAS), sections A–F
  TS    : DGD (3-hourly K-indices + A-indices, 3 stations)
          + DSD (solar flux, sunspot number/area, X-ray background, flare counts)

Sources (NGDC archive, no authentication required):
  SGAS  https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/daily_reports/solar_geophysical_activity_summaries/YYYY/MM/yyyymmddSGAS.txt
  DGD   https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/annual_reports/daily_solar_indices_summaries/daily_geomagnetic_data/yyyy_DGD.txt
  DSD   https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/annual_reports/daily_solar_indices_summaries/daily_solar_data/yyyy_DSD.txt
"""

import re
import sys
import json
import time
import copy
import logging
import argparse
from pathlib import Path
from datetime import date, timedelta
from typing import Optional

import requests
import yaml

# shared v1-compliant record builder (self-validates against schema/validate.py --strict)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── URLs ──────────────────────────────────────────────────────────────────

BASE_SGAS = "https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/daily_reports/solar_geophysical_activity_summaries"
BASE_DGD  = "https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/annual_reports/daily_solar_indices_summaries/daily_geomagnetic_data"
BASE_DSD  = "https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/annual_reports/daily_solar_indices_summaries/daily_solar_data"

# ─── Default config ────────────────────────────────────────────────────────

DEFAULT_CFG = {
    "data": {
        "cache_dir": ".cache",
        "start_date": "2000-01-01",
        "end_date":   "2001-01-01",
        "request_timeout": 30,
        "retry_delay": 1.0,
    },
    "filters": {
        "require_dgd": True,
        "require_dsd": True,
        "min_text_chars": 80,
        "min_ts_channels": 3,
    },
    "text": {
        "ts_intro_sentence": (
            "Geomagnetic K-indices (3-hourly intervals), daily A-indices, "
            "and solar measurements for this observation day: <ts></ts>"
        ),
    },
    "output": {
        "output_path":  "output/noaa_swpc_daily_cpt.jsonl",
        "report_path":  "output/run_report_daily.json",
        "max_records":  50,
        "indent":       None,
    },
}

# ─── Config loading ────────────────────────────────────────────────────────

def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result

def load_config(config_path: Optional[str], overrides: list[str]) -> dict:
    cfg = copy.deepcopy(DEFAULT_CFG)
    if config_path:
        with open(config_path) as f:
            cfg = deep_merge(cfg, yaml.safe_load(f) or {})
    for override in overrides:
        key, _, val = override.partition("=")
        parts = key.strip().split(".")
        node = cfg
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        try:
            node[parts[-1]] = json.loads(val)
        except json.JSONDecodeError:
            node[parts[-1]] = val
    return cfg

# ─── HTTP helpers ──────────────────────────────────────────────────────────

def fetch(url: str, cache_path: Path, timeout: int, session: requests.Session,
          retry_delay: float = 1.0) -> Optional[str]:
    if cache_path.exists():
        return cache_path.read_text(errors="replace")
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(resp.content)
            return resp.text
        except requests.RequestException as e:
            if attempt == 2:
                log.warning("Failed %s after 3 attempts: %s", url, e)
                return None
            time.sleep(retry_delay * (attempt + 1))
    return None

# ─── DGD parsing ──────────────────────────────────────────────────────────

def _int_or_none(s: str, missing: int = -1) -> Optional[int]:
    try:
        v = int(s)
        return None if v == missing else v
    except ValueError:
        return None

_INT_TOKEN_RE = re.compile(r"-?\d+")

def parse_dgd_file(text: str) -> dict:
    """
    Returns {date_str → {fr_a, fr_k, co_a, co_k, pl_a, pl_k}}.
    date_str format: 'YYYY-MM-DD'.
    K-index lists contain None for missing values (-1 in source).

    Uses regex tokenisation instead of split() because some DGD lines have
    -1 values concatenated directly to adjacent digits (e.g. '3 2-1 2').
    """
    records = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ":")):
            continue
        parts = _INT_TOKEN_RE.findall(line)
        if len(parts) < 30:
            continue
        try:
            date_str = f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
            records[date_str] = {
                "fr_a": _int_or_none(parts[3]),
                "fr_k": [_int_or_none(parts[4 + i]) for i in range(8)],
                "co_a": _int_or_none(parts[12]),
                "co_k": [_int_or_none(parts[13 + i]) for i in range(8)],
                "pl_a": _int_or_none(parts[21]),
                "pl_k": [_int_or_none(parts[22 + i]) for i in range(8)],
            }
        except (IndexError, ValueError):
            continue
    return records

def load_dgd_for_year(year: int, cache_dir: Path, session: requests.Session,
                      timeout: int, retry_delay: float) -> dict:
    """Download and parse DGD for a given year. Tries annual file, then quarterly."""
    annual_cache = cache_dir / "dgd" / f"{year}_DGD.txt"
    text = fetch(f"{BASE_DGD}/{year}_DGD.txt", annual_cache, timeout, session, retry_delay)
    if text:
        return parse_dgd_file(text)
    combined = {}
    for q in range(1, 5):
        q_cache = cache_dir / "dgd" / f"{year}Q{q}_DGD.txt"
        t = fetch(f"{BASE_DGD}/{year}Q{q}_DGD.txt", q_cache, timeout, session, retry_delay)
        if t:
            combined.update(parse_dgd_file(t))
    return combined

# ─── DSD parsing ──────────────────────────────────────────────────────────

XRAY_EXPONENT = {"A": -8, "B": -7, "C": -6, "M": -5, "X": -4}

def _parse_xray_bkgd(s: str) -> Optional[float]:
    """Convert 'B5.7' → 5.7e-7 W/m²."""
    if not s or s in ("-1", "-999", "####"):
        return None
    letter = s[0].upper()
    exp = XRAY_EXPONENT.get(letter)
    if exp is None:
        return None
    try:
        value = float(s[1:]) * (10 ** exp)
        return round(value, abs(exp) + 2)  # avoid IEEE 754 artifacts like 5.699999e-07
    except ValueError:
        return None

def parse_dsd_file(text: str) -> dict:
    """
    Returns {date_str → {radio_flux, ssn, sunspot_area, new_regions,
                          xray_bkgd_wm2, c_flares, m_flares, x_flares,
                          s_flares, o1_flares, o2_flares, o3_flares}}.
    Stanford Mean Field (column 7) is intentionally skipped (systematic -999 gaps).
    """
    records = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ":")):
            continue
        parts = line.split()
        if len(parts) < 16:
            continue
        try:
            date_str = f"{parts[0]}-{parts[1]}-{parts[2]}"
            records[date_str] = {
                "radio_flux":    _int_or_none(parts[3]),
                "ssn":           _int_or_none(parts[4]),
                "sunspot_area":  _int_or_none(parts[5]),
                "new_regions":   _int_or_none(parts[6]),
                # parts[7] = Stanford field — skipped
                "xray_bkgd_wm2": _parse_xray_bkgd(parts[8]),
                "c_flares":      _int_or_none(parts[9]),
                "m_flares":      _int_or_none(parts[10]),
                "x_flares":      _int_or_none(parts[11]),
                "s_flares":      _int_or_none(parts[12]),
                "o1_flares":     _int_or_none(parts[13]),
                "o2_flares":     _int_or_none(parts[14]),
                "o3_flares":     _int_or_none(parts[15]),
            }
        except (IndexError, ValueError):
            continue
    return records

def load_dsd_for_year(year: int, cache_dir: Path, session: requests.Session,
                      timeout: int, retry_delay: float) -> dict:
    annual_cache = cache_dir / "dsd" / f"{year}_DSD.txt"
    text = fetch(f"{BASE_DSD}/{year}_DSD.txt", annual_cache, timeout, session, retry_delay)
    if text:
        return parse_dsd_file(text)
    combined = {}
    for q in range(1, 5):
        q_cache = cache_dir / "dsd" / f"{year}Q{q}_DSD.txt"
        t = fetch(f"{BASE_DSD}/{year}Q{q}_DSD.txt", q_cache, timeout, session, retry_delay)
        if t:
            combined.update(parse_dsd_file(t))
    return combined

# ─── SGAS parsing ─────────────────────────────────────────────────────────

_SECTION_A_RE = re.compile(r"^A\.\s+", re.IGNORECASE)
_OBS_DATE_RE  = re.compile(
    r"compiled from data received at swo on (\d+)\s+([A-Za-z]{3})",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], 1
)}

def get_obs_date(sgas_text: str, issue_date: date) -> date:
    """Parse 'DATA RECEIVED AT SWO ON DD MON' from SGAS header → observation date."""
    m = _OBS_DATE_RE.search(sgas_text)
    if m:
        day = int(m.group(1))
        mon = _MONTHS.get(m.group(2).lower())
        if mon:
            year = issue_date.year
            # Handle year boundary: Jan issue reporting Dec obs
            if issue_date.month == 1 and mon == 12:
                year -= 1
            try:
                return date(year, mon, day)
            except ValueError:
                pass
    return issue_date - timedelta(days=1)

def extract_sgas_text(sgas_text: str, obs_date: date) -> Optional[str]:
    """Strip header lines; return sections A–F prefixed with date context."""
    lines = sgas_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _SECTION_A_RE.match(line.strip()):
            start = i
            break
    if start is None:
        return None
    body = "\n".join(lines[start:]).strip()
    obs_str = obs_date.strftime("%B %-d, %Y")
    return f"Joint USAF/NOAA Solar and Geophysical Activity Summary for {obs_str}:\n{body}"

# ─── TS builder ───────────────────────────────────────────────────────────

def build_timeseries(dgd: Optional[dict], dsd: Optional[dict]) -> list:
    ts = []

    if dgd:
        for k_key, unit in [
            ("fr_k", "kp_fredericksburg"),
            ("co_k", "kp_college"),
            ("pl_k", "kp_planetary"),
        ]:
            vals = dgd[k_key]
            if vals and all(v is not None for v in vals):
                ts.append({"values": vals, "unit": unit, "freq": "3h"})

        for a_key, unit in [
            ("fr_a", "a_index_fredericksburg"),
            ("co_a", "a_index_college"),
            ("pl_a", "a_index_planetary"),
        ]:
            v = dgd[a_key]
            if v is not None:
                ts.append({"values": [v], "unit": unit, "freq": "1d"})

    if dsd:
        scalar_channels = [
            ("radio_flux",   "radio_flux_10_7cm_sfu"),
            ("ssn",          "sunspot_number"),
            ("sunspot_area", "sunspot_area_millionths_hemis"),
            ("new_regions",  "new_sunspot_regions"),
            ("c_flares",     "c_flare_count"),
            ("m_flares",     "m_flare_count"),
            ("x_flares",     "x_flare_count"),
            ("s_flares",     "optical_s_flare_count"),
            ("o1_flares",    "optical_1_flare_count"),
            ("o2_flares",    "optical_2_flare_count"),
            ("o3_flares",    "optical_3_flare_count"),
        ]
        for key, unit in scalar_channels:
            v = dsd.get(key)
            if v is not None:
                ts.append({"values": [v], "unit": unit, "freq": "1d"})

        xb = dsd.get("xray_bkgd_wm2")
        if xb is not None:
            ts.append({"values": [xb], "unit": "xray_background_flux_wm2", "freq": "1d"})

    return ts

# ─── Main pipeline ────────────────────────────────────────────────────────

def run_pipeline(cfg: dict) -> None:
    dcfg  = cfg["data"]
    fcfg  = cfg["filters"]
    ocfg  = cfg["output"]
    tcfg  = cfg["text"]

    start_date = date.fromisoformat(dcfg["start_date"])
    end_date   = date.fromisoformat(dcfg["end_date"])
    cache_dir  = Path(dcfg["cache_dir"])
    timeout    = dcfg["request_timeout"]
    retry_del  = dcfg["retry_delay"]
    max_recs   = ocfg["max_records"]  # None = unlimited
    ts_intro   = tcfg["ts_intro_sentence"]

    out_path     = Path(ocfg["output_path"])
    report_path  = Path(ocfg["report_path"])

    for p in (out_path, report_path):
        p.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = "CPT-dataset-builder/1.0 (research)"

    # Load DGD and DSD for all years we might need.
    # obs_date = issue_date - 1, so earliest obs is start_date - 1 day.
    obs_start = start_date - timedelta(days=1)
    years_needed = range(obs_start.year, end_date.year + 1)

    log.info("Loading DGD for years %s–%s …", years_needed.start, years_needed.stop - 1)
    dgd_data: dict[str, dict] = {}
    for y in years_needed:
        log.info("  DGD %d", y)
        dgd_data.update(load_dgd_for_year(y, cache_dir, session, timeout, retry_del))

    log.info("Loading DSD for years %s–%s …", years_needed.start, years_needed.stop - 1)
    dsd_data: dict[str, dict] = {}
    for y in years_needed:
        log.info("  DSD %d", y)
        dsd_data.update(load_dsd_for_year(y, cache_dir, session, timeout, retry_del))

    log.info("DGD rows: %d  DSD rows: %d", len(dgd_data), len(dsd_data))

    # Iterate issue dates and emit records.
    stats = {"attempted": 0, "emitted": 0, "skip_no_sgas": 0,
             "skip_text_short": 0, "skip_no_dgd": 0, "skip_no_dsd": 0,
             "skip_few_ts": 0, "skip_invalid": 0}
    validation_errors: list[str] = []

    out_f = out_path.open("w")

    issue_date = start_date
    while issue_date < end_date:
        if max_recs is not None and stats["emitted"] >= max_recs:
            break

        stats["attempted"] += 1
        date_str = issue_date.strftime("%Y%m%d")
        sgas_url   = f"{BASE_SGAS}/{issue_date.year}/{issue_date.month:02d}/{date_str}SGAS.txt"
        sgas_cache = cache_dir / "sgas" / str(issue_date.year) / f"{issue_date.month:02d}" / f"{date_str}SGAS.txt"

        raw = fetch(sgas_url, sgas_cache, timeout, session, retry_del)
        if raw is None:
            stats["skip_no_sgas"] += 1
            issue_date += timedelta(days=1)
            continue

        obs_date = get_obs_date(raw, issue_date)
        obs_str  = obs_date.isoformat()

        text_body = extract_sgas_text(raw, obs_date)
        if not text_body or len(text_body) < fcfg["min_text_chars"]:
            stats["skip_text_short"] += 1
            issue_date += timedelta(days=1)
            continue

        dgd = dgd_data.get(obs_str)
        if fcfg["require_dgd"] and dgd is None:
            stats["skip_no_dgd"] += 1
            issue_date += timedelta(days=1)
            continue

        dsd = dsd_data.get(obs_str)
        if fcfg["require_dsd"] and dsd is None:
            stats["skip_no_dsd"] += 1
            issue_date += timedelta(days=1)
            continue

        ts = build_timeseries(dgd, dsd)
        if len(ts) < fcfg["min_ts_channels"]:
            stats["skip_few_ts"] += 1
            issue_date += timedelta(days=1)
            continue

        full_text = text_body + "\n" + ts_intro

        # SGAS text is a first-party official narrative of the day's space-weather
        # episode (energetic events, geomagnetic/solar activity summary); it does not
        # literally recite the Kp/A-index/flux values that form the series → "describes".
        try:
            record = emit_record(
                text=full_text,
                timeseries=ts,
                alignment="describes",
                license="public-domain-us-gov",
                text_source="first_party_official",
                source=sgas_url,
                dataset="noaa_swpc",
                series_id=f"noaa_swpc:daily:{obs_str}",
                domain="space_weather",
                region="global",
                period_start=obs_str,
                period_end=obs_str,
                meta={
                    "obs_date":      obs_str,
                    "sgas_issue":    issue_date.isoformat(),
                    "n_ts_channels": len(ts),
                },
            )
        except ValueError as exc:
            stats["skip_invalid"] += 1
            validation_errors.append(f"{obs_str}: {exc}")
            issue_date += timedelta(days=1)
            continue

        indent = ocfg["indent"]
        line = json.dumps(record, indent=indent, ensure_ascii=False)
        out_f.write(line + "\n")
        stats["emitted"] += 1
        if stats["emitted"] % 10 == 0:
            log.info("  emitted %d records …", stats["emitted"])

        issue_date += timedelta(days=1)

    out_f.close()

    report = {
        "stats": stats,
        "config": cfg,
        "dgd_rows_loaded": len(dgd_data),
        "dsd_rows_loaded": len(dsd_data),
        "date_range": {"start": dcfg["start_date"], "end": dcfg["end_date"]},
        "validation_errors": validation_errors[:20],
    }
    report_path.write_text(json.dumps(report, indent=2))

    log.info("Done. %d records → %s", stats["emitted"], out_path)
    log.info("Stats: %s", stats)

# ─── CLI ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Build NOAA SWPC daily CPT JSONL")
    parser.add_argument("--config", help="Path to YAML config file")
    parser.add_argument("--set", dest="overrides", action="append", default=[],
                        metavar="KEY=VALUE", help="Override config key (dotted path)")
    args = parser.parse_args()
    cfg = load_config(args.config, args.overrides)
    run_pipeline(cfg)

if __name__ == "__main__":
    main()
