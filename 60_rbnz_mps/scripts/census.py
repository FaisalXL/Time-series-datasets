#!/usr/bin/env python3
"""Enumerate every RBNZ Monetary Policy Statement via Wayback CDX -> a statement index.

WHY: rbnz.govt.nz answers 403 to automated fetches (re-verified 2026-08-19 WITH a full browser
User-Agent -- the response is RBNZ's own "Website unavailable" page, so it is a bot-wall, not a
missing UA). Wayback has the pages, so enumeration and fetching both go through CDX.

FOUR URL GENERATIONS (the README knew of two; CDX shows four):
    gen1  /monetary-policy/monetary-policy-statement/mps{YYYY}-{MM}                 1996-12..2015-09
    gen2  /monetary-policy/monetary-policy-statement/mps-{month}-{year}             2015-12..2022-02
    gen3  /monetary-policy/monetary-policy-statement/monetary-policy-statement-filtered-listing-page/{year}/{slug}/monetary-policy-statement-{month}-{year}
    gen4  /hub/publications/monetary-policy-statement[/{year}[/{slug}]]/monetary-policy-statement-{month}-{year}
The README's "~110 statements / 117 captures" was right on count but attributed it to two schemes;
gen1 alone is 76 pages and reaches back to 1996, well past the 2021 floor the demo implied.

Statement identity is (year, month) -- NOT the URL, because the same statement is archived under
several paths across redesigns (e.g. Nov 2023 appears under both a bare and a year-nested hub
path). Collapsing on the URL would have double-counted ~100 pages as ~213.

Usage:
    python scripts/census.py --config config.example.yaml
    python scripts/census.py --config config.example.yaml --summary
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent

CDX = "https://web.archive.org/cdx/search/cdx"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"])}


def cdx(prefix: str, tries: int = 6, **extra) -> list[list[str]]:
    """CDX prefix query. NEVER treat an empty body as 'nothing archived' -- archive.org throttles
    by returning an empty 200, which is indistinguishable from a real miss without a retry. (This
    corpus has already been burned once by caching a throttle as a final answer.)"""
    q = {"url": prefix, "matchType": "prefix", "fl": "timestamp,original,statuscode,mimetype",
         "limit": "40000", **extra}
    url = f"{CDX}?{urllib.parse.urlencode(q)}"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=240) as r:
                body = r.read().decode("utf-8", "replace")
            rows = [ln.split() for ln in body.splitlines() if ln.strip()]
            if rows:
                return rows
            wait = min(5 * 2 ** attempt, 90)
            print(f"    empty CDX body (throttle?) -- retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
        except Exception as e:                                  # noqa: BLE001
            wait = min(5 * 2 ** attempt, 90)
            print(f"    {type(e).__name__} {e} -- retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    print(f"    CDX gave up on {prefix}", file=sys.stderr)
    return []


def statement_identity(path: str) -> tuple[int, int] | None:
    """(year, month) for a statement page, else None. Ordered most-specific first."""
    p = path.rstrip("/")
    m = re.search(r"/mps(\d{4})-(\d{2})$", p)                   # gen1
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"/mps-([a-z]+)-(\d{4})$", p)                 # gen2
    if m and m.group(1) in MONTHS:
        return int(m.group(2)), MONTHS[m.group(1)]
    m = re.search(r"/monetary-policy-statement-([a-z]+)-(\d{4})$", p)   # gen3/gen4 named
    if m and m.group(1) in MONTHS:
        return int(m.group(2)), MONTHS[m.group(1)]
    # gen4 date-coded slug, e.g. .../monetary-policy-statement-291124 -> 29 Nov 2024
    m = re.search(r"/monetary-policy-statement-(\d{2})(\d{2})(\d{2})$", p)
    if m:
        dd, mm, yy = (int(x) for x in m.groups())
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return 2000 + yy, mm
    return None


PACK_RE = re.compile(r"\.(xlsx?)(\?|$)", re.I)


def walk_packs(cfg: dict, out_path: Path) -> dict:
    """Index every archived MPS data pack, keyed by filename.

    WHY NOT JUST USE THE LINK ON THE PAGE: the pack path RBNZ serves depends on which CMS was live
    when the page was crawled. A 2005-era capture links
        /-/media/ReserveBank/Files/Publications/Monetary policy statements/2005/jun05-data.xls
    while a 2026 capture of the SAME statement links
        /-/media/project/sites/rbnz/files/publications/monetary-policy-statements/1997/sep97-data.xls
    Because the census keeps each statement's LATEST capture, the link it yields is usually the
    modern path -- which Wayback often never crawled, so fetching it 404s even though the pack is
    archived under its older URL. Measured: sep97-data.xls 404s on the modern path at every
    timestamp, while CDX lists real captures for the packs it did crawl.
    So: take the FILENAME from the page link, and resolve the fetchable URL+timestamp here.
    """
    packs: dict[str, dict] = {}
    for pref in cfg["census"]["pack_cdx_prefixes"]:
        rows = cdx(pref, **{"filter": "statuscode:200"})
        print(f"{pref}: {len(rows)} captures")
        for row in rows:
            if len(row) < 2 or not PACK_RE.search(row[1]):
                continue
            ts, url = row[0], row[1]
            base = urllib.parse.unquote(url.split("?")[0].rsplit("/", 1)[-1]).lower()
            # prefer the LATEST capture of each pack
            if base not in packs or ts > packs[base]["wayback_ts"]:
                packs[base] = {"wayback_ts": ts, "url": url.split("?")[0]}
        time.sleep(2)
    payload = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "n_packs": len(packs), "packs": packs}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    tmp.replace(out_path)
    print(f"\nindexed {len(packs)} data packs -> {out_path}")
    return payload


def walk(cfg: dict, out_path: Path) -> dict:
    prefixes = cfg["census"]["cdx_prefixes"]
    caps: list[list[str]] = []
    for p in prefixes:
        rows = cdx(p, **{"filter": "statuscode:200"})
        print(f"{p}: {len(rows)} captures")
        caps.extend(rows)
        time.sleep(2)

    cands: dict[tuple[int, int], list[tuple[str, str]]] = {}
    unparsed: set[str] = set()
    for row in caps:
        if len(row) < 4:
            continue
        ts, original, _sc, mime = row[0], row[1], row[2], row[3]
        if "html" not in mime:
            continue
        path = re.sub(r"^https?://(www\.)?rbnz\.govt\.nz(:\d+)?", "",
                      original).split("?")[0]
        key = statement_identity(path)
        if key is None:
            if "monetary-policy-statement" in path:
                unparsed.add(path)
            continue
        cands.setdefault(key, []).append((ts, original.split("?")[0]))

    # KEEP SEVERAL CANDIDATES PER STATEMENT, not just the newest. Measured 2026-08-19: 30 of 119
    # statements had their newest capture on a Jan-2026 crawl of the /hub/... path that CDX lists as
    # statuscode 200 but that 404s when the original bytes are requested -- while the SAME statement
    # fetches fine from its older gen1/gen2 capture. Keeping only the latest silently lost a quarter
    # of the archive; the builder now walks these in order until one yields a usable page.
    by: dict[tuple[int, int], dict] = {}
    for key, lst in cands.items():
        seen, ordered = set(), []
        for ts, url in sorted(lst, key=lambda x: x[0], reverse=True):
            if url in seen:
                continue
            seen.add(url)
            ordered.append({"wayback_ts": ts, "page_url": url})
            if len(ordered) >= 8:
                break
        by[key] = {"year": key[0], "month": key[1],
                   "wayback_ts": ordered[0]["wayback_ts"],
                   "page_url": ordered[0]["page_url"],
                   "path": re.sub(r"^https?://(www\.)?rbnz\.govt\.nz(:\d+)?", "",
                                  ordered[0]["page_url"]),
                   "candidates": ordered}
    payload = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "n_statements": len(by),
               "statements": [by[k] for k in sorted(by)],
               "unparsed_paths": sorted(unparsed)[:200]}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    tmp.replace(out_path)                                        # atomic
    return payload


def summarize(payload: dict) -> None:
    sts = payload["statements"]
    print(f"\nstatements indexed: {len(sts)}")
    if not sts:
        return
    per = collections.Counter(s["year"] for s in sts)
    print(f"range: {sts[0]['year']}-{sts[0]['month']:02d} .. "
          f"{sts[-1]['year']}-{sts[-1]['month']:02d}")
    print("\nper year (MPS is quarterly -> 4 is complete):")
    for y in sorted(per):
        print(f"  {y}: {per[y]}{'' if per[y] == 4 else '   <-- incomplete'}")
    gaps = []
    for y in range(sts[0]["year"], sts[-1]["year"] + 1):
        if per.get(y, 0) < 4:
            gaps.append(y)
    print(f"\nyears without 4 statements: {gaps}")
    if payload.get("unparsed_paths"):
        print(f"\nunparsed statement-ish paths ({len(payload['unparsed_paths'])} shown):")
        for p in payload["unparsed_paths"][:15]:
            print("   ", p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    cfg = yaml.safe_load((PKG_ROOT / a.config).read_text())
    out_path = PKG_ROOT / cfg["census"]["index_path"]
    if a.summary:
        if not out_path.exists():
            print(f"no index at {out_path}", file=sys.stderr)
            return 1
        payload = json.loads(out_path.read_text())
    else:
        payload = walk(cfg, out_path)
        print(f"\nwrote index -> {out_path}")
        walk_packs(cfg, PKG_ROOT / cfg["census"]["pack_index_path"])
    summarize(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
