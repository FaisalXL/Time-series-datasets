#!/usr/bin/env python3
"""Enumerate the GAIN report archive -> a resumable report index.

WHY THIS EXISTS: the original build pinned two filenames in config because no report-LIST endpoint
was known ("Report enumeration is unsolved", README). It is solved: the GAIN SPA's own search
service is a public POST API, reachable with the ANONYMOUS token the SPA itself hands out.

    POST {api}/Search/GetSearchResults      body: SearchFilter (categoryIds/postIds/postNames/
                                                  countryIds/fromDate/toDate)
    POST {api}/../token                     client_credentials + the SPA's fixed anonymous secret

Both were read out of the SPA's own bundle (gain.fas.usda.gov/main-es2018.js): `getFixedToken()`
returns a hardcoded all-zeroes "anonymous user token", which the bundle swaps for a bearer token.
This is the site's own unauthenticated read path, not a credential bypass -- without the bearer the
same endpoint answers 401 {"message":"Token has expired"}.

NO SILENT CAP (measured 2026-08-19): a single 2025-01-01..2025-12-31 call returns 1372 rows, and
the sum of that year's twelve monthly calls is also 1372, all reportIds distinct. So the API is not
truncating a year window and the cheap per-year walk is safe. 2025-10 legitimately returns 0 rows
(the Oct-2025 US federal shutdown), which is a real publishing gap, not a fetch failure -- the
per-year total corroborates it.

Usage:
    python scripts/census.py --config config.example.yaml            # full walk -> index
    python scripts/census.py --config config.example.yaml --summary  # report on an existing index
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent

API = "https://apps.fas.usda.gov/newgainapi/api"
TOKEN_URL = "https://apps.fas.usda.gov/newgainapi/token"
# the SPA's own getFixedToken() -- "anonymous user token"
ANON_SECRET = ("00000000-0000-0000-0000-00000000000000000000-0000-0000-0000-000000000000")
HDRS = {"origin": "https://gain.fas.usda.gov", "referer": "https://gain.fas.usda.gov/"}


def get_token() -> str:
    body = (f"client_id=eAuthClient&client_secret={ANON_SECRET}"
            "&grant_type=client_credentials").encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={**HDRS, "content-type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)["access_token"]


def search(token: str, from_date: str, to_date: str, tries: int = 6) -> list[dict]:
    body = json.dumps({"categoryIds": [], "postIds": [], "postNames": [], "countryIds": [],
                       "fromDate": from_date, "toDate": to_date}).encode()
    for attempt in range(tries):
        req = urllib.request.Request(
            f"{API}/Search/GetSearchResults", data=body,
            headers={**HDRS, "content-type": "application/json",
                     "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:                      # token aged out mid-walk
                raise
            wait = min(2 ** attempt, 30)
            print(f"    HTTP {e.code} -- retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
        except Exception as e:                     # noqa: BLE001 - transient network
            wait = min(2 ** attempt, 30)
            print(f"    {type(e).__name__} {e} -- retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"search failed after {tries} tries: {from_date}..{to_date}")


def walk(cfg: dict, out_path: Path) -> dict:
    ccfg = cfg.get("census", {})
    y0, y1 = ccfg.get("year_from", 1995), ccfg.get("year_to", 2026)
    token = get_token()
    reports: dict[str, dict] = {}
    if out_path.exists():                          # resume
        for r in json.loads(out_path.read_text())["reports"]:
            reports[r["reportId"]] = r
        print(f"resuming from {len(reports)} already-indexed reports")
    per_year = {}
    for y in range(y0, y1 + 1):
        try:
            rows = search(token, f"{y}-01-01", f"{y}-12-31")
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            token = get_token()                    # refresh and retry once
            rows = search(token, f"{y}-01-01", f"{y}-12-31")
        per_year[y] = len(rows)
        for x in rows:
            reports[x["reportId"]] = x
        print(f"{y}: {len(rows):5d}  (index {len(reports)})")
    payload = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "per_year": per_year, "n_reports": len(reports),
               "reports": sorted(reports.values(), key=lambda r: r.get("publishDate") or "")}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    tmp.replace(out_path)                          # atomic: no partial index
    return payload


def summarize(payload: dict, cfg: dict) -> None:
    reps = payload["reports"]
    print(f"\nindexed reports: {len(reps)}")
    cats = collections.Counter((r.get("reportCategory") or "").strip() for r in reps)
    want = set(cfg.get("census", {}).get("psd_categories") or [])
    print(f"distinct categories: {len(cats)}")
    print("\n=== categories carrying PSD balance sheets (configured) ===")
    tot = 0
    for c in sorted(want):
        n = cats.get(c, 0)
        tot += n
        print(f"{n:7d}  {c}")
    print(f"{tot:7d}  TOTAL in PSD categories")
    print("\n=== top 15 categories overall ===")
    for c, n in cats.most_common(15):
        mark = "*" if c in want else " "
        print(f"{mark}{n:7d}  {c}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--summary", action="store_true", help="summarize existing index only")
    a = ap.parse_args()
    cfg = yaml.safe_load((PKG_ROOT / a.config).read_text())
    out_path = PKG_ROOT / cfg.get("census", {}).get("index_path", "output/report_index.json")
    if a.summary:
        if not out_path.exists():
            print(f"no index at {out_path}", file=sys.stderr)
            return 1
        payload = json.loads(out_path.read_text())
    else:
        payload = walk(cfg, out_path)
        print(f"\nwrote index -> {out_path}")
    summarize(payload, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
