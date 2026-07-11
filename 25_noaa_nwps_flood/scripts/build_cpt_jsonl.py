#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from NOAA NWPS river-flood impacts + USGS gage height.

One record = one FLOOD EVENT at a river gauge. We take the hourly stage hydrograph around
the crest (Option B — event-anchored, fixed hourly window) and pair it with the National
Weather Service flood-category definitions (action/minor/moderate/major stages) plus the
official impact statement(s) for the stage the river actually reached.

Alignment = "describes" via threshold semantics: the series *reaches* a crest stage, and
the text says what that stage means (its flood category + the real-world impacts at that
level). Weaker than the value-reciting Fed/EIA sources but stronger than co-location. The
one-sentence event framing (crest value/date/category) is derived from the series; the
substantive descriptive content is official NWS impact text. See README for the alignment
+ text_quality caveat.

Series : USGS NWIS instantaneous values (00065 gage height, ft), deep sub-daily history,
         decimated to hourly. Aligned to the NWPS gauge datum via a per-gauge offset
         (median NWPS-observed - USGS over their recent overlap) so the values line up
         with the NWS flood thresholds/impacts.
Text   : NWPS gauge flood.categories + flood.impacts[] (stage -> statement). Public domain.

Enumeration: NWPS lists ~12,756 gauges; ~53% carry >=1 impact statement, ~21% (~2,760)
have >=5. The demo runs a verified seed (config `gauges`); full national harvest is the
documented scale-up (README).

Examples:
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required. pip install -r requirements.txt") from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE
_CAT_ORDER = ["action", "minor", "moderate", "major"]
_CAT_LABEL = {"action": "action", "minor": "minor", "moderate": "moderate", "major": "major"}


# --- config helpers (same conventions as the other packages) ---------------

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


# --- HTTP (cached JSON) ----------------------------------------------------

def http_get(url: str, ua: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL).read()


def get_json_cached(url: str, dest: Path, ua: str, timeout: int, delay: float) -> Optional[Any]:
    if dest.exists():
        try:
            return json.loads(dest.read_text())
        except Exception:
            pass
    try:
        raw = http_get(url, ua, timeout)
    except Exception as e:
        print(f"  fetch failed ({getattr(e, 'code', type(e).__name__)}): {url}", file=sys.stderr)
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    time.sleep(delay)
    try:
        return json.loads(raw)
    except Exception:
        return None


# --- NWPS: flood categories + impacts --------------------------------------

def parse_categories(gauge: Dict[str, Any]) -> Dict[str, float]:
    cats = ((gauge.get("flood") or {}).get("categories") or {})
    out: Dict[str, float] = {}
    for c in _CAT_ORDER:
        st = (cats.get(c) or {}).get("stage")
        if isinstance(st, (int, float)) and st not in (-9999, 0):
            out[c] = float(st)
    return out


def parse_impacts(gauge: Dict[str, Any]) -> List[Dict[str, Any]]:
    imps = ((gauge.get("flood") or {}).get("impacts") or [])
    out = []
    for im in imps:
        st, stmt = im.get("stage"), (im.get("statement") or "").strip()
        if isinstance(st, (int, float)) and stmt:
            out.append({"stage": float(st), "statement": re.sub(r"\s+", " ", stmt)})
    return sorted(out, key=lambda x: x["stage"])


# --- USGS iv: deep hourly gage-height series -------------------------------

def _parse_iv(doc: Any) -> List[Tuple[dt.datetime, float]]:
    """USGS NWIS iv JSON -> [(utc datetime, value)] for 00065 gage height."""
    out: List[Tuple[dt.datetime, float]] = []
    try:
        series = doc["value"]["timeSeries"]
    except (KeyError, TypeError):
        return out
    for ts in series:
        for blk in ts.get("values", []):
            for v in blk.get("value", []):
                raw = v.get("value", "")
                if raw in ("", "-999999", "-999999.0"):
                    continue
                try:
                    val = float(raw)
                except ValueError:
                    continue
                t = v.get("dateTime", "")
                try:
                    d = dt.datetime.fromisoformat(t)
                    d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
                except ValueError:
                    continue
                out.append((d, val))
    out.sort(key=lambda x: x[0])
    return out


def load_usgs_hourly(usgs: str, d: Dict[str, Any], cache: Path) -> List[Tuple[dt.datetime, float]]:
    """Fetch USGS iv per-year, decimate to hourly buckets (bucket max — flood-relevant)."""
    ua, timeout, delay = d["user_agent"], int(d["timeout_s"]), float(d["request_delay_s"])
    step = int(d["resample_minutes"])
    raw: List[Tuple[dt.datetime, float]] = []
    for yr in range(int(d["usgs_year_start"]), int(d["usgs_year_end"]) + 1):
        url = d["usgs_iv_template"].format(usgs=usgs, start=f"{yr}-01-01", end=f"{yr}-12-31")
        dest = cache / "usgs" / f"{usgs}_{yr}.json"
        doc = get_json_cached(url, dest, ua, timeout, delay)
        if doc:
            raw.extend(_parse_iv(doc))
    if not raw:
        return []
    # hourly buckets keyed by (date, hour); keep the max within the bucket
    buckets: Dict[dt.datetime, float] = {}
    for t, v in raw:
        key = t.replace(minute=0, second=0, microsecond=0)
        if step != 60:  # generalize to arbitrary bucket size
            bmin = (t.minute // step) * step
            key = t.replace(minute=bmin, second=0, microsecond=0)
        buckets[key] = v if key not in buckets else max(buckets[key], v)
    return sorted(buckets.items())


def datum_offset(lid: str, usgs_hourly: List[Tuple[dt.datetime, float]],
                 d: Dict[str, Any], cache: Path) -> float:
    """NWPS stage - USGS gage height, from the recent overlap. 0.0 if no overlap."""
    ua, timeout, delay = d["user_agent"], int(d["timeout_s"]), float(d["request_delay_s"])
    url = d["nwps_stageflow_template"].format(lid=lid)
    doc = get_json_cached(url, cache / "nwps" / f"{lid}_stageflow.json", ua, timeout, delay)
    obs = (((doc or {}).get("observed") or {}).get("data") or []) if doc else []
    nwps: Dict[dt.datetime, float] = {}
    for pt in obs:
        try:
            t = dt.datetime.fromisoformat(pt["validTime"].replace("Z", "+00:00"))
            t = t.astimezone(dt.timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
            nwps[t] = float(pt["primary"])
        except (KeyError, ValueError, TypeError):
            continue
    usgs_map = {t: v for t, v in usgs_hourly}
    diffs = [nwps[t] - usgs_map[t] for t in nwps if t in usgs_map]
    if not diffs:
        return 0.0
    diffs.sort()
    return round(diffs[len(diffs) // 2], 3)   # median


# --- event detection (Option B) --------------------------------------------

def detect_events(series: List[Tuple[dt.datetime, float]], threshold: float,
                  min_hours: int, gap_hours: int) -> List[Tuple[int, dt.datetime, float]]:
    """Group above-threshold samples into events (merging dips shorter than `gap_hours`).
    O(n): scan the indices at/above threshold and split a new event whenever the time gap
    to the previous above-threshold sample exceeds `gap_hours`. -> (crest_idx, crest_time, crest_val)."""
    above = [k for k, (_, v) in enumerate(series) if v >= threshold]
    if not above:
        return []
    groups: List[List[int]] = [[above[0]]]
    for k in above[1:]:
        prev = groups[-1][-1]
        gap_h = (series[k][0] - series[prev][0]).total_seconds() / 3600.0
        if gap_h <= gap_hours:
            groups[-1].append(k)
        else:
            groups.append([k])
    events: List[Tuple[int, dt.datetime, float]] = []
    for grp in groups:
        lo, hi = grp[0], grp[-1]
        dur_h = (series[hi][0] - series[lo][0]).total_seconds() / 3600.0
        if dur_h < min_hours:
            continue
        ci = max(range(lo, hi + 1), key=lambda k: series[k][1])
        events.append((ci, series[ci][0], series[ci][1]))
    return events


# --- text assembly ---------------------------------------------------------

def category_reached(crest: float, cats: Dict[str, float]) -> Optional[str]:
    reached = [c for c in _CAT_ORDER if c in cats and crest >= cats[c]]
    return reached[-1] if reached else None


def match_impacts(crest: float, impacts: List[Dict[str, Any]], band: float, k: int = 2) -> List[Dict[str, Any]]:
    at_or_below = [im for im in impacts if im["stage"] <= crest + 0.05]
    if not at_or_below:
        return []
    hi = at_or_below[-1]["stage"]
    near = [im for im in at_or_below if im["stage"] >= hi - band]
    return near[-k:]


def build_text(gauge_name: str, state: str, river: str, crest: float, crest_time: dt.datetime,
               cats: Dict[str, float], cat: Optional[str], impacts: List[Dict[str, Any]],
               intro: str, window_days: int) -> Optional[str]:
    if not impacts:
        return None
    stage_defs = ", ".join(f"{c} flood stage {cats[c]:g} ft" for c in _CAT_ORDER if c in cats)
    cat_phrase = (f"reaching {_CAT_LABEL[cat]} flood stage" if cat
                  else "remaining below flood stage")
    when = crest_time.strftime("%B %-d, %Y") if hasattr(crest_time, "strftime") else str(crest_time)
    frame = (f"{gauge_name} ({state}) crested at {crest:.2f} ft on {when}, {cat_phrase}. "
             f"Defined flood stages here: {stage_defs}.")
    lines = "\n".join(f"- At {im['stage']:g} ft: {im['statement']}" for im in impacts)
    body = (f"{frame}\n\nNational Weather Service flood-impact statements for this location:\n{lines}")
    return f"{body}\n\n{intro}"


# --- pipeline --------------------------------------------------------------

def build(cfg: Dict[str, Any]) -> Tuple[List[dict], Dict[str, int]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    cache = rp(d["cache_dir"])
    ua, timeout, delay = d["user_agent"], int(d["timeout_s"]), float(d["request_delay_s"])
    maxrec = out_cfg.get("max_records")
    wb, wa = int(d["window_before_hours"]), int(d["window_after_hours"])
    window_days = round((wb + wa) / 24)

    stat = {"gauges": 0, "gauges_ok": 0, "events_found": 0, "emitted": 0,
            "no_impacts": 0, "no_series": 0, "no_threshold": 0, "short_text": 0,
            "no_matched_impact": 0, "invalid": 0}
    records: List[dict] = []

    for g in d["gauges"]:
        if maxrec is not None and len(records) >= int(maxrec):
            break
        lid, usgs = g["lid"], g.get("usgs", "")
        stat["gauges"] += 1
        gauge = get_json_cached(d["nwps_gauge_template"].format(lid=lid),
                                cache / "nwps" / f"{lid}.json", ua, timeout, delay)
        if not gauge:
            continue
        cats = parse_categories(gauge)
        impacts = parse_impacts(gauge)
        thr_cat = d["event_threshold"]
        if thr_cat not in cats:
            stat["no_threshold"] += 1
            continue
        if not impacts:
            stat["no_impacts"] += 1
            continue
        if not usgs:
            stat["no_series"] += 1
            continue
        series = load_usgs_hourly(usgs, d, cache)
        if len(series) < wb + wa:
            stat["no_series"] += 1
            continue
        off = datum_offset(lid, series, d, cache)
        aligned = [(tm, round(v + off, 3)) for tm, v in series]

        gauge_name = gauge.get("name") or lid
        state = gauge.get("state") or (gauge.get("state", {}) or {})
        if isinstance(state, dict):
            state = state.get("abbreviation") or state.get("name") or ""
        river = ""
        idx_by_time = aligned
        events = detect_events(aligned, cats[thr_cat], int(d["min_event_hours"]),
                               int(d["event_gap_hours"]))
        stat["events_found"] += len(events)
        if events:
            stat["gauges_ok"] += 1
        events = sorted(events, key=lambda e: e[2], reverse=True)[:int(d["max_events_per_gauge"])]

        for (ci, ctime, cval) in events:
            if maxrec is not None and len(records) >= int(maxrec):
                break
            lo, hi = max(0, ci - wb), min(len(aligned), ci + wa + 1)
            win = aligned[lo:hi]
            if len(win) < int(d["min_event_hours"]):
                continue
            cat = category_reached(cval, cats)
            matched = match_impacts(cval, impacts, float(d["impact_band_ft"]))
            if not matched:
                stat["no_matched_impact"] += 1
                continue
            intro = t["ts_intro_sentence"].format(window_days=window_days,
                                                  gauge=gauge_name, state=state)
            text = build_text(gauge_name, state, river, cval, ctime, cats, cat,
                              matched, intro, window_days)
            if not text or len(text) < int(t["min_text_chars"]):
                stat["short_text"] += 1
                continue
            values = [round(v, 3) for _, v in win]
            timestamps = [tm.strftime("%Y-%m-%dT%H:00:00Z") for tm, _ in win]
            ev_id = ctime.strftime("%Y%m%dT%H")
            rec = {
                "text": text,
                "timeseries": [{"values": values, "unit": "stage_ft", "freq": "1h"}],
                "timestamps": timestamps,
                "task_type": "world_knowledge",
                "text_quality": "real",
                "gauge_lid": lid,
                "usgs_site": usgs,
                "gauge_name": gauge_name,
                "state": state,
                "flood_stages": {c: cats[c] for c in _CAT_ORDER if c in cats},
                "crest_ft": round(cval, 2),
                "crest_time": ctime.strftime("%Y-%m-%dT%H:00:00Z"),
                "category_reached": cat,
                "datum_offset_ft": off,
                "window_hours": len(win),
                "dataset": "noaa_nwps_flood",
                "source": d["source"],
                "license": d["license"],
                "series_id": f"nwps_{lid}_{ev_id}",
            }
            verr = validate(rec)
            if verr:
                stat["invalid"] += 1
                continue
            records.append(rec)
            stat["emitted"] += 1
    return records, stat


def validate(rec: dict) -> List[str]:
    e = []
    if rec["text"].count("<ts></ts>") != 1:
        e.append("ts token count")
    ch = rec["timeseries"][0]
    if len(ch["values"]) != len(rec["timestamps"]):
        e.append("values/timestamps length mismatch")
    if len(ch["values"]) < 2:
        e.append("empty series")
    return e


def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    d, out_cfg = cfg["data"], cfg["output"]
    records, stats = build(cfg)
    report = {"dataset": "noaa_nwps_flood", "source": d["source"],
              "window_hours": int(d["window_before_hours"]) + int(d["window_after_hours"]) + 1,
              "event_threshold": d["event_threshold"], "stats": stats,
              "config_snapshot": cfg, "dry_run": dry}
    if dry:
        if records:
            print("\n--- sample record ---")
            r0 = dict(records[0])
            r0["text"] = r0["text"][:900] + "…"
            r0["timeseries"] = [{"values": r0["timeseries"][0]["values"][:6] + ["…"],
                                 "unit": "stage_ft", "freq": "1h"}]
            r0["timestamps"] = r0["timestamps"][:3] + ["…"]
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:3000])
        print("\n" + json.dumps(stats, indent=2))
        return report
    op = rp(out_cfg["output_path"]); op.parent.mkdir(parents=True, exist_ok=True)
    with op.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if records and out_cfg.get("samples_path"):
        sp = rp(out_cfg["samples_path"]); sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("w", encoding="utf-8") as fh:
            json.dump(records[:3], fh, ensure_ascii=False, indent=2); fh.write("\n")
    rpath = rp(out_cfg["report_path"]); rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Build NOAA NWPS/USGS river-flood → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s['emitted']} records from {s['gauges_ok']}/{s['gauges']} gauges "
          f"({s['events_found']} events found; no_series={s['no_series']}, "
          f"no_impacts={s['no_impacts']}, invalid={s['invalid']}).", file=sys.stderr)


if __name__ == "__main__":
    main()
