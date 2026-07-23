#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from BLS CPI monthly press releases + BLS API.

One record per monthly release: BLS HTML narrative (via Internet Archive Wayback
Machine) paired with an EXPANDING window of CPI index values from the BLS
public API (v1, no key required) — the full monthly history of every configured
series from a common start month through that release's data month. The window
grows over time, so recent releases carry hundreds of monthly points.

The window is anchored at `common_start`: the earliest month where every configured
series already has a value (max over series of each series' first month). Interior
gaps become null (JSON null) so all channels stay equal-length; genuine gaps are not
fabricated. Records are dropped only if fewer than `min_points` months are available,
or if no Wayback Machine snapshot is available for that release.

Note: bls.gov blocks automated access; HTML is fetched via the Wayback Machine CDX
API. Effective text coverage is ~2008–2026. Time series data is available 1994+
via the BLS public API.

Examples:
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --set output.max_records=10
  python scripts/build_cpt_jsonl.py --set data.start_year=2010 --set output.max_records=null
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import requests
except ImportError as exc:
    raise SystemExit("requests is required. Install with: pip install -r requirements.txt") from exc

try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML is required. Install with: pip install -r requirements.txt") from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

# shared v1-compliant record builder (self-validates against schema/validate.py --strict)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

CDX_API_URL = "http://web.archive.org/cdx/search/cdx"
BLS_ARCHIVE_BASE = "https://www.bls.gov/news.release/archives/"
BLS_HISTORY_URL = "https://www.bls.gov/news.release/history/"
WAYBACK_BASE = "https://web.archive.org/web"
BLS_API_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"

TXT_FILENAME_RE = re.compile(r"\bcpi_(\d{6}|\d{8})\.txt\b", re.IGNORECASE)

CPI_FILENAME_RE = re.compile(r"cpi_(\d{8})\.htm$", re.IGNORECASE)
WAYBACK_TOOLBAR_RE = re.compile(
    r"<!-- BEGIN WAYBACK TOOLBAR INSERT -->.*?<!-- END WAYBACK TOOLBAR INSERT -->",
    re.DOTALL | re.IGNORECASE,
)

SKIP_STARTS = (
    "table ",
    "note:",
    "source:",
    "http",
    "[1]", "[2]", "[3]",
    "bureau of labor statistics",
    "u.s. bureau of labor",
    "consumer price index—all",
    "consumer price index -",
    "not seasonally adjusted",
    "seasonally adjusted",
    "percent change",
    "unadjusted",
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
# Date helpers
# ---------------------------------------------------------------------------


def previous_month(year: int, month: int) -> Tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def ym_str(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def next_month(year: int, month: int) -> Tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def months_between(start_ym: str, end_ym: str) -> List[str]:
    """Return the inclusive list of 'YYYY-MM' strings from start_ym through end_ym."""
    sy, sm = int(start_ym[:4]), int(start_ym[5:7])
    ey, em = int(end_ym[:4]), int(end_ym[5:7])
    result: List[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        result.append(ym_str(y, m))
        y, m = next_month(y, m)
    return result


# ---------------------------------------------------------------------------
# HTTP fetch + cache
# ---------------------------------------------------------------------------


def fetch_html(
    session: requests.Session,
    url: str,
    cache_file: Path,
    timeout_s: float,
) -> Tuple[Optional[str], bool]:
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8", errors="replace"), True

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        response = session.get(url, timeout=timeout_s)
    except requests.RequestException as exc:
        print(f"  warning: request failed for {url}: {exc}", file=sys.stderr)
        return None, False

    if response.status_code != 200:
        print(f"  warning: HTTP {response.status_code} for {url}", file=sys.stderr)
        return None, False

    html = response.text
    cache_file.write_text(html, encoding="utf-8")
    return html, False


# ---------------------------------------------------------------------------
# Archive discovery via Internet Archive CDX
# ---------------------------------------------------------------------------


def discover_archive_releases(
    session: requests.Session,
    cache_dir: Path,
    timeout_s: float,
    data_start_year: int,
    data_end_year: int,
) -> List[Tuple[str, str, int, int]]:
    """Use Internet Archive CDX to find BLS CPI press release snapshots.

    Returns list of (mmddyyyy, wayback_url, data_year, data_month) sorted oldest first.
    """
    cache_file = cache_dir / "cdx_listing.json"
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            rows = json.load(fh)
    else:
        try:
            resp = session.get(
                CDX_API_URL,
                params={
                    "url": "www.bls.gov/news.release/archives/cpi_",
                    "matchType": "prefix",
                    "output": "json",
                    "fl": "original,timestamp",
                    "sort": "reverse",       # newest snapshot first
                    "collapse": "original",  # one entry per unique URL (most recent)
                    "limit": 2000,
                },
                timeout=timeout_s,
            )
            resp.raise_for_status()
            rows = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(f"CDX query failed: {exc}") from exc

        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with cache_file.open("w", encoding="utf-8") as fh:
            json.dump(rows, fh)

    # rows[0] is the header ["original","timestamp"]; rows[1:] are data
    seen_dates: Dict[str, Tuple[str, str]] = {}  # mmddyyyy → (original, timestamp)
    for row in rows[1:]:
        original, timestamp = row[0], row[1]
        m = CPI_FILENAME_RE.search(original)
        if not m:
            continue
        mmddyyyy = m.group(1).lower()
        # Keep only the first entry we see (most recent snapshot, due to sort=reverse)
        if mmddyyyy not in seen_dates:
            seen_dates[mmddyyyy] = (original, timestamp)

    releases = []
    for mmddyyyy, (original_url, timestamp) in seen_dates.items():
        rel_month = int(mmddyyyy[0:2])
        rel_year = int(mmddyyyy[4:8])
        data_year, data_month = previous_month(rel_year, rel_month)
        if not (data_start_year <= data_year <= data_end_year):
            continue
        wayback_url = f"{WAYBACK_BASE}/{timestamp}/{original_url}"
        releases.append((
            mmddyyyy, wayback_url, data_year, data_month,
            rel_year, rel_month, int(mmddyyyy[2:4]),
        ))

    # Sort by release date ascending
    releases.sort(key=lambda x: (x[4], x[5], x[6]))
    return [(r[0], r[1], r[2], r[3]) for r in releases]


# ---------------------------------------------------------------------------
# TXT release discovery + fetch (1994–2007 historical releases)
# ---------------------------------------------------------------------------


def parse_txt_filename_date(date_str: str) -> Tuple[int, int, int]:
    """Parse MMDDYY (6-char) or MMDDYYYY (8-char) from BLS TXT filenames."""
    if len(date_str) == 6:
        return int(date_str[:2]), int(date_str[2:4]), 1900 + int(date_str[4:])
    return int(date_str[:2]), int(date_str[2:4]), int(date_str[4:])


def discover_txt_releases(
    session: requests.Session,
    cache_dir: Path,
    timeout_s: float,
    data_start_year: int,
    data_end_year: int,
) -> List[Tuple[str, str, Optional[str], int, int]]:
    """Find BLS CPI TXT releases (1994-2007).

    Tries the BLS history directory listing first (works when bls.gov is reachable),
    then falls back to Wayback Machine CDX. Returns list of:
    (date_str, bls_direct_url, wayback_url_or_None, data_year, data_month)
    """
    found: Dict[Tuple[int, int, int], Tuple[str, str, Optional[str], int, int]] = {}

    # 1. Try BLS history directory listing (succeeds on user machines; 403 on servers)
    dir_cache = cache_dir / "txt_directory.html"
    dir_html, _ = fetch_html(session, BLS_HISTORY_URL, dir_cache, timeout_s)
    if dir_html:
        for m in TXT_FILENAME_RE.finditer(dir_html):
            date_str = m.group(1)
            rel_month, rel_day, rel_year = parse_txt_filename_date(date_str)
            data_year, data_month = previous_month(rel_year, rel_month)
            if not (data_start_year <= data_year <= data_end_year):
                continue
            key = (rel_year, rel_month, rel_day)
            bls_url = f"{BLS_HISTORY_URL}cpi_{date_str}.txt"
            found[key] = (date_str, bls_url, None, data_year, data_month)
        print(f"  BLS history listing: {len(found)} TXT files", file=sys.stderr)

    # 2. Wayback CDX for TXT files (supplements or replaces directory listing)
    cdx_cache = cache_dir / "cdx_txt_listing.json"
    if cdx_cache.exists():
        rows = json.loads(cdx_cache.read_text(encoding="utf-8"))
    else:
        try:
            resp = session.get(CDX_API_URL, params={
                "url": "www.bls.gov/news.release/history/cpi_",
                "matchType": "prefix",
                "output": "json",
                "fl": "original,timestamp",
                "sort": "reverse",
                "collapse": "original",
                "limit": 500,
            }, timeout=timeout_s)
            resp.raise_for_status()
            rows = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  warning: CDX TXT query failed: {exc}", file=sys.stderr)
            rows = [[]]
        cdx_cache.parent.mkdir(parents=True, exist_ok=True)
        cdx_cache.write_text(json.dumps(rows), encoding="utf-8")

    for row in rows[1:]:
        original, timestamp = row[0], row[1]
        m = TXT_FILENAME_RE.search(original)
        if not m:
            continue
        date_str = m.group(1)
        rel_month, rel_day, rel_year = parse_txt_filename_date(date_str)
        data_year, data_month = previous_month(rel_year, rel_month)
        if not (data_start_year <= data_year <= data_end_year):
            continue
        key = (rel_year, rel_month, rel_day)
        bls_url = f"{BLS_HISTORY_URL}cpi_{date_str}.txt"
        wayback_url = f"{WAYBACK_BASE}/{timestamp}/{original}"
        if key in found:
            # Augment existing entry with wayback fallback
            existing = found[key]
            found[key] = (existing[0], existing[1], wayback_url, existing[3], existing[4])
        else:
            found[key] = (date_str, bls_url, wayback_url, data_year, data_month)

    releases = sorted(found.values(), key=lambda x: (x[3], x[4]))  # sort by data_year, data_month
    return releases


def fetch_txt(
    session: requests.Session,
    bls_url: str,
    wayback_url: Optional[str],
    cache_file: Path,
    timeout_s: float,
) -> Tuple[Optional[str], bool, str]:
    """Fetch TXT content: try BLS directly first, fall back to Wayback. Returns (text, from_cache, used_url)."""
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8", errors="replace"), True, bls_url

    # Try BLS directly (fast, works when user has bls.gov access)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = session.get(bls_url, timeout=timeout_s)
        if resp.status_code == 200:
            cache_file.write_text(resp.text, encoding="utf-8")
            return resp.text, False, bls_url
    except requests.RequestException:
        pass

    # Fall back to Wayback Machine
    if wayback_url:
        try:
            resp = session.get(wayback_url, timeout=timeout_s)
            if resp.status_code == 200:
                cache_file.write_text(resp.text, encoding="utf-8")
                return resp.text, False, wayback_url
        except requests.RequestException:
            pass

    return None, False, bls_url


# ---------------------------------------------------------------------------
# HTML narrative extraction
# ---------------------------------------------------------------------------


def strip_wayback_wrapper(html: str) -> str:
    """Remove Wayback Machine toolbar injection from archived HTML."""
    html = WAYBACK_TOOLBAR_RE.sub("", html)
    # Strip Wayback URL rewrites so relative URLs resolve correctly
    html = re.sub(r"https?://web\.archive\.org/web/\d+[a-z]*/", "", html)
    return html


NORMALNEWS_RE = re.compile(
    r'class="normalnews"[^>]*>.*?<pre>(.*?)</pre>',
    re.DOTALL | re.IGNORECASE,
)

PRE_LEDE_RE = re.compile(
    r"The Consumer Price Index for All Urban Consumers\s*\(CPI-U\)",
    re.IGNORECASE,
)

LEDE_RE = re.compile(
    r"Consumer prices?\s+for\s+all\s+urban\s+consumers?\s+"
    r"(?:rose|fell|declined|increased|decreased|were unchanged|remained unchanged)",
    re.IGNORECASE,
)


STOP_RE = re.compile(
    r"\bTechnical\s+Notes?\b|Last Modified Date:",
    re.IGNORECASE,
)

# Heuristic: BLS text-formatted table rows use dot leaders — "Label ........ 123.4 456.7"
TABLE_LINE_RE = re.compile(r"\.{3,}\s*[\d\-]|\d+\.\d+\s{2,}\d+\.\d+\s{2,}\d+\.\d+")
# "Table A." / "Table 1." headings signal data tables in pre-formatted text
TABLE_HEADER_RE = re.compile(r"^Table\s+[A-Z0-9][\.\:]", re.IGNORECASE)


class CPIHTMLParser(HTMLParser):
    """Extract narrative paragraphs; skip table content and stop at stop markers."""

    SKIP_TAGS = {"script", "style", "noscript", "pre"}
    # Exclude "li" — it's navigation on BLS pages, not narrative prose
    BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self._table_depth = 0
        self._skip_depth = 0
        self._done = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if self._done:
            return
        if tag == "table":
            self._table_depth += 1
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag in self.BLOCK_TAGS and self._table_depth == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._table_depth = max(0, self._table_depth - 1)
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in self.BLOCK_TAGS and self._table_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._done or self._table_depth > 0 or self._skip_depth > 0:
            return
        if STOP_RE.search(data):
            self._done = True
            return
        self.parts.append(data)

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()


def clean_paragraph(para: str) -> str:
    para = re.sub(r"\s+", " ", para).strip()
    if len(para) < 30:
        return ""
    lowered = para.lower()
    if any(lowered.startswith(pat) for pat in SKIP_STARTS):
        return ""
    return para


def extract_narrative_from_pre(pre_text: str) -> str:
    """Extract CPI narrative from plain-text <pre> content (both old and new BLS format)."""
    # Find CPI-U lede
    lede_match = PRE_LEDE_RE.search(pre_text)
    if not lede_match:
        # Fallback: try alternative phrasing
        lede_match = LEDE_RE.search(pre_text)
    if not lede_match:
        return ""

    # Step back to paragraph start
    para_break = pre_text.rfind("\n\n", 0, lede_match.start())
    start = (para_break + 2) if para_break >= 0 else lede_match.start()
    content = pre_text[start:]

    # Normalize leading spaces (pre-text has lots of whitespace padding)
    lines = []
    for line in content.split("\n"):
        stripped = line.strip()
        lines.append(stripped)
    content = "\n".join(lines)
    content = re.sub(r"\n{3,}", "\n\n", content)

    # Collect paragraphs until first text-formatted table row
    paras = []
    for para in re.split(r"\n\s*\n", content):
        para = para.strip()
        if not para:
            continue
        if TABLE_LINE_RE.search(para) or TABLE_HEADER_RE.match(para):
            break
        if len(para) < 30:
            continue
        paras.append(para)

    return "\n\n".join(paras)


def extract_narrative(html: str) -> str:
    clean_html = strip_wayback_wrapper(html)

    # Primary path: extract from <div class="normalnews"><pre> block (all BLS CPI releases)
    pre_match = NORMALNEWS_RE.search(clean_html)
    if pre_match:
        pre_text = pre_match.group(1)
        # Decode HTML entities
        pre_text = (pre_text
                    .replace("&amp;", "&").replace("&lt;", "<")
                    .replace("&gt;", ">").replace("&nbsp;", " "))
        return extract_narrative_from_pre(pre_text)

    # Fallback: modern HTML <p> parser (in case format changes)
    parser = CPIHTMLParser()
    parser.feed(clean_html)
    raw = parser.get_text()
    lede_match = LEDE_RE.search(raw)
    if lede_match:
        para_break = raw.rfind("\n\n", 0, lede_match.start())
        raw = raw[(para_break + 2) if para_break >= 0 else lede_match.start():]

    paras_raw = [clean_paragraph(p) for p in re.split(r"\n\s*\n+", raw)]
    clean = []
    for p in paras_raw:
        if not p:
            continue
        if TABLE_LINE_RE.search(p):
            break
        clean.append(p)
    return "\n\n".join(clean)


# ---------------------------------------------------------------------------
# BLS API time series
# ---------------------------------------------------------------------------


def fetch_api_chunk(
    session: requests.Session,
    series_ids: List[str],
    start_year: int,
    end_year: int,
    api_url: str,
    cache_file: Path,
    timeout_s: float,
) -> Optional[Dict[str, Any]]:
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            return json.load(fh)

    payload = {
        "seriesid": series_ids,
        "startyear": str(start_year),
        "endyear": str(end_year),
    }
    try:
        response = session.post(api_url, json=payload, timeout=timeout_s)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"  warning: BLS API request failed: {exc}", file=sys.stderr)
        return None

    data = response.json()
    if data.get("status") != "REQUEST_SUCCEEDED":
        print(
            f"  warning: BLS API status={data.get('status')}: {data.get('message')}",
            file=sys.stderr,
        )
        return None

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with cache_file.open("w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data


def load_api_timeseries(
    session: requests.Session,
    series_spec: List[Dict[str, str]],
    start_year: int,
    end_year: int,
    api_url: str,
    cache_dir: Path,
    timeout_s: float,
    delay_s: float,
) -> Dict[str, Dict[str, float]]:
    """Pull and cache all CPI series. Returns {series_id: {"YYYY-MM": float}}."""
    series_ids = [s["id"] for s in series_spec]
    all_data: Dict[str, Dict[str, float]] = {sid: {} for sid in series_ids}

    chunk_size = 10  # BLS API v1 limit: 10 years per query
    year = start_year
    first_call = True
    while year <= end_year:
        chunk_end = min(year + chunk_size - 1, end_year)
        cache_file = cache_dir / f"api_{year}_{chunk_end}.json"

        if not first_call and not cache_file.exists():
            time.sleep(delay_s)
        first_call = False

        chunk = fetch_api_chunk(
            session, series_ids, year, chunk_end, api_url, cache_file, timeout_s
        )
        if chunk:
            for series in chunk.get("Results", {}).get("series", []):
                sid = series["seriesID"]
                for point in series.get("data", []):
                    period = point.get("period", "")
                    if not period.startswith("M") or period == "M13":
                        continue
                    month_num = int(period[1:])
                    pt_year = int(point["year"])
                    ym = ym_str(pt_year, month_num)
                    try:
                        all_data[sid][ym] = float(point["value"])
                    except (ValueError, KeyError):
                        pass
        year = chunk_end + 1

    return all_data


def compute_common_start(
    api_data: Dict[str, Dict[str, float]],
    series_spec: List[Dict[str, str]],
) -> Optional[str]:
    """Earliest 'YYYY-MM' where EVERY configured series already has a value.

    = max over series of each series' first non-null month. Returns None if any series
    is empty (no common anchor exists), which makes the whole build skip.
    """
    firsts: List[str] = []
    for spec in series_spec:
        series_map = api_data.get(spec["id"], {})
        if not series_map:
            return None
        firsts.append(min(series_map))
    return max(firsts) if firsts else None


def build_timeseries_expanding(
    api_data: Dict[str, Dict[str, float]],
    series_spec: List[Dict[str, str]],
    common_start: str,
    data_year: int,
    data_month: int,
    min_points: int,
) -> Optional[Tuple[List[Dict[str, Any]], List[str]]]:
    """Build the expanding-window TS: full monthly history from common_start through the
    release's data month. Each channel spans the same months; missing values become None
    (JSON null) — do NOT fabricate. Returns (channels, window_months) or None if the
    window is shorter than min_points."""
    data_ym = ym_str(data_year, data_month)
    if data_ym < common_start:
        return None
    window_months = months_between(common_start, data_ym)
    if len(window_months) < min_points:
        return None
    channels = []
    for spec in series_spec:
        series_map = api_data.get(spec["id"], {})
        values = [series_map.get(ym) for ym in window_months]  # None -> JSON null
        channels.append({"values": values, "unit": spec["unit"], "freq": "1M"})
    # all channels must share length (equal to the window)
    assert len({len(c["values"]) for c in channels}) == 1, "channel lengths differ"
    return channels, window_months


# ---------------------------------------------------------------------------
# Record construction + validation
# ---------------------------------------------------------------------------


# Per-record validation now lives in emit_record(): each record is self-checked against
# schema/validate.py --strict at construction time, raising ValueError on any violation.


def build_record(
    narrative: str,
    ts_channels: List[Dict[str, Any]],
    window_months: List[str],
    data_year: int,
    data_month: int,
    release_date_iso: str,
    report_url: str,
    ts_intro: str,
    fetch_url: Optional[str] = None,
) -> Dict[str, Any]:
    dm = ym_str(data_year, data_month)
    series_start = window_months[0]
    intro = ts_intro.format(start=series_start, data_month=dm)
    text = f"{narrative}\n\n{intro}"

    # period covered = the expanding window: series_start (first month) through data_month
    period_start = f"{series_start}-01"
    period_end = f"{dm}-01"

    meta: Dict[str, Any] = {
        "data_month": dm,
        "series_start": series_start,
        "n_points": len(window_months),
        "release_date": release_date_iso,
        "report_url": report_url,
    }
    if fetch_url and fetch_url != report_url:
        meta["fetch_url"] = fetch_url

    return emit_record(
        text=text,
        timeseries=ts_channels,
        alignment="describes",
        license="public-domain-us-gov",
        text_source="first_party_official",
        source=report_url,
        dataset="bls_cpi",
        series_id=f"bls_cpi:{dm}",
        domain="macro_econ",
        region="US",
        period_start=period_start,
        period_end=period_end,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_jsonl(records: List[Dict[str, Any]], path: Path, indent: Optional[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if indent is None:
        with path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    else:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=int(indent))
            fh.write("\n")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_pipeline(cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    data_cfg = cfg["data"]
    text_cfg = cfg["text"]
    out_cfg = cfg["output"]

    html_cache_dir = resolve_path(data_cfg.get("html_cache_dir", ".cache/html"))
    api_cache_dir = resolve_path(data_cfg.get("api_cache_dir", ".cache/api"))
    delay_s = float(data_cfg.get("request_delay_s", 1.0))
    timeout_s = float(data_cfg.get("timeout_s", 15))
    start_year = int(data_cfg.get("start_year", 2009))
    end_year = int(data_cfg.get("end_year", 2026))
    min_points = int(data_cfg.get("min_points", 2))
    min_text_chars = int(text_cfg.get("min_text_chars", 200))
    ts_intro = text_cfg["ts_intro_sentence"]
    series_spec = data_cfg["series"]
    max_records = out_cfg.get("max_records")
    api_url = data_cfg.get("api_url", BLS_API_URL)

    include_txt = bool(data_cfg.get("include_txt", False))
    txt_start_year = int(data_cfg.get("txt_start_year", 1994))

    stats = {
        "html_releases_in_cdx": 0,
        "txt_releases_found": 0,
        "releases_attempted": 0,
        "records_emitted": 0,
        "skipped_short_window": 0,
        "skipped_no_text": 0,
        "skipped_short_text": 0,
        "skipped_validation": 0,
    }

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; CPTDatasetBuilder/1.0; research use)"
    })

    # 1. Load BLS API time series (one extra year back for the rolling window)
    api_start = max(1900, min(start_year, txt_start_year) - 1) if include_txt else max(1900, start_year - 1)
    print("Loading BLS API time series data...", file=sys.stderr)
    api_data = load_api_timeseries(
        session, series_spec, api_start, end_year,
        api_url, api_cache_dir, timeout_s, delay_s,
    )
    for spec in series_spec:
        n = len(api_data.get(spec["id"], {}))
        flag = "OK" if n > 0 else "WARNING: 0 values — check series ID"
        print(f"  {spec['id']} ({spec['unit']}): {n} months — {flag}", file=sys.stderr)

    # Expanding-window anchor: earliest month where EVERY configured series has begun.
    common_start = compute_common_start(api_data, series_spec)
    if common_start is None:
        print("  WARNING: no common start month across series — no records will be emitted",
              file=sys.stderr)
    else:
        print(f"  common_start (expanding-window anchor): {common_start}", file=sys.stderr)

    # 2. Discover HTML releases via Wayback CDX (2009–2026)
    print("\nQuerying Wayback CDX for HTML CPI releases...", file=sys.stderr)
    html_releases = discover_archive_releases(
        session, html_cache_dir, timeout_s, start_year, end_year
    )
    stats["html_releases_in_cdx"] = len(html_releases)
    print(f"  Found {len(html_releases)} HTML releases ({start_year}–{end_year})", file=sys.stderr)

    # 3. Optionally discover TXT releases (1994–2007)
    txt_releases_raw: List[Tuple[str, str, Optional[str], int, int]] = []
    if include_txt:
        print("\nDiscovering TXT releases (1994–2007)...", file=sys.stderr)
        txt_releases_raw = discover_txt_releases(
            session, html_cache_dir, timeout_s, txt_start_year, start_year - 1
        )
        stats["txt_releases_found"] = len(txt_releases_raw)
        print(f"  Found {len(txt_releases_raw)} TXT releases ({txt_start_year}–{start_year - 1})", file=sys.stderr)

    # 4. Process TXT releases first (chronologically older)
    records: List[Dict[str, Any]] = []
    print("", file=sys.stderr)

    for date_str, bls_url, wayback_url, data_year, data_month in txt_releases_raw:
        if max_records is not None and len(records) >= int(max_records):
            break

        stats["releases_attempted"] += 1
        dm = ym_str(data_year, data_month)
        label = f"cpi_{date_str}.txt ({dm})"

        built = None if common_start is None else build_timeseries_expanding(
            api_data, series_spec, common_start, data_year, data_month, min_points)
        if built is None:
            stats["skipped_short_window"] += 1
            print(f"{label}: skipped (window shorter than {min_points} points)")
            continue
        ts_channels, window_months = built

        cache_file = html_cache_dir / f"cpi_{date_str}.txt"
        content, from_cache, used_url = fetch_txt(session, bls_url, wayback_url, cache_file, timeout_s)
        if content is None:
            stats["skipped_no_text"] += 1
            print(f"{label}: skipped (not accessible)")
            time.sleep(delay_s)
            continue

        cache_note = "cache" if from_cache else "fetched"
        # TXT content might be Wayback-wrapped HTML or plain text
        if "<html" in content[:200].lower():
            narrative = extract_narrative(content)
        else:
            narrative = extract_narrative_from_pre(content)

        if len(narrative) < min_text_chars:
            stats["skipped_short_text"] += 1
            print(f"{label}: {cache_note}, skipped (short text: {len(narrative)} chars)")
            if not from_cache:
                time.sleep(delay_s)
            continue

        # Reconstruct ISO release date from date_str (MMDDYY or MMDDYYYY)
        rel_month, rel_day, rel_year = parse_txt_filename_date(date_str)
        release_date_iso = f"{rel_year:04d}-{rel_month:02d}-{rel_day:02d}"
        report_url = f"{BLS_HISTORY_URL}cpi_{date_str}.txt"

        try:
            record = build_record(
                narrative, ts_channels, window_months, data_year, data_month,
                release_date_iso, report_url, ts_intro,
                fetch_url=used_url if used_url != report_url else None,
            )
        except ValueError as exc:
            stats["skipped_validation"] += 1
            print(f"{label}: {cache_note}, validation failed: {exc}", file=sys.stderr)
            if not from_cache:
                time.sleep(delay_s)
            continue

        records.append(record)
        stats["records_emitted"] += 1
        print(f"{label}: {cache_note}, emitted ({len(narrative)} chars)")
        if not from_cache:
            time.sleep(delay_s)

    # 5. Process HTML releases (2009–2026)
    for mmddyyyy, wayback_url, data_year, data_month in html_releases:
        if max_records is not None and len(records) >= int(max_records):
            break

        stats["releases_attempted"] += 1
        dm = ym_str(data_year, data_month)
        label = f"cpi_{mmddyyyy} ({dm})"

        built = None if common_start is None else build_timeseries_expanding(
            api_data, series_spec, common_start, data_year, data_month, min_points)
        if built is None:
            stats["skipped_short_window"] += 1
            print(f"{label}: skipped (window shorter than {min_points} points)")
            continue
        ts_channels, window_months = built

        cache_file = html_cache_dir / f"cpi_{mmddyyyy}.html"
        html, from_cache = fetch_html(session, wayback_url, cache_file, timeout_s)
        if html is None:
            stats["skipped_no_text"] += 1
            print(f"{label}: skipped (no HTML from Wayback)")
            time.sleep(delay_s)
            continue

        cache_note = "cache" if from_cache else "fetched"
        narrative = extract_narrative(html)
        if len(narrative) < min_text_chars:
            stats["skipped_short_text"] += 1
            print(f"{label}: {cache_note}, skipped (short text: {len(narrative)} chars)")
            if not from_cache:
                time.sleep(delay_s)
            continue

        release_date_iso = f"{mmddyyyy[4:8]}-{mmddyyyy[0:2]}-{mmddyyyy[2:4]}"
        report_url = f"{BLS_ARCHIVE_BASE}cpi_{mmddyyyy}.htm"
        try:
            record = build_record(
                narrative, ts_channels, window_months, data_year, data_month,
                release_date_iso, report_url, ts_intro,
                fetch_url=wayback_url if wayback_url != report_url else None,
            )
        except ValueError as exc:
            stats["skipped_validation"] += 1
            print(f"{label}: {cache_note}, validation failed: {exc}", file=sys.stderr)
            if not from_cache:
                time.sleep(delay_s)
            continue

        records.append(record)
        stats["records_emitted"] += 1
        print(f"{label}: {cache_note}, emitted ({len(narrative)} chars)")
        if not from_cache:
            time.sleep(delay_s)

    # 4. Write outputs
    output_path = resolve_path(out_cfg["output_path"])
    write_jsonl(records, output_path, out_cfg.get("indent"))

    samples_path = resolve_path(out_cfg["samples_path"])
    write_jsonl(records[:3], samples_path, indent=2)

    report_path = resolve_path(out_cfg["report_path"])
    report = {
        "run_date": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "start_year": start_year,
        "end_year": end_year,
        "window": "expanding (full series history from common_start to each release month)",
        "common_start": common_start,
        "min_points": min_points,
        "series": series_spec,
        **stats,
        "config_snapshot": cfg,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    return report, records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build CPT JSONL from BLS CPI press releases (via Wayback Machine) and BLS API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/build_cpt_jsonl.py\n"
            "  python scripts/build_cpt_jsonl.py --set output.max_records=10\n"
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config, args.set)
    report, records = run_pipeline(cfg)

    print(
        f"\nDone: {report['records_emitted']} records emitted "
        f"({report['releases_attempted']} releases attempted, "
        f"{report['html_releases_in_cdx']} in CDX).",
        file=sys.stderr,
    )
    print(
        f"  skipped: {report['skipped_short_window']} short window, "
        f"{report['skipped_no_text']} no text, "
        f"{report['skipped_short_text']} short text.",
        file=sys.stderr,
    )

    if records:
        print("\n--- First record text field ---\n")
        print(records[0]["text"])


if __name__ == "__main__":
    main()
