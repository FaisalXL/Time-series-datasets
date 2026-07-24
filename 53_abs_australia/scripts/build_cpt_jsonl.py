#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from ABS (Australian Bureau of Statistics) Consumer
Price Index releases.

One record = one CPI release: the release's own narrative (intro sentence + "Key statistics"
bullets, e.g. "The Consumer Price Index (CPI) rose 4.0%, down from 4.2% in the 12 months to
April 2026.") paired, under a SINGLE <ts>, with a trailing window of the annual
(through-the-year) percentage change for the headline All-groups CPI plus 3 major components
(Housing, Electricity, Food and non-alcoholic beverages) -- multi-channel, WASDE-style, so the
release paragraph is not duplicated across per-component records.

Three real, distinct release tracks (confirmed live against the site, not assumed):
  1. quarterly         .../consumer-price-index-australia/{slug}           2019-Q3 -> 2025-Q3
  2. monthly_indicator .../monthly-consumer-price-index-indicator/{slug}    2022-10 -> 2025-09
  3. monthly_primary   .../consumer-price-index-australia/{slug}           2025-10 -> present
     (monthly CPI became the PRIMARY release from the Oct-2025 reference month, replacing both
     the quarterly release and the separate monthly-indicator release, which both ended then)

Series: live ABS Data API (SDMX-JSON), https://data.api.abs.gov.au/rest/data/... . The old
`api.data.abs.gov.au` host is DEAD -- not used here. Quarterly annual %chg is NOT published
as its own measure for these indexes in dataflow ABS,CPI,2.0.0 (verified: HTTP 404), so it is
DERIVED from the real published Index Numbers (measure=1), 4-quarter lag -- verified against a
live release: derived 2024-Q1 = 3.6%, matching the release's own stated "3.6%" exactly. Monthly
annual %chg IS published directly (measure=3) in ABS,CPI_M,1.2.0 and is used as-is.

REGION-CODE TRAP (real, handled): an unqualified/wildcarded REGION dimension on this API
returns EVERY capital city bundled with Australia (Sydney, Melbourne, ..., Australia) -- so
naively grabbing "the first series key" could silently substitute a city for the nation. Every
fetch here passes REGION=50 (Australia; NOT "AUS") explicitly in the SDMX key, and
`assert_national()` re-checks the response's own REGION dimension metadata equals exactly
{"50"} before any value is accepted.

Examples:
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as htmlmod
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required. pip install -r requirements.txt") from exc

# shared v1-compliant record builder (self-validates against schema/validate.py)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

_MONTH_ABBR = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTH_NAME = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
_QUARTER_MONTHS = {3: "Q1", 6: "Q2", 9: "Q3", 12: "Q4"}   # calendar month -> quarter label


# --- config helpers (same conventions as sibling packages) ------------------

def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    m = dict(base)
    for k, v in over.items():
        m[k] = deep_merge(m[k], v) if k in m and isinstance(m[k], dict) and isinstance(v, dict) else v
    return m


def coerce(raw: str) -> Any:
    low = raw.strip().lower()
    if low in {"true", "yes"}: return True
    if low in {"false", "no"}: return False
    if low in {"null", "none", "~"}: return None
    if re.fullmatch(r"-?\d+", raw): return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw): return float(raw)
    return raw


def parse_sets(sets: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for it in sets:
        k, v = it.split("=", 1)
        cur = out
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = coerce(v)
    return out


def load_config(path: Path, sets: Sequence[str]) -> Dict[str, Any]:
    cfg = yaml.safe_load(path.read_text())
    return deep_merge(cfg, parse_sets(sets)) if sets else cfg


def rp(s: str) -> Path:
    p = Path(s)
    return p if p.is_absolute() else ROOT / p


# --- HTTP --------------------------------------------------------------------

def _http(url: str, ua: str, timeout: int, tries: int = 3, accept: Optional[str] = None) -> bytes:
    headers = {"User-Agent": ua}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    last_exc = None
    for i in range(tries):
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=_SSL).read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last_exc = e
        except Exception as e:
            last_exc = e
        time.sleep(1.0 * (i + 1))
    raise last_exc


def fetch_cached(url: str, dest: Path, ua: str, timeout: int, accept: Optional[str] = None) -> Optional[bytes]:
    if dest.exists():
        return dest.read_bytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = _http(url, ua, timeout, accept=accept)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"  fetch failed ({e}): {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  fetch failed ({e}): {url}", file=sys.stderr)
        return None
    dest.write_bytes(raw)
    return raw


# --- ABS Data API (SDMX-JSON) ------------------------------------------------

class NotNationalError(RuntimeError):
    pass


def assert_national(doc: dict, region_code: str) -> None:
    """Hard check for the region-code trap: the response's OWN dimension metadata must show
    the REGION series-dimension resolved to exactly the requested national code (e.g. "50" =
    Australia), never silently expanded to a bundle of cities/NSW etc. Raises if not."""
    structs = doc["data"]["structures"][0]
    region_dim = next((d for d in structs["dimensions"]["series"] if d["id"] == "REGION"), None)
    if region_dim is None:
        raise NotNationalError("REGION dimension missing from response structure")
    codes = {v["id"] for v in region_dim["values"]}
    if codes != {region_code}:
        raise NotNationalError(
            f"expected REGION == {{{region_code!r}}} (national), got {sorted(codes)} "
            "-- refusing to use this series (region-code trap)"
        )


def parse_sdmx_json(raw: bytes) -> Dict[str, float]:
    """Parse one SDMX-JSON data response (single series) into {period_id: value}."""
    doc = json.loads(raw)
    structs = doc["data"]["structures"][0]
    obs_dim = next(d for d in structs["dimensions"]["observation"] if d["id"] == "TIME_PERIOD")
    periods = [v["id"] for v in obs_dim["values"]]
    series = doc["data"]["dataSets"][0]["series"]
    if not series:
        return {}, doc
    key = next(iter(series))
    obs = series[key]["observations"]
    return {periods[int(i)]: v[0] for i, v in obs.items() if v and v[0] is not None}, doc


def fetch_index_series(api_base: str, dataflow: str, measure: str, index_code: str,
                       tsest: str, region: str, freq: str, cache: Path,
                       ua: str, timeout: int, delay: float) -> Dict[str, float]:
    """Fetch one (measure, index, tsest, region, freq) series from the ABS Data API. Explicitly
    keys REGION (the region-code trap) and re-validates via assert_national() before returning."""
    key = f"{measure}.{index_code}.{tsest}.{region}.{freq}"
    url = f"{api_base}/{dataflow}/{key}"
    cache_file = cache / "api" / f"{dataflow.replace(',', '_')}__{key}.json"
    raw = fetch_cached(url, cache_file, ua, timeout, accept="application/vnd.sdmx.data+json")
    time.sleep(delay)
    if raw is None:
        return {}
    try:
        values, doc = parse_sdmx_json(raw)
    except (json.JSONDecodeError, KeyError, StopIteration):
        return {}
    assert_national(doc, region)   # raises NotNationalError if this isn't really national AUS
    return values


# --- release period enumeration ---------------------------------------------

def quarter_periods(start_y: int, start_q: int, end_y: int, end_q: int) -> List[Tuple[int, int]]:
    """[(year, month) ...] for quarter-ending months (3,6,9,12) from (start_y,start_q*3) to
    (end_y,end_q*3) inclusive, oldest first."""
    out = []
    y, m = start_y, start_q * 3
    while (y, m) <= (end_y, end_q * 3):
        out.append((y, m))
        m += 3
        if m > 12:
            m = 3
            y += 1
    return out


def month_periods(start_ym: str, end_ym: Optional[str], max_lookahead: int = 0) -> List[Tuple[int, int]]:
    sy, sm = map(int, start_ym.split("-"))
    if end_ym:
        ey, em = map(int, end_ym.split("-"))
    else:
        today = dt.date.today()
        ey, em = today.year, today.month
        # allow probing a bit past "today" in case a release just dropped
        em += max_lookahead
        while em > 12:
            em -= 12
            ey += 1
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def fetch_release_page(topic_base: str, year: int, month: int, slug_patterns: List[str],
                       cache: Path, ua: str, timeout: int, delay: float) -> Optional[Tuple[str, str]]:
    """Try each slug pattern for (year, month) until one 200s. Returns (html_text, url) or None."""
    mon = _MONTH_ABBR[month - 1]
    for pat in slug_patterns:
        slug = pat.format(mon=mon, year=year)
        url = f"{topic_base}/{slug}"
        cache_file = cache / "pages" / f"{Path(topic_base).name}__{slug}.html"
        raw = fetch_cached(url, cache_file, ua, timeout)
        time.sleep(delay)
        if raw is not None:
            return raw.decode("utf-8", "replace"), url
    return None


# --- prose extraction (HTML "Key statistics" + intro) ------------------------

_STOP_MARKERS = ("Data downloads", "Methodology", "Explanatory notes", "Technical note",
                "Quality declaration", "Related information", "Back to top",
                "History of changes", "Previous releases")


def extract_prose(html_text: str, max_items: int, max_chars: int) -> Optional[str]:
    """Extract the release's own narrative: the intro sentence + "Key statistics" bullets +
    leading detail paragraphs, in document order, from <main>. Filters out nav/breadcrumb
    chrome, "Next Release"/"Reference Period" metadata rows, chart-title rows that embed raw
    JSON (Highcharts data serialized inline as `&quot;...&quot;` inside the <p>), download-button
    labels, and referent-less deep sub-section bullets ("...the group rose X%" with no heading
    captured). Best-effort, same spirit as the Richmond/WASDE chart-stripping extractors."""
    m = re.search(r"<main.*?</main>", html_text, re.S)
    body = m.group(0) if m else html_text
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S)

    tokens = re.findall(r"<(p|li)[^>]*>(.*?)</\1>", body, re.S)
    out: List[str] = []
    started = False
    for tag, raw in tokens:
        if "&quot;" in raw or "[[" in raw or "axis_id" in raw:
            continue   # embedded chart JSON masquerading as a <p>
        txt = htmlmod.unescape(re.sub(r"<[^>]+>", " ", raw))
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt:
            continue
        if re.match(r"^Download (table|graph)\b", txt):
            continue
        if "Reference Period" in txt or txt.startswith("Next Release"):
            continue    # metadata chrome, e.g. "Monthly Consumer Price Index Indicator Reference Period ..."
        if re.match(r"^(Over the last twelve months|In seasonally adjusted terms),? the group\b", txt):
            continue    # unlabeled sub-section bullet (heading not captured) -> no clear referent
        if re.match(r"^\w+ Quarter \d{4}$", txt):
            continue    # bare title fragment, e.g. "March Quarter 2024"
        if any(mk.lower() in txt.lower() for mk in _STOP_MARKERS):
            break
        if not started:
            if re.search(r"Main Menu|Breadcrumb|^Detail$|^APA", txt):
                continue
            if len(txt) < 30:
                continue
            started = True
        if len(txt) < 15:
            continue
        if tag == "li" and not re.search(r"\d", txt):
            continue    # breadcrumb/title <li> chrome (real key-stat bullets always have a number)
        alpha = sum(c.isalpha() for c in txt)
        digit = sum(c.isdigit() for c in txt)
        if digit > alpha:
            continue    # residual table/chart junk
        out.append(txt)
        if len(out) >= max_items or sum(len(t) for t in out) > max_chars:
            break
    text = " ".join(out).strip()
    return text or None


# --- alignment (recites vs describes) ----------------------------------------

def endpoint_recited(prose: str, value: float) -> bool:
    """True iff the channel's endpoint (the release's own current-period figure) is stated in
    the prose as a percentage. ABS phrasing states the magnitude ("rose 4.0%" / "fell 0.7%"),
    with direction carried by the verb rather than a numeric sign, so we match on |value|."""
    av = abs(value)
    forms = {f"{av:.1f}%", f"{av:.1f} per cent", f"{av:.1f} percent"}
    return any(re.search(re.escape(f), prose, re.I) for f in forms)


# --- pipeline -----------------------------------------------------------------

def enumerate_all_periods(d: dict) -> List[dict]:
    """Return every candidate release as a dict with track/cadence/year/month/quarter_label,
    sorted newest-first by real calendar date."""
    releases = []
    q = d["quarterly"]
    for y, m in quarter_periods(q["start_year"], q["start_quarter"], q["end_year"], q["end_quarter"]):
        releases.append({"track": "quarterly", "cadence": "quarter", "year": y, "month": m,
                          "topic_base": q["topic_base"], "slug_patterns": q["slug_patterns"]})
    mi = d["monthly_indicator"]
    for y, m in month_periods(mi["start_ym"], mi["end_ym"]):
        releases.append({"track": "monthly_indicator", "cadence": "month", "year": y, "month": m,
                          "topic_base": mi["topic_base"], "slug_patterns": [mi.get("slug_pattern", "{mon}-{year}")]})
    mp = d["monthly_primary"]
    for y, m in month_periods(mp["start_ym"], mp.get("end_ym"), mp.get("max_lookahead_months", 0)):
        releases.append({"track": "monthly_primary", "cadence": "month", "year": y, "month": m,
                          "topic_base": mp["topic_base"], "slug_patterns": [mp.get("slug_pattern", "{mon}-{year}")]})
    releases.sort(key=lambda r: (r["year"], r["month"]), reverse=True)
    return releases


def period_label(year: int, month: int, cadence: str) -> str:
    if cadence == "quarter":
        return f"{year}-{_QUARTER_MONTHS[month]}"
    return f"{year}-{month:02d}"


def build(cfg: dict) -> Tuple[List[dict], Dict[str, Any]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    maxrec = out_cfg.get("max_records")
    cache = rp(d["cache_dir"])
    ua, timeout, delay = d["user_agent"], int(d["timeout_s"]), float(d["request_delay_s"])
    region, tsest = d["region_code"], d["tsest_code"]
    window_max, min_series = int(d["window_max"]), int(d["min_series"])
    indexes = d["indexes"]
    anchor_code = d["anchor_code"]

    releases = enumerate_all_periods(d)

    stat = {"candidate_periods": len(releases), "pages_fetched": 0, "pages_missing": 0,
            "emitted": 0, "recites": 0, "describes": 0, "channels_emitted": 0,
            "no_prose": 0, "short_text": 0, "short_series": 0, "no_value": 0, "invalid": 0,
            "by_track": {"quarterly": 0, "monthly_indicator": 0, "monthly_primary": 0}}

    # --- precompute per-track series dicts (annual %change per index code) --------------
    # quarterly: dataflow ABS,CPI 2.0.0, measure=1 Index Numbers -> derive annual %chg (4q lag)
    quarterly_pct: Dict[str, Dict[str, float]] = {}
    for ix in indexes:
        code = ix["code"]
        idx_vals = fetch_index_series(d["api_base"], d["quarterly_dataflow"], "1", code,
                                      tsest, region, "Q", cache, ua, timeout, delay)
        periods = sorted(idx_vals)   # 'YYYY-Qn' sorts correctly lexicographically within a year run
        pos = {p: i for i, p in enumerate(periods)}
        pct: Dict[str, float] = {}
        for p in periods:
            y, qn = p.split("-Q")
            prev = f"{int(y) - 1}-Q{qn}"
            if prev in idx_vals and idx_vals[prev]:
                pct[p] = round((idx_vals[p] / idx_vals[prev] - 1) * 100, 1)
        quarterly_pct[code] = pct

    # monthly: measure=3 (Percentage Change from Corresponding Month of the Previous Year) is
    # published directly -- no derivation needed. It lives in two dataflows across time: the
    # legacy standalone monthly-indicator dataflow (2018-09 -> 2025-09) and the unified "CPI"
    # dataflow's FREQ=M slice (2025-04 -> present, once monthly became the primary release).
    # Fetch both and merge (primary wins the small overlap) into one time axis per index code.
    monthly_pct: Dict[str, Dict[str, float]] = {}
    for ix in indexes:
        code = ix["code"]
        legacy = fetch_index_series(d["api_base"], d["monthly_dataflow"], "3", code,
                                    tsest, region, "M", cache, ua, timeout, delay)
        primary = fetch_index_series(d["api_base"], d["monthly_primary_dataflow"], "3", code,
                                     tsest, region, "M", cache, ua, timeout, delay)
        monthly_pct[code] = {**legacy, **primary}

    stat["quarterly_series_points"] = {ix["code"]: len(quarterly_pct[ix["code"]]) for ix in indexes}
    stat["monthly_series_points"] = {ix["code"]: len(monthly_pct[ix["code"]]) for ix in indexes}

    records: List[dict] = []
    for rel in releases:
        if maxrec is not None and len(records) >= int(maxrec):
            break
        y, m, cadence, track = rel["year"], rel["month"], rel["cadence"], rel["track"]
        period = period_label(y, m, cadence)

        pct_by_code = quarterly_pct if cadence == "quarter" else monthly_pct
        anchor_series = pct_by_code.get(anchor_code, {})
        if period not in anchor_series:
            stat["no_value"] += 1
            continue

        all_periods = sorted(anchor_series, key=lambda p: (p[:4], p[-2:]) if cadence == "quarter" else p)
        try:
            end_i = all_periods.index(period)
        except ValueError:
            stat["no_value"] += 1
            continue
        window_periods = all_periods[max(0, end_i - window_max + 1): end_i + 1]
        if len(window_periods) < min_series:
            stat["short_series"] += 1
            continue

        page = fetch_release_page(rel["topic_base"], y, m, rel["slug_patterns"], cache, ua, timeout, delay)
        if page is None:
            stat["pages_missing"] += 1
            continue
        stat["pages_fetched"] += 1
        html_text, page_url = page
        prose = extract_prose(html_text, int(t["max_items"]), int(t["max_chars"]))
        if not prose:
            stat["no_prose"] += 1
            continue
        if len(prose) < int(t["min_text_chars"]):
            stat["short_text"] += 1
            continue

        ts_channels, used_humans, endpoints = [], [], []
        freq_token = "1q" if cadence == "quarter" else "1M"
        for ix in indexes:
            code = ix["code"]
            series = pct_by_code.get(code, {})
            vals = [series.get(p) for p in window_periods]
            if any(v is None for v in vals):
                continue   # keep channels equal-length: drop a channel missing at any window step
            ts_channels.append({"values": vals, "unit": ix["unit"], "freq": freq_token})
            used_humans.append(ix["human"])
            endpoints.append(vals[-1])
        if not ts_channels:
            stat["no_value"] += 1
            continue

        align = "recites" if any(endpoint_recited(prose, e) for e in endpoints) else "describes"
        stat["recites" if align == "recites" else "describes"] += 1
        stat["channels_emitted"] += len(ts_channels)
        stat["by_track"][track] += 1

        # No generated/templated framing text: the <ts></ts> placeholder is appended directly
        # to the real scraped release prose, nothing else is added.
        text = f"{prose}\n\n<ts></ts>"

        start_period = window_periods[0]
        try:
            rec = emit_record(
                text=text,
                timeseries=ts_channels,
                alignment=align,
                license="cc-by-4.0",
                source=page_url,
                dataset="abs_australia",
                series_id=f"abs_cpi_{track}_{period}",
                domain="macro_econ",
                region="AU",
                period_start=f"{start_period}-01" if cadence != "quarter" else start_period,
                period_end=f"{period}-01" if cadence != "quarter" else period,
                meta={
                    "track": track,
                    "cadence": cadence,
                    "release_period": period,
                    "channels": used_humans,
                    "n_channels": len(ts_channels),
                    "window": len(window_periods),
                    "window_periods": window_periods,
                    "quarterly_pct_derived": cadence == "quarter",
                    "region_code_verified_national": True,
                    "series_note": ("annual (through-the-year) percentage change; quarterly values "
                                    "derived from real published Index Numbers (4-quarter lag), "
                                    "monthly values published directly by the ABS Data API"),
                },
            )
        except ValueError as e:
            stat["invalid"] += 1
            print(f"  invalid record dropped ({period}): {e}", file=sys.stderr)
            continue
        records.append(rec)
        stat["emitted"] += 1

    return records, stat


def run(cfg: dict, dry: bool) -> Dict[str, Any]:
    records, stats = build(cfg)
    report = {"dataset": "abs_australia", "stats": stats, "config_snapshot": cfg, "dry_run": dry}
    if dry:
        if records:
            r0 = dict(records[0]); r0["text"] = r0["text"][:700] + "…"
            print("\n--- sample record ---")
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:2600])
        print("\n" + json.dumps(stats, indent=2))
        return report
    op = rp(cfg["output"]["output_path"]); op.parent.mkdir(parents=True, exist_ok=True)
    with op.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if records and cfg["output"].get("samples_path"):
        sp = rp(cfg["output"]["samples_path"]); sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("w", encoding="utf-8") as fh:
            json.dump(records[:3], fh, ensure_ascii=False, indent=2); fh.write("\n")
    rpath = rp(cfg["output"]["report_path"]); rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    ap = argparse.ArgumentParser(description="Build ABS Australia CPI -> CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s['emitted']} records from {s['candidate_periods']} candidate periods "
          f"({s['pages_fetched']} pages fetched, {s['pages_missing']} missing), "
          f"{s['channels_emitted']} channels total [{s['recites']} recites + {s['describes']} describes] "
          f"by track {s['by_track']} "
          f"(no_prose={s['no_prose']}, short_text={s['short_text']}, short_series={s['short_series']}, "
          f"no_value={s['no_value']}, invalid={s['invalid']}).", file=sys.stderr)


if __name__ == "__main__":
    main()
