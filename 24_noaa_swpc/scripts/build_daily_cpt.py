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
from collections import Counter
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
        # Bounded by the TS side: SGAS text runs 1996→present, but the NGDC annual
        # DGD/DSD index files stop at 2018 (2019+ are 404), so there is no series to
        # pair after that.
        "start_date": "1996-01-01",
        "end_date":   "2019-01-01",
        "request_timeout": 30,
        "retry_delay": 1.0,
        # Trailing window length in days; 32 targets a patch-32 model.
        "window_days": 32,
    },
    "filters": {
        "require_dgd": True,
        "require_dsd": True,
        "min_text_chars": 80,
        "min_ts_channels": 3,
    },
    "text": {},
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
    if s == "*":                 # the archive's other missing marker
        return None
    try:
        v = int(s)
        return None if v == missing else v
    except ValueError:
        return None

_INT_TOKEN_RE = re.compile(r"\*|-?\d+")

# The NGDC archive carries two layouts. 1997+ uses 'YYYY MM DD' with space-separated
# indices; 1996 uses 'DD Mon YY' with hyphen-separated K-indices ('2-0-0-1-2-2-2-2').
# Both mark missing values as either -1 or '*'.
_DGD_NEW_DATE_RE = re.compile(r"^(\d{4})\s+(\d{1,2})\s+(\d{1,2})\s+(.*)$")
_DGD_OLD_DATE_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2})\s+(.*)$")


def split_index_date(line: str) -> tuple[Optional[str], Optional[str], bool]:
    """Split a DGD/DSD data line into (iso_date, remainder, is_old_layout)."""
    m = _DGD_NEW_DATE_RE.match(line)
    if m:
        return (f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}",
                m.group(4), False)
    m = _DGD_OLD_DATE_RE.match(line)
    if m:
        mon = _MONTHS.get(m.group(2).lower())
        if mon:
            yy = int(m.group(3))
            year = 1900 + yy if yy >= 50 else 2000 + yy
            return f"{year}-{mon:02d}-{int(m.group(1)):02d}", m.group(4), True
    return None, None, False


def _dgd_tokens(rest: str, old_layout: bool) -> list[str]:
    """Tokenise the index columns of a DGD line under either layout."""
    if old_layout:
        out: list[str] = []
        for field in rest.split():
            # '2-0-0-1-2-2-2-2' is eight K values, not a negative number.
            out.extend(field.split("-") if not field.startswith("-") else [field])
        return out
    # 1997+ can concatenate -1 markers ('-1-1-1-1'), which split() would fuse.
    return _INT_TOKEN_RE.findall(rest)


def parse_dgd_file(text: str) -> dict:
    """
    Returns {date_str → {fr_a, fr_k, co_a, co_k, pl_a, pl_k}}.
    date_str format: 'YYYY-MM-DD'.
    A-index and K-index values are None where the source has -1 or '*'.

    Handles both archive layouts and treats '*' as a missing marker. An earlier
    version tokenised with a bare int regex, which could not see '*' at all: a row
    with any starred station fell under the token-count floor and was dropped
    whole, losing the two stations that *were* observed (all of 1996, 20 days of
    1997).
    """
    records = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ":")):
            continue
        date_str, rest, old = split_index_date(line)
        if date_str is None:
            continue
        parts = _dgd_tokens(rest, old)
        if len(parts) < 27:              # 3 stations x (1 A-index + 8 K-indices)
            continue
        try:
            records[date_str] = {
                "fr_a": _int_or_none(parts[0]),
                "fr_k": [_int_or_none(parts[1 + i]) for i in range(8)],
                "co_a": _int_or_none(parts[9]),
                "co_k": [_int_or_none(parts[10 + i]) for i in range(8)],
                "pl_a": _int_or_none(parts[18]),
                "pl_k": [_int_or_none(parts[19 + i]) for i in range(8)],
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
    Stanford Mean Field is intentionally skipped (systematic -999 gaps).

    Handles both archive layouts: 1997+ carries 13 index columns, 1996 carries only
    9 (no optical flare counts), and 1996 dates its rows 'DD Mon YY'. Missing values
    are marked -1 or '*'.
    """
    records = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ":")):
            continue
        date_str, rest, _old = split_index_date(line)
        if date_str is None:
            continue
        p = rest.split()
        if len(p) < 9:
            continue

        def col(i: int):
            return p[i] if i < len(p) else "-1"

        try:
            records[date_str] = {
                "radio_flux":    _int_or_none(col(0)),
                "ssn":           _int_or_none(col(1)),
                "sunspot_area":  _int_or_none(col(2)),
                "new_regions":   _int_or_none(col(3)),
                # col(4) = Stanford mean field — skipped (systematic -999 gaps)
                "xray_bkgd_wm2": _parse_xray_bkgd(col(5)),
                "c_flares":      _int_or_none(col(6)),
                "m_flares":      _int_or_none(col(7)),
                "x_flares":      _int_or_none(col(8)),
                "s_flares":      _int_or_none(col(9)),
                "o1_flares":     _int_or_none(col(10)),
                "o2_flares":     _int_or_none(col(11)),
                "o3_flares":     _int_or_none(col(12)),
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

_TITLE_RE = re.compile(r"^JOINT\s+USAF/NOAA", re.IGNORECASE)


def extract_sgas_text(sgas_text: str, obs_date: date) -> Optional[str]:
    """Return the report's own text: its real title block through section F.

    Starts at the source's own 'JOINT USAF/NOAA ...' title line, which is followed by
    the real 'SGAS NUMBER ... ISSUED AT ...' and 'COMPILED FROM DATA RECEIVED AT SWO ON
    ...' datelines, so the record carries the report's authentic header rather than a
    dateline we synthesized. Only the machine-readable `:Product:`/`#` preamble is
    dropped. Falls back to section A when a file has no title line.
    """
    lines = sgas_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _TITLE_RE.match(line.strip()):
            start = i
            break
    if start is None:
        for i, line in enumerate(lines):
            if _SECTION_A_RE.match(line.strip()):
                start = i
                break
    if start is None:
        return None
    return "\n".join(lines[start:]).strip()

# ─── TS builder ───────────────────────────────────────────────────────────

# Daily channels the window is assembled from: (source, key, unit).
# Everything is daily, so a window is uniformly `1d` — the old build mixed a single
# 8-point 3-hourly K-index channel with 15 one-value channels, which is a scalar
# snapshot rather than a series.
WINDOW_CHANNELS = [
    ("dgd", "pl_a",         "a_index_planetary"),
    ("dgd", "fr_a",         "a_index_fredericksburg"),
    ("dgd", "co_a",         "a_index_college"),
    ("dgd", "pl_k_max",     "k_index_planetary_daily_max"),
    ("dgd", "fr_k_max",     "k_index_fredericksburg_daily_max"),
    ("dsd", "radio_flux",   "radio_flux_10_7cm_sfu"),
    ("dsd", "ssn",          "sunspot_number"),
    ("dsd", "sunspot_area", "sunspot_area_millionths_hemis"),
    ("dsd", "new_regions",  "new_sunspot_regions"),
    ("dsd", "xray_bkgd_wm2", "xray_background_flux_wm2"),
    ("dsd", "c_flares",     "c_flare_count"),
    ("dsd", "m_flares",     "m_flare_count"),
    ("dsd", "x_flares",     "x_flare_count"),
    ("dsd", "s_flares",     "optical_s_flare_count"),
    ("dsd", "o1_flares",    "optical_1_flare_count"),
    ("dsd", "o2_flares",    "optical_2_flare_count"),
    ("dsd", "o3_flares",    "optical_3_flare_count"),
]


def daily_value(dgd_data: dict, dsd_data: dict, src: str, key: str,
                day: str) -> Optional[float]:
    """One channel's value on one day, or None when the source has no usable number."""
    row = (dgd_data if src == "dgd" else dsd_data).get(day)
    if row is None:
        return None
    if key.endswith("_k_max"):
        ks = row.get(key[:-4])          # 'pl_k_max' -> 'pl_k'
        if not ks or any(v is None for v in ks):
            return None
        return max(ks)
    return row.get(key)


def build_window_timeseries(dgd_data: dict, dsd_data: dict, obs_date: date,
                            window_days: int) -> tuple[list, list[str]]:
    """Trailing `window_days` of daily indices ENDING at obs_date.

    A channel is emitted only if it is present on EVERY day of the window — no
    imputation, so a partially-observed channel is dropped rather than filled. The
    observation day the SGAS text reports on is always the series' terminal point,
    which is what makes the alignment structural.
    """
    days = [(obs_date - timedelta(days=window_days - 1 - i)).isoformat()
            for i in range(window_days)]
    ts = []
    for src, key, unit in WINDOW_CHANNELS:
        vals = [daily_value(dgd_data, dsd_data, src, key, d) for d in days]
        if any(v is None for v in vals):
            continue
        ts.append({"values": vals, "unit": unit, "freq": "1d"})
    return ts, days


# ─── Terminal-point alignment check ───────────────────────────────────────
#
# SGAS section E recites that day's own indices, e.g.
#   10 CM 181  SSN 141  AFR/AP 026/023   X-RAY BACKGROUND B8.7
# Those are the report's REAL-TIME PRELIMINARY values while DGD/DSD carry the later
# final ones, so they agree often but not always. We measure the agreement rather
# than assume it, and report it per record.

_E_10CM = re.compile(r"\b10\s*CM\s+(\d+)", re.IGNORECASE)
_E_SSN = re.compile(r"\bSSN\s+(\d+)", re.IGNORECASE)
_E_AFRAP = re.compile(r"\bAFR/AP\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)


def terminal_recite_check(text: str, ts: list) -> dict:
    """Compare the values SGAS states for the observation day to the series terminal."""
    term = {c["unit"]: c["values"][-1] for c in ts}
    out: dict = {}

    def cmp(unit: str, stated: Optional[float]) -> None:
        if stated is None or unit not in term:
            return
        out[unit] = {"stated": stated, "series": term[unit],
                     "match": abs(float(stated) - float(term[unit])) < 0.5}

    m = _E_10CM.search(text)
    cmp("radio_flux_10_7cm_sfu", int(m.group(1)) if m else None)
    m = _E_SSN.search(text)
    cmp("sunspot_number", int(m.group(1)) if m else None)
    m = _E_AFRAP.search(text)
    if m:
        cmp("a_index_fredericksburg", int(m.group(1)))
        cmp("a_index_planetary", int(m.group(2)))
    return out

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
    window_days = int(dcfg.get("window_days", 32))

    out_path     = Path(ocfg["output_path"])
    report_path  = Path(ocfg["report_path"])

    for p in (out_path, report_path):
        p.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = "CPT-dataset-builder/1.0 (research)"

    # Load DGD and DSD for all years we might need. obs_date = issue_date - 1, and the
    # series reaches a further `window_days - 1` days back, so a January observation
    # needs the previous year's indices too.
    obs_start = start_date - timedelta(days=window_days)
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
             "skip_few_ts": 0, "skip_invalid": 0, "skip_duplicate_obs": 0}
    validation_errors: list[str] = []
    seen_obs: set[str] = set()
    recite_stats: Counter = Counter()
    channel_hist: Counter = Counter()

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

        # Occasionally two consecutive SGAS issues report the same observation day
        # (e.g. 2004-10-28 and 2004-10-29 both compiled from 27 Oct data). The window
        # is keyed on obs_date, so the second would duplicate the first's series under
        # a duplicate series_id. Keep the earliest issue.
        if obs_str in seen_obs:
            stats["skip_duplicate_obs"] += 1
            issue_date += timedelta(days=1)
            continue

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

        ts, window_days_list = build_window_timeseries(
            dgd_data, dsd_data, obs_date, window_days
        )
        if len(ts) < fcfg["min_ts_channels"]:
            stats["skip_few_ts"] += 1
            issue_date += timedelta(days=1)
            continue

        # Nothing generated: <ts></ts> is appended directly to the real SGAS prose.
        full_text = text_body + "\n\n<ts></ts>"
        recite = terminal_recite_check(text_body, ts)
        for unit, r in recite.items():
            recite_stats[f"{unit}:{'match' if r['match'] else 'drift'}"] += 1

        # The SGAS report narrates the observation day that terminates the window and
        # recites that day's own indices in section E, but says nothing about the 31
        # preceding days the series also carries → "describes", not "recites".
        try:
            record = emit_record(
                text=full_text,
                timeseries=ts,
                timestamps=window_days_list,
                alignment="describes",
                license="public-domain-us-gov",
                text_source="first_party_official",
                source=sgas_url,
                dataset="noaa_swpc",
                series_id=f"noaa_swpc:daily:{obs_str}",
                domain="space_weather",
                region="global",
                period_start=window_days_list[0],
                period_end=obs_str,
                meta={
                    "obs_date":      obs_str,
                    "sgas_issue":    issue_date.isoformat(),
                    "n_ts_channels": len(ts),
                    "window_days":   len(window_days_list),
                    # the observation day IS the series' terminal point
                    "terminal_date": window_days_list[-1],
                    "terminal_recite": recite or None,
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
        seen_obs.add(obs_str)
        channel_hist[len(ts)] += 1
        stats["emitted"] += 1
        if stats["emitted"] % 10 == 0:
            log.info("  emitted %d records …", stats["emitted"])

        issue_date += timedelta(days=1)

    out_f.close()

    # Terminal-point agreement: SGAS section E states the observation day's own
    # real-time preliminary indices; DGD/DSD carry the later final values.
    recite_summary = {}
    for unit in sorted({k.rsplit(":", 1)[0] for k in recite_stats}):
        ok, drift = recite_stats[f"{unit}:match"], recite_stats[f"{unit}:drift"]
        if ok + drift:
            recite_summary[unit] = {
                "stated_in_text": ok + drift, "match": ok, "drift": drift,
                "pct_match": round(100.0 * ok / (ok + drift), 2),
            }

    report = {
        "stats": stats,
        "config": cfg,
        "dgd_rows_loaded": len(dgd_data),
        "dsd_rows_loaded": len(dsd_data),
        "date_range": {"start": dcfg["start_date"], "end": dcfg["end_date"]},
        "window_days": window_days,
        "channels_per_record": {str(k): v for k, v in sorted(channel_hist.items())},
        "terminal_recite": recite_summary,
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
