#!/usr/bin/env python3
"""Index every ONS *time series* dataset (the CDID-bearing CSVs) -> datasets_index.json.

WHY THIS EXISTS. Channel discovery reads a family's series from the dataset CSVs linked on the
bulletin's own `relateddata` page. For CPI that works (`mm23`). For `uklabourmarket` -- 136
editions, one of the densest number-reciting bulletins on the site -- it does not: every dataset
that page links is **xlsx-only**, with no CDID time series in sight. The series exist, just not
where the bulletin points: `labourmarketstatistics/current/lms.csv`.

Probing all 3,914 ONS dataset landing pages for a CSV would cost ~3 hours at this source's
throttle. But ONS names the CDID datasets distinctively -- their titles end in "time series" --
and there are only **59** of them across 26 subtopics, covering exactly the families that carry
the scale: Labour market statistics, Retail Sales, Average weekly earnings, Public sector
finances, UK trade, Index of Production, Index of Services, monthly GDP, Business investment.

So discovery takes the union of (a) the CSVs the bulletin itself links, which is the precise
signal, and (b) the time-series datasets in the family's own subtopic, which is the fallback.
Widening the candidate pool raises the coincidence floor, which is exactly what the control in
discover_channels.py measures -- so this is a change whose cost is visible, not assumed.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from onsfetch import fetch, stats                                         # noqa: E402

PKG = HERE.parent
API = ("https://api.beta.ons.gov.uk/v1/search?content_type=dataset_landing_page"
       "&limit={lim}&offset={off}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(PKG / "datasets_index.json"))
    ap.add_argument("--match", default="time series",
                    help="title substring identifying CDID time-series datasets")
    args = ap.parse_args()

    c, b = fetch(API.format(lim=1, off=0))
    if c != 200:
        raise SystemExit(f"dataset index: API returned {c} -- refusing a partial index")
    total = json.loads(b).get("count", 0)
    items = []
    for off in range(0, total, 1000):
        c, b = fetch(API.format(lim=1000, off=off))
        if c != 200:
            raise SystemExit(f"dataset index: offset {off} returned {c} -- refusing partial "
                             f"write (a throttled page is UNKNOWN, not empty)")
        items.extend(json.loads(b).get("items", []))
    print(f"dataset landing pages: {len(items)} of {total}")

    cands = [it for it in items if args.match in (it.get("title") or "").lower()]
    print(f"titled '...{args.match}': {len(cands)}")

    out, no_csv = [], []
    for it in cands:
        uri = (it.get("uri") or "").strip("/")
        if not uri:
            continue
        c, b = fetch(f"https://www.ons.gov.uk/{uri}/current")
        if c != 200:
            no_csv.append((uri, f"http_{c}"))
            continue
        ids = sorted(set(re.findall(rf"{re.escape('/' + uri)}/current/([a-z0-9]+)\.csv",
                                    b.decode("utf8", "replace"))))
        if not ids:
            no_csv.append((uri, "no_csv_link"))
            continue
        subtopic = "/".join(uri.split("/")[:-2])
        for ds in ids:
            out.append({"uri_path": uri, "dataset_id": ds, "subtopic": subtopic,
                        "title": it.get("title")})
    Path(args.out).write_text(json.dumps(out, indent=1))
    bysub = collections.Counter(r["subtopic"] for r in out)
    print(f"\nCSV-bearing time-series datasets: {len(out)} across {len(bysub)} subtopics")
    for s, n in bysub.most_common():
        print(f"   {n:>2}  {s}")
    if no_csv:
        print(f"\nno CSV ({len(no_csv)}):")
        for u, why in no_csv[:12]:
            print(f"   {why:<12} {u}")
    print(f"\nwrote {args.out}  fetch {stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
