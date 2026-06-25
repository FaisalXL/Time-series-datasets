#!/usr/bin/env python3
"""
build_weekly_cpt.py  —  NOAA SWPC PRF Weekly PDFs  →  CPT world-knowledge JSONL

One record per weekly Preliminary Report and Forecast (PRF):
  Text  : Space Weather Highlights narrative (official NOAA/USAF expert prose)
          Stops at Space Weather Outlook section to avoid forward-looking leakage.
  TS    : Daily Solar Data + Daily Geomagnetic Data extracted from page 2/3 tables.
          All 7 channels have length 7 (one value per day of the week), freq "1d".

Sources: NGDC archive
  PRF PDFs   https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/weekly_reports/PRFs_of_SGD/YYYY/MM/prfXXXX.pdf
  Coverage   1997–present (weekly, ~52/year → ~1,500 total)
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
import fitz      # pymupdf — handles both old and new PDF formats
import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── URLs ──────────────────────────────────────────────────────────────────

BASE_PRF = "https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/weekly_reports/PRFs_of_SGD"

# ─── Default config ────────────────────────────────────────────────────────

DEFAULT_CFG = {
    "data": {
        "cache_dir":       ".cache",
        "year_start":      2000,
        "year_end":        2001,  # exclusive
        "request_timeout": 30,
        "retry_delay":     1.0,
    },
    "filters": {
        "min_text_chars":    200,
        "min_ts_channels":   5,
        "min_solar_rows":    5,
        "min_geo_rows":      5,
    },
    "text": {
        "ts_intro_sentence": (
            "Daily solar flux, sunspot activity, X-ray flux, flare counts, "
            "and geomagnetic indices for each day of this observation week: <ts></ts>"
        ),
    },
    "output": {
        "output_path":  "output/noaa_swpc_weekly_cpt.jsonl",
        "report_path":  "output/run_report_weekly.json",
        "max_records":  5,
        "indent":       None,
    },
}

# ─── Constants ─────────────────────────────────────────────────────────────

MONTHS = {m: i for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], 1
)}
MONTH_PAT = (
    r"January|February|March|April|May|June|"
    r"July|August|September|October|November|December"
)
DATE_ROW_RE = re.compile(rf"\b(\d{{1,2}})\s+({MONTH_PAT})\b", re.IGNORECASE)
K_RE        = re.compile(r"[\d*](?:-[\d*]){7}")
XRAY_EXP    = {"A": -8, "B": -7, "C": -6, "M": -5, "X": -4}

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

def http_get(url: str, timeout: int, session: requests.Session,
             retry_delay: float = 1.0, binary: bool = False) -> Optional[bytes | str]:
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.content if binary else resp.text
        except requests.RequestException as e:
            if attempt == 2:
                log.warning("Failed %s: %s", url, e)
                return None
            time.sleep(retry_delay * (attempt + 1))
    return None

def fetch_pdf(url: str, cache_path: Path, timeout: int,
              session: requests.Session, retry_delay: float) -> Optional[Path]:
    if cache_path.exists():
        return cache_path
    data = http_get(url, timeout, session, retry_delay, binary=True)
    if not data or len(data) < 10_000:  # tiny response = 404 HTML page
        return None
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return cache_path

# ─── Directory discovery ───────────────────────────────────────────────────

def discover_prf_urls_for_year(
    year: int, cache_dir: Path, session: requests.Session,
    timeout: int, retry_delay: float
) -> list[tuple[str, str]]:
    """
    Crawl YYYY/MM/ directory listings. Returns [(pdf_url, filename), ...].
    Result is cached to a manifest file per year.
    """
    manifest = cache_dir / "prf" / f"{year}_manifest.json"
    if manifest.exists():
        return [(u, f) for u, f in json.loads(manifest.read_text())]

    year_url = f"{BASE_PRF}/{year}/"
    html = http_get(year_url, timeout, session, retry_delay)
    if not html:
        return []

    months = re.findall(r'href="(\d{2})/"', html)
    results = []
    for mm in months:
        month_url = f"{year_url}{mm}/"
        mhtml = http_get(month_url, timeout, session, retry_delay)
        if not mhtml:
            continue
        pdfs = re.findall(r'href="(prf\d+\.pdf)"', mhtml, re.IGNORECASE)
        for pdf in pdfs:
            results.append((f"{month_url}{pdf}", pdf))

    if results:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(results))
    return results

# ─── PDF text extraction ───────────────────────────────────────────────────

def pdf_full_text(pdf_path: Path) -> str:
    """Extract and concatenate text from all pages via fitz (pymupdf)."""
    doc = fitz.open(str(pdf_path))
    pages = [doc[i].get_text() for i in range(len(doc))]
    doc.close()
    return "\n".join(pages)

def _clean_page_artifacts(text: str) -> str:
    """Strip repeated page-header/footer lines ('SWPC PRF 1234 11 Apr 2000', lone digit lines)."""
    text = re.sub(r"(?m)^(?:SWPC|SWO) PRF \d+.*$\n?", "", text)
    text = re.sub(r"(?m)^\d+\n",                       "", text)
    return text

def section_text(full: str, header_re: str, stop_re: str) -> str:
    """Return text between two header patterns."""
    s = re.search(header_re, full, re.IGNORECASE)
    if not s:
        return ""
    e = re.search(stop_re, full[s.end():], re.IGNORECASE)
    return full[s.end(): s.end() + e.start()] if e else full[s.end():]

# ─── Week date parsing ─────────────────────────────────────────────────────

def parse_week_end_date(full_text: str) -> Optional[date]:
    """
    Extract the week end date from the PRF header.
    Handles:
      'DD - DD Month YYYY'        (old format, same-month weeks)
      'DD Month - DD Month YYYY'  (new format, any week)
      'DD Month YYYY - DD Month YYYY'  (cross-year edge case)
    Always returns the END date; start = end - 6.
    """
    # Old: "03 - 09 April 2000"
    m = re.search(r"\d{1,2}\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", full_text)
    if not m:
        # New: "08 December - 14 December 2025"
        m = re.search(
            r"\d{1,2}\s+[A-Za-z]+\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", full_text
        )
    if m:
        mon = MONTHS.get(m.group(2).lower()[:3])
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(1)))
            except ValueError:
                pass
    return None

def row_date(day: int, mon: int, end_date: date) -> Optional[date]:
    """Map (day, month) from a table row to a full date within the 7-day week."""
    start_date = end_date - timedelta(days=6)
    for yr in [end_date.year, end_date.year - 1]:
        try:
            d = date(yr, mon, day)
            if start_date <= d <= end_date:
                return d
        except ValueError:
            continue
    return None

# ─── Highlights text extraction ────────────────────────────────────────────

def extract_highlights(full_text: str) -> Optional[str]:
    """
    Extract Space Weather Highlights section.
    Strips everything from 'Space Weather Outlook' onwards (forward-looking content).
    """
    start = re.search(r"Space\s+Weather\s+Highlights", full_text, re.IGNORECASE)
    if not start:
        return None
    end = re.search(r"Space\s+Weather\s+Outlook", full_text[start.end():], re.IGNORECASE)
    raw = (
        full_text[start.end(): start.end() + end.start()]
        if end else full_text[start.end():]
    )
    text = _clean_page_artifacts(raw)
    # Remove leading lines that are date headers (contain a 4-digit year or are very short)
    lines = text.strip().splitlines()
    while lines and (re.search(r"\d{4}", lines[0]) or len(lines[0].strip()) < 10):
        lines.pop(0)
    return "\n".join(lines).strip()

# ─── Solar data parsing ────────────────────────────────────────────────────

def _parse_xray(s: str) -> Optional[float]:
    if not s:
        return None
    letter = s[0].upper()
    exp = XRAY_EXP.get(letter)
    if exp is None:
        return None
    try:
        return round(float(s[1:]) * 10**exp, abs(exp) + 2)
    except ValueError:
        return None

def parse_solar_section(full_text: str, end_date: date) -> list[dict]:
    """
    Parse Daily Solar Data table → list of per-day dicts ordered by date.
    Works for both old (one-line rows) and new (multi-line rows) formats.
    """
    sec = section_text(full_text, r"Daily Solar Data", r"Daily Particle Data")
    if not sec:
        return []

    date_matches = list(DATE_ROW_RE.finditer(sec))
    rows = []
    for i, dm in enumerate(date_matches):
        day = int(dm.group(1))
        mon = MONTHS.get(dm.group(2).lower()[:3])
        if not mon:
            continue
        obs = row_date(day, mon, end_date)
        if not obs:
            continue

        chunk_end = date_matches[i + 1].start() if i + 1 < len(date_matches) else len(sec)
        chunk = sec[dm.end():chunk_end]

        # X-ray background is the anchor: e.g. C1.0, B9.6, A4.5
        xray_m = re.search(r"\b([A-Z]\d+\.\d+)\b", chunk, re.IGNORECASE)
        if not xray_m:
            continue

        # 3 integers immediately before xray token → radio_flux, ssn, area
        before = chunk[: xray_m.start()]
        ints_before = [int(x) for x in re.findall(r"\b\d+\b", before) if len(x) <= 6]
        if len(ints_before) < 3:
            continue
        radio_flux, ssn, area = ints_before[-3], ints_before[-2], ints_before[-1]

        # Integers after xray token → C M X S 1 2 3 [4]
        ints_after = [int(x) for x in re.findall(r"\b\d+\b", chunk[xray_m.end():])]
        c, m, x = (ints_after[k] if len(ints_after) > k else 0 for k in range(3))
        s_v, o1, o2, o3 = (ints_after[k] if len(ints_after) > k else 0 for k in range(3, 7))

        rows.append({
            "obs_date":       obs,
            "radio_flux":     radio_flux,
            "ssn":            ssn,
            "sunspot_area":   area,
            "xray_bkgd_wm2":  _parse_xray(xray_m.group(1)),
            "c_flares":       c,
            "m_flares":       m,
            "x_flares":       x,
            "s_flares":       s_v,
            "o1_flares":      o1,
            "o2_flares":      o2,
            "o3_flares":      o3,
        })
    return sorted(rows, key=lambda r: r["obs_date"])

# ─── Geomagnetic data parsing ──────────────────────────────────────────────

def _kmax(k_str: str) -> Optional[int]:
    vals = [None if v == "*" else int(v) for v in k_str.split("-")]
    valid = [v for v in vals if v is not None]
    return max(valid) if valid else None

def parse_geomag_section(full_text: str, end_date: date) -> list[dict]:
    """
    Parse Daily Geomagnetic Data table → list of per-day dicts.
    Extracts: Planetary A-index, daily max Kp (Planetary) and Fredericksburg equivalents.
    Works for both inline (old) and multi-line (new) formats.
    """
    sec = section_text(
        full_text,
        r"Daily Geomagnetic Data",
        r"Alerts|27.Day|Space Weather Outlook|\Z",
    )
    if not sec:
        return []

    date_matches = list(DATE_ROW_RE.finditer(sec))
    rows = []
    for i, dm in enumerate(date_matches):
        day = int(dm.group(1))
        mon = MONTHS.get(dm.group(2).lower()[:3])
        if not mon:
            continue
        obs = row_date(day, mon, end_date)
        if not obs:
            continue

        chunk_end = date_matches[i + 1].start() if i + 1 < len(date_matches) else len(sec)
        chunk = sec[dm.end():chunk_end]

        k_matches = list(K_RE.finditer(chunk))
        if len(k_matches) < 2:
            continue

        k_pos = [m.start() for m in k_matches]

        # Fredericksburg: first K-string; A-index is integer before it
        fr_k_max = _kmax(k_matches[0].group())
        before_fr = chunk[: k_pos[0]]
        fr_a_m = re.search(r"\d+", before_fr)
        fr_a = int(fr_a_m.group()) if fr_a_m else None

        # Planetary: last K-string; A-index is integer between 2nd-to-last and last K-strings
        pl_k_max = _kmax(k_matches[-1].group())
        between = chunk[k_pos[-2] + len(k_matches[-2].group()): k_pos[-1]]
        pl_a_m = re.search(r"\d+", between)
        pl_a = int(pl_a_m.group()) if pl_a_m else None

        rows.append({
            "obs_date":             obs,
            "fr_a":                 fr_a,
            "fr_kp_max":            fr_k_max,
            "pl_a":                 pl_a,
            "pl_kp_max":            pl_k_max,
        })
    return sorted(rows, key=lambda r: r["obs_date"])

# ─── TS builder ────────────────────────────────────────────────────────────

def build_weekly_timeseries(solar: list, geo: list) -> list:
    """
    Align 7-day solar and geomagnetic data into CPT timeseries channels.
    Each channel has length = number of days with valid data (ideally 7), freq "1d".
    Channels with all-None values are omitted.
    """
    # Index geo by date for O(1) lookup
    geo_by_date = {r["obs_date"]: r for r in geo}
    dates = [r["obs_date"] for r in solar]

    def collect(key, source_list, fallback=None):
        vals = [row.get(key, fallback) for row in source_list]
        return vals if any(v is not None for v in vals) else None

    def geo_collect(key):
        vals = [geo_by_date.get(d, {}).get(key) for d in dates]
        return vals if any(v is not None for v in vals) else None

    ts = []
    solar_channels = [
        ("radio_flux",    "radio_flux_10_7cm_sfu"),
        ("ssn",           "sunspot_number"),
        ("sunspot_area",  "sunspot_area_millionths_hemis"),
        ("c_flares",      "c_flare_count"),
        ("m_flares",      "m_flare_count"),
        ("x_flares",      "x_flare_count"),
        ("s_flares",      "optical_s_flare_count"),
        ("o1_flares",     "optical_1_flare_count"),
        ("o2_flares",     "optical_2_flare_count"),
        ("o3_flares",     "optical_3_flare_count"),
    ]
    for key, unit in solar_channels:
        vals = collect(key, solar)
        if vals:
            ts.append({"values": vals, "unit": unit, "freq": "1d"})

    xray_vals = collect("xray_bkgd_wm2", solar)
    if xray_vals:
        ts.append({"values": xray_vals, "unit": "xray_background_flux_wm2", "freq": "1d"})

    geo_channels = [
        ("pl_a",      "a_index_planetary"),
        ("fr_a",      "a_index_fredericksburg"),
        ("pl_kp_max", "kp_daily_max_planetary"),
        ("fr_kp_max", "kp_daily_max_fredericksburg"),
    ]
    for key, unit in geo_channels:
        vals = geo_collect(key)
        if vals:
            ts.append({"values": vals, "unit": unit, "freq": "1d"})

    return ts

# ─── Main pipeline ────────────────────────────────────────────────────────

def run_pipeline(cfg: dict) -> None:
    dcfg   = cfg["data"]
    fcfg   = cfg["filters"]
    ocfg   = cfg["output"]
    tcfg   = cfg["text"]

    cache_dir   = Path(dcfg["cache_dir"])
    timeout     = dcfg["request_timeout"]
    retry_del   = dcfg["retry_delay"]
    year_start  = dcfg["year_start"]
    year_end    = dcfg["year_end"]
    max_recs    = ocfg["max_records"]
    ts_intro    = tcfg["ts_intro_sentence"]

    out_path     = Path(ocfg["output_path"])
    report_path  = Path(ocfg["report_path"])
    for p in (out_path, report_path):
        p.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = "CPT-dataset-builder/1.0 (research)"

    stats = {
        "attempted": 0, "emitted": 0,
        "skip_download": 0, "skip_no_dates": 0, "skip_no_highlights": 0,
        "skip_text_short": 0, "skip_few_solar": 0, "skip_few_geo": 0,
        "skip_few_ts": 0,
    }

    out_f = out_path.open("w")

    for year in range(year_start, year_end):
        if max_recs is not None and stats["emitted"] >= max_recs:
            break

        log.info("Discovering PRF URLs for %d …", year)
        prf_list = discover_prf_urls_for_year(year, cache_dir, session, timeout, retry_del)
        log.info("  Found %d PRFs", len(prf_list))

        for prf_url, prf_filename in prf_list:
            if max_recs is not None and stats["emitted"] >= max_recs:
                break

            stats["attempted"] += 1
            prf_num = re.search(r"\d+", prf_filename)
            prf_id  = prf_num.group() if prf_num else prf_filename

            pdf_cache = cache_dir / "prf" / str(year) / prf_filename
            pdf_path  = fetch_pdf(prf_url, pdf_cache, timeout, session, retry_del)
            if pdf_path is None:
                stats["skip_download"] += 1
                continue

            try:
                full_text = pdf_full_text(pdf_path)
            except Exception as e:
                log.warning("PDF parse error %s: %s", prf_filename, e)
                stats["skip_download"] += 1
                continue

            end_date = parse_week_end_date(full_text)
            if not end_date:
                stats["skip_no_dates"] += 1
                continue
            start_date = end_date - timedelta(days=6)

            highlights = extract_highlights(full_text)
            if not highlights:
                stats["skip_no_highlights"] += 1
                continue
            if len(highlights) < fcfg["min_text_chars"]:
                stats["skip_text_short"] += 1
                continue

            solar = parse_solar_section(full_text, end_date)
            if len(solar) < fcfg["min_solar_rows"]:
                stats["skip_few_solar"] += 1
                continue

            geo = parse_geomag_section(full_text, end_date)
            if len(geo) < fcfg["min_geo_rows"]:
                stats["skip_few_geo"] += 1
                continue

            ts = build_weekly_timeseries(solar, geo)
            if len(ts) < fcfg["min_ts_channels"]:
                stats["skip_few_ts"] += 1
                continue

            full_text_out = f"Space Weather Highlights for the week of {start_date.strftime('%B %-d')}–{end_date.strftime('%B %-d, %Y')}:\n{highlights}\n{ts_intro}"
            record = {
                "text":          full_text_out,
                "timeseries":    ts,
                "task_type":     "world_knowledge",
                "text_quality":  "real",
                "week_start":    start_date.isoformat(),
                "week_end":      end_date.isoformat(),
                "prf_id":        prf_id,
                "n_ts_channels": len(ts),
                "n_days":        len(solar),
            }

            indent = ocfg["indent"]
            line = json.dumps(record, indent=indent)
            out_f.write(line + "\n")
            stats["emitted"] += 1
            if stats["emitted"] % 10 == 0:
                log.info("  emitted %d records …", stats["emitted"])

    out_f.close()

    report = {"stats": stats, "config": cfg}
    report_path.write_text(json.dumps(report, indent=2))
    log.info("Done. %d records → %s", stats["emitted"], out_path)
    log.info("Stats: %s", stats)

# ─── CLI ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Build NOAA SWPC weekly PRF CPT JSONL")
    parser.add_argument("--config", help="Path to YAML config file")
    parser.add_argument("--set", dest="overrides", action="append", default=[],
                        metavar="KEY=VALUE", help="Override config key (dotted path)")
    args = parser.parse_args()
    cfg = load_config(args.config, args.overrides)
    run_pipeline(cfg)

if __name__ == "__main__":
    main()
