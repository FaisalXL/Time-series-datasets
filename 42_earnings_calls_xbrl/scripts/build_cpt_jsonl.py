#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from earnings-call transcripts + SEC XBRL fundamentals.

One record = one (company, fiscal quarter): the call's own verbatim prepared remarks paired with
that company's trailing 12-quarter fundamentals (revenue, net income, diluted EPS) from SEC EDGAR
XBRL.

Text:   HuggingFace `Bose345/sp500_earnings_transcripts` (MIT), read with duckdb.
Series: SEC EDGAR XBRL companyfacts (public domain), joined ticker -> CIK.

Run `scripts/index_series.py` first: it turns the 2.1 GB of cached companyfacts into a 2 MB
per-filer series index. This builder reads that index, so the join never holds parsed
companyfacts in memory (the previous version did, which was invisible on a 50-record demo and
an OOM on the full 26,361-row scan).

  python scripts/index_series.py
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=5
  python scripts/build_cpt_jsonl.py --set output.max_records=null      # full build
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import duckdb
except ImportError as exc:                                       # pragma: no cover
    raise SystemExit("duckdb required. pip install -r requirements.txt") from exc
try:
    import yaml
except ImportError as exc:                                       # pragma: no cover
    raise SystemExit("PyYAML required. pip install -r requirements.txt") from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
CHANNEL_ORDER = ("revenue_usd", "net_income_usd", "eps_diluted_usd_per_share")


# --- config helpers (same conventions as the other packages) ----------------

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
        cur, parts = out, k.split(".")
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


# --- text ------------------------------------------------------------------

def prepared_remarks(structured: Any, raw: str, tcfg: dict) -> Tuple[str, str]:
    """(text, how) -- whole speaker turns up to a character budget.

    The old builder took `content[:max_text_chars]`. On a 53,501-char mean transcript that is a
    blind cut that lands mid-word -- the README's own example record ended
    `"...was down year-over-year at $379 million, r"`. `structured_content` is a list of
    {speaker, text} turns, so the budget can be spent in whole turns instead, at no cost. The
    raw-string path remains as a fallback for rows where the structured column is absent.
    """
    budget = int(tcfg["max_text_chars"]) if tcfg.get("max_text_chars") else None
    if isinstance(structured, (list, tuple)) and structured:
        parts: List[str] = []
        used = 0
        for turn in structured:
            if not isinstance(turn, dict):
                continue
            spk = (turn.get("speaker") or "").strip()
            txt = (turn.get("text") or "").strip()
            if not txt:
                continue
            block = f"{spk}: {txt}" if spk else txt
            if budget and parts and used + len(block) > budget:
                break
            parts.append(block)
            used += len(block) + 2
            if budget and used >= budget:
                break
        if parts:
            return "\n\n".join(parts), "speaker_turns"
    body = (raw or "").strip()
    if budget:
        body = body[:budget].rstrip()
    return body, "raw_prefix"


# --- series ----------------------------------------------------------------

def window_for(channels: dict, call_date: str, win: int) -> Tuple[List[str], Dict[str, dict]]:
    """(quarter-ends, {channel: points}) for the trailing `win` quarters ending <= the call.

    Ends are the UNION across channels, not the intersection. Measured on the full scan: the
    intersection yields 9,372 records and the union 22,593, because filers do not start every
    concept in the same quarter. A channel with no point at a given end gets `null` there, which
    is this corpus's existing convention -- but a channel with NO point anywhere in the window is
    dropped rather than shipped as twelve nulls.
    """
    avail = {}
    for name in CHANNEL_ORDER:
        pts = channels.get(name, {}).get("points", {})
        got = {e: v for e, v in pts.items() if e <= call_date}
        if got:
            avail[name] = got
    if len(avail) < 2:
        return [], {}
    ends = sorted(set().union(*[set(v) for v in avail.values()]))[-win:]
    live = {n: v for n, v in avail.items() if any(e in v for e in ends)}
    return ends, live


# --- record ----------------------------------------------------------------

# `recites` is tagged on an EXACT, SIGNED, TERMINAL-QUARTER match -- deliberately stricter than
# the gate that judges it. Passing `schema/validate.py --strict` is necessary but NOT sufficient
# here, and this is the measurement that says so.
#
# The gate's test (`_recites_a_value`) accepts any text number within 1% of any value in any
# channel. On a 12,000-char earnings-call excerpt -- which is dense with numbers -- that test
# fires on 62.5% of records, against a permutation control of 59.7%: a lift of **1.05x**. It is
# almost pure coincidence, and 23,202/23,202 still passed `--strict`, because the gate can only
# ask "is any number close to any value", not "is this the same number".
#
# Narrowing to the value the call is actually about -- the TERMINAL quarter, matched as an exact
# signed 2-decimal string -- gives 13.8% real against a **0.3%** control: a **42.4x lift**.
# Allowing any of the 12 window quarters instead drops the lift to 4.3x, because each extra
# quarter adds coincidence without adding meaning: the exec is discussing this quarter.
#
# In practice only diluted EPS can qualify. Revenue as raw digits appears in 6 of 19,454 records
# (0.0%) because execs say "$12.8 billion", and unit-scale reconciliation is explicitly NOT
# reciting under SCHEMA §7 -- the same ruling already applied to `58_fas_gain_attache`.
# An exact match is inside the gate's 1% tolerance by construction, so every record tagged here
# also passes the shared gate.
def recite_evidence(text: str, ends: List[str], live: Dict[str, dict]) -> List[dict]:
    ev = []
    if not ends:
        return ev
    last = ends[-1]
    for name in CHANNEL_ORDER:
        pts = live.get(name)
        if not pts or last not in pts:
            continue
        v = pts[last]
        if re.search(r"(?<![\d.])%s(?![\d])" % re.escape(f"{v:.2f}"), text):
            ev.append({"unit": name, "period_end": last, "value": v,
                       "literal": f"{v:.2f}"})
    return ev


def build_record(row: dict, entry: dict, cfg: dict) -> Tuple[Optional[dict], Optional[str]]:
    d, t, o = cfg["data"], cfg["text"], cfg["output"]
    win = int(d["window_quarters"])
    call = str(row["date"])[:10]

    ends, live = window_for(entry["channels"], call, win)
    if not ends:
        return None, "fewer_than_2_channels"
    if len(ends) < win:
        return None, "short_window"

    max_null = float(d.get("max_null_fraction", 0.25))
    slots = len(live) * len(ends)
    nulls = sum(1 for p in live.values() for e in ends if e not in p)
    if slots and nulls / slots > max_null:
        return None, "too_many_nulls"

    text, how = prepared_remarks(row.get("structured_content"), row.get("content"), t)
    if len(text) < int(t["min_text_chars"]):
        return None, "short_text"

    channels = [{"values": [live[n].get(e) for e in ends], "unit": n, "freq": "1q"}
                for n in CHANNEL_ORDER if n in live]
    ev = recite_evidence(text, ends, live)

    # The call discusses a quarter the series may not contain: for a Q4 call the 10-K reports the
    # year, so no standalone Q4 quarterly fact exists and the window ends at Q3. That is recorded
    # rather than hidden, because `fiscal_quarter` and `reported_quarter_end` disagreeing silently
    # is how the demo record ended up labelled "Q4 2025" over a series ending 2024-12-31.
    lag = (dt.date.fromisoformat(call) - dt.date.fromisoformat(ends[-1])).days
    q, yr = int(row["quarter"]), int(row["year"])

    rec = {
        "text": f"{text}\n\n<ts></ts>",
        "timeseries": channels,
        "task_type": "world_knowledge",
        "text_quality": "real",
        "alignment": "recites" if ev else "describes",
        "license": d["license"],
        "text_source": d["text_source"],
        "domain": d["domain"],
        "region": d["region"],
        "source": d["source_url"],
        "dataset": "earnings_calls_xbrl",
        "series_id": f"ecxbrl_{row['symbol']}_{yr}Q{q}",
        "meta": {
            "ticker": row["symbol"],
            "cik": entry["cik"],
            "company_name": row.get("company_name") or entry.get("entity"),
            "fiscal_quarter_label": f"Q{q} {yr}",
            "call_date": call,
            "series_end": ends[-1],
            "series_end_lag_days": lag,
            "reported_quarter_in_series": lag <= 60,
            "window_quarters": win,
            "n_channels": len(channels),
            "null_slots": nulls,
            "xbrl_concepts": {n: entry["channels"][n].get("concepts") for n in live},
            "xbrl_concepts_rejected": {n: r for n in live
                                       if (r := entry["channels"][n].get("rejected"))},
            "text_extraction": how,
            "text_chars": len(text),
            "recite_evidence": ev,
            "series_source": d["companyfacts_url"].format(cik=entry["cik"]),
        },
    }
    return rec, None


def check(rec: dict, win: int) -> List[str]:
    errs = []
    if rec["text"].count("<ts></ts>") != 1:
        errs.append("ts token count")
    lens = {len(c["values"]) for c in rec["timeseries"]}
    if len(lens) != 1:
        errs.append(f"channel length mismatch {sorted(lens)}")
    elif next(iter(lens)) != win:
        errs.append(f"window {sorted(lens)} != {win}")
    if not rec["timeseries"]:
        errs.append("no channels")
    return errs


# --- pipeline --------------------------------------------------------------

def load_index(cfg: dict) -> Dict[str, dict]:
    p = rp(cfg["data"]["series_index_path"])
    if not p.exists():
        raise SystemExit(f"missing {p} -- run scripts/index_series.py first")
    idx = json.loads(p.read_text())
    for cik, e in idx.items():
        e["cik"] = cik
    return idx


def load_tickers(cfg: dict) -> Tuple[Dict[str, str], int]:
    """ticker -> CIK. `company_tickers.json` lists only CURRENTLY-listed filers, so it misses
    every acquired, renamed or de-listed company: measured, 74 of 651 symbols (2,218 transcripts,
    8.4%) -- `BK` now trades as `BNY` and `MMC` as `MRSH` (same CIK, still filing), while EA,
    Juniper, Kellanova and Walgreens left the file in 2025. Their XBRL is still on file under the
    old CIK, so the overrides map recovers them (built by --resolve-tickers)."""
    cache = rp(cfg["data"]["cache_dir"])
    m = json.loads((cache / "company_tickers.json").read_text())
    t2c = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in m.values()}
    ov_path = cache / "ticker_overrides.json"
    ov = json.loads(ov_path.read_text()) if ov_path.exists() else {}
    for k, v in ov.items():
        t2c.setdefault(k.upper(), str(v).zfill(10))
    return t2c, len(ov)


def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    d, out_cfg = cfg["data"], cfg["output"]
    win = int(d["window_quarters"])
    maxrec = out_cfg.get("max_records")
    cache = rp(d["cache_dir"])

    idx = load_index(cfg)
    t2c, n_ov = load_tickers(cfg)
    print(f"series index: {len(idx)} filers | ticker map: {len(t2c):,} "
          f"({n_ov} recovered de-listed)", file=sys.stderr)

    con = duckdb.connect()
    pq = (cache / "transcripts.parquet").as_posix()
    cur = con.execute(f"""SELECT symbol, quarter, year, CAST(date AS VARCHAR) date, content,
                                 structured_content, company_name
                          FROM read_parquet('{pq}')
                          WHERE CAST(date AS VARCHAR) >= '{d['min_transcript_date']}'
                          ORDER BY CAST(date AS VARCHAR) DESC""")

    stats = collections.Counter()
    align = collections.Counter()
    lagb = collections.Counter()
    seen_text: set = set()
    seen_sid: set = set()

    op = rp(out_cfg["output_path"])
    op.parent.mkdir(parents=True, exist_ok=True)
    fh = None if dry else op.open("w", encoding="utf-8")
    samples: List[dict] = []
    first: Optional[dict] = None
    try:
        while True:
            batch = cur.fetchmany(500)
            if not batch:
                break
            cols = [c[0] for c in cur.description]
            for tup in batch:
                row = dict(zip(cols, tup))
                stats["scanned"] += 1
                cik = t2c.get((row["symbol"] or "").upper())
                if not cik:
                    stats["no_cik"] += 1; continue
                entry = idx.get(cik)
                if not entry:
                    stats["no_xbrl_facts"] += 1; continue
                rec, why = build_record(row, entry, cfg)
                if rec is None:
                    stats[why] += 1; continue
                errs = check(rec, win)
                if errs:
                    stats["invalid"] += 1; continue
                if rec["series_id"] in seen_sid:
                    stats["dup_series_id"] += 1; continue
                h = hash(rec["text"])
                if h in seen_text:
                    stats["dup_text"] += 1; continue
                seen_sid.add(rec["series_id"]); seen_text.add(h)
                stats["emitted"] += 1
                align[rec["alignment"]] += 1
                lg = rec["meta"]["series_end_lag_days"]
                lagb["<=60d" if lg <= 60 else "61-120d" if lg <= 120
                     else "121-210d" if lg <= 210 else ">210d"] += 1
                if first is None:
                    first = rec
                if fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if len(samples) < int(out_cfg.get("n_samples", 3)):
                    samples.append(rec)
                if stats["emitted"] % 2000 == 0:
                    print(f"  {stats['emitted']:,} emitted "
                          f"({stats['scanned']:,} scanned)", file=sys.stderr, flush=True)
                if maxrec is not None and stats["emitted"] >= int(maxrec):
                    break
            if maxrec is not None and stats["emitted"] >= int(maxrec):
                break
    finally:
        if fh:
            fh.close()

    report = {
        "dataset": "earnings_calls_xbrl",
        "min_transcript_date": d["min_transcript_date"],
        "window_quarters": win,
        "stats": dict(stats),
        "alignment": dict(align),
        "series_end_lag": dict(lagb),
        "config_snapshot": cfg,
        "dry_run": dry,
    }
    if dry:
        if first:
            r0 = dict(first); r0["text"] = r0["text"][:400] + "…"
            print("\n--- sample record ---")
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:2600])
        print("\n" + json.dumps({"stats": dict(stats), "alignment": dict(align),
                                 "series_end_lag": dict(lagb)}, indent=2))
        return report

    # samples as REAL JSONL. The committed sample was a pretty-printed JSON array under a
    # .jsonl name -- json.loads dies on line 2, so any per-line consumer globbing *.jsonl
    # breaks on it. That defect has now shipped four times in this corpus.
    if samples and out_cfg.get("samples_path"):
        sp = rp(out_cfg["samples_path"]); sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("w", encoding="utf-8") as sfh:
            for r in samples:
                sfh.write(json.dumps(r, ensure_ascii=False) + "\n")
    rpath = rp(out_cfg["report_path"]); rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Build earnings-calls + XBRL -> CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    drops = {k: v for k, v in s.items() if k not in ("scanned", "emitted")}
    print(f"\nDone: {s.get('emitted', 0):,} records from {s.get('scanned', 0):,} transcripts.",
          file=sys.stderr)
    print(f"  alignment: {rep['alignment']}", file=sys.stderr)
    print(f"  skipped:   {drops}", file=sys.stderr)


if __name__ == "__main__":
    main()
