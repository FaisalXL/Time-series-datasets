#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from CDC FluView weekly reports + surveillance series.

PER-TOPIC national records. Each weekly FluView report is split into up to four
topic records, each pairing the report paragraph(s) about ONE surveillance topic
with ONLY that topic's national season-to-date time series (an expanding window
from MMWR week 40 through the report week). So the text of every record tightly
describes the exact series attached to it — not one national bundle re-sliced.

Topics
  ili            outpatient influenza-like illness (ILINet)              -> local ILINet.csv
  lab_composition virus typing / positivity (NREVSS clinical + PHL)      -> local NREVSS CSVs
  hospitalization FluSurv-NET lab-confirmed hospitalization rates        -> Socrata kvib-3txy
  mortality      NCHS % of deaths due to influenza                       -> Socrata 4bc2-bbpq

Text is REAL CDC report narrative only — the paragraph/sentence(s) that describe
each topic, extracted by keyword anchors. A topic record is emitted ONLY when BOTH
the real narrative for that topic AND a real series for it are available; otherwise
the topic is dropped for that week (and counted). Alignment = "describes" (text cites the
week's as-published figures; series carry CDC's current revised values — minor drift).

Examples:
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --config config.example.yaml
  python scripts/build_cpt_jsonl.py --set data.seasons=[2024-2025]
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    import requests
except ImportError as exc:
    raise SystemExit(
        "requests is required. Install with: pip install -r requirements.txt"
    ) from exc

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required. Install with: pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

# shared v1-compliant record builder (self-validates against schema/validate.py --strict)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

# Socrata is fetched with urllib; some corporate/proxied hosts present certs that
# fail strict verification, so mirror the sibling packages' relaxed SSL context.
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

PATTERN_A_SEASONS = {
    "2015-2016",
    "2016-2017",
    "2017-2018",
    "2018-2019",
    "2019-2020",
    "2021-2022",
    "2023-2024",
}

CSV_FILES = {
    "ilin": "ILINet.csv",
    "clinical": "ICL_NREVSS_Clinical_Labs.csv",
    "public_health": "ICL_NREVSS_Public_Health_Labs.csv",
}

# --- per-topic channel specs ------------------------------------------------
# Each topic becomes its OWN record carrying ONLY these channels over the
# season-to-date window. Channel `unit` labels are snake_case and distinct
# within a record (strict validation rejects repeated units).

TOPIC_ILI = "ili"
TOPIC_LAB = "lab_composition"
TOPIC_HOSP = "hospitalization"
TOPIC_MORT = "mortality"

# ILINet outpatient illness: (csv_table, column, unit_label)
ILI_SPEC: Sequence[Tuple[str, str, str]] = (
    ("ilin", "% WEIGHTED ILI", "ili_pct_weighted"),
    ("ilin", "ILITOTAL", "ili_total_visits"),
    ("ilin", "AGE 0-4", "ili_age_0_4"),
    ("ilin", "AGE 5-24", "ili_age_5_24"),
    ("ilin", "AGE 25-49", "ili_age_25_49"),
    ("ilin", "AGE 50-64", "ili_age_50_64"),
    ("ilin", "AGE 65", "ili_age_65_plus"),
)

# Virus typing / positivity: clinical-lab percentages + public-health-lab subtype counts
LAB_SPEC: Sequence[Tuple[str, str, str]] = (
    ("clinical", "PERCENT POSITIVE", "clinical_pct_positive"),
    ("clinical", "PERCENT A", "clinical_pct_A"),
    ("clinical", "PERCENT B", "clinical_pct_B"),
    ("public_health", "A (2009 H1N1)", "phl_A_H1N1pdm09_count"),
    ("public_health", "A (H3)", "phl_A_H3_count"),
    ("public_health", "B", "phl_B_count"),
    ("public_health", "BVic", "phl_B_Victoria_count"),
    ("public_health", "BYam", "phl_B_Yamagata_count"),
    ("public_health", "A (H5)", "phl_A_H5_count"),
)

# FluSurv-NET hospitalization rates: (data_type, age_category, unit_label)
HOSP_SPEC: Sequence[Tuple[str, str, str]] = (
    ("Weekly Rate", "Overall", "flusurv_weekly_rate_per_100k"),
    ("Cumulative Rate", "Overall", "flusurv_cumulative_rate_per_100k"),
    ("Cumulative Rate", "0-4 yr", "flusurv_cum_rate_age_0_4"),
    ("Cumulative Rate", "5-17 yr", "flusurv_cum_rate_age_5_17"),
    ("Cumulative Rate", "18-49 yr", "flusurv_cum_rate_age_18_49"),
    ("Cumulative Rate", "50-64 yr", "flusurv_cum_rate_age_50_64"),
    ("Cumulative Rate", "65+ yr", "flusurv_cum_rate_age_65_plus"),
)

# NCHS mortality: single channel (percent of deaths due to influenza)
MORT_PATHOGEN = "Influenza"
MORT_LABEL = "nchs_pct_deaths_due_to_influenza"

SKIP_PHRASES = (
    "view larger",
    "view chart data",
    "show more",
    "surveillance methods|",
    "additional information about",
    "all data in this report are preliminary",
)

WEEK_ENDING_RE = re.compile(
    r"(?:ending|week ending)\s+([A-Za-z]+ \d{1,2}, \d{4})",
    re.IGNORECASE,
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
# Season / URL helpers
# ---------------------------------------------------------------------------


def parse_season(season: str) -> int:
    match = re.fullmatch(r"(\d{4})-(\d{4})", season.strip())
    if not match:
        raise ValueError(f"Invalid season format: {season}")
    start = int(match.group(1))
    end = int(match.group(2))
    if end != start + 1:
        raise ValueError(f"Season end year must be start+1: {season}")
    return start


def season_for_week(year: int, week: int) -> str:
    start = year if week >= 40 else year - 1
    return f"{start}-{start + 1}"


def season_short(season: str) -> str:
    """'2024-2025' -> '2024-25' (the form FluSurv-NET / RSV-NET Socrata use)."""
    start = parse_season(season)
    return f"{start}-{(start + 1) % 100:02d}"


def mmwr_week_ending(year: int, week: int) -> str:
    """Saturday date on which MMWR (epidemiological) `week` of `year` ends.

    MMWR week 1 ends on the first Saturday of the year that has at least four
    days in January; weeks run Sunday–Saturday. Used to give the record real
    ISO-8601 `period_start` / `period_end` dates for the season-to-date window,
    and to align FluSurv-NET / NCHS week-ending dates (also MMWR Saturdays) to
    the ILINet (year, week) grid.
    """
    jan1 = date(year, 1, 1)
    # Saturday of the week containing Jan 1 (weekday(): Mon=0 .. Sun=6).
    first_saturday = jan1 + timedelta(days=(5 - jan1.weekday()) % 7)
    # If that first Saturday leaves < 4 days of January in its week, MMWR week 1
    # is the following week.
    if first_saturday.day < 4:
        first_saturday += timedelta(days=7)
    return (first_saturday + timedelta(weeks=week - 1)).isoformat()


def weeks_for_season(season: str, available: set[Tuple[int, int]]) -> List[Tuple[int, int]]:
    weeks = [
        (year, week)
        for year, week in sorted(available)
        if season_for_week(year, week) == season
    ]
    return weeks


def report_urls(season: str, year: int, week: int) -> List[str]:
    week_str = f"{week:02d}"
    archive = (
        f"https://www.cdc.gov/flu/weekly/weeklyarchives{season}/week{week_str}.htm"
    )
    modern = f"https://www.cdc.gov/fluview/surveillance/{year}-week-{week_str}.html"
    if season in PATTERN_A_SEASONS:
        return [archive, modern]
    return [modern, archive]


def report_url(season: str, year: int, week: int) -> str:
    return report_urls(season, year, week)[0]


def cache_path(cache_dir: Path, season: str, week: int) -> Path:
    return cache_dir / season / f"week{week:02d}.html"


# ---------------------------------------------------------------------------
# CSV loading (local national ILINet / NREVSS tables)
# ---------------------------------------------------------------------------


def parse_csv_value(raw: str) -> Optional[float]:
    value = raw.strip()
    if not value or value.upper() == "X":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def load_national_csv(path: Path) -> Dict[Tuple[int, int], Dict[str, str]]:
    rows: Dict[Tuple[int, int], Dict[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        lines = fh.readlines()
    if not lines:
        return rows
    reader = csv.DictReader(lines[1:])
    for row in reader:
        if row.get("REGION TYPE", "").strip() != "National":
            continue
        key = (int(row["YEAR"]), int(row["WEEK"]))
        rows[key] = row
    return rows


def load_csv_tables(csv_dir: Path) -> Dict[str, Dict[Tuple[int, int], Dict[str, str]]]:
    tables: Dict[str, Dict[Tuple[int, int], Dict[str, str]]] = {}
    for name, filename in CSV_FILES.items():
        path = csv_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Missing CSV: {path}")
        tables[name] = load_national_csv(path)
    return tables


def joined_week_keys(tables: Mapping[str, Dict[Tuple[int, int], Dict[str, str]]]) -> set[Tuple[int, int]]:
    keys = set(next(iter(tables.values())).keys())
    for table in tables.values():
        keys &= set(table.keys())
    return keys


def season_to_date_window(
    season: str,
    key: Tuple[int, int],
    available: set[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """All weeks in `season` from week 40 up to and including `key`.

    Returns chronologically-ordered (year, week) keys that are present in the
    joined CSV tables — so every local channel shares the same length/alignment.
    """
    return [wk for wk in weeks_for_season(season, available) if wk <= key]


# ---------------------------------------------------------------------------
# Socrata fetch (FluSurv-NET hospitalization, NCHS mortality) + local caching
# ---------------------------------------------------------------------------


def _socrata_get(base_url: str, params: Dict[str, str], ua: str, timeout: float) -> List[dict]:
    url = base_url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
        return json.loads(resp.read())


def _socrata_fetch_all(
    base_url: str,
    where: str,
    select: str,
    ua: str,
    timeout: float,
    page: int = 50000,
) -> List[dict]:
    """Fetch every row matching `where`, paginating on $offset."""
    out: List[dict] = []
    offset = 0
    while True:
        rows = _socrata_get(
            base_url,
            {"$where": where, "$select": select, "$order": "date" if "date" in select else ":id",
             "$limit": str(page), "$offset": str(offset)},
            ua,
            timeout,
        )
        out.extend(rows)
        if len(rows) < page:
            break
        offset += page
    return out


def _iso_date(raw: str) -> str:
    """Socrata floating timestamp 'YYYY-MM-DDT00:00:00.000' -> 'YYYY-MM-DD'."""
    return str(raw)[:10]


def load_flusurv_net(
    base_url: str,
    cache_file: Path,
    ua: str,
    timeout: float,
) -> Dict[Tuple[str, str], Dict[str, float]]:
    """National FluSurv-NET rates -> {(data_type, age_category): {week_ending_iso: rate}}.

    National all-demographics aggregate = state='Overall', sex='All', race='All'
    (rate_type is 'Observed' for every national row). Rows are cached to a local CSV
    so re-runs are fully offline. FluSurv-NET week-ending dates are MMWR Saturdays,
    so they align directly to the ILINet (year, week) grid via mmwr_week_ending().
    """
    if not cache_file.exists():
        where = ("surveillance_network='FluSurv-NET' AND state='Overall' "
                 "AND sex='All' AND race='All'")
        select = "date,age_category,data_type,estimate,season"
        rows = _socrata_fetch_all(base_url, where, select, ua, timeout)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with cache_file.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["season", "week_ending", "age_category", "data_type", "estimate"])
            for r in rows:
                if r.get("estimate") in (None, ""):
                    continue
                w.writerow([r.get("season", ""), _iso_date(r.get("date", "")),
                            r.get("age_category", ""), r.get("data_type", ""), r["estimate"]])
        print(f"  fetched {len(rows)} FluSurv-NET national rows -> {cache_file.name}",
              file=sys.stderr)

    table: Dict[Tuple[str, str], Dict[str, float]] = {}
    with cache_file.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                val = float(row["estimate"])
            except (ValueError, KeyError):
                continue
            key = (row["data_type"], row["age_category"])
            table.setdefault(key, {})[row["week_ending"]] = val
    return table


def load_nchs_mortality(
    base_url: str,
    cache_file: Path,
    ua: str,
    timeout: float,
) -> Dict[str, float]:
    """NCHS percent of deaths due to influenza -> {week_ending_iso: percent}.

    Source: data.cdc.gov 4bc2-bbpq 'Provisional Percent of Deaths for COVID-19,
    Influenza, and RSV' (national weekly). This is the NCHS mortality-surveillance
    percent FluView recites ('X% of the deaths ... were due to influenza'); it is
    NOT the COVID-19 death-count dataset. Cached locally for offline re-runs.
    """
    if not cache_file.exists():
        rows = _socrata_get(
            base_url,
            {"$where": f"pathogen='{MORT_PATHOGEN}'", "$select": "week_end,percent_deaths",
             "$order": "week_end", "$limit": "50000"},
            ua,
            timeout,
        )
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with cache_file.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["week_ending", "pct_deaths_influenza"])
            for r in rows:
                if r.get("percent_deaths") in (None, ""):
                    continue
                w.writerow([_iso_date(r.get("week_end", "")), r["percent_deaths"]])
        print(f"  fetched {len(rows)} NCHS mortality rows -> {cache_file.name}",
              file=sys.stderr)

    out: Dict[str, float] = {}
    with cache_file.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                out[row["week_ending"]] = float(row["pct_deaths_influenza"])
            except (ValueError, KeyError):
                continue
    return out


# ---------------------------------------------------------------------------
# Channel builders (one bundle per topic, over the season-to-date window)
# ---------------------------------------------------------------------------


def build_local_channels(
    spec: Sequence[Tuple[str, str, str]],
    tables: Mapping[str, Dict[Tuple[int, int], Dict[str, str]]],
    window: Sequence[Tuple[int, int]],
) -> List[Dict[str, Any]]:
    series: List[Dict[str, Any]] = []
    for table_name, column, unit in spec:
        table = tables[table_name]
        values = [parse_csv_value(table[key].get(column, "")) for key in window]
        series.append({"values": values, "unit": unit, "freq": "1w"})
    return series


def build_flusurv_channels(
    flusurv: Mapping[Tuple[str, str], Dict[str, float]],
    window: Sequence[Tuple[int, int]],
) -> List[Dict[str, Any]]:
    series: List[Dict[str, Any]] = []
    for data_type, age, unit in HOSP_SPEC:
        m = flusurv.get((data_type, age), {})
        values = [m.get(mmwr_week_ending(y, w)) for (y, w) in window]
        series.append({"values": values, "unit": unit, "freq": "1w"})
    return series


def build_mortality_channels(
    nchs: Mapping[str, float],
    window: Sequence[Tuple[int, int]],
) -> List[Dict[str, Any]]:
    values = [nchs.get(mmwr_week_ending(y, w)) for (y, w) in window]
    return [{"values": values, "unit": MORT_LABEL, "freq": "1w"}]


def _has_signal(channels: Sequence[Dict[str, Any]], report_idx: int) -> bool:
    """True if any channel carries a non-null value at the report week (last idx)."""
    return any(ch["values"][report_idx] is not None for ch in channels)


# ---------------------------------------------------------------------------
# HTML fetch + text extraction
# ---------------------------------------------------------------------------


class HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "br", "section"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = True
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = False
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()


def html_to_text(html: str) -> str:
    parser = HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


def clean_paragraph(paragraph: str) -> str:
    paragraph = re.sub(r"\s+", " ", paragraph).strip()
    if len(paragraph) < 20:
        return ""
    lowered = paragraph.lower()
    if any(phrase in lowered for phrase in SKIP_PHRASES):
        return ""
    if lowered.startswith("http"):
        return ""
    return paragraph


def split_paragraphs(text: str) -> List[str]:
    return [p for p in (clean_paragraph(x) for x in re.split(r"\n\s*\n+", text)) if p]


def raw_paragraphs(text: str) -> List[str]:
    return [
        re.sub(r"\s+", " ", p).strip()
        for p in re.split(r"\n\s*\n+", text)
        if len(re.sub(r"\s+", " ", p).strip()) >= 20
    ]


# Sentence splitter (guards "U.S." and "No." so they don't end a sentence;
# decimals/percentages like 55.3% are safe — no space after the dot).
_ABBR = "\x00"


def split_sentences(text: str) -> List[str]:
    flat = re.sub(r"\s+", " ", text).strip()
    prot = flat.replace("U.S.", "U" + _ABBR + "S" + _ABBR)
    prot = re.sub(r"\b([Nn]o)\.(\s)", r"\1" + _ABBR + r"\2", prot)
    parts = re.split(r"(?<=[.!?])\s+", prot)
    return [p.replace(_ABBR, ".").strip() for p in parts if p.strip()]


def extract_key_points(text: str) -> str:
    matches = list(re.finditer(r"\bKey Points\b", text, re.IGNORECASE))
    if matches:
        start = matches[-1].end()
        chunk = text[start:]
    else:
        synopsis = re.search(r"\bSynopsis:?\b", text, re.IGNORECASE)
        if not synopsis:
            return ""
        start = synopsis.end()
        chunk = text[start:]

    end_markers = [
        "U.S. virologic surveillance",
        "U.S. Virologic Surveillance",
        "Influenza-like Illness Surveillance",
        "Outpatient Respiratory Illness Visits",
        "COVID-19, flu, and RSV",
    ]
    end = len(chunk)
    for marker in end_markers:
        idx = chunk.find(marker)
        if idx > 0:
            end = min(end, idx)

    bullets = split_paragraphs(chunk[:end])
    if not bullets:
        return ""
    return "Key Points: " + " ".join(bullets)


def first_paragraph_starting(paragraphs: Sequence[str], *prefixes: str) -> str:
    for paragraph in paragraphs:
        for prefix in prefixes:
            if paragraph.lower().startswith(prefix.lower()):
                return paragraph
    return ""


def extract_outpatient_ili(paragraphs: Sequence[str]) -> str:
    """The outpatient-ILI paragraph, across report eras: modern prose ('Nationally, during
    Week N, X% of patient visits ...') and the older 'Synopsis'-style national headline
    ('...The proportion of outpatient visits for influenza-like illness (ILI) was X%, which
    is above/below the national baseline ...'). We match the data sentence (proportion +
    baseline) so we skip the section HEADER and the regional-range / map-footnote lines."""
    p = first_paragraph_starting(paragraphs, "Nationally, during Week")
    if p:
        return p
    for paragraph in paragraphs:
        pl = paragraph.lower()
        if ("proportion of outpatient visits for influenza-like illness" in pl
                and "baseline" in pl):
            return paragraph
    return ""


def extract_lab_composition(text: str) -> str:
    """Virus-typing / positivity sentences describing the NREVSS lab channels.

    Real report sentences only: the public-health-laboratory typing sentences
    ('of the N viruses reported by public health laboratories ... A(H1N1)pdm09,
    A(H3N2) ...') plus the national clinical-laboratory percent-positive sentence
    when present. Preserves report order; de-duplicates.
    """
    sentences = split_sentences(text)
    picked: List[str] = []

    def add(s: str) -> None:
        if s and s not in picked:
            picked.append(s)

    # National clinical-lab positivity (the % positive / % A / % B channels).
    for s in sentences:
        sl = s.lower()
        if ("respiratory specimens" in sl and "positive for influenza" in sl
                and sl.startswith("nationally")):
            add(s)
            break
    # Public-health-lab counts + subtype breakdown (the PHL subtype-count channels).
    for s in sentences:
        sl = s.lower()
        if "reported by public health laboratories" in sl or "viruses subtyped" in sl:
            add(s)
    return " ".join(picked)


def extract_hospitalization(paragraphs: Sequence[str]) -> str:
    """FluSurv-NET hospitalization paragraph(s): the 'A total of N ... hospitalizations'
    paragraph plus the following 'When examining rates by age ...' paragraph."""
    # The detailed hospitalization paragraph exists in every era and reads '... N
    # laboratory-confirmed influenza-associated hospitalizations were reported ...':
    # modern it starts 'A total of ...', older it starts 'Between <date> and <date>, ...'.
    # Match the phrase (not the prefix) and, when present, append the modern by-age para.
    for idx, paragraph in enumerate(paragraphs):
        pl = paragraph.lower()
        if ("laboratory-confirmed influenza-associated hospitalizations were reported" in pl):
            parts = [paragraph]
            for follow in paragraphs[idx + 1 : idx + 3]:
                if follow.startswith("When examining rates by age"):
                    parts.append(follow)
                    break
            return "\n\n".join(parts)
    # older 'Synopsis'-style one-liner fallback ('Influenza-associated Hospitalizations: A
    # cumulative rate for the season of X ... per 100,000 ...').
    p = first_paragraph_starting(paragraphs, "Influenza-associated Hospitalizations")
    return p


def extract_mortality(paragraphs: Sequence[str]) -> str:
    """The NCHS mortality paragraph, across eras: modern ('Based on NCHS ... were due to
    influenza') and the older 'Synopsis'-style section ('Pneumonia and Influenza Mortality:
    The proportion of deaths attributed to pneumonia and influenza (P&I) ...')."""
    for paragraph in paragraphs:
        if paragraph.startswith("Based on NCHS") and "were due to influenza" in paragraph:
            return paragraph
    # older 'Synopsis'-style: match the data sentence (not a bare section header).
    for paragraph in paragraphs:
        if "attributed to pneumonia and influenza" in paragraph.lower():
            return paragraph
    return ""


def parse_week_ending_date(html: str, year: int, week: int) -> Optional[str]:
    for source in (html, html_to_text(html)):
        match = WEEK_ENDING_RE.search(source)
        if match:
            try:
                parsed = datetime.strptime(match.group(1), "%B %d, %Y").date()
                return parsed.isoformat()
            except ValueError:
                pass
    title_match = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if title_match:
        match = WEEK_ENDING_RE.search(title_match.group(1))
        if match:
            try:
                parsed = datetime.strptime(match.group(1), "%B %d, %Y").date()
                return parsed.isoformat()
            except ValueError:
                pass
    return None


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
    except requests.Timeout:
        try:
            response = session.get(url, timeout=timeout_s)
        except requests.RequestException as exc:
            print(f"  warning: request failed for {url}: {exc}", file=sys.stderr)
            return None, False
    except requests.RequestException as exc:
        print(f"  warning: request failed for {url}: {exc}", file=sys.stderr)
        return None, False

    if response.status_code != 200:
        print(
            f"  warning: HTTP {response.status_code} for {url}",
            file=sys.stderr,
        )
        return None, False

    html = response.text
    cache_file.write_text(html, encoding="utf-8")
    return html, False


# ---------------------------------------------------------------------------
# Record construction (per topic)
# ---------------------------------------------------------------------------


def build_topic_records(
    season: str,
    year: int,
    week: int,
    html: str,
    url: str,
    tables: Mapping[str, Dict[Tuple[int, int], Dict[str, str]]],
    available: set[Tuple[int, int]],
    flusurv: Mapping[Tuple[str, str], Dict[str, float]],
    nchs: Mapping[str, float],
    ts_intros: Mapping[str, str],
    min_topic_chars: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Split one weekly report into up to four per-topic records.

    Returns (records, tally). tally counts, per topic, records emitted and the
    reasons a topic was dropped (no narrative paragraph / no real series)."""
    key = (year, week)
    window = season_to_date_window(season, key, available)
    n_weeks = len(window)
    report_idx = len(window) - 1  # index of the report week within the window

    text_all = html_to_text(html)
    paragraphs = raw_paragraphs(text_all)
    week_ending = parse_week_ending_date(html, year, week)

    start_key = window[0] if window else key
    period_start = mmwr_week_ending(start_key[0], start_key[1])
    period_end = week_ending or mmwr_week_ending(year, week)

    tally: Dict[str, int] = {}

    def bump(name: str) -> None:
        tally[name] = tally.get(name, 0) + 1

    def make(topic: str, narrative: str, channels: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        intro = ts_intros[topic].format(season=season, week=week, n_weeks=n_weeks)
        text = f"{narrative}\n\n{intro}"
        try:
            return emit_record(
                text=text,
                timeseries=channels,
                # 'describes' not 'recites': the narrative cites each week's as-published
                # figures, but the attached series carry CDC's current REVISED values
                # (ILINet/FluSurv-NET/NCHS are revised over time; ~1-2% drift, no step
                # changes), so the text narrates the indicator rather than stating its
                # exact current values. Honest tier for a contemporaneous-text/revised-DB pair.
                alignment="describes",
                license="public-domain-us-gov",
                text_source="first_party_official",
                source=url,
                dataset="cdc_fluview",
                series_id=f"cdc_fluview:{topic}:{year}:w{week:02d}",
                domain="public_health",
                region="US",
                period_start=period_start,
                period_end=period_end,
                meta={
                    "topic": topic,
                    "season": season,
                    "year": year,
                    "week": week,
                    "week_ending_date": week_ending,
                    "window_n_weeks": n_weeks,
                    "window_start_week": window[0][1] if window else week,
                    "report_url": url,
                },
            )
        except ValueError as exc:
            bump(f"{topic}:invalid")
            print(f"    {topic}: validation failed: {exc}", file=sys.stderr)
            return None

    records: List[Dict[str, Any]] = []

    # --- topic: ili ---------------------------------------------------------
    ili_text = extract_outpatient_ili(paragraphs)
    if len(ili_text) < min_topic_chars:
        bump("ili:no_text")
    else:
        channels = build_local_channels(ILI_SPEC, tables, window)
        if not _has_signal(channels, report_idx):
            bump("ili:no_series")
        else:
            rec = make(TOPIC_ILI, ili_text, channels)
            if rec:
                records.append(rec)
                bump("ili:emitted")

    # --- topic: lab_composition --------------------------------------------
    lab_text = extract_lab_composition(text_all)
    if len(lab_text) < min_topic_chars:
        bump("lab_composition:no_text")
    else:
        channels = build_local_channels(LAB_SPEC, tables, window)
        if not _has_signal(channels, report_idx):
            bump("lab_composition:no_series")
        else:
            rec = make(TOPIC_LAB, lab_text, channels)
            if rec:
                records.append(rec)
                bump("lab_composition:emitted")

    # --- topic: hospitalization (FluSurv-NET) ------------------------------
    hosp_text = extract_hospitalization(paragraphs)
    if len(hosp_text) < min_topic_chars:
        bump("hospitalization:no_text")
    else:
        channels = build_flusurv_channels(flusurv, window)
        if not _has_signal(channels, report_idx):
            bump("hospitalization:no_series")
        else:
            rec = make(TOPIC_HOSP, hosp_text, channels)
            if rec:
                records.append(rec)
                bump("hospitalization:emitted")

    # --- topic: mortality (NCHS) -------------------------------------------
    mort_text = extract_mortality(paragraphs)
    if len(mort_text) < min_topic_chars:
        bump("mortality:no_text")
    else:
        channels = build_mortality_channels(nchs, window)
        if not _has_signal(channels, report_idx):
            bump("mortality:no_series")
        else:
            rec = make(TOPIC_MORT, mort_text, channels)
            if rec:
                records.append(rec)
                bump("mortality:emitted")

    return records, tally


# ---------------------------------------------------------------------------
# Pipeline
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


def run_pipeline(cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    data_cfg = cfg["data"]
    text_cfg = cfg["text"]
    out_cfg = cfg["output"]

    csv_dir = resolve_path(data_cfg["csv_dir"])
    cache_dir = resolve_path(data_cfg.get("html_cache_dir", ".cache/html"))
    delay_s = float(data_cfg.get("request_delay_s", 1.0))
    timeout_s = float(data_cfg.get("timeout_s", 15))
    min_topic_chars = int(text_cfg.get("min_topic_chars", 60))
    min_window_weeks = int(data_cfg.get("min_window_weeks", 1))
    ts_intros = text_cfg["ts_intro_by_topic"]
    max_records = out_cfg.get("max_records")

    socrata_ua = data_cfg.get("socrata_user_agent", "CPT-dataset-research flnu@usc.edu")
    socrata_timeout = float(data_cfg.get("socrata_timeout_s", 120))
    flusurv_url = data_cfg["flusurv_socrata_url"]
    nchs_url = data_cfg["nchs_mortality_socrata_url"]
    flusurv_cache = resolve_path(data_cfg.get("flusurv_cache", "data/raw_csv/flusurv_net_national.csv"))
    nchs_cache = resolve_path(data_cfg.get("nchs_mortality_cache", "data/raw_csv/nchs_mortality_influenza.csv"))

    tables = load_csv_tables(csv_dir)
    available_weeks = joined_week_keys(tables)
    flusurv = load_flusurv_net(flusurv_url, flusurv_cache, socrata_ua, socrata_timeout)
    nchs = load_nchs_mortality(nchs_url, nchs_cache, socrata_ua, socrata_timeout)

    stats: Dict[str, Any] = {
        "weeks_attempted": 0,
        "weeks_with_html": 0,
        "weeks_with_csv": len(available_weeks),
        "reports_yielding_records": 0,
        "records_emitted": 0,
        "reports_skipped_no_html": 0,
        "reports_skipped_short_window": 0,
    }
    topic_tally: Dict[str, int] = {}

    records: List[Dict[str, Any]] = []
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (compatible; CPTDatasetBuilder/1.0; "
                "+https://www.cdc.gov/fluview/)"
            )
        }
    )

    seasons_processed: List[str] = []
    for season in data_cfg.get("seasons", []):
        seasons_processed.append(season)
        weeks = weeks_for_season(season, available_weeks)
        for year, week in weeks:
            stats["weeks_attempted"] += 1
            label = f"{year}-W{week:02d}"

            urls = report_urls(season, year, week)
            cache_file = cache_path(cache_dir, season, week)
            html = None
            from_cache = False
            winning_url = urls[0]
            network_activity = False
            for u in urls:
                html, from_cache = fetch_html(session, u, cache_file, timeout_s)
                if html is not None:
                    winning_url = u
                    break
                if not from_cache:
                    network_activity = True

            if html is None:
                stats["reports_skipped_no_html"] += 1
                print(f"Week {label}: skipped (no HTML)")
                if network_activity:
                    time.sleep(delay_s)
                continue

            stats["weeks_with_html"] += 1
            cache_note = "from cache" if from_cache else "fetched HTML"

            window_n_weeks = len(season_to_date_window(season, (year, week), available_weeks))
            if window_n_weeks < min_window_weeks:
                stats["reports_skipped_short_window"] += 1
                print(f"Week {label}: skipped (window {window_n_weeks} < min {min_window_weeks})")
                if not from_cache:
                    time.sleep(delay_s)
                continue

            recs, tally = build_topic_records(
                season=season,
                year=year,
                week=week,
                html=html,
                url=winning_url,
                tables=tables,
                available=available_weeks,
                flusurv=flusurv,
                nchs=nchs,
                ts_intros=ts_intros,
                min_topic_chars=min_topic_chars,
            )
            for k, v in tally.items():
                topic_tally[k] = topic_tally.get(k, 0) + v

            if recs:
                stats["reports_yielding_records"] += 1
                records.extend(recs)
                stats["records_emitted"] += len(recs)
            topics_here = ",".join(sorted(r["meta"]["topic"] for r in recs)) or "none"
            print(f"Week {label}: {cache_note}, emitted {len(recs)} topic records ({topics_here})")

            if not from_cache:
                time.sleep(delay_s)

            if max_records is not None and len(records) >= int(max_records):
                break

        if max_records is not None and len(records) >= int(max_records):
            break

    if max_records is not None:
        records = records[: int(max_records)]
        stats["records_emitted"] = len(records)

    stats["topic_breakdown"] = topic_tally

    output_path = resolve_path(out_cfg["output_path"])
    write_jsonl(records, output_path, out_cfg.get("indent"))

    samples_path = resolve_path(out_cfg["samples_path"])
    write_jsonl(records[:4], samples_path, indent=2)

    report = {
        "run_date": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "seasons_processed": seasons_processed,
        "topics": [TOPIC_ILI, TOPIC_LAB, TOPIC_HOSP, TOPIC_MORT],
        "flusurv_source": flusurv_url,
        "nchs_mortality_source": nchs_url,
        **stats,
        "config_snapshot": cfg,
    }
    report_path = resolve_path(out_cfg["report_path"])
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
        description="Build per-topic CPT JSONL from CDC FluView weekly reports and surveillance series.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/build_cpt_jsonl.py\n"
            "  python scripts/build_cpt_jsonl.py --set output.max_records=10\n"
            "  python scripts/build_cpt_jsonl.py --set data.seasons=[2024-2025]\n"
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
        f"\nDone: emitted {report['records_emitted']} topic records from "
        f"{report['reports_yielding_records']} reports "
        f"({report['weeks_attempted']} weeks attempted).",
        file=sys.stderr,
    )
    print(f"Per-topic: {json.dumps(report['topic_breakdown'])}", file=sys.stderr)

    if records:
        print("\n--- First record text field ---\n")
        print(records[0]["text"])


if __name__ == "__main__":
    main()
