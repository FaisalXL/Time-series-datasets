#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from CDC FluView weekly reports + surveillance CSVs.

One record per epidemiological week: scraped CDC weekly HTML narrative paired with
national ILINet / NREVSS clinical / public-health lab indicators for that week.

Example:
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --config config.example.yaml
  python scripts/build_cpt_jsonl.py --set output.max_records=10
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
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

TIMESERIES_SPEC: Sequence[Tuple[str, str, str]] = (
    ("ilin", "% WEIGHTED ILI", "ili_pct_weighted"),
    ("ilin", "ILITOTAL", "ili_total_visits"),
    ("ilin", "AGE 0-4", "age_0_4"),
    ("ilin", "AGE 5-24", "age_5_24"),
    ("ilin", "AGE 25-49", "age_25_49"),
    ("ilin", "AGE 50-64", "age_50_64"),
    ("ilin", "AGE 65", "age_65_plus"),
    ("clinical", "PERCENT POSITIVE", "clinical_pct_positive"),
    ("clinical", "PERCENT A", "clinical_pct_A"),
    ("clinical", "PERCENT B", "clinical_pct_B"),
    ("public_health", "A (2009 H1N1)", "ph_H1N1"),
    ("public_health", "A (H3)", "ph_H3"),
    ("public_health", "B", "ph_B"),
    ("public_health", "BVic", "ph_BVic"),
    ("public_health", "BYam", "ph_BYam"),
)

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


def mmwr_week_ending(year: int, week: int) -> str:
    """Saturday date on which MMWR (epidemiological) `week` of `year` ends.

    MMWR week 1 ends on the first Saturday of the year that has at least four
    days in January; weeks run Sunday–Saturday. Used to give the record real
    ISO-8601 `period_start` / `period_end` dates for the season-to-date window.
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
    start_year = parse_season(season)
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
# CSV loading
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
    joined CSV tables — so every channel shares the same length and alignment.
    """
    return [wk for wk in weeks_for_season(season, available) if wk <= key]


def build_timeseries(
    tables: Mapping[str, Dict[Tuple[int, int], Dict[str, str]]],
    window: Sequence[Tuple[int, int]],
) -> List[Dict[str, Any]]:
    """One channel per indicator; `values` is the season-to-date trailing window."""
    series: List[Dict[str, Any]] = []
    for table_name, column, unit in TIMESERIES_SPEC:
        table = tables[table_name]
        values = [parse_csv_value(table[key].get(column, "")) for key in window]
        series.append({"values": values, "unit": unit, "freq": "1w"})
    return series


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


def extract_virologic(paragraphs: Sequence[str]) -> str:
    return first_paragraph_starting(
        paragraphs,
        "Nationally and in all ten HHS regions",
        "Nationally, influenza",
        "Nationally, the percentage of respiratory specimens",
    )


def extract_outpatient_ili(paragraphs: Sequence[str]) -> str:
    return first_paragraph_starting(paragraphs, "Nationally, during Week")


def extract_hospitalization(paragraphs: Sequence[str]) -> str:
    for idx, paragraph in enumerate(paragraphs):
        if paragraph.startswith("A total of") and "hospitalizations were reported" in paragraph:
            parts = [paragraph]
            for follow in paragraphs[idx + 1 : idx + 3]:
                if follow.startswith("When examining rates by age"):
                    parts.append(follow)
                    break
            return "\n\n".join(parts)
    return ""


def extract_mortality(paragraphs: Sequence[str]) -> str:
    for paragraph in paragraphs:
        if paragraph.startswith("Based on NCHS") and "were due to influenza" in paragraph:
            return paragraph
    return ""


def extract_pediatric_deaths(paragraphs: Sequence[str]) -> str:
    for paragraph in paragraphs:
        if "influenza-associated pediatric deaths were reported" in paragraph.lower():
            return paragraph
    return ""


def extract_report_text(html: str) -> str:
    text = html_to_text(html)
    paragraphs = raw_paragraphs(text)
    sections: List[str] = []

    key_points = extract_key_points(text)
    if key_points:
        sections.append(key_points)

    virologic = extract_virologic(paragraphs)
    if virologic:
        sections.append(virologic)

    ili = extract_outpatient_ili(paragraphs)
    if ili:
        sections.append(ili)

    hospitalization = extract_hospitalization(paragraphs)
    if hospitalization:
        sections.append(hospitalization)

    mortality = extract_mortality(paragraphs)
    if mortality:
        sections.append(mortality)

    pediatric = extract_pediatric_deaths(paragraphs)
    if pediatric:
        sections.append(pediatric)

    return "\n\n".join(sections)


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
# Record construction
# ---------------------------------------------------------------------------


def build_record(
    season: str,
    year: int,
    week: int,
    narrative: str,
    ts_intro: str,
    html: str,
    url: str,
    tables: Mapping[str, Dict[Tuple[int, int], Dict[str, str]]],
    available: set[Tuple[int, int]],
) -> Dict[str, Any]:
    key = (year, week)
    window = season_to_date_window(season, key, available)
    n_weeks = len(window)
    intro = ts_intro.format(season=season, week=week, n_weeks=n_weeks)
    text = f"{narrative}\n\n{intro}" if narrative else intro
    week_ending = parse_week_ending_date(html, year, week)

    start_key = window[0] if window else key
    period_start = mmwr_week_ending(start_key[0], start_key[1])
    # Prefer the week-ending date parsed from the report HTML for the report week;
    # fall back to the computed MMWR Saturday when the page did not carry it.
    period_end = week_ending or mmwr_week_ending(year, week)

    return emit_record(
        text=text,
        timeseries=build_timeseries(tables, window),
        alignment="recites",
        license="public-domain-us-gov",
        text_source="first_party_official",
        source=url,
        dataset="cdc_fluview",
        series_id=f"cdc_fluview:{year}:w{week:02d}",
        domain="public_health",
        region="US",
        period_start=period_start,
        period_end=period_end,
        meta={
            "season": season,
            "year": year,
            "week": week,
            "week_ending_date": week_ending,
            "window_n_weeks": n_weeks,
            "window_start_week": window[0][1] if window else week,
            "report_url": url,
        },
    )


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


def run_pipeline(cfg: Dict[str, Any]) -> Dict[str, Any]:
    data_cfg = cfg["data"]
    text_cfg = cfg["text"]
    out_cfg = cfg["output"]

    csv_dir = resolve_path(data_cfg["csv_dir"])
    cache_dir = resolve_path(data_cfg.get("html_cache_dir", ".cache/html"))
    delay_s = float(data_cfg.get("request_delay_s", 1.0))
    timeout_s = float(data_cfg.get("timeout_s", 15))
    min_text_chars = int(text_cfg.get("min_text_chars", 200))
    min_window_weeks = int(data_cfg.get("min_window_weeks", 1))
    ts_intro = text_cfg["ts_intro_sentence"]
    max_records = out_cfg.get("max_records")

    tables = load_csv_tables(csv_dir)
    available_weeks = joined_week_keys(tables)

    stats = {
        "weeks_attempted": 0,
        "weeks_with_html": 0,
        "weeks_with_csv": len(available_weeks),
        "weeks_with_both": 0,
        "records_emitted": 0,
        "records_skipped_no_html": 0,
        "records_skipped_no_csv": 0,
        "records_skipped_short_text": 0,
        "records_skipped_short_window": 0,
        "records_skipped_invalid": 0,
    }

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

            if (year, week) not in available_weeks:
                stats["records_skipped_no_csv"] += 1
                print(f"Week {label}: skipped (no CSV join)")
                continue

            urls = report_urls(season, year, week)
            cache_file = cache_path(cache_dir, season, week)
            html = None
            from_cache = False
            winning_url = urls[0]
            network_activity = False
            for url in urls:
                html, from_cache = fetch_html(session, url, cache_file, timeout_s)
                if html is not None:
                    winning_url = url
                    break
                if not from_cache:
                    network_activity = True

            if html is None:
                stats["records_skipped_no_html"] += 1
                print(f"Week {label}: skipped (no HTML)")
                if network_activity:
                    time.sleep(delay_s)
                continue

            stats["weeks_with_html"] += 1
            cache_note = "from cache" if from_cache else "fetched HTML"
            narrative = extract_report_text(html)

            if len(narrative) < min_text_chars:
                stats["records_skipped_short_text"] += 1
                print(
                    f"Week {label}: {cache_note}, joined CSV, "
                    f"skipped (short text: {len(narrative)} chars)"
                )
                if not from_cache:
                    time.sleep(delay_s)
                continue

            window_n_weeks = len(
                season_to_date_window(season, (year, week), available_weeks)
            )
            if window_n_weeks < min_window_weeks:
                stats["records_skipped_short_window"] += 1
                print(
                    f"Week {label}: skipped (window {window_n_weeks} "
                    f"< min_window_weeks {min_window_weeks})"
                )
                if not from_cache:
                    time.sleep(delay_s)
                continue

            # emit_record() self-validates against schema/validate.py --strict and
            # raises ValueError on any violation; count those as validation failures.
            try:
                record = build_record(
                    season=season,
                    year=year,
                    week=week,
                    narrative=narrative,
                    ts_intro=ts_intro,
                    html=html,
                    url=winning_url,
                    tables=tables,
                    available=available_weeks,
                )
            except ValueError as exc:
                stats["records_skipped_invalid"] += 1
                print(f"Week {label}: validation failed: {exc}", file=sys.stderr)
                if not from_cache:
                    time.sleep(delay_s)
                continue

            stats["weeks_with_both"] += 1
            records.append(record)
            stats["records_emitted"] += 1
            print(f"Week {label}: {cache_note}, joined CSV, emitted record")

            if max_records is not None and len(records) >= int(max_records):
                if not from_cache:
                    time.sleep(delay_s)
                break

            if not from_cache:
                time.sleep(delay_s)

        if max_records is not None and len(records) >= int(max_records):
            break

    output_path = resolve_path(out_cfg["output_path"])
    write_jsonl(records, output_path, out_cfg.get("indent"))

    samples_path = resolve_path(out_cfg["samples_path"])
    write_jsonl(records[:3], samples_path, indent=2)

    report = {
        "run_date": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "seasons_processed": seasons_processed,
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
        description="Build CPT JSONL from CDC FluView weekly reports and CSVs.",
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
        f"\nDone: emitted {report['records_emitted']} records "
        f"({report['weeks_attempted']} weeks attempted).",
        file=sys.stderr,
    )

    if records:
        print("\n--- First record text field ---\n")
        print(records[0]["text"])


if __name__ == "__main__":
    main()
