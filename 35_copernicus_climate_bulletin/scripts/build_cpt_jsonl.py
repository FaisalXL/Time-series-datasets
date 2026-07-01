#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from the Copernicus C3S Monthly Climate Bulletin.

Two record types (one per theme), one record per month per theme:
  - temperature: the "Surface air temperature for {month}" narrative paired with a
    12-month trailing window of global/European/pre-industrial anomalies (ERA5).
  - sea_ice: the "Sea ice cover for {month}" narrative paired with Arctic + Antarctic
    extent anomalies for that calendar month across years (this-month-across-years,
    which is exactly what the ranking prose describes).

Text is scraped from each bulletin page's HTML (analytical prose only — figure
captions/nav stripped). Time-series CSVs are discovered from the page's own hrefs
(robust to Copernicus filename/folder changes), then parsed and windowed.

Examples:
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=4
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import calendar
import html as _html
import json
import math
import re
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit("requests is required. pip install -r requirements.txt") from exc
try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required. pip install -r requirements.txt") from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

# --- theme definitions -----------------------------------------------------
# Each channel: (csv keyword match list, column selector, unit). Column selector
# is a name (temperature CSVs have named columns) or an int index (sea-ice CSVs).
THEMES: Dict[str, Dict[str, Any]] = {
    "temperature": {
        "slug": "surface-air-temperature",
        "title_prefix": "Surface air temperature for",
        "freq": "1m",
        "channels": [
            (["global_allmonths", "1991-2020"], "ano_91-20", "global_sat_anomaly_degc_1991_2020"),
            (["global_allmonths", "1991-2020"], "ano_pi", "global_sat_anomaly_degc_preindustrial"),
            (["Europe_allmonths", "1991-2020"], "ano_91-20", "europe_sat_anomaly_degc_1991_2020"),
        ],
    },
    "sea_ice": {
        "slug": "sea-ice-cover",
        "title_prefix": "Sea ice cover for",
        "freq": "1y",
        "channels": [
            (["Arctic", "monthly_extent_anomalies"], 1, "arctic_sie_anomaly_mkm2_1991_2020"),
            (["Antarctic", "monthly_extent_anomalies"], 1, "antarctic_sie_anomaly_mkm2_1991_2020"),
        ],
    },
}

DROP_LINES = (
    "data source:", "credit:", "use the grey", "download png", "download high-res",
    "table of contents", "global map", "polar regions", "back to top", "see all months",
    "jump to another month", "about the data and analysis", "reference period will not",
    "ccl-icon", "implemented by ecmwf", "skip to main content", "subscribe to the newsletter",
    "privacy policy", "feedback survey",
)


# --- config helpers (same conventions as the other packages) ---------------

def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def coerce_value(raw: str) -> Any:
    low = raw.strip().lower()
    if low in {"true", "yes"}: return True
    if low in {"false", "no"}: return False
    if low in {"null", "none", "~"}: return None
    if re.fullmatch(r"-?\d+", raw): return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw): return float(raw)
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [coerce_value(p.strip()) for p in inner.split(",")] if inner else []
    return raw


def parse_set_args(set_args: Sequence[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for item in set_args:
        if "=" not in item:
            raise ValueError(f"Invalid --set (need key=value): {item}")
        key, raw = item.split("=", 1)
        cur = result
        parts = key.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = coerce_value(raw)
    return result


def load_config(path: Path, overrides: Sequence[str]) -> Dict[str, Any]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    return deep_merge(cfg, parse_set_args(overrides)) if overrides else cfg


def resolve_path(s: str) -> Path:
    p = Path(s)
    return p if p.is_absolute() else ROOT / p


# --- date helpers ----------------------------------------------------------

def months_in_range(start: str, end: Optional[str]) -> List[Tuple[int, int]]:
    sy, sm = map(int, start.split("-"))
    if end:
        ey, em = map(int, end.split("-"))
    else:
        t = date.today()
        ey, em = t.year, t.month
    out: List[Tuple[int, int]] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1; y += 1
    return out


# --- HTTP + caching --------------------------------------------------------

def fetch_text(session, url, cache_file: Path, timeout, binary=False):
    """Return (content, from_cache). content is None on 404/error."""
    if cache_file.exists():
        return (cache_file.read_bytes() if binary else cache_file.read_text(encoding="utf-8")), True
    try:
        r = session.get(url, timeout=timeout)
    except requests.RequestException:
        return None, False
    if r.status_code != 200 or not r.content:
        return None, False
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        cache_file.write_bytes(r.content); return r.content, False
    cache_file.write_text(r.text, encoding="utf-8"); return r.text, False


# --- text extraction -------------------------------------------------------

def extract_narrative(html: str, title_prefix: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html)
    raw = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|section|figcaption)>", "\n", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    txt = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in txt.split("\n")]
    start = next((i for i, ln in enumerate(lines) if ln.startswith(title_prefix)), 0)
    end = next((i for i, ln in enumerate(lines) if ln.startswith("Bulletin navigation")), len(lines))
    keep: List[str] = []
    for ln in lines[start + 1:end]:
        if len(ln) < 40 or "." not in ln:  # drops icon-sprite blob, headings, link text
            continue
        low = ln.lower()
        if any(d in low for d in DROP_LINES):
            continue
        if not keep or keep[-1] != ln:
            keep.append(ln)
    text = "\n\n".join(keep)
    text = re.sub(r"\s+([,.;:])", r"\1", text)  # tag-strip leaves "globally ," → "globally,"
    return text


# --- CSV discovery + parsing ----------------------------------------------

def discover_csvs(html: str, base_url: str) -> List[str]:
    hrefs = re.findall(r'href="([^"]+?\.csv[^"]*)"', html)
    out, seen = [], set()
    for h in hrefs:
        u = h if h.startswith("http") else base_url + h
        if u not in seen:
            seen.add(u); out.append(u)
    return out


def match_csv(csvs: List[str], keywords: List[str]) -> Optional[str]:
    for u in csvs:
        name = u.rsplit("/", 1)[-1].lower()
        if all(k.lower() in name for k in keywords):
            return u
    return None


def parse_temp_csv(text: str) -> Tuple[List[str], Dict[str, Dict[str, float]]]:
    """Return (column_names, {date 'YYYY-MM': {col: value}})."""
    cols: List[str] = []
    data: Dict[str, Dict[str, float]] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if parts[0] == "month":
            cols = parts
            continue
        if not cols or not re.match(r"\d{4}-\d{2}", parts[0]):
            continue
        ym = parts[0][:7]
        row = {}
        for c, v in zip(cols[1:], parts[1:]):
            try:
                f = float(v)
                row[c] = None if math.isnan(f) else f
            except ValueError:
                row[c] = None
        data[ym] = row
    return cols, data


def parse_seaice_csv(text: str, col_idx: int) -> List[Tuple[int, float]]:
    """Return [(year, value)] for the this-month-across-years series (nan dropped)."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\d{4})-\d{2}\s*,(.*)$", line)
        if not m:
            continue
        year = int(m.group(1))
        cells = [c.strip() for c in line.split(",")]
        try:
            val = float(cells[col_idx])
        except (ValueError, IndexError):
            continue  # missing
        if math.isnan(val):
            continue  # 'nan' parses to float NaN — drop it
        out.append((year, val))
    out.sort()
    return out


# --- record building -------------------------------------------------------

_TEXT_CACHE: Dict[str, Optional[str]] = {}


def _dl_text(url, cfg):
    if url in _TEXT_CACHE:
        return _TEXT_CACHE[url]
    txt, _ = fetch_text(_SESSION, url, resolve_path(cfg["data"]["csv_cache_dir"]) / url.rsplit("/", 1)[-1],
                        int(cfg["data"]["timeout_s"]))
    _TEXT_CACHE[url] = txt
    return txt


def temperature_series(csvs, cfg) -> Dict[str, Dict[str, float]]:
    """Return {ym 'YYYY-MM': {'global':v,'europe':v}} across current + mid (2021-24) eras.
    Both use the 1991-2020 baseline; monthly continuous series."""
    # current era: Fig1b (global, named col ano_91-20) + Fig6b (Europe, ano_91-20)
    g_url = match_csv(csvs, ["global_allmonths", "1991-2020"])
    e_url = match_csv(csvs, ["Europe_allmonths", "1991-2020"])
    if g_url and e_url:
        gt, et = _dl_text(g_url, cfg), _dl_text(e_url, cfg)
        if gt and et:
            _, gd = parse_temp_csv(gt)
            _, ed = parse_temp_csv(et)
            out = {}
            for ym in gd:
                gv, ev = gd[ym].get("ano_91-20"), ed.get(ym, {}).get("ano_91-20")
                if gv is not None and ev is not None:
                    out[ym] = {"global": gv, "europe": ev}
            if out:
                return out
    # mid era (~2021-2024): one ts_1month file, positional col1=YYYYMM, col2=global, col3=europe
    m_url = match_csv(csvs, ["ts_1month_anomaly_Global", "1991-2020"])
    if m_url:
        txt = _dl_text(m_url, cfg)
        if txt:
            out = {}
            for line in txt.splitlines():
                m = re.match(r"^\s*(\d{4})(\d{2})\s*,(.*)$", line)
                if not m:
                    continue
                cells = [c.strip() for c in line.split(",")]
                try:
                    gv, ev = float(cells[1]), float(cells[2])
                except (ValueError, IndexError):
                    continue
                if math.isnan(gv) or math.isnan(ev):
                    continue
                out[f"{m.group(1)}-{m.group(2)}"] = {"global": gv, "europe": ev}
            return out
    return {}


def build_temperature_record(cfg, year, month, narrative, csvs, page_url):
    win = int(cfg["data"]["window_months"])
    ym = f"{year:04d}-{month:02d}"
    series = temperature_series(csvs, cfg)
    months = sorted(d for d in series if d <= ym)[-win:]
    if len(months) < win:
        return None, f"short/absent temp series ({len(months)}/{win})"
    channels = [
        {"values": [round(series[m]["global"], 4) for m in months],
         "unit": "global_sat_anomaly_degc_1991_2020", "freq": "1m"},
        {"values": [round(series[m]["europe"], 4) for m in months],
         "unit": "europe_sat_anomaly_degc_1991_2020", "freq": "1m"},
    ]
    intro = (f"Global and European monthly surface air temperature anomalies "
             f"(ERA5, degrees C vs 1991-2020) for the 12 months ending {ym}")
    text = f"{narrative}\n\n{intro}: <ts></ts>"
    rec = {
        "text": text, "timeseries": channels,
        "task_type": "world_knowledge", "text_quality": "real",
        "theme": "temperature", "data_month": ym, "window_months": win,
        "report_url": page_url, "dataset": "copernicus_climate_bulletin",
        "source": "climate.copernicus.eu", "series_id": f"c3s_temperature_{ym}",
    }
    return rec, None


def seaice_channel(csvs, cfg, which, year) -> Optional[Dict[int, float]]:
    """{year: SIE anomaly} for Arctic|Antarctic across current + mid eras (col idx 1, same layout)."""
    url = (match_csv(csvs, ["seaice", which, "monthly_extent_anomalies"])
           or match_csv(csvs, [which + "_OSI-SAF_sie", "1991-2020"]))
    if not url:
        return None
    txt = _dl_text(url, cfg)
    if not txt:
        return None
    return {y: v for (y, v) in parse_seaice_csv(txt, 1) if y <= year}


def build_sea_ice_record(cfg, year, month, narrative, csvs, page_url):
    ym = f"{year:04d}-{month:02d}"
    win_years = cfg["data"].get("sea_ice_window_years")
    monthname = calendar.month_name[month]
    arctic = seaice_channel(csvs, cfg, "Arctic", year)
    antarctic = seaice_channel(csvs, cfg, "Antarctic", year)
    if not arctic or not antarctic:
        return None, "missing Arctic/Antarctic sea-ice CSV"
    common = sorted(set(arctic) & set(antarctic))
    if win_years:
        common = common[-int(win_years):]
    if len(common) < 2:
        return None, f"short sea-ice series ({len(common)})"
    channels = [
        {"values": [round(arctic[y], 4) for y in common],
         "unit": "arctic_sie_anomaly_mkm2_1991_2020", "freq": "1y"},
        {"values": [round(antarctic[y], 4) for y in common],
         "unit": "antarctic_sie_anomaly_mkm2_1991_2020", "freq": "1y"},
    ]
    intro = (f"Arctic and Antarctic {monthname} sea-ice extent anomalies (million sq km, "
             f"vs 1991-2020) for each {monthname} through {year}")
    text = f"{narrative}\n\n{intro}: <ts></ts>"
    rec = {
        "text": text, "timeseries": channels,
        "task_type": "world_knowledge", "text_quality": "real",
        "theme": "sea_ice", "data_month": ym, "calendar_month": monthname,
        "n_years": len(channels[0]["values"]),
        "report_url": page_url, "dataset": "copernicus_climate_bulletin",
        "source": "climate.copernicus.eu", "series_id": f"c3s_sea_ice_{ym}",
    }
    return rec, None


def validate_record(rec: Dict[str, Any]) -> List[str]:
    errs = []
    if rec["text"].count("<ts></ts>") != 1:
        errs.append("text needs exactly one <ts></ts>")
    ts = rec.get("timeseries", [])
    if not ts:
        errs.append("no timeseries")
    lengths = {len(c.get("values", [])) for c in ts}
    if len(lengths) > 1:
        errs.append(f"channel lengths differ: {sorted(lengths)}")
    for c in ts:
        if not c.get("values") or "unit" not in c or "freq" not in c:
            errs.append("bad channel")
        for v in c.get("values", []):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                errs.append(f"NaN/None value in {c.get('unit')}")
                break
    return errs


# --- pipeline --------------------------------------------------------------

_SESSION: Any = None


def run_pipeline(cfg: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    global _SESSION
    data_cfg, text_cfg, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    base = data_cfg["base_url"]
    min_chars = int(text_cfg.get("min_text_chars", 200))
    delay = float(data_cfg.get("request_delay_s", 1.0))
    timeout = int(data_cfg.get("timeout_s", 30))
    max_records = out_cfg.get("max_records")
    themes = data_cfg.get("themes", ["temperature", "sea_ice"])
    html_cache = resolve_path(data_cfg["html_cache_dir"])

    _SESSION = requests.Session()
    _SESSION.headers.update({"User-Agent": "CPTDatasetBuilder/1.0 (+research)"})

    stats = {"attempted": 0, "emitted": 0, "skipped_no_page": 0,
             "skipped_short_text": 0, "skipped_ts": 0, "skipped_validation": 0}
    per_theme = {t: 0 for t in themes}
    records: List[Dict[str, Any]] = []
    errors: List[str] = []

    # newest-first so a demo cap lands on recent (reliable, current-format) months;
    # full build (max_records=null) covers everything regardless of order.
    for (year, month) in reversed(months_in_range(data_cfg["start_month"], data_cfg.get("end_month"))):
        monthname = calendar.month_name[month].lower()
        for theme in themes:
            spec = THEMES[theme]
            stats["attempted"] += 1
            label = f"{theme} {year}-{month:02d}"
            page_url = f"{base}/{spec['slug']}-{monthname}-{year}"
            cache_file = html_cache / f"{theme}_{year}{month:02d}.html"
            html, cached = fetch_text(_SESSION, page_url, cache_file, timeout)
            if html is None:
                stats["skipped_no_page"] += 1
                if not cached:
                    time.sleep(delay)
                continue
            narrative = extract_narrative(html, spec["title_prefix"])
            if len(narrative) < min_chars:
                stats["skipped_short_text"] += 1
                print(f"{label}: skipped (short text {len(narrative)})")
                continue
            csvs = discover_csvs(html, base)
            builder = build_temperature_record if theme == "temperature" else build_sea_ice_record
            rec, err = builder(cfg, year, month, narrative, csvs, page_url)
            if rec is None:
                stats["skipped_ts"] += 1
                print(f"{label}: skipped ({err})")
                if not cached:
                    time.sleep(delay)
                continue
            verrs = validate_record(rec)
            if verrs:
                stats["skipped_validation"] += 1
                errors.extend(f"{label}: {e}" for e in verrs)
                continue
            records.append(rec)
            per_theme[theme] += 1
            stats["emitted"] += 1
            print(f"{label}: emitted ({len(rec['timeseries'])} ch, {len(rec['timeseries'][0]['values'])} steps)")
            if not cached:
                time.sleep(delay)
            if max_records is not None and len(records) >= int(max_records):
                break
        if max_records is not None and len(records) >= int(max_records):
            break

    report = {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "start_month": data_cfg["start_month"],
        "end_month": data_cfg.get("end_month") or f"{date.today():%Y-%m}",
        "themes": themes, "stats": stats, "per_theme": per_theme,
        "validation_errors": errors[:20], "config_snapshot": cfg, "dry_run": dry_run,
    }
    if dry_run:
        if records:
            print("\n--- sample record ---")
            print(json.dumps(records[0], ensure_ascii=False, indent=2)[:2200])
        print("\n", json.dumps(stats, indent=2))
        return report

    out_path = resolve_path(out_cfg["output_path"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if records and out_cfg.get("samples_path"):
        sp = resolve_path(out_cfg["samples_path"]); sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("w", encoding="utf-8") as fh:
            json.dump(records[:4], fh, ensure_ascii=False, indent=2); fh.write("\n")
    rp = resolve_path(out_cfg["report_path"]); rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Build Copernicus C3S bulletin → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config, args.set)
    report = run_pipeline(cfg, dry_run=args.dry_run)
    s = report["stats"]
    print(f"\nDone: {s['emitted']} records {report['per_theme']} "
          f"(no_page={s['skipped_no_page']}, short_text={s['skipped_short_text']}, "
          f"ts={s['skipped_ts']}, invalid={s['skipped_validation']}).", file=sys.stderr)


if __name__ == "__main__":
    main()
