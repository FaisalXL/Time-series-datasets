#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from Statistics Canada "The Daily" — Consumer Price Index.

One record = one CPI release (one data month): the release's own lead + major-component
narrative paragraphs (real prose) paired, under a single <ts>, with a trailing N-month window
of the NOT-SEASONALLY-ADJUSTED 12-month (year-over-year) percentage change for the all-items
index + 4 major components (gasoline, food purchased from stores, shelter, transportation).
text_quality "real".

Text  : "The Daily" bulletin HTML (https://www150.statcan.gc.ca/n1/daily-quotidien/...). No
        clean archive listing of *just* CPI releases exists, and URL dates/letter-suffixes are
        NOT guessable (many unrelated Daily articles share a date). Instead every release page
        links its own "Previous release" — the prior month's CPI article specifically. The build
        starts at a configured seed URL (a real, verified CPI release) and walks that link
        BACKWARD, discovering every earlier release URL from the page itself (no guessing).
Series: Statistics Canada Web Data Service (WDS) REST API — keyless JSON, table 18-10-0004
        "Consumer Price Index, monthly, not seasonally adjusted" (NSA). We fetch each channel's
        StatCan "vector" (a stable per-series ID; see config.example.yaml for how the 5 vector
        IDs were resolved via getSeriesInfoFromCubePidCoord) as raw monthly INDEX LEVELS, then
        compute the 12-month %-change ourselves (base-invariant — sidesteps historical CPI
        reference-base rebasing; verified to reproduce the release's own stated % exactly).

Examples:
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import ssl
import sys
import time
import urllib.parse
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

_MONTH_NUM = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}

_PREV_RE = re.compile(
    r'href="(/daily-quotidien/\d{6}/dq\d{6}[a-z]-eng\.htm)"[^>]*>\s*Previous release', re.I)
_TITLE_RE = re.compile(r"Consumer Price Index,\s*([A-Za-z]+)\s+(\d{4})")
_RELDATE_RE = re.compile(r"Released:&#160;([\d-]+)")
_BLOCK_RE = re.compile(r"<(p|h2|h3)\b[^>]*>(.*?)</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


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

def http_get(url: str, ua: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL).read()


def download_cached(url: str, dest: Path, ua: str, timeout: int, delay: float) -> bytes:
    if dest.exists():
        return dest.read_bytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Fetching {url} ...", file=sys.stderr)
    raw = http_get(url, ua, timeout)
    dest.write_bytes(raw)
    time.sleep(delay)
    return raw


# --- WDS series (index levels -> YoY %) --------------------------------------

def fetch_vectors(d: Dict[str, Any], cache: Path) -> Dict[int, Dict[str, float]]:
    """Fetch every configured channel's vector as {ym -> index level}, one WDS call for all
    vectors combined (getDataFromVectorByReferencePeriodRange accepts a comma list)."""
    ids = [str(c["vector_id"]) for c in d["channels"]]
    dest = cache / "vectors" / f"{'_'.join(ids)}.json"
    if dest.exists():
        doc = json.loads(dest.read_text())
    else:
        url = (f"{d['wds_base']}/getDataFromVectorByReferencePeriodRange"
               f"?vectorIds={','.join(ids)}"
               f"&startRefPeriod={d['vector_fetch_start']}"
               f"&endReferencePeriod=2035-01-01")
        raw = download_cached(url, dest, d["user_agent"], int(d["timeout_s"]),
                              float(d.get("request_delay_s", 2.0)))
        doc = json.loads(raw)
    out: Dict[int, Dict[str, float]] = {}
    for r in doc:
        if r.get("status") != "SUCCESS":
            continue
        o = r["object"]
        series: Dict[str, float] = {}
        for p in o["vectorDataPoint"]:
            ym = p["refPer"][:7]
            if p.get("value") is not None:
                series[ym] = float(p["value"])
        out[o["vectorId"]] = series
    return out


def yoy_series(levels: Dict[str, float]) -> Dict[str, Optional[float]]:
    """{ym -> 12-month %-change} computed from raw index levels. Base-invariant (sidesteps CPI
    reference-base rebasing across eras) and matches the release's own stated % to 1 decimal."""
    out: Dict[str, Optional[float]] = {}
    for ym, v in levels.items():
        y, m = int(ym[:4]), int(ym[5:7])
        py = y - 1
        prev_ym = f"{py:04d}-{m:02d}"
        pv = levels.get(prev_ym)
        out[ym] = round((v / pv - 1.0) * 100.0, 1) if pv else None
    return out


def add_months(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    total = y * 12 + (m - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


# --- "The Daily" HTML: navigation + narrative extraction ---------------------

def strip_div_blocks(text: str, class_marker: str) -> str:
    """Remove every <div ...class="...marker...">...</div> block (nesting-aware scan). Used to
    drop chart-image panels and the top indicator dashboard, both wrapped in class="sd-thumbnail"."""
    out: List[str] = []
    i, n = 0, len(text)
    open_re = re.compile(r'<div\b[^>]*class="([^"]*)"[^>]*>', re.I)
    tag_re = re.compile(r"<(/?)div\b[^>]*>", re.I)
    while i < n:
        m = open_re.search(text, i)
        if not m:
            out.append(text[i:])
            break
        if class_marker in m.group(1).split():
            out.append(text[i:m.start()])
            depth = 1
            j = m.end()
            while depth > 0:
                tm = tag_re.search(text, j)
                if not tm:
                    j = n
                    break
                depth += -1 if tm.group(1) == "/" else 1
                j = tm.end()
            i = j
        else:
            out.append(text[i:m.end()])
            i = m.end()
    return "".join(out)


def clean_block(inner_html: str) -> str:
    txt = _TAG_RE.sub("", inner_html)          # empty replacement: keeps "-2</span>.5%" -> "-2.5%"
    txt = html_lib.unescape(txt).replace("\xa0", " ")
    return re.sub(r"\s+", " ", txt).strip()


def extract_narrative(raw_html: str, stop_markers: Sequence[str], max_chars: int) -> Optional[str]:
    body = strip_div_blocks(raw_html, "sd-thumbnail")
    i = body.find("sd-release-date")
    if i < 0:
        return None
    p_end = body.find("</p>", i)
    body = body[p_end + 4:] if p_end > 0 else body[i:]
    blocks: List[str] = []
    total = 0
    for m in _BLOCK_RE.finditer(body):
        clean = clean_block(m.group(2))
        if not clean:
            continue
        if any(clean.startswith(sm) for sm in stop_markers):
            break
        blocks.append(clean)
        total += len(clean)
        if total >= max_chars:
            break
    if not blocks:
        return None
    return "\n\n".join(blocks)


def parse_release(raw_html: str) -> Optional[Dict[str, Any]]:
    tm = _TITLE_RE.search(raw_html)
    rm = _RELDATE_RE.search(raw_html)
    if not tm or not rm:
        return None
    month_name, year = tm.group(1), int(tm.group(2))
    if month_name not in _MONTH_NUM:
        return None
    data_month = f"{year:04d}-{_MONTH_NUM[month_name]:02d}"
    pm = _PREV_RE.search(raw_html)
    prev_url = ("https://www150.statcan.gc.ca" + pm.group(1)) if pm else None
    return {"data_month": data_month, "release_date": rm.group(1),
            "month_name": month_name, "prev_url": prev_url}


def endpoint_recited(narrative: str, v: float) -> bool:
    """True iff the channel's own current-month YoY% value is stated verbatim in the prose
    (WASDE-style per-record alignment tagging: recites if ANY channel endpoint is stated)."""
    pat = re.compile(r"(?<![\d.])" + re.escape(f"{v:.1f}") + r"\s?%")
    return bool(pat.search(narrative))


# --- pipeline -----------------------------------------------------------------

def build(cfg: Dict[str, Any]) -> Tuple[List[dict], Dict[str, Any]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    cache = rp(d["cache_dir"])
    maxrec = out_cfg.get("max_records")
    win = int(d["window_months"])
    min_win = int(d["min_window_months"])
    min_date = str(d["min_release_date"])
    max_hops = int(d["max_hops"])
    anchor = d["anchor_channel"]

    levels_by_vec = fetch_vectors(d, cache)
    yoy_by_vec = {vid: yoy_series(lv) for vid, lv in levels_by_vec.items()}
    chans = d["channels"]
    for c in chans:
        if c["vector_id"] not in yoy_by_vec:
            raise SystemExit(f"WDS returned no data for vector {c['vector_id']} ({c['name']})")

    stat = {"hops": 0, "candidates": 0, "emitted": 0, "recites": 0, "describes": 0,
            "no_title_match": 0, "no_narrative": 0, "short_text": 0,
            "short_window": 0, "invalid": 0, "stopped_min_date": False,
            "stopped_max_hops": False, "stopped_no_prev_link": False}
    records: List[dict] = []

    url = d["seed_release_url"]
    ua, timeout = d["user_agent"], int(d["timeout_s"])
    delay = float(d.get("request_delay_s", 2.0))
    releases_dir = cache / "releases"

    while url:
        if stat["hops"] >= max_hops:
            stat["stopped_max_hops"] = True
            break
        stat["hops"] += 1
        slug_m = re.search(r"/(dq\d{6}[a-z])-eng\.htm", url)
        slug = slug_m.group(1) if slug_m else f"hop{stat['hops']}"
        raw = download_cached(url, releases_dir / f"{slug}.html", ua, timeout, delay)
        raw_text = raw.decode("utf-8", "replace")
        info = parse_release(raw_text)
        if not info:
            stat["no_title_match"] += 1
            break  # chain is expected to be pure CPI-only; a mismatch means we've walked off it
        if info["release_date"] < min_date:
            stat["stopped_min_date"] = True
            break
        if maxrec is not None and len(records) >= int(maxrec):
            break

        stat["candidates"] += 1
        data_month = info["data_month"]

        # trailing window of `win` months ending at data_month, all channels index-aligned
        win_months = [add_months(data_month, -k) for k in range(win - 1, -1, -1)]
        chan_series = {}
        ok = True
        for c in chans:
            vals = [yoy_by_vec[c["vector_id"]].get(m) for m in win_months]
            if any(v is None for v in vals) or len(vals) < min_win:
                ok = False
                break
            chan_series[c["name"]] = vals
        if not ok:
            stat["short_window"] += 1
            url = info["prev_url"]
            if not url:
                stat["stopped_no_prev_link"] = True
            continue

        narrative = extract_narrative(raw_text, t["stop_markers"], int(t.get("max_chars", 6000)))
        if not narrative:
            stat["no_narrative"] += 1
            url = info["prev_url"]
            if not url:
                stat["stopped_no_prev_link"] = True
            continue
        if len(narrative) < int(t.get("min_text_chars", 200)):
            stat["short_text"] += 1
            url = info["prev_url"]
            if not url:
                stat["stopped_no_prev_link"] = True
            continue

        endpoints = {name: vals[-1] for name, vals in chan_series.items()}
        align = "recites" if any(endpoint_recited(narrative, v) for v in endpoints.values()) else "describes"
        stat["recites" if align == "recites" else "describes"] += 1

        # No generated/templated framing text: the <ts></ts> placeholder is appended directly
        # to the real scraped narrative, nothing else is added.
        text = f"{narrative}\n\n<ts></ts>"

        timeseries = [{"values": chan_series[c["name"]], "unit": c["name"], "freq": "1M"} for c in chans]

        try:
            rec = emit_record(
                text=text,
                timeseries=timeseries,
                alignment=align,
                license="cc-by-4.0",
                source=url,
                dataset="statcan_daily",
                series_id=f"statcan_cpi:{data_month}",
                domain="macro_econ",
                region="CA",
                period_start=f"{win_months[0]}-01",
                period_end=f"{data_month}-01",
                meta={
                    "release_date": info["release_date"],
                    "data_month": data_month,
                    "table": "18-10-0004 (CPI, monthly, not seasonally adjusted)",
                    "anchor_channel": anchor,
                    "channels_human": [c["human"] for c in chans],
                    "vectors": {c["name"]: c["vector_id"] for c in chans},
                    "window_months": win,
                    "endpoints_pct": endpoints,
                    "series_note": ("12-month (year-over-year) percentage change, computed from "
                                     "raw NSA index levels (base-invariant; sidesteps historical "
                                     "CPI reference-base rebasing)"),
                    "true_license": "Statistics Canada Open Licence",
                    "true_license_url": "https://www.statcan.gc.ca/en/reference/licence",
                },
            )
        except ValueError as e:
            stat["invalid"] += 1
            print(f"  invalid record for {data_month}: {e}", file=sys.stderr)
            url = info["prev_url"]
            if not url:
                stat["stopped_no_prev_link"] = True
            continue

        records.append(rec)
        stat["emitted"] += 1
        url = info["prev_url"]
        if not url:
            stat["stopped_no_prev_link"] = True

    return records, stat


def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    records, stats = build(cfg)
    report = {"dataset": "statcan_daily", "stats": stats, "config_snapshot": cfg, "dry_run": dry}
    if dry:
        if records:
            r0 = dict(records[0]); r0["text"] = r0["text"][:700] + "…"
            print("\n--- sample record ---")
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:2600])
        print("\n" + json.dumps(stats, indent=2))
        return report
    out_cfg = cfg["output"]
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
    ap = argparse.ArgumentParser(description="Build StatCan Daily CPI -> CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s['emitted']} records from {s['hops']} hops "
          f"[{s['recites']} recites + {s['describes']} describes] "
          f"(short_window={s['short_window']}, no_narrative={s['no_narrative']}, "
          f"short_text={s['short_text']}, invalid={s['invalid']}).", file=sys.stderr)


if __name__ == "__main__":
    main()
