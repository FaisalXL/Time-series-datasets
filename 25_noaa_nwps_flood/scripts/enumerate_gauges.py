#!/usr/bin/env python3
"""enumerate_gauges.py — build the national gauge list for the NWPS flood harvest.

The main builder (build_cpt_jsonl.py) takes a `gauges` list of {lid, usgs}. The demo uses a
hand-verified seed of 9. National scale needs ALL impact-bearing gauges, which the NWPS API
does not give directly:

  Stage 1 (list):   GET /nwps/v1/gauges  -> ~12,756 gauges, but NO impact fields and (often)
                    no USGS id. ~13 MB, endpoint is flaky (intermittent 504s) -> retry.
  Stage 2 (detail): GET /nwps/v1/gauges/{lid} per gauge -> flood.impacts[] + usgs id. This is
                    the expensive part: one request per gauge to learn which gauges qualify.

This script does Stage 1 fully (cached) and Stage 2 over a bounded SAMPLE (default 200) to
MEASURE the qualifying fraction, crawl rate, and failure rate, then EXTRAPOLATE the full cost.
Pass --limit 0 to crawl all 12,756 (slow: ~1h+ at the polite delay). Qualifying gauges
({lid, usgs}, >=min_impacts impacts AND a USGS id) are written to a JSON the builder can load.

Usage:
  python enumerate_gauges.py                 # list + sample 200 details, report + extrapolate
  python enumerate_gauges.py --limit 500
  python enumerate_gauges.py --limit 0       # full crawl (writes the complete gauges file)
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "enumerate"
LIST_URL = "https://api.water.noaa.gov/nwps/v1/gauges"
DETAIL_URL = "https://api.water.noaa.gov/nwps/v1/gauges/{lid}"
UA = "cpt-dataset-builder/1.0 (research; contact flnu@usc.edu)"
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


def http_get(url: str, timeout: int, retries: int = 4) -> bytes | None:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            return urllib.request.urlopen(req, timeout=timeout, context=_SSL).read()
        except Exception as e:
            code = getattr(e, "code", type(e).__name__)
            if attempt == retries - 1:
                print(f"  FAIL after {retries} tries ({code}): {url}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))  # backoff
    return None


def fetch_list(timeout: int) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / "gauge_list.json"
    if dest.exists():
        doc = json.loads(dest.read_text())
    else:
        print("Stage 1: fetching national gauge list (~13 MB, may 504 -> retry) ...", file=sys.stderr)
        raw = http_get(LIST_URL, timeout)
        if not raw:
            raise SystemExit("could not fetch gauge list after retries")
        dest.write_bytes(raw)
        doc = json.loads(raw)
    gauges = doc.get("gauges", doc if isinstance(doc, list) else [])
    print(f"Stage 1: {len(gauges)} gauges in list; first-item keys: {sorted(gauges[0].keys())[:20]}")
    return gauges


def usgs_of(g: dict) -> str:
    for k in ("usgsId", "usgs_id", "usgs", "usgsID"):
        v = g.get(k)
        if v:
            return str(v)
    return ""


def detail_impacts_usgs(lid: str, timeout: int, delay: float) -> tuple[int, str]:
    dest = CACHE / "detail" / f"{lid}.json"
    if dest.exists():
        doc = json.loads(dest.read_text())
    else:
        raw = http_get(DETAIL_URL.format(lid=lid), timeout)
        if raw is None:
            return -1, ""       # -1 = fetch failed
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        time.sleep(delay)
        doc = json.loads(raw)
    flood = doc.get("flood") or {}
    impacts = [im for im in (flood.get("impacts") or []) if (im.get("statement") or "").strip()]
    return len(impacts), usgs_of(doc) or usgs_of({k: doc.get(k) for k in doc})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200, help="detail crawl size (0 = all)")
    ap.add_argument("--min-impacts", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--delay", type=float, default=0.25)
    args = ap.parse_args()

    gauges = fetch_list(args.timeout)
    n_total = len(gauges)
    list_with_usgs = sum(1 for g in gauges if usgs_of(g))
    print(f"Stage 1: {list_with_usgs}/{n_total} gauges carry a USGS id in the LIST itself.")

    crawl = gauges if args.limit == 0 else gauges[: args.limit]
    print(f"\nStage 2: crawling detail for {len(crawl)} gauges (delay={args.delay}s) ...")
    t0 = time.time()
    ge1 = ge5 = qualifying = failed = 0
    out = []
    for i, g in enumerate(crawl):
        lid = g.get("lid") or g.get("lidId") or ""
        if not lid:
            continue
        n_imp, usgs = detail_impacts_usgs(lid, args.timeout, args.delay)
        if n_imp < 0:
            failed += 1
            continue
        if n_imp >= 1:
            ge1 += 1
        if n_imp >= args.min_impacts:
            ge5 += 1
            usgs = usgs or usgs_of(g)
            if usgs:
                qualifying += 1
                out.append({"lid": lid, "usgs": usgs, "impacts": n_imp})
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(crawl)}  qualifying={qualifying} failed={failed}", flush=True)
    dt = time.time() - t0

    n = len(crawl) or 1
    rate = dt / n
    print("\n===== ENUMERATION SAMPLE RESULTS =====")
    print(f"crawled={len(crawl)}  failed={failed} ({failed/n:.1%})  wall={dt:.0f}s ({rate:.2f}s/gauge)")
    print(f">=1 impact:        {ge1}/{n} = {ge1/n:.1%}")
    print(f">={args.min_impacts} impacts:        {ge5}/{n} = {ge5/n:.1%}")
    print(f">={args.min_impacts} impacts + USGS: {qualifying}/{n} = {qualifying/n:.1%}  (harvestable)")
    if args.limit != 0:
        proj_q = round(qualifying / n * n_total)
        proj_t = rate * n_total / 60
        print(f"\nEXTRAPOLATION to all {n_total}:")
        print(f"  ~{proj_q} harvestable gauges; Stage-2 crawl ~{proj_t:.0f} min at this delay.")
    qfile = CACHE / "qualifying_gauges.json"
    qfile.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {len(out)} qualifying gauges -> {qfile}")


if __name__ == "__main__":
    main()
