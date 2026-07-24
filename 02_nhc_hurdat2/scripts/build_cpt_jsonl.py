#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from NHC HURDAT2 + public advisory/report text.

One record per tropical cyclone (Atlantic + East Pacific) pairing real NHC text
— the post-storm Tropical Cyclone Report (TCR) and/or public advisories — with a
9-channel 6-hourly time series over the storm's qualifying tropical/subtropical
life. Storms without any retrievable real text are dropped — no synthetic
fallback.

Example:
  python scripts/build_cpt_jsonl.py --config config.example.yaml
  python scripts/build_cpt_jsonl.py --set data.source=local --set data.local_path=.cache/hurdat2/hurdat2-1851-2023-051124.txt
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required. Install with: pip install pyyaml\n"
        "Or: pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
HURDAT_INDEX_URL = "https://www.nhc.noaa.gov/data/hurdat/"
NHC_ARCHIVE_BASE = "https://www.nhc.noaa.gov/archive"

# shared v1-compliant record builder (self-validates against schema/validate.py --strict)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

QUALIFYING_STATUSES = frozenset({"TD", "TS", "HU", "SS", "SD"})

_SAFFIR_SIMPSON = (
    (137, 5),
    (113, 4),
    (96, 3),
    (83, 2),
    (64, 1),
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
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TrackPoint:
    timestamp: datetime
    record_id: str
    status: str
    lat: float
    lon: float
    max_wind_kt: int
    min_pressure_mb: int
    wind_radii: List[int] = field(default_factory=lambda: [-999] * 12)


@dataclass
class Storm:
    storm_id: str
    name: str
    year: int
    basin: str
    track: List[TrackPoint] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HURDAT2 parsing
# ---------------------------------------------------------------------------


def _parse_lat(raw: str) -> float:
    raw = raw.strip()
    sign = -1.0 if raw.endswith("S") else 1.0
    return round(sign * float(raw[:-1]), 4)


def _parse_lon(raw: str) -> float:
    raw = raw.strip()
    sign = -1.0 if raw.endswith("W") else 1.0
    return round(sign * float(raw[:-1]), 4)


def _safe_int(raw: str, default: int = -999) -> int:
    raw = raw.strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_header(line: str) -> Optional[Tuple[str, str, int, str]]:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) >= 6:
        return None
    storm_id = parts[0].strip()
    if not re.fullmatch(r"[A-Z]{2}\d{6}", storm_id):
        return None
    try:
        num_records = int(parts[2])
    except (ValueError, IndexError):
        return None
    name = parts[1].strip().title()
    basin = storm_id[:2].upper()
    return storm_id, name, num_records, basin


def _parse_data_line(line: str) -> Optional[TrackPoint]:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 8:
        return None
    try:
        date_str = parts[0].strip()
        time_str = parts[1].strip().zfill(4)
        ts = datetime.strptime(date_str + time_str, "%Y%m%d%H%M")
    except ValueError:
        return None

    record_id = parts[2].strip()
    status = parts[3].strip()
    try:
        lat = _parse_lat(parts[4])
        lon = _parse_lon(parts[5])
    except (ValueError, IndexError):
        return None

    max_wind = _safe_int(parts[6])
    min_pres = _safe_int(parts[7])
    radii = [_safe_int(parts[i]) for i in range(8, min(8 + 12, len(parts)))]
    while len(radii) < 12:
        radii.append(-999)

    return TrackPoint(
        timestamp=ts,
        record_id=record_id,
        status=status,
        lat=lat,
        lon=lon,
        max_wind_kt=max_wind,
        min_pressure_mb=min_pres,
        wind_radii=radii,
    )


def parse_hurdat2_text(text: str) -> List[Storm]:
    storms: List[Storm] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    i = 0
    while i < len(lines):
        header = _parse_header(lines[i])
        if header is None:
            i += 1
            continue
        storm_id, name, num_records, basin = header
        year = int(storm_id[-4:])
        storm = Storm(storm_id=storm_id, name=name, year=year, basin=basin)
        i += 1
        for _ in range(num_records):
            if i >= len(lines):
                break
            point = _parse_data_line(lines[i])
            if point is not None:
                storm.track.append(point)
            i += 1
        storms.append(storm)
    return storms


def download_hurdat2(url: str, cache_dir: Path) -> str:
    cache_dir.mkdir(parents=True, exist_ok=True)
    filename = url.rsplit("/", 1)[-1]
    dest = cache_dir / filename
    if dest.exists():
        return dest.read_text(encoding="utf-8", errors="replace")
    print(f"Downloading {url} ...", file=sys.stderr)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Download failed ({exc.code}) for {url}.\n"
            f"Check the current filename at {HURDAT_INDEX_URL}"
        ) from exc
    dest.write_text(text, encoding="utf-8")
    return text


def load_storms(cfg: Dict[str, Any]) -> List[Storm]:
    data_cfg = cfg["data"]
    source = data_cfg.get("source", "download")
    basin = str(data_cfg.get("basin", "atlantic")).lower()
    cache_dir = ROOT / ".cache" / "hurdat2"

    if source == "local":
        local_path = data_cfg.get("local_path")
        if not local_path:
            raise SystemExit("data.local_path is required when data.source=local")
        text = resolve_path(local_path).read_text(encoding="utf-8", errors="replace")
        return parse_hurdat2_text(text)

    if source != "download":
        raise SystemExit(f"Unknown data.source: {source}")

    urls: List[str] = []
    if basin in {"atlantic", "both"}:
        urls.append(data_cfg["atlantic_url"])
    if basin in {"east_pacific", "both"}:
        urls.append(data_cfg["east_pacific_url"])
    if not urls:
        raise SystemExit(f"Unknown data.basin: {basin}")

    storms: List[Storm] = []
    for url in urls:
        text = download_hurdat2(url, cache_dir)
        storms.extend(parse_hurdat2_text(text))
    return storms


# ---------------------------------------------------------------------------
# Storm filtering + track helpers
# ---------------------------------------------------------------------------


def qualifying_track(storm: Storm) -> List[TrackPoint]:
    return [p for p in storm.track if p.status in QUALIFYING_STATUSES]


def saffir_simpson_category(wind_kt: int, status: str = "HU") -> int:
    for threshold, category in _SAFFIR_SIMPSON:
        if wind_kt >= threshold:
            return category
    if wind_kt >= 34 and status == "TS":
        return 0
    return -1


def format_datetime_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def should_skip_storm(storm: Storm, cfg: Dict[str, Any]) -> Optional[str]:
    data_cfg = cfg["data"]
    season_start = int(data_cfg.get("season_start", 0))
    season_end = int(data_cfg.get("season_end", 9999))
    min_obs = int(data_cfg.get("min_qualifying_obs", 1))

    if storm.year < season_start or storm.year > season_end:
        return "season_filter"

    storm_filter = [s.strip().upper() for s in data_cfg.get("storm_filter", []) if s]
    if storm_filter:
        name_key = storm.name.upper()
        id_key = storm.storm_id.upper()
        if name_key not in storm_filter and id_key not in storm_filter:
            return "storm_filter"

    if len(qualifying_track(storm)) < min_obs:
        return "min_qualifying_obs"

    return None


def landfall_points(storm: Storm) -> List[TrackPoint]:
    return [p for p in storm.track if p.record_id.upper() == "L"]


def peak_observation(qtrack: List[TrackPoint]) -> TrackPoint:
    return max(qtrack, key=lambda p: p.max_wind_kt)


# Wind-radii missing codes: -999 (standard) and -99 (some radii fields).
_RADII_MISSING = frozenset({-999, -99})

# Mean Earth radius in nautical miles (for great-circle distance).
_EARTH_RADIUS_NM = 3440.065


def _max_radius(point: TrackPoint, start: int) -> Optional[int]:
    """Max wind radius across the four quadrants starting at index `start`;
    null if all quadrants are missing. Radii exist only for storms 2004+."""
    valid = [r for r in point.wind_radii[start : start + 4] if r not in _RADII_MISSING]
    if not valid:
        return None
    return max(valid)


def r34_max_nm(point: TrackPoint) -> Optional[int]:
    """Max 34-kt wind radius across quadrants; null if all missing."""
    return _max_radius(point, 0)


def r50_max_nm(point: TrackPoint) -> Optional[int]:
    """Max 50-kt wind radius across quadrants; null if all missing."""
    return _max_radius(point, 4)


def r64_max_nm(point: TrackPoint) -> Optional[int]:
    """Max 64-kt wind radius across quadrants; null if all missing."""
    return _max_radius(point, 8)


def _great_circle_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in nautical miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_NM * c


def _initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, in degrees (0–360)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    theta = math.atan2(y, x)
    return (math.degrees(theta) + 360.0) % 360.0


def translation_speeds_kt(qtrack: List[TrackPoint]) -> List[Optional[float]]:
    """Forward speed (kt) between consecutive fixes; first fix is null."""
    speeds: List[Optional[float]] = [None]
    for prev, cur in zip(qtrack, qtrack[1:]):
        hours = (cur.timestamp - prev.timestamp).total_seconds() / 3600.0
        if hours <= 0:
            speeds.append(None)
            continue
        dist = _great_circle_nm(prev.lat, prev.lon, cur.lat, cur.lon)
        speeds.append(round(dist / hours, 1))
    return speeds


def headings_deg(qtrack: List[TrackPoint]) -> List[Optional[float]]:
    """Initial bearing (deg) from each fix to the next; last fix is null."""
    headings: List[Optional[float]] = []
    for cur, nxt in zip(qtrack, qtrack[1:]):
        if cur.lat == nxt.lat and cur.lon == nxt.lon:
            headings.append(None)
        else:
            headings.append(round(_initial_bearing_deg(cur.lat, cur.lon, nxt.lat, nxt.lon), 1))
    headings.append(None)
    return headings


def build_timeseries(qtrack: List[TrackPoint]) -> List[Dict[str, Any]]:
    return [
        {"values": [p.max_wind_kt for p in qtrack], "unit": "max_wind_kt", "freq": "6h"},
        {
            "values": [
                None if p.min_pressure_mb == -999 else p.min_pressure_mb for p in qtrack
            ],
            "unit": "min_pressure_mb",
            "freq": "6h",
        },
        {"values": [p.lat for p in qtrack], "unit": "lat", "freq": "6h"},
        {"values": [p.lon for p in qtrack], "unit": "lon", "freq": "6h"},
        {"values": [r34_max_nm(p) for p in qtrack], "unit": "r34_max_nm", "freq": "6h"},
        {"values": [r50_max_nm(p) for p in qtrack], "unit": "r50_max_nm", "freq": "6h"},
        {"values": [r64_max_nm(p) for p in qtrack], "unit": "r64_max_nm", "freq": "6h"},
        {"values": translation_speeds_kt(qtrack), "unit": "translation_speed_kt", "freq": "6h"},
        {"values": headings_deg(qtrack), "unit": "heading_deg", "freq": "6h"},
    ]


# ---------------------------------------------------------------------------
# NHC advisory acquisition
# ---------------------------------------------------------------------------


def storm_archive_path(storm_id: str) -> Tuple[str, str]:
    """Return (year, archive slug) e.g. ('2021', 'al09') for AL092021."""
    year = storm_id[-4:]
    basin = storm_id[:2].lower()
    number = storm_id[2:4]
    return year, f"{basin}{number}"


def fetch_url(url: str, timeout: int) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CPT-dataset-builder/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def list_public_advisory_numbers(index_html: str, storm_id_lower: str) -> List[int]:
    pattern = re.compile(rf"{re.escape(storm_id_lower)}\.public\.(\d+)\.shtml", re.I)
    numbers = sorted({int(m.group(1)) for m in pattern.finditer(index_html)})
    return numbers


def select_advisory_numbers(
    numbers: List[int], qtrack: List[TrackPoint], max_per_storm: int
) -> List[int]:
    """Spread up to `max_per_storm` advisories evenly across the storm's life.

    Advisories are numbered 1..final with no gaps, so evenly spacing by list
    index covers formation -> peak -> landfall -> dissipation. Always includes
    the first and last advisory. `qtrack` is unused but kept for call-site
    compatibility."""
    del qtrack  # selection is now purely by advisory-number spacing
    if not numbers or max_per_storm <= 0:
        return []
    n = len(numbers)
    if n <= max_per_storm:
        return list(numbers)
    if max_per_storm == 1:
        return [numbers[-1]]
    idxs = sorted({round(i * (n - 1) / (max_per_storm - 1)) for i in range(max_per_storm)})
    return [numbers[i] for i in idxs]


def probe_old_advisories(
    storm_id_lower: str, base_url: str, timeout: int, max_probe: int = 150
) -> Dict[int, str]:
    """OLD-layout (~1998–2005) fallback: probe /pub/{id}.public.{NNN}.shtml
    sequentially from 001, stopping at the first miss. Returns {number: page}."""
    pages: Dict[int, str] = {}
    for n in range(1, max_probe + 1):
        url = f"{base_url}{storm_id_lower}.public.{n:03d}.shtml"
        page = fetch_url(url, timeout)
        if page is None:
            break
        pages[n] = page
    return pages


def discover_advisories(
    storm: Storm, timeout: int
) -> Tuple[List[int], str, Dict[int, str]]:
    """Locate a storm's public advisories.

    Tries the NEW archive layout (index at /archive/{year}/{slug}/) first; if
    that 404s or lists no advisories, falls back to OLD-layout /pub/ probing.
    Returns (advisory_numbers, base_url_for_new_layout, prefetched_old_pages).
    For the NEW layout, pages are fetched on demand from base_url; for the OLD
    layout, base_url is "" and pages are returned prefetched by number."""
    year, slug = storm_archive_path(storm.storm_id)
    storm_id_lower = storm.storm_id.lower()

    index_url = f"{NHC_ARCHIVE_BASE}/{year}/{slug}/"
    index_html = fetch_url(index_url, timeout)
    if index_html:
        numbers = list_public_advisory_numbers(index_html, storm_id_lower)
        if numbers:
            return numbers, index_url, {}

    # OLD-layout fallback.
    base_url = f"{NHC_ARCHIVE_BASE}/{year}/pub/"
    pages = probe_old_advisories(storm_id_lower, base_url, timeout)
    if pages:
        return sorted(pages), "", pages

    return [], "", {}


def strip_advisory_html(page_html: str) -> str:
    # Prefer the <pre> product block when present.
    pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", page_html, re.I | re.S)
    if pre_match:
        text = pre_match.group(1)
    else:
        text = re.sub(r"(?is)<script.*?>.*?</script>", " ", page_html)
        text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^ZCZC\b", stripped):
            continue
        if stripped == "NNNN":
            continue
        if re.match(r"^TTAA\d+\s", stripped):
            continue
        lines.append(line.rstrip())
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def truncate_advisory(text: str, limit: int) -> str:
    text = " ".join(text.split()) if limit < 200 else text
    if len(text) <= limit:
        return text
    clipped = text[: limit - 3].rsplit("\n", 1)[0] if "\n" in text[:limit] else text[: limit - 3]
    if len(clipped) >= limit:
        clipped = text[: limit - 3].rsplit(" ", 1)[0]
    return clipped.rstrip() + "..."


def fetch_advisory_text(
    storm: Storm, qtrack: List[TrackPoint], cfg: Dict[str, Any]
) -> Optional[str]:
    """Return concatenated advisory prose (no ts-intro), or None if none found.

    Handles both the NEW (~2006+) and OLD (~1998–2005) archive layouts via
    discover_advisories()."""
    adv_cfg = cfg.get("advisories", {})
    if not adv_cfg.get("enabled", True):
        return None

    timeout = int(adv_cfg.get("timeout_seconds", 10))
    char_limit = int(adv_cfg.get("char_limit_per_advisory", 1500))
    max_per_storm = int(adv_cfg.get("max_per_storm", 3))

    numbers, base_url, old_pages = discover_advisories(storm, timeout)
    if not numbers:
        return None

    storm_id_lower = storm.storm_id.lower()
    selected = select_advisory_numbers(numbers, qtrack, max_per_storm)
    chunks: List[str] = []
    for num in selected:
        if base_url:  # NEW layout: fetch on demand
            filename = f"{storm_id_lower}.public.{num:03d}.shtml"
            page = fetch_url(f"{base_url}{filename}", timeout)
        else:  # OLD layout: page already prefetched during probing
            page = old_pages.get(num)
        if not page:
            continue
        cleaned = strip_advisory_html(page)
        if cleaned:
            chunks.append(truncate_advisory(cleaned, char_limit))

    if not chunks:
        return None

    body = "\n\n---\n\n".join(chunks)
    return body.strip() or None


# ---------------------------------------------------------------------------
# NHC Tropical Cyclone Report (TCR) acquisition
# ---------------------------------------------------------------------------


def fetch_bytes(url: str, timeout: int) -> Optional[Tuple[bytes, str]]:
    """Fetch raw bytes + content-type for a URL, or None on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CPT-dataset-builder/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            ctype = resp.headers.get_content_type()
            return data, ctype
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def clean_tcr_text(raw: str) -> str:
    """Normalize pdftotext output: drop page-breaks, collapse whitespace."""
    text = raw.replace("\x0c", "\n")
    text = html.unescape(text)
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def fetch_tcr_text(storm: Storm, cfg: Dict[str, Any]) -> Optional[str]:
    """Best-effort fetch of the storm's post-storm Tropical Cyclone Report PDF,
    converted to text via pdftotext. Returns cleaned/truncated text or None."""
    tcr_cfg = cfg.get("tcr", {})
    if not tcr_cfg.get("enabled", False):
        return None

    template = tcr_cfg.get(
        "tcr_url_template", "https://www.nhc.noaa.gov/data/tcr/{storm_id}_{name}.pdf"
    )
    char_limit = int(tcr_cfg.get("tcr_char_limit", 6000))
    timeout = int(
        tcr_cfg.get("timeout_seconds", cfg.get("advisories", {}).get("timeout_seconds", 30))
    )

    cache_dir = ROOT / ".cache" / "tcr"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{storm.storm_id}.pdf"

    if cache_path.exists():
        pdf_bytes = cache_path.read_bytes()
    else:
        url = template.format(storm_id=storm.storm_id, name=storm.name)
        result = fetch_bytes(url, timeout)
        if result is None:
            return None
        data, ctype = result
        if "pdf" not in ctype.lower() or not data[:5].startswith(b"%PDF"):
            return None
        cache_path.write_bytes(data)
        pdf_bytes = data

    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=pdf_bytes,
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None

    text = clean_tcr_text(proc.stdout.decode("utf-8", errors="replace"))
    if not text:
        return None
    return truncate_advisory(text, char_limit)


def assemble_record_text(
    tcr_text: Optional[str], advisory_body: Optional[str], cfg: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str]]:
    """Compose the final record text and a text_source_product label.

    Order: TCR narrative (definitive post-storm report), then advisories, then
    the ts-intro sentence carrying the single <ts></ts> token. Returns
    (text, product) or (None, None) if there is no real text."""
    text_cfg = cfg["text"]
    ts_intro = text_cfg["ts_intro_sentence"]
    tcr_label = text_cfg.get(
        "tcr_label", "NOAA National Hurricane Center — Tropical Cyclone Report:"
    )
    advisory_label = text_cfg.get("advisory_label", "NHC Public Advisories:")

    parts: List[str] = []
    products: List[str] = []
    if tcr_text:
        parts.append(f"{tcr_label}\n\n{tcr_text}")
        products.append("tcr")
    if advisory_body:
        parts.append(f"{advisory_label}\n\n{advisory_body}")
        products.append("advisory")
    if not parts:
        return None, None

    parts.append(ts_intro)
    return "\n\n".join(parts), "+".join(products)


# ---------------------------------------------------------------------------
# Record building + validation
# ---------------------------------------------------------------------------


def basin_metadata(basin: str) -> Tuple[str, str]:
    """Return (region, canonical HURDAT2 URL) for a basin code (AL / EP / CP)."""
    if basin == "AL":
        return "North Atlantic", (
            "https://www.nhc.noaa.gov/data/hurdat/hurdat2-1851-2023-051124.txt"
        )
    # EP and CP are covered by the eastern North Pacific best-track file.
    return "Eastern North Pacific", (
        "https://www.nhc.noaa.gov/data/hurdat/hurdat2-nepac-1949-2023-042624.txt"
    )


def storm_to_record(
    storm: Storm, record_text: str, text_product: str, cfg: Dict[str, Any]
) -> Dict[str, Any]:
    qtrack = qualifying_track(storm)
    peak = peak_observation(qtrack)
    peak_cat = saffir_simpson_category(peak.max_wind_kt, peak.status)
    region, source_url = basin_metadata(storm.basin)

    # HURDAT2 records are nominally 6-hourly but include off-synoptic points
    # (e.g. landfall/peak fixes), so the series is irregular in time. Emit an
    # explicit timestamps array parallel to the channel values.
    timestamps = [format_datetime_iso(p.timestamp) for p in qtrack]

    return emit_record(
        text=record_text,
        timeseries=build_timeseries(qtrack),
        alignment="describes",
        license="public-domain-us-gov",
        text_source="first_party_official",
        source=source_url,
        dataset="nhc_hurdat2",
        series_id=storm.storm_id,
        domain="meteorology",
        region=region,
        period_start=format_datetime_iso(qtrack[0].timestamp),
        period_end=format_datetime_iso(qtrack[-1].timestamp),
        timestamps=timestamps,
        meta={
            "storm_name": storm.name.upper(),
            "storm_id": storm.storm_id,
            "basin": storm.basin,
            "season": storm.year,
            "peak_wind_kt": peak.max_wind_kt,
            "peak_category": peak_cat,
            "made_landfall": bool(landfall_points(storm)),
            "text_source_product": text_product,
            "track_date_range": [
                format_datetime_iso(qtrack[0].timestamp),
                format_datetime_iso(qtrack[-1].timestamp),
            ],
        },
    )


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
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    report_path = resolve_path(cfg["output"]["report_path"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_pipeline(cfg: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    storms = load_storms(cfg)
    skipped: Dict[str, int] = {}
    records: List[Dict[str, Any]] = []
    validation_errors: List[str] = []
    storms_with_text = 0
    max_records = cfg["output"].get("max_records")

    if not cfg.get("advisories", {}).get("enabled", True):
        skipped["advisories_disabled"] = len(storms)

    for storm in storms:
        reason = should_skip_storm(storm, cfg)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue

        if not cfg.get("advisories", {}).get("enabled", True):
            continue

        qtrack = qualifying_track(storm)
        tcr_text = fetch_tcr_text(storm, cfg)
        advisory_body = fetch_advisory_text(storm, qtrack, cfg)
        record_text, text_product = assemble_record_text(tcr_text, advisory_body, cfg)
        if not record_text:
            skipped["no_text"] = skipped.get("no_text", 0) + 1
            continue

        storms_with_text += 1
        try:
            record = storm_to_record(storm, record_text, text_product, cfg)
        except ValueError as exc:
            skipped["validation_error"] = skipped.get("validation_error", 0) + 1
            validation_errors.append(f"{storm.storm_id}: {exc}")
            continue

        records.append(record)
        if max_records is not None and len(records) >= int(max_records):
            break

    qualifying_count = sum(
        1 for storm in storms if should_skip_storm(storm, cfg) is None
    )

    report = {
        "storms_seen": len(storms),
        "storms_qualifying": qualifying_count,
        "storms_with_text": storms_with_text,
        "storms_skipped": dict(sorted(skipped.items())),
        "records_written": len(records),
        "validation_errors": validation_errors[:20],
        "config_snapshot": cfg,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "dry_run": dry_run,
    }

    write_output(records, cfg, dry_run=dry_run)
    write_report(report, cfg, dry_run=dry_run)
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build CPT JSONL from HURDAT2 + NHC public advisories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config, args.set)
    report = run_pipeline(cfg, dry_run=args.dry_run)
    if not args.dry_run:
        print(
            f"Wrote {report['records_written']} records "
            f"({report['storms_seen']} storms seen, "
            f"{report['storms_qualifying']} qualifying, "
            f"{report['storms_with_text']} with text, "
            f"{sum(report['storms_skipped'].values())} skipped).",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
