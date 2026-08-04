#!/usr/bin/env python3
"""Harvest every Richmond Fed Fifth District survey release document to `.cache/docs/`.

Split from the build deliberately: caching *raw bytes* is what makes every later design
question answerable by measurement instead of argument (the `56_nass_crop_progress`
lesson), and it means the extractor can be rewritten without re-fetching ~600 documents.

Two rules this script exists to enforce:

  * **A rate limit is never recorded as a fact about the source.** Wayback throttles at the
    TCP level, not with an HTTP 429, so a naive fetcher turns congestion into "this month
    was never published". Fetch statuses are kept verbatim (`ok` / `http_404` /
    `giveup_*`) and `--report` counts them separately; only `http_404` means absent.
  * **The universe is listed, not computed.** See `richsrc` for the three site eras.

Usage:
  python scripts/harvest.py --list          # CDX enumeration only, writes .cache/cdx/
  python scripts/harvest.py                 # enumerate + fetch what is missing
  python scripts/harvest.py --report        # coverage table from what is already cached
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import polite_fetch as pf                                   # noqa: E402
import richsrc                                              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

EXT = {"narr_html": "html", "tbl_html": "html", "pdf": "pdf", "csv": "csv", "page": "html"}


def cache_dirs(cfg: dict) -> tuple[Path, Path, Path]:
    cache = ROOT / cfg["data"]["cache_dir"]
    return cache, cache / "cdx", cache / "docs"


# --- CDX enumeration -------------------------------------------------------


def cdx_fetch(session, rate, q: Dict[str, str], dest: Path) -> Optional[List[List[str]]]:
    """One CDX listing, cached. Returns None if the query could not be completed --
    which is NOT the same as "the archive holds nothing", and is never cached."""
    if dest.exists():
        return [l.split() for l in dest.read_text().splitlines() if l.strip()]
    params = {k: v for k, v in q.items() if k != "name"}
    params.update({"output": "text", "fl": "original,timestamp,statuscode",
                   "collapse": "urlkey", "limit": "60000"})
    url = richsrc.CDX + "?" + urllib.parse.urlencode(params)
    body, status = pf.get(session, url, rate, attempts=8, timeout=180)
    if status != "ok" or body is None:
        print(f"  CDX {q['name']}: {status} -- NOT cached (a throttle is not an empty archive)",
              file=sys.stderr)
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return [l.split() for l in body.decode("utf8", "ignore").splitlines() if l.strip()]


def enumerate_docs(cfg: dict, session, rate) -> tuple[List[richsrc.Doc], Dict[str, str]]:
    survey = cfg["data"]["survey_key"]
    _, cdxdir, _ = cache_dirs(cfg)
    docs: List[richsrc.Doc] = []
    listing_status: Dict[str, str] = {}
    for q in richsrc.cdx_queries(survey):
        rows = cdx_fetch(session, rate, q, cdxdir / f"{survey}_{q['name']}.txt")
        if rows is None:
            listing_status[q["name"]] = "INCOMPLETE"
            continue
        listing_status[q["name"]] = f"{len(rows)} rows"
        for r in rows:
            if len(r) < 2:
                continue
            url, ts = r[0], r[1]
            st = r[2] if len(r) > 2 else "200"
            if st not in ("200", "-"):
                continue
            d = richsrc.classify(url, ts, survey)
            if d:
                docs.append(d)
    # earliest 200 capture per URL, and dedupe
    best: Dict[str, richsrc.Doc] = {}
    for d in sorted(docs, key=lambda d: d.timestamp):
        best.setdefault(d.url, d)
    return list(best.values()), listing_status


# --- fetching --------------------------------------------------------------


def local_name(d: richsrc.Doc) -> str:
    return f"{d.release}__{d.kind}.{EXT[d.kind]}"


def harvest(cfg: dict, only_missing: bool = True) -> dict:
    survey = cfg["data"]["survey_key"]
    cache, _, docdir = cache_dirs(cfg)
    docdir.mkdir(parents=True, exist_ok=True)
    session = pf.make_session()
    rate = pf.AdaptiveRate(rate=3.0, min_rate=0.4, max_rate=5.0)

    docs, listing_status = enumerate_docs(cfg, session, rate)
    print(f"[{survey}] CDX listings: {listing_status}", file=sys.stderr)
    print(f"[{survey}] {len(docs)} candidate documents enumerated", file=sys.stderr)

    statuses = collections.Counter()
    ledger_path = cache / "harvest_ledger.json"
    ledger: Dict[str, str] = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}

    # Prefer one document per (release, kind); retired-era files exist under three
    # equivalent archive directories, so group and try them in turn.
    by_slot: Dict[tuple, List[richsrc.Doc]] = collections.defaultdict(list)
    for d in docs:
        by_slot[(d.release, d.kind)].append(d)

    # The original build already cached the live-site PDF for every month it could reach,
    # so those slots need no Wayback round trip.
    live = ROOT / cfg["data"]["cache_dir"] / "releases"
    live_months = {p.stem for p in live.glob("*.pdf")} if live.exists() else set()
    # The release *page* is fetched even where a PDF exists for the same month. An earlier
    # version skipped it as a duplicate of the PDF's narrative, which cost ~170 requests per
    # survey but was wrong: some releases print their results table only on the page, and
    # the PDF is narrative-and-charts only. Measured on the service-sector survey, that
    # assumption left 6 months (2008-11, 2009-03/04, 2011-09/10, 2012-02) with no table at
    # all, and a month with no table drops every one of its narrative blocks. The only skip
    # kept is the live-site PDF, which is the same file by a different URL.
    slots = [s for s in sorted(by_slot)
             if not (s[1] == "pdf" and s[0][:7] in live_months)]
    statuses["skipped_have_live_pdf"] = sum(
        1 for s in by_slot if s[1] == "pdf" and s[0][:7] in live_months)
    by_slot = {s: by_slot[s] for s in slots}

    for i, (slot, cands) in enumerate(sorted(by_slot.items()), 1):
        dest = docdir / local_name(cands[0])
        if dest.exists() and dest.stat().st_size > 0:
            statuses["cached"] += 1
            continue
        if only_missing and ledger.get(dest.name) == "http_404":
            statuses["known_absent"] += 1
            continue
        got = False
        for d in cands:
            body, status = pf.get(session, pf.wayback_url(d.timestamp, d.url), rate)
            if status == "ok" and body:
                dest.write_bytes(body)
                ledger[dest.name] = f"ok:{d.url}@{d.timestamp}"
                statuses["fetched"] += 1
                got = True
                break
            ledger[dest.name] = status
        if not got:
            statuses[ledger.get(dest.name, "unknown")] += 1
        if i % 25 == 0:
            ledger_path.write_text(json.dumps(ledger, indent=1))
            print(f"  [{survey}] {i}/{len(by_slot)} slots  {dict(statuses)}  "
                  f"rate={rate.snapshot()}", file=sys.stderr)
    ledger_path.write_text(json.dumps(ledger, indent=1))
    print(f"[{survey}] done: {dict(statuses)}", file=sys.stderr)
    return {"listing_status": listing_status, "slots": len(by_slot),
            "fetch": dict(statuses), "rate": rate.snapshot()}


# --- coverage report -------------------------------------------------------


def report(cfg: dict) -> dict:
    """What is on disk, by release month and kind. Distinguishes 'absent at source'
    from 'we failed to fetch it', because only the first is a fact about the source."""
    survey = cfg["data"]["survey_key"]
    cache, _, docdir = cache_dirs(cfg)
    have: Dict[str, set] = collections.defaultdict(set)
    for p in docdir.glob("*__*"):
        rel, kind = p.name.split("__")
        have[rel[:7]].add(kind.split(".")[0])
    # the live-site cache the original build left behind (2018-01 .. present)
    live = ROOT / cfg["data"]["cache_dir"] / "releases"
    if live.exists():
        for p in sorted(live.glob("*.pdf")):
            have[p.stem].add("pdf_live")

    ledger_path = cache / "harvest_ledger.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    failed = {k: v for k, v in ledger.items() if v.startswith("giveup")}

    months = sorted(have)
    peryear = collections.Counter(m[:4] for m in months)
    return {"survey": survey, "months_with_any_doc": len(months),
            "span": [months[0], months[-1]] if months else None,
            "per_year": dict(sorted(peryear.items())),
            "kinds": dict(collections.Counter(k for v in have.values() for k in v)),
            "unresolved_fetches": len(failed),
            "unresolved_examples": list(failed)[:8]}


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest Richmond Fed survey release documents")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--list", action="store_true", help="CDX enumeration only")
    ap.add_argument("--report", action="store_true", help="coverage of what is cached")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-attempt slots previously recorded 404 (use after a throttle)")
    args = ap.parse_args()
    cfg = yaml.safe_load(args.config.read_text())
    if args.report:
        print(json.dumps(report(cfg), indent=2))
        return
    if args.list:
        session = pf.make_session()
        rate = pf.AdaptiveRate(rate=2.0)
        docs, st = enumerate_docs(cfg, session, rate)
        by = collections.Counter((d.era, d.kind) for d in docs)
        print(json.dumps({"listings": st, "n": len(docs),
                          "by_era_kind": {f"{a}/{b}": c for (a, b), c in sorted(by.items())}},
                         indent=2))
        return
    rep = harvest(cfg, only_missing=not args.retry_failed)
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
