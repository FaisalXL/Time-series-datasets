#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from earnings-call transcripts + SEC XBRL fundamentals.

One record = one (company, fiscal quarter): the earnings-call transcript (the exec
recites revenue / net income / EPS) paired with that company's trailing 12-quarter
fundamentals from SEC EDGAR XBRL. The narration describes the numbers → "describes".

Text: HuggingFace `Bose345/sp500_earnings_transcripts` (MIT), read with duckdb.
Series: SEC EDGAR XBRL companyfacts (public domain), joined by ticker->CIK.

Examples:
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=5
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.request
import ssl
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import duckdb
except ImportError as exc:  # pragma: no cover
    raise SystemExit("duckdb required. pip install -r requirements.txt") from exc
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
    dest.write_bytes(http_get(url, ua, timeout))
    return dest


# --- SEC XBRL --------------------------------------------------------------

def load_ticker_map(cfg, cache: Path) -> Dict[str, str]:
    d = cfg["data"]
    f = download_cached(d["ticker_map_url"], cache / "company_tickers.json",
                        d["sec_user_agent"], int(d["timeout_s"]))
    m = json.loads(f.read_text())
    return {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in m.values()}


def get_companyfacts(cik: str, cfg, cache: Path) -> Optional[dict]:
    d = cfg["data"]
    fp = cache / f"cf_{cik}.json"
    if fp.exists():
        try: return json.loads(fp.read_text())
        except Exception: return None
    url = d["companyfacts_url"].format(cik=cik)
    try:
        raw = http_get(url, d["sec_user_agent"], int(d["timeout_s"]))
    except Exception:
        return None
    fp.write_bytes(raw)
    time.sleep(float(d.get("request_delay_s", 0.15)))
    try: return json.loads(raw)
    except Exception: return None


def quarterly_series(cf: dict, concept: str, unit: str, fallback: Optional[str]) -> Dict[str, float]:
    """{period-end 'YYYY-MM-DD': value} for quarterly-duration (80-100 day) facts."""
    facts = cf.get("facts", {}).get("us-gaap", {})
    for c in (concept, fallback):
        if not c or c not in facts:
            continue
        units = facts[c].get("units", {}).get(unit)
        if not units:
            continue
        out: Dict[str, float] = {}
        for f in units:
            s, e = f.get("start"), f.get("end")
            if not (s and e):
                continue
            try:
                days = (dt.date.fromisoformat(e) - dt.date.fromisoformat(s)).days
            except ValueError:
                continue
            if 80 <= days <= 100:
                out[e] = f["val"]   # later filings overwrite the same period-end (fine)
        if out:
            return out
    return {}


# --- record construction ---------------------------------------------------

def build_record(row: dict, cf: dict, cfg) -> Tuple[Optional[dict], Optional[str]]:
    d, t = cfg["data"], cfg["text"]
    win = int(d["window_quarters"])
    tdate = str(row["date"])[:10]

    # per-channel {end: val}, aligned on common quarter-ends <= transcript date
    series: List[Tuple[str, Dict[str, float]]] = []
    for ch in d["channels"]:
        s = quarterly_series(cf, ch["concept"], ch["unit"], ch.get("fallback"))
        s = {e: v for e, v in s.items() if e <= tdate}
        series.append((ch["name"], s))
    common = sorted(set.intersection(*[set(s.keys()) for _, s in series])) if all(s for _, s in series) else []
    ends = common[-win:]
    if len(ends) < win:
        return None, f"short window ({len(ends)}/{win})"

    channels = [{"values": [round(float(s[e]), 4) for e in ends], "unit": name, "freq": "1q"}
                for name, s in series]

    q = int(row["quarter"]); yr = int(row["year"])
    fq = f"Q{q} {yr}"
    body = (row.get("content") or "").strip()
    maxc = t.get("max_text_chars")
    if maxc:
        body = body[:int(maxc)].rstrip()
    if len(body) < int(t.get("min_text_chars", 200)):
        return None, "short text"
    intro = t["ts_intro_sentence"].format(n=win, quarter=fq)
    text = f"{body}\n\n{intro}"

    rec = {
        "text": text,
        "timeseries": channels,
        "task_type": "world_knowledge",
        "text_quality": "real",
        "ticker": row["symbol"],
        "cik": cf.get("cik") and str(cf["cik"]).zfill(10),
        "company_name": row.get("company_name"),
        "fiscal_quarter": fq,
        "reported_quarter_end": ends[-1],
        "call_date": tdate,
        "window_quarters": win,
        "dataset": "earnings_calls_xbrl",
        "source": "huggingface.co/Bose345 + data.sec.gov",
        "series_id": f"ecxbrl_{row['symbol']}_{yr}Q{q}",
    }
    return rec, None


def validate(rec: dict, win: int) -> List[str]:
    e = []
    if rec["text"].count("<ts></ts>") != 1:
        e.append("ts token count")
    ts = rec.get("timeseries", [])
    lens = {len(c["values"]) for c in ts}
    if len(lens) != 1:
        e.append(f"channel length mismatch {sorted(lens)}")
    if lens and next(iter(lens)) != win:
        e.append(f"window {sorted(lens)} != {win}")
    return e


# --- pipeline --------------------------------------------------------------

def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    d, out_cfg = cfg["data"], cfg["output"]
    cache = rp(d["cache_dir"])
    win = int(d["window_quarters"])
    maxrec = out_cfg.get("max_records")

    print("Downloading transcript parquet (cached)...", file=sys.stderr)
    pq = download_cached(d["transcript_parquet_url"], cache / "transcripts.parquet",
                         "Mozilla/5.0", int(d["timeout_s"]))
    t2c = load_ticker_map(cfg, cache)

    con = duckdb.connect()
    q = f"""SELECT symbol, quarter, year, CAST(date AS VARCHAR) date, content, company_name
            FROM read_parquet('{pq.as_posix()}')
            WHERE CAST(date AS VARCHAR) >= '{d['min_transcript_date']}'
            ORDER BY CAST(date AS VARCHAR) DESC"""
    cur = con.execute(q)

    stats = {"scanned": 0, "emitted": 0, "no_cik": 0, "no_facts": 0,
             "short_window": 0, "short_text": 0, "invalid": 0}
    records: List[dict] = []
    cf_cache: Dict[str, Optional[dict]] = {}

    while True:
        batch = cur.fetchmany(200)
        if not batch:
            break
        cols = [c[0] for c in cur.description]
        for tup in batch:
            row = dict(zip(cols, tup))
            stats["scanned"] += 1
            tk = (row["symbol"] or "").upper()
            cik = t2c.get(tk)
            if not cik:
                stats["no_cik"] += 1; continue
            if cik not in cf_cache:
                cf_cache[cik] = get_companyfacts(cik, cfg, cache)
            cf = cf_cache[cik]
            if not cf:
                stats["no_facts"] += 1; continue
            rec, err = build_record(row, cf, cfg)
            if rec is None:
                stats["short_window" if "window" in err else "short_text"] += 1; continue
            verr = validate(rec, win)
            if verr:
                stats["invalid"] += 1; continue
            records.append(rec); stats["emitted"] += 1
            if maxrec is not None and len(records) >= int(maxrec):
                batch = []; break
        if maxrec is not None and len(records) >= int(maxrec):
            break

    report = {
        "start_utc": "n/a",
        "min_transcript_date": d["min_transcript_date"],
        "window_quarters": win,
        "channels": [c["name"] for c in d["channels"]],
        "stats": stats,
        "config_snapshot": cfg,
        "dry_run": dry,
    }
    if dry:
        if records:
            print("\n--- sample record ---")
            r0 = dict(records[0]); r0["text"] = r0["text"][:400] + "…"
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:2200])
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
    ap = argparse.ArgumentParser(description="Build earnings-calls + XBRL → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s['emitted']} records (scanned {s['scanned']}, no_cik={s['no_cik']}, "
          f"no_facts={s['no_facts']}, short_window={s['short_window']}, invalid={s['invalid']}).",
          file=sys.stderr)


if __name__ == "__main__":
    main()
