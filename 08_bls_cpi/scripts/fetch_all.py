#!/usr/bin/env python3
"""Harvest every BLS CPI news release document Wayback holds (txt / htm / pdf).

Writes `.cache/releases/<release_date>.<ext>` plus `.cache/probe/fetch_log.json`.
Idempotent: re-runs skip anything already on disk. bls.gov itself 403s automated
access, so every fetch goes through web.archive.org.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / ".cache/probe"
REL = ROOT / ".cache/releases"
CDX = "http://web.archive.org/cdx/search/cdx"
DATE_RE = re.compile(r"/cpi_(\d{6}|\d{8})\.(txt|htm|html|pdf)$", re.I)
EXT_PREF = ("txt", "htm", "pdf")

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (compatible; CPTDatasetBuilder/1.0; research use)"})


def cdx(prefix: str, name: str, status_filter: bool = True, limit: int = 20000):
    """One row per unique URL. Without `collapse` the row count blows past `limit`
    and the tail of the alphabet is silently truncated. Called twice per prefix:
    once filtered to 200s (best snapshot per URL) and once unfiltered, because
    collapse runs before filtering and would otherwise drop a URL whose *oldest*
    capture was a 404 even though later captures are fine."""
    f = PROBE / f"cdx_{name}.json"
    if f.exists():
        return json.loads(f.read_text())
    params = {"url": prefix, "matchType": "prefix", "output": "json",
              "fl": "original,timestamp,statuscode", "collapse": "urlkey", "limit": limit}
    if status_filter:
        params["filter"] = "statuscode:200"
    r = S.get(CDX, params=params, timeout=180)
    r.raise_for_status()
    rows = r.json()
    PROBE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rows))
    return rows


def build_universe() -> dict:
    """(release_date, ext) -> list of (timestamp, original_url) snapshot candidates."""
    uni: dict = defaultdict(lambda: defaultdict(list))
    prefixes = (("hist", "www.bls.gov/news.release/history/cpi_"),
                ("arch", "www.bls.gov/news.release/archives/cpi_"))
    jobs = [(f"{n}_{'ok' if sf else 'any'}", p, sf)
            for n, p in prefixes for sf in (True, False)]
    for name, prefix, sf in jobs:
        for row in cdx(prefix, name, status_filter=sf)[1:]:
            orig, ts = row[0], row[1]
            status = row[2] if len(row) > 2 else "-"
            if status not in ("200", "-"):
                continue
            m = DATE_RE.search(orig.split("?")[0])
            if not m:
                continue
            d, ext = m.group(1), m.group(2).lower()
            if len(d) == 6:
                mo, dy, yr = int(d[:2]), int(d[2:4]), 1900 + int(d[4:])
            else:
                mo, dy, yr = int(d[:2]), int(d[2:4]), int(d[4:])
            if not (1990 <= yr <= 2027 and 1 <= mo <= 12 and 1 <= dy <= 31):
                continue
            ext = "htm" if ext in ("htm", "html") else ext
            uni[f"{yr:04d}-{mo:02d}-{dy:02d}"][ext].append((ts, orig, d))
    return uni


def get(url: str, timeout: int = 60, tries: int = 3):
    for attempt in range(tries):
        try:
            r = S.get(url, timeout=timeout)
        except requests.RequestException as exc:
            last = str(exc)
        else:
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code in (404, 403):
                return None
        time.sleep(2.0 * (attempt + 1))
    print(f"    give up: {last}", file=sys.stderr)
    return None


def main() -> None:
    REL.mkdir(parents=True, exist_ok=True)
    uni = build_universe()
    dates = sorted(uni)
    print(f"{len(dates)} release dates in CDX", file=sys.stderr)

    log = {}
    logf = PROBE / "fetch_log.json"
    if logf.exists():
        log = json.loads(logf.read_text())

    for i, date in enumerate(dates):
        got = log.get(date, {})
        for ext in EXT_PREF:
            if ext not in uni[date]:
                continue
            out = REL / f"{date}.{ext}"
            if out.exists() and out.stat().st_size > 0:
                got[ext] = {"ok": True, "bytes": out.stat().st_size, **got.get(ext, {})}
                continue
            if got.get(ext, {}).get("dead"):
                continue
            # newest snapshot first: most complete rendering of the page
            cands = sorted(uni[date][ext], key=lambda x: x[0], reverse=True)[:4]
            saved = False
            for ts, orig, fname in cands:
                r = get(f"https://web.archive.org/web/{ts}id_/{orig}")
                time.sleep(0.6)
                if r is None or len(r.content) < 400:
                    continue
                out.write_bytes(r.content)
                got[ext] = {"ok": True, "bytes": len(r.content), "ts": ts,
                            "orig": orig, "fname": fname}
                saved = True
                break
            if not saved:
                got[ext] = {"dead": True, "n_cands": len(uni[date][ext])}
        log[date] = got
        if i % 20 == 0:
            logf.write_text(json.dumps(log, indent=0))
            print(f"  [{i}/{len(dates)}] {date} {list(got)}", file=sys.stderr)
    logf.write_text(json.dumps(log, indent=0))

    have = sum(1 for d in log.values() if any(v.get("ok") for v in d.values()))
    print(f"\ndocs on disk for {have}/{len(dates)} release dates", file=sys.stderr)


if __name__ == "__main__":
    main()
