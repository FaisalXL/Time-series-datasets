#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from cricket match narration + per-over ball-by-ball series.

One record = one INNINGS: the innings' per-over time series (runs, wickets, cumulative
runs, run-rate) paired with the real ESPNcricinfo match report that narrates that match
over by over. The report *describes* the progression the series quantifies → "describes".
text_quality is always "real"; an innings with no usable recap is dropped (no synthetic
fallback).

Series: Cricsheet bulk per-delivery CSV (ODC-BY 1.0), aggregated per over. Stdlib only.
Text  : ESPNcricinfo report prose, fetched via the ESPN parent API (site.api.espn.com),
        which resolves by match_id alone. Report body = JSON `article.story`.

Cricsheet match_id == ESPNcricinfo match ID → the report join key.

⚠️ LICENSE: ESPNcricinfo report prose is copyrighted / ToS-restricted. The committed
   output/ + samples/ are a capped 50-record demo for the lead to inspect (internal
   review, not distribution) — see NOTION_PAGE.md. Keep output.max_records small; do not
   run/commit a full build or publish until redistribution is cleared.

Examples:
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=5
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import csv
import html as _html
import io
import json
import re
import ssl
import sys
import time
import urllib.request
import zipfile
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


# --- HTTP ------------------------------------------------------------------

def http_get(url: str, ua: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL).read()


def download_cached(url: str, dest: Path, ua: str, timeout: int) -> Path:
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url} -> {dest.name} (cached after first run)...", file=sys.stderr)
    dest.write_bytes(http_get(url, ua, timeout))
    return dest


# --- Cricsheet parsing -----------------------------------------------------

def parse_info(raw: bytes) -> Dict[str, Any]:
    """{match_id}_info.csv -> dict. Repeated keys (team, umpire) collect into lists."""
    info: Dict[str, Any] = {"team": []}
    for row in csv.reader(io.StringIO(raw.decode("utf-8"))):
        if len(row) >= 3 and row[0] == "info":
            key, val = row[1], row[2]
            if key == "team":
                info["team"].append(val)
            elif key not in info:
                info[key] = val
    return info


def _num(s: str) -> float:
    return float(s) if s not in ("", "None", None) else 0.0


def per_over_channels(deliveries: List[dict]) -> Tuple[List[int], Dict[str, list]]:
    """Aggregate one innings' deliveries into per-over channels.

    over index = int(float(ball)); runs = runs_off_bat + extras; wickets = count.
    Returns (per_over_run_list_indices, {channel_name: [values]}).
    """
    runs: Dict[int, int] = {}
    wkts: Dict[int, int] = {}
    for d in deliveries:
        o = int(float(d["ball"]))
        runs[o] = runs.get(o, 0) + int(_num(d["runs_off_bat"]) + _num(d["extras"]))
        if d.get("wicket_type"):
            wkts[o] = wkts.get(o, 0) + 1
    n = max(runs) + 1 if runs else 0
    runs_per_over = [runs.get(i, 0) for i in range(n)]
    wickets_per_over = [wkts.get(i, 0) for i in range(n)]
    cumulative, s = [], 0
    for v in runs_per_over:
        s += v
        cumulative.append(s)
    run_rate = [round(cumulative[i] / (i + 1), 2) for i in range(n)]
    return runs_per_over, {
        "runs_per_over": runs_per_over,
        "wickets_per_over": wickets_per_over,
        "cumulative_runs": cumulative,
        "run_rate": run_rate,
    }


# --- record construction ---------------------------------------------------

def build_records_for_match(match_id: str, deliveries_csv: bytes, info_csv: bytes,
                            cfg: Dict[str, Any], cache: Path) -> Tuple[List[dict], Dict[str, int]]:
    d, t = cfg["data"], cfg["text"]
    info = parse_info(info_csv)
    rows = list(csv.DictReader(io.StringIO(deliveries_csv.decode("utf-8"))))
    stat = {"emitted": 0, "short_innings": 0, "no_report": 0}
    out: List[dict] = []

    # Fetch the (whole-match) ESPNcricinfo report once, shared across the innings.
    report_text, art = fetch_report(match_id, d, cache)
    min_chars = int(t.get("min_report_chars", 400))

    innings_ids = sorted({r["innings"] for r in rows}, key=lambda x: int(x))
    for inn in innings_ids:
        deliveries = [r for r in rows if r["innings"] == inn]
        if not deliveries:
            continue
        batting = deliveries[0]["batting_team"]
        bowling = deliveries[0]["bowling_team"]
        _, chans = per_over_channels(deliveries)
        n_over = len(chans["runs_per_over"])
        if n_over < int(d["min_overs"]):
            stat["short_innings"] += 1
            continue

        # No usable recap for this match (~26% of IPL) → drop (no synthetic fallback).
        if not report_text or len(report_text) < min_chars:
            stat["no_report"] += 1
            continue

        report_url = d["espn_report_url_template"].format(match_id=match_id)
        intro = t["ts_intro_sentence"].format(team=batting)
        text = f"{report_text}\n\n{intro}"

        timeseries = [
            {"values": chans["runs_per_over"], "unit": "runs_per_over", "freq": "1over"},
            {"values": chans["wickets_per_over"], "unit": "wickets_per_over", "freq": "1over"},
            {"values": chans["cumulative_runs"], "unit": "cumulative_runs", "freq": "1over"},
            {"values": chans["run_rate"], "unit": "run_rate", "freq": "1over"},
        ]

        rec = {
            "text": text,
            "timeseries": timeseries,
            "task_type": "world_knowledge",
            "text_quality": "real",
            "match_id": match_id,
            "event": info.get("event"),
            "season": info.get("season"),
            "match_type": info.get("match_type"),
            "batting_team": batting,
            "bowling_team": bowling,
            "innings": int(inn),
            "venue": info.get("venue"),
            "city": info.get("city"),
            "start_date": info.get("date"),
            "overs_bowled": n_over,
            "total_runs": chans["cumulative_runs"][-1],
            "wickets": sum(chans["wickets_per_over"]),
            "winner": info.get("winner"),
            "report_url": report_url,
            "report_headline": art.get("headline"),
            "report_byline": art.get("byline"),
            "report_published": art.get("published"),
            "report_type": art.get("type"),
            "dataset": "cricket_report_overseries",
            "source": "cricsheet.org + site.api.espn.com",
            "series_id": f"cros_{match_id}_inn{inn}",
        }
        out.append(rec)
        stat["emitted"] += 1
    return out, stat


def _strip_html(s: str) -> str:
    """ESPN `article.story` is HTML with `<video1>`/`<photo1>` placeholder tags."""
    s = re.sub(r"<(?:video|photo|inline)\d*[^>]*>", "", s)
    s = re.sub(r"</(?:p|h[1-6]|li|div|ul|ol)>", "\n\n", s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def fetch_report(match_id: str, d: dict, cache: Path) -> Tuple[Optional[str], dict]:
    """Fetch the ESPNcricinfo match report via the ESPN parent sports API.

    KEY: www.espncricinfo.com and hs-consumer-api.espncricinfo.com 403 bots, but the
    ESPN *parent* host `site.api.espn.com` does not, and resolves purely by `event` —
    so the Cricsheet match_id alone is enough (the {league} path segment is just a
    required carrier; any valid cricket league id works for any match). Returns
    (clean_report_text | None, article_meta). Cached per match_id.

    NOTE: `article.story` is the whole-MATCH report, so both innings of a match share
    the same prose (localized by the per-innings <ts></ts> framing sentence).
    """
    fp = cache / "espn" / f"{match_id}.json"
    if fp.exists():
        raw = fp.read_bytes()
    else:
        url = d["espn_summary_url"].format(league=d["espn_carrier_league"], match_id=match_id)
        try:
            raw = http_get(url, d["user_agent"], int(d["timeout_s"]))
        except Exception:
            return None, {}
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(raw)
        time.sleep(float(d.get("request_delay_s", 0.4)))
    try:
        art = (json.loads(raw).get("article") or {})
    except Exception:
        return None, {}
    story = _strip_html(art.get("story") or "")
    return (story or None), art


def validate(rec: dict, channels: Sequence[str]) -> List[str]:
    e = []
    if rec["text"].count("<ts></ts>") != 1:
        e.append("ts token count")
    ts = rec.get("timeseries", [])
    if [c["unit"] for c in ts] != list(channels):
        e.append("channel set/order mismatch")
    lens = {len(c["values"]) for c in ts}
    if len(lens) != 1:
        e.append(f"channel length mismatch {sorted(lens)}")
    if lens and next(iter(lens)) != rec["overs_bowled"]:
        e.append("length != overs_bowled")
    return e


# --- pipeline --------------------------------------------------------------

def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    cache = rp(d["cache_dir"])
    channels = d["channels"]
    maxrec = out_cfg.get("max_records")

    zpath = download_cached(d["cricsheet_zip_url"], cache / "cricsheet.zip",
                            d["user_agent"], int(d["timeout_s"]))
    z = zipfile.ZipFile(zpath)
    match_ids = sorted(
        n[:-4] for n in z.namelist()
        if n.endswith(".csv") and not n.endswith("_info.csv")
    )

    stats = {"matches_scanned": 0, "innings_seen": 0, "emitted": 0,
             "short_innings": 0, "no_report": 0, "invalid": 0, "no_info": 0}
    records: List[dict] = []

    for mid in match_ids:
        info_name = f"{mid}_info.csv"
        if info_name not in z.namelist():
            stats["no_info"] += 1
            continue
        stats["matches_scanned"] += 1
        recs, mstat = build_records_for_match(
            mid, z.read(f"{mid}.csv"), z.read(info_name), cfg, cache)
        stats["innings_seen"] += mstat["emitted"] + mstat["short_innings"] + mstat["no_report"]
        stats["short_innings"] += mstat["short_innings"]
        stats["no_report"] += mstat["no_report"]
        for rec in recs:
            verr = validate(rec, channels)
            if verr:
                stats["invalid"] += 1
                continue
            records.append(rec)
            stats["emitted"] += 1
            if maxrec is not None and len(records) >= int(maxrec):
                break
        if maxrec is not None and len(records) >= int(maxrec):
            break

    report = {
        "cricsheet_zip_url": d["cricsheet_zip_url"],
        "report_source": "site.api.espn.com",
        "min_overs": d["min_overs"],
        "channels": channels,
        "stats": stats,
        "config_snapshot": cfg,
        "dry_run": dry,
    }

    if dry:
        if records:
            print("\n--- sample record ---")
            r0 = dict(records[0])
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:2400])
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
    ap = argparse.ArgumentParser(description="Build cricket per-over + narration → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s['emitted']} records "
          f"(matches {s['matches_scanned']}, innings {s['innings_seen']}, "
          f"short={s['short_innings']}, no_report={s['no_report']}, invalid={s['invalid']}).",
          file=sys.stderr)


if __name__ == "__main__":
    main()
