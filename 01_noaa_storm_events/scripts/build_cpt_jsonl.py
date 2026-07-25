#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from NOAA NCEI Storm Events CSV.

Groups event rows by (EPISODE_ID, STATE), aggregates three daily metrics
(injuries, property damage, event count), and pairs them with official NOAA
episode/event narratives. Output is one natural-text record per episode with a
<ts></ts> placeholder — not Alpaca instruction format.

Example:
  python scripts/build_cpt_jsonl.py --config config.example.yaml
  python scripts/build_cpt_jsonl.py --set data.years=[2023] --set output.max_records=10
  python scripts/build_cpt_jsonl.py --dry-run
"""

from __future__ import annotations

import argparse
import calendar
import csv
import gzip
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required. Install with: pip install pyyaml\n"
        "Or: pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
NOAA_INDEX_URL = (
    "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"
)

# shared v1-compliant record builder (self-validates against schema/validate.py --strict)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402


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
# Parsing helpers
# ---------------------------------------------------------------------------


_DATE_FORMATS = (
    "%d-%b-%y %H:%M:%S",
    "%d-%b-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H",
    "%d-%b-%y",
    "%Y-%m-%d",
)


def parse_begin_date(raw: str) -> Optional[date]:
    """Parse NOAA BEGIN_DATE_TIME to a calendar date."""
    text = (raw or "").strip()
    if not text:
        return None
    # Try full string first, then common truncated prefixes.
    for chunk in (text, text[:19], text[:16], text[:11], text[:10]):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(chunk, fmt).date()
            except ValueError:
                continue
    return None


def parse_damage_usd(raw: str) -> int:
    """Convert DAMAGE_PROPERTY strings like '50K', '1.5M', '200' to integer USD."""
    text = (raw or "").strip().upper().replace(",", "").replace("$", "")
    if not text or text in {"0", "0.0", "0.00"}:
        return 0
    multiplier = 1
    if text.endswith("K"):
        multiplier = 1_000
        text = text[:-1]
    elif text.endswith("M"):
        multiplier = 1_000_000
        text = text[:-1]
    try:
        return int(round(float(text) * multiplier))
    except ValueError:
        return 0


def safe_int(raw: str) -> int:
    text = (raw or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def slug_event_type(event_type: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", event_type.lower()).strip("_") or "unknown"


def normalize_row(row: Mapping[str, str]) -> Dict[str, str]:
    return {k.strip().lower(): (v or "").strip() for k, v in row.items()}


def iter_date_range(first: date, last: date) -> List[date]:
    days = (last - first).days + 1
    return [first + timedelta(days=i) for i in range(days)]


def truncate_text(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    clipped = text[: limit - 3].rsplit(" ", 1)[0]
    return clipped + "..."


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class EventRow:
    episode_id: str
    state: str
    event_type: str
    event_date: date
    injuries: int
    damage_usd: int
    episode_narrative: str
    event_narrative: str


@dataclass
class EpisodeGroup:
    episode_id: str
    state: str
    rows: List[EventRow] = field(default_factory=list)


@dataclass
class StateMonthGroup:
    state: str
    year: int
    month: int
    rows: List[EventRow] = field(default_factory=list)


@dataclass
class EpisodeWindowGroup:
    """One episode, plus the trailing state-wide daily window that ends with it.

    `rows` are the episode's own event rows (they supply the narrative and the
    window's terminal segment); `state_rows` are every row for the same state
    inside the window, which is what the series actually aggregates.
    """

    episode_id: str
    state: str
    rows: List[EventRow] = field(default_factory=list)
    state_rows: List[EventRow] = field(default_factory=list)
    win_start: Optional[date] = None
    win_end: Optional[date] = None


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


_INDEX_CACHE: Dict[str, str] = {}


def _resolve_year_filename(year: int) -> Optional[str]:
    """Look up the real detail filename for `year` in the NCEI index.

    Each annual file carries a compile-date suffix (`..._d2024_c20260421.csv.gz`) that
    NOAA bumps whenever it re-issues a year, so a hardcoded suffix silently rots. The
    index is fetched once per run and memoized.
    """
    if not _INDEX_CACHE:
        try:
            with urllib.request.urlopen(NOAA_INDEX_URL, timeout=60) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as exc:
            print(f"Warning: could not fetch NCEI index ({exc})", file=sys.stderr)
            return None
        for match in re.finditer(
            r"StormEvents_details-ftp_v1\.0_d(\d{4})_c\d{8}\.csv\.gz", html
        ):
            _INDEX_CACHE[match.group(1)] = match.group(0)
    return _INDEX_CACHE.get(str(year))


def download_year_csv(url_template: str, year: int, cache_dir: Path) -> Path:
    url = url_template.format(year=year)
    cache_dir.mkdir(parents=True, exist_ok=True)
    filename = url.rsplit("/", 1)[-1]
    dest = cache_dir / filename
    if dest.exists():
        return dest

    # Templated compile-date suffix may not match what NOAA currently serves; if any
    # cached file already covers this year, reuse it before going to the network.
    existing = sorted(cache_dir.glob(f"StormEvents_details-ftp_v1.0_d{year}_c*.csv.gz"))
    if existing:
        return existing[-1]

    resolved = _resolve_year_filename(year)
    if resolved and resolved != filename:
        dest = cache_dir / resolved
        if dest.exists():
            return dest
        url = NOAA_INDEX_URL + resolved
    print(f"Downloading {url} ...", file=sys.stderr)
    try:
        urllib.request.urlretrieve(url, dest)
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Download failed ({exc.code}) for {url}.\n"
            f"Check the NOAA index for the current filename: {NOAA_INDEX_URL}"
        ) from exc
    return dest


def open_csv_source(path: Path) -> Iterable[Dict[str, str]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            yield normalize_row(row)


def load_rows(cfg: Dict[str, Any]) -> List[EventRow]:
    data_cfg = cfg["data"]
    source = data_cfg.get("source", "download")
    paths: List[Path] = []

    if source == "local":
        local_path = data_cfg.get("local_path")
        if not local_path:
            raise SystemExit("data.local_path is required when data.source=local")
        paths.append(resolve_path(local_path))
    elif source == "download":
        template = data_cfg["download_url_template"]
        cache_dir = ROOT / ".cache" / "noaa_storm_events"
        for year in data_cfg.get("years", []):
            paths.append(download_year_csv(template, int(year), cache_dir))
    else:
        raise SystemExit(f"Unknown data.source: {source}")

    rows: List[EventRow] = []
    for path in paths:
        for raw in open_csv_source(path):
            event_date = parse_begin_date(raw.get("begin_date_time", ""))
            if event_date is None:
                continue
            episode_id = raw.get("episode_id", "").strip()
            state = raw.get("state", "").strip().upper()
            if not state:
                continue
            rows.append(
                EventRow(
                    episode_id=episode_id,
                    state=state,
                    event_type=raw.get("event_type", "").strip(),
                    event_date=event_date,
                    injuries=safe_int(raw.get("injuries_direct", ""))
                    + safe_int(raw.get("injuries_indirect", "")),
                    damage_usd=parse_damage_usd(raw.get("damage_property", "")),
                    episode_narrative=raw.get("episode_narrative", "").strip(),
                    event_narrative=raw.get("event_narrative", "").strip(),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Episode processing
# ---------------------------------------------------------------------------


def group_episodes(rows: Iterable[EventRow]) -> List[EpisodeGroup]:
    buckets: DefaultDict[Tuple[str, str], List[EventRow]] = defaultdict(list)
    for row in rows:
        key = (row.episode_id or f"__no_id__{row.event_date.isoformat()}", row.state)
        buckets[key].append(row)
    return [
        EpisodeGroup(episode_id=key[0], state=key[1], rows=group_rows)
        for key, group_rows in buckets.items()
    ]


def filter_episode_rows(rows: List[EventRow], cfg: Dict[str, Any]) -> List[EventRow]:
    data_cfg = cfg["data"]
    event_filter = [e.strip() for e in data_cfg.get("event_type_filter", []) if e]
    if not event_filter:
        return rows
    allowed = {e.lower() for e in event_filter}
    return [r for r in rows if r.event_type.lower() in allowed]


def build_daily_arrays(
    rows: List[EventRow], first_date: date, last_date: date
) -> Tuple[List[int], List[int], List[int]]:
    day_count = (last_date - first_date).days + 1
    injuries = [0] * day_count
    damage = [0] * day_count
    events = [0] * day_count
    for row in rows:
        idx = (row.event_date - first_date).days
        if 0 <= idx < day_count:
            injuries[idx] += row.injuries
            damage[idx] += row.damage_usd
            events[idx] += 1
    return injuries, damage, events


def assemble_text(
    rows: List[EventRow],
    cfg: Dict[str, Any],
    episode_limit: Optional[int] = None,
) -> str:
    text_cfg = cfg["text"]
    max_event_narratives = int(text_cfg.get("max_event_narratives", 3))
    event_limit = int(text_cfg.get("event_narrative_char_limit", 400))
    if episode_limit is None:
        episode_limit = int(text_cfg.get("episode_narrative_char_limit", 1200))

    # Episode narrative is usually identical across rows — keep unique non-empty texts.
    episode_parts: List[str] = []
    seen_episode: set[str] = set()
    for row in rows:
        if row.episode_narrative and row.episode_narrative not in seen_episode:
            seen_episode.add(row.episode_narrative)
            episode_parts.append(row.episode_narrative)

    body = " ".join(episode_parts)
    if body:
        body = truncate_text(body, episode_limit)

    # Append up to N distinct event narratives (prefer rows with non-empty text).
    event_parts: List[str] = []
    seen_event: set[str] = set()
    for row in rows:
        if not row.event_narrative or row.event_narrative in seen_event:
            continue
        seen_event.add(row.event_narrative)
        event_parts.append(truncate_text(row.event_narrative, event_limit))
        if len(event_parts) >= max_event_narratives:
            break

    segments = [part for part in [body, *event_parts] if part]
    narrative = " ".join(segments)
    if narrative and not narrative.endswith((".", "!", "?")):
        narrative += "."

    # No generated/templated framing text: the <ts></ts> placeholder is appended directly
    # to the real scraped narrative, nothing else is added.
    return f"{narrative}\n\n<ts></ts>"


def make_series_id(episode_id: str, state: str, rows: List[EventRow]) -> str:
    if episode_id and not episode_id.startswith("__no_id__"):
        return f"{episode_id}_{state}"
    first_date = min(r.event_date for r in rows)
    first_type = rows[0].event_type if rows else "unknown"
    return f"{first_date.isoformat()}_{state}_{slug_event_type(first_type)}"


def _year_source_url(cfg: Dict[str, Any], year: int) -> str:
    """Canonical URL for the NCEI Storm Events source covering `year`."""
    tmpl = cfg["data"].get("download_url_template")
    return tmpl.format(year=year) if tmpl else NOAA_INDEX_URL


def episode_to_record(group: EpisodeGroup, cfg: Dict[str, Any]) -> Dict[str, Any]:
    rows = filter_episode_rows(group.rows, cfg)
    first_date = min(r.event_date for r in rows)
    last_date = max(r.event_date for r in rows)
    injuries, damage, events = build_daily_arrays(rows, first_date, last_date)

    event_types = sorted({r.event_type for r in rows if r.event_type})
    text = assemble_text(rows, cfg)

    return emit_record(
        text=text,
        timeseries=[
            {"values": injuries, "unit": "injuries/day", "freq": "1d"},
            {"values": damage, "unit": "USD/day", "freq": "1d"},
            {"values": events, "unit": "events/day", "freq": "1d"},
        ],
        alignment="describes",
        license="public-domain-us-gov",
        text_source="first_party_official",
        source=_year_source_url(cfg, first_date.year),
        dataset="noaa_storm_events",
        series_id=make_series_id(group.episode_id, group.state, rows),
        domain="meteorology",
        region="US",
        period_start=first_date.isoformat(),
        period_end=last_date.isoformat(),
        meta={
            "geography": group.state,
            "event_types": event_types,
            "date_range": [first_date.isoformat(), last_date.isoformat()],
        },
    )


def month_bounds(year: int, month: int) -> Tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def group_state_months(rows: Iterable[EventRow]) -> List[StateMonthGroup]:
    buckets: DefaultDict[Tuple[str, int, int], List[EventRow]] = defaultdict(list)
    for row in rows:
        key = (row.state, row.event_date.year, row.event_date.month)
        buckets[key].append(row)
    return [
        StateMonthGroup(state=s, year=y, month=m, rows=group_rows)
        for (s, y, m), group_rows in buckets.items()
    ]


def state_month_to_record(group: StateMonthGroup, cfg: Dict[str, Any]) -> Dict[str, Any]:
    rows = filter_episode_rows(group.rows, cfg)
    first_date, last_date = month_bounds(group.year, group.month)
    injuries, damage, events = build_daily_arrays(rows, first_date, last_date)

    event_types = sorted({r.event_type for r in rows if r.event_type})
    n_episodes = len({r.episode_id for r in rows if r.episode_id})
    month_label = f"{group.year}-{group.month:02d}"

    text_cfg = cfg["text"]
    month_limit = int(text_cfg.get("month_narrative_char_limit", 2500))
    text = assemble_text(rows, cfg, episode_limit=month_limit)

    return emit_record(
        text=text,
        timeseries=[
            {"values": injuries, "unit": "injuries/day", "freq": "1d"},
            {"values": damage, "unit": "USD/day", "freq": "1d"},
            {"values": events, "unit": "events/day", "freq": "1d"},
        ],
        alignment="describes",
        license="public-domain-us-gov",
        text_source="first_party_official",
        source=_year_source_url(cfg, group.year),
        dataset="noaa_storm_events",
        series_id=f"{group.state.replace(' ', '_')}_{month_label}",
        domain="meteorology",
        region="US",
        period_start=first_date.isoformat(),
        period_end=last_date.isoformat(),
        meta={
            "month": month_label,
            "geography": group.state,
            "event_types": event_types,
            "n_episodes": n_episodes,
            "n_events": len(rows),
            "date_range": [first_date.isoformat(), last_date.isoformat()],
        },
    )


# ---------------------------------------------------------------------------
# Episode + trailing state window
# ---------------------------------------------------------------------------


def group_episode_windows(
    rows: List[EventRow], cfg: Dict[str, Any]
) -> List[EpisodeWindowGroup]:
    """One group per (episode, state), carrying the trailing state-wide daily window.

    The window is the `window_days` calendar days ENDING on the episode's last event
    day, so the episode's own activity is always the series' terminal segment. The
    series aggregates *every* event in that state over the window (not just this
    episode's), which is what makes the quiet days genuine state-level quiet rather
    than the artefact of slicing one episode.
    """
    data_cfg = cfg["data"]
    window_days = int(data_cfg.get("window_days", 32))

    # state -> date -> rows, so a window lookup is a cheap per-day gather.
    by_state_day: DefaultDict[str, DefaultDict[date, List[EventRow]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        by_state_day[row.state][row.event_date].append(row)

    episodes = group_episodes(rows)
    groups: List[EpisodeWindowGroup] = []
    for ep in episodes:
        win_end = max(r.event_date for r in ep.rows)
        win_start = win_end - timedelta(days=window_days - 1)
        day_index = by_state_day[ep.state]
        state_rows = [
            r for day in iter_date_range(win_start, win_end) for r in day_index.get(day, [])
        ]
        groups.append(
            EpisodeWindowGroup(
                episode_id=ep.episode_id,
                state=ep.state,
                rows=ep.rows,
                state_rows=state_rows,
                win_start=win_start,
                win_end=win_end,
            )
        )
    return groups


def should_skip_episode_window(
    group: EpisodeWindowGroup, cfg: Dict[str, Any], loaded_years: set
) -> Optional[str]:
    data_cfg, text_cfg = cfg["data"], cfg["text"]

    state_filter = [s.strip().upper() for s in data_cfg.get("state_filter", []) if s]
    if state_filter and group.state not in state_filter:
        return "state_filter"

    rows = filter_episode_rows(group.rows, cfg)
    if not rows:
        return "event_type_filter"

    # A window reaching outside the loaded years would read as quiet days that are
    # really just unloaded data. Drop rather than fabricate zeros.
    if group.win_start.year not in loaded_years:
        return "window_outside_loaded_years"

    if data_cfg.get("require_episode_narrative", True):
        if not any(r.episode_narrative for r in rows):
            return "missing_episode_narrative"

    min_text = int(text_cfg.get("min_text_chars", 0))
    if min_text:
        narrative_chars = len(" ".join(
            sorted({r.episode_narrative for r in rows if r.episode_narrative})
        ))
        if narrative_chars < min_text:
            return "short_text"

    state_rows = filter_episode_rows(group.state_rows, cfg)

    # The zero-sparsity guard. A window whose only active days are this episode's own
    # is the degenerate case the state-month design suffered from; require the state
    # to have been genuinely active across the window.
    min_active = int(data_cfg.get("min_active_days_in_window", 0))
    if min_active:
        active_days = len({r.event_date for r in state_rows})
        if active_days < min_active:
            return "sparse_window"

    # Optionally require the episode to account for a real share of the window, so the
    # narrative is describing the window's dominant activity rather than a footnote.
    min_share = data_cfg.get("min_episode_share")
    if min_share:
        share = len(rows) / len(state_rows) if state_rows else 0.0
        if share < float(min_share):
            return "episode_share"

    return None


def episode_window_to_record(
    group: EpisodeWindowGroup, cfg: Dict[str, Any]
) -> Dict[str, Any]:
    rows = filter_episode_rows(group.rows, cfg)
    state_rows = filter_episode_rows(group.state_rows, cfg)
    win_start, win_end = group.win_start, group.win_end

    injuries, damage, events = build_daily_arrays(state_rows, win_start, win_end)
    window_dates = [d.isoformat() for d in iter_date_range(win_start, win_end)]

    ep_first = min(r.event_date for r in rows)
    ep_last = max(r.event_date for r in rows)
    event_types = sorted({r.event_type for r in rows if r.event_type})
    text = assemble_text(rows, cfg)

    return emit_record(
        text=text,
        timeseries=[
            {"values": injuries, "unit": "injuries/day", "freq": "1d"},
            {"values": damage, "unit": "USD/day", "freq": "1d"},
            {"values": events, "unit": "events/day", "freq": "1d"},
        ],
        timestamps=window_dates,
        alignment="describes",
        license="public-domain-us-gov",
        text_source="first_party_official",
        source=_year_source_url(cfg, win_end.year),
        dataset="noaa_storm_events",
        series_id=(
            f"{make_series_id(group.episode_id, group.state, rows)}_{win_end.isoformat()}"
        ),
        domain="meteorology",
        region="US",
        period_start=win_start.isoformat(),
        period_end=win_end.isoformat(),
        meta={
            "geography": group.state,
            "event_types": event_types,
            "episode_id": group.episode_id,
            # the episode IS the window's terminal segment (structural alignment)
            "episode_date_range": [ep_first.isoformat(), ep_last.isoformat()],
            "episode_n_events": len(rows),
            "episode_injuries": sum(r.injuries for r in rows),
            "episode_damage_usd": sum(r.damage_usd for r in rows),
            "episode_share_of_window_events": (
                round(len(rows) / len(state_rows), 4) if state_rows else None
            ),
            "window_days": len(window_dates),
            "window_active_days": sum(1 for v in events if v > 0),
            "window_n_events": len(state_rows),
            "window_n_episodes": len({r.episode_id for r in state_rows if r.episode_id}),
            "date_range": [win_start.isoformat(), win_end.isoformat()],
        },
    )


def thin_overlapping_windows(
    groups: List[EpisodeWindowGroup], min_gap_days: int
) -> Tuple[List[EpisodeWindowGroup], int]:
    """Greedily drop windows that end too close to the previously kept one.

    Consecutive episodes in the same state share most of their trailing window, so
    without a floor on the spacing the corpus carries near-duplicate series. Returns
    the kept groups (input order preserved) and the number dropped.
    """
    if min_gap_days <= 0:
        return groups, 0
    order = sorted(range(len(groups)), key=lambda i: (groups[i].state, groups[i].win_end))
    keep = [False] * len(groups)
    last_state, last_end = None, None
    for i in order:
        g = groups[i]
        if g.state != last_state or (g.win_end - last_end).days >= min_gap_days:
            keep[i] = True
            last_state, last_end = g.state, g.win_end
    return [g for g, k in zip(groups, keep) if k], keep.count(False)


def should_skip_state_month(
    group: StateMonthGroup, cfg: Dict[str, Any]
) -> Optional[str]:
    data_cfg = cfg["data"]
    state_filter = [s.strip().upper() for s in data_cfg.get("state_filter", []) if s]
    if state_filter and group.state not in state_filter:
        return "state_filter"

    rows = filter_episode_rows(group.rows, cfg)
    if not rows:
        return "event_type_filter"

    min_events = int(data_cfg.get("min_month_events", 1))
    if len(rows) < min_events:
        return "min_month_events"

    if data_cfg.get("require_episode_narrative", True):
        if not any(r.episode_narrative for r in rows):
            return "missing_episode_narrative"

    return None


def should_skip_episode(
    group: EpisodeGroup, cfg: Dict[str, Any]
) -> Optional[str]:
    data_cfg = cfg["data"]
    state_filter = [s.strip().upper() for s in data_cfg.get("state_filter", []) if s]
    if state_filter and group.state not in state_filter:
        return "state_filter"

    rows = filter_episode_rows(group.rows, cfg)
    if not rows:
        return "event_type_filter"

    min_events = int(data_cfg.get("min_episode_events", 1))
    if len(rows) < min_events:
        return "min_episode_events"

    first_date = min(r.event_date for r in rows)
    last_date = max(r.event_date for r in rows)
    episode_days = (last_date - first_date).days + 1
    min_days = int(data_cfg.get("min_episode_days", 1))
    if episode_days < min_days:
        return "min_episode_days"

    if data_cfg.get("require_episode_narrative", True):
        has_narrative = any(r.episode_narrative for r in rows)
        if not has_narrative:
            return "missing_episode_narrative"

    return None


# ---------------------------------------------------------------------------
# Output + validation
# ---------------------------------------------------------------------------


# Per-record validation now lives in emit_record(): each record is self-checked against
# schema/validate.py --strict at construction time, raising ValueError on any violation.


def _pct(sorted_vals: List[float], q: float) -> Optional[float]:
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def summarize_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Series-length and window-density stats — the two things this design must prove."""
    if not records:
        return {}
    lengths = sorted(len(r["timeseries"][0]["values"]) for r in records)
    active = sorted(
        r["meta"]["window_active_days"] for r in records if "window_active_days" in r["meta"]
    )
    shares = sorted(
        r["meta"]["episode_share_of_window_events"]
        for r in records
        if r["meta"].get("episode_share_of_window_events") is not None
    )
    text_lens = sorted(len(r["text"]) for r in records)
    out: Dict[str, Any] = {
        "series_len": {
            "min": lengths[0], "p50": _pct(lengths, 0.5), "max": lengths[-1],
            "pct_ge_32": round(100.0 * sum(1 for v in lengths if v >= 32) / len(lengths), 2),
        },
        "text_chars": {"min": text_lens[0], "p50": _pct(text_lens, 0.5), "max": text_lens[-1]},
    }
    if active:
        out["window_active_days"] = {
            "min": active[0], "p10": _pct(active, 0.10), "p50": _pct(active, 0.5),
            "max": active[-1],
            "mean_pct_of_window": round(
                100.0 * sum(active) / sum(lengths[: len(active)]), 2
            ),
        }
    if shares:
        out["episode_share_of_window_events"] = {
            "min": round(shares[0], 4), "p50": round(_pct(shares, 0.5), 4),
            "max": round(shares[-1], 4),
        }

    # Two episodes in the same state ending on the same day yield the same window, so
    # their series are identical while their narratives differ. That is real many-to-one
    # source structure, not template inflation — but it is reported, not hidden.
    series_sig = Counter(
        hashlib.md5(
            json.dumps([c["values"] for c in r["timeseries"]]).encode()
        ).hexdigest()
        for r in records
    )
    text_sig = Counter(r["text"] for r in records)
    out["redundancy"] = {
        "distinct_series": len(series_sig),
        "records_sharing_a_series": sum(v for v in series_sig.values() if v > 1),
        "pct_records_sharing_a_series": round(
            100.0 * sum(v for v in series_sig.values() if v > 1) / len(records), 2
        ),
        "max_records_per_series": max(series_sig.values()),
        "distinct_texts": len(text_sig),
        "max_records_per_text": max(text_sig.values()),
    }
    return out


def write_output(
    records: List[Dict[str, Any]], cfg: Dict[str, Any], dry_run: bool
) -> None:
    if dry_run:
        return
    out_cfg = cfg["output"]
    output_path = resolve_path(out_cfg["output_path"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    indent = out_cfg.get("indent")

    if indent is None:
        with output_path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    else:
        with output_path.open("w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=int(indent))
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
    rows = load_rows(cfg)
    grouping = cfg["data"].get("grouping", "state_month")

    skipped: DefaultDict[str, int] = defaultdict(int)
    records: List[Dict[str, Any]] = []
    max_records = cfg["output"].get("max_records")
    validation_errors: List[str] = []

    thinned = 0
    if grouping == "state_month":
        groups = group_state_months(rows)
        skip_fn = should_skip_state_month
        record_fn = state_month_to_record
        label_fn = lambda g: f"{g.state}/{g.year}-{g.month:02d}"
    elif grouping == "episode":
        groups = group_episodes(rows)
        skip_fn = should_skip_episode
        record_fn = episode_to_record
        label_fn = lambda g: f"{g.episode_id}/{g.state}"
    elif grouping == "episode_window":
        loaded_years = {r.event_date.year for r in rows}
        groups = group_episode_windows(rows, cfg)
        # Thin before the per-group filters so the spacing floor is applied to the
        # episode timeline itself, not to whatever survives filtering.
        groups, thinned = thin_overlapping_windows(
            groups, int(cfg["data"].get("min_days_between_records", 0))
        )
        skip_fn = lambda g, c: should_skip_episode_window(g, c, loaded_years)
        record_fn = episode_window_to_record
        label_fn = lambda g: f"{g.episode_id}/{g.state}/{g.win_end}"
    else:
        raise SystemExit(f"Unknown data.grouping: {grouping}")

    if thinned:
        skipped["window_overlap_thinned"] = thinned

    for group in groups:
        reason = skip_fn(group, cfg)
        if reason:
            skipped[reason] += 1
            continue

        try:
            record = record_fn(group, cfg)
        except ValueError as exc:
            skipped["validation_error"] += 1
            validation_errors.append(f"{label_fn(group)}: {exc}")
            continue

        records.append(record)
        if max_records is not None and len(records) >= int(max_records):
            break

    report = {
        "grouping": grouping,
        "groups_seen": len(groups),
        "groups_skipped": dict(sorted(skipped.items())),
        "records_written": len(records),
        "rows_loaded": len(rows),
        "validation_errors": validation_errors[:20],
        "series_stats": summarize_records(records),
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
        description="Build CPT JSONL from NOAA Storm Events CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/build_cpt_jsonl.py --config config.example.yaml\n"
            "  python scripts/build_cpt_jsonl.py --set data.state_filter=[OKLAHOMA] --dry-run\n"
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute stats and print report; do not write output files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config, args.set)
    report = run_pipeline(cfg, dry_run=args.dry_run)
    if not args.dry_run:
        print(
            f"Wrote {report['records_written']} records "
            f"({report['groups_seen']} {report['grouping']} groups seen, "
            f"{sum(report['groups_skipped'].values())} skipped).",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
