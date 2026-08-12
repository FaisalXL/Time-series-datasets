#!/usr/bin/env python3
"""Enumerate every ONS statistical bulletin edition -> census.json.

WHY THE API AND NOT WAYBACK. The scale estimate for this package was 2,979 archived
family-editions counted via the Wayback CDX index. ONS's own search API reports 5,788 bulletin
editions across 495 families -- nearly double, because CDX only sees what the crawler happened
to archive. The live site is the authoritative enumeration and costs 6 requests.

WHY NOT previousreleases PAGINATION. That works too (it agreed with CDX at 130 editions for
consumerpriceinflation, vs CDX's 129) but costs ~1 request per 10 editions per family, i.e.
~600 requests against a source that hard-429s after a burst of 5. The API gives the same
answer in 6.

The edition SLUG is recorded but is deliberately NOT treated as a date: slugs come in 14 shapes
(`june2026`, `6august2026`, `weekending12may2023`, `2019to2020`, `quarter1julytosept2021`) and a
slug is not a dateline. Each edition's period is read from its own h1 at build time.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from onsfetch import fetch, stats                                         # noqa: E402

API = "https://api.beta.ons.gov.uk/v1/search?content_type=bulletin&limit={lim}&offset={off}"


def sweep(page_size: int = 1000) -> list[dict]:
    c, b = fetch(API.format(lim=1, off=0))
    if c != 200:
        raise SystemExit(f"census: API returned {c} -- refusing to write a partial census")
    total = json.loads(b).get("count", 0)
    items = []
    for off in range(0, total, page_size):
        c, b = fetch(API.format(lim=page_size, off=off))
        if c != 200:
            raise SystemExit(f"census: page at offset {off} returned {c} -- refusing to write "
                             f"a partial census (a throttled page is UNKNOWN, not empty)")
        items.extend(json.loads(b).get("items", []))
    if len(items) < total:
        raise SystemExit(f"census: got {len(items)} of {total} items -- refusing partial write")
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(HERE.parent / "census.json"))
    args = ap.parse_args()

    items = sweep()
    fams: dict[str, list] = collections.defaultdict(list)
    unparsed = 0
    for it in items:
        u = (it.get("uri") or "").strip("/")
        m = re.match(r"^(?P<path>.+?)/bulletins/(?P<fam>[^/]+)/(?P<ed>[^/]+)$", u)
        if not m:
            unparsed += 1
            continue
        fams[f"{m.group('path')}||{m.group('fam')}"].append(
            [m.group("ed"), it.get("release_date"), it.get("title")])
    for v in fams.values():                    # newest first, by release date
        v.sort(key=lambda r: (r[1] or ""), reverse=True)
    Path(args.out).write_text(json.dumps(fams, indent=1))

    sizes = sorted((len(v) for v in fams.values()), reverse=True)
    print(f"editions={len(items)} unparsed_uris={unparsed} families={len(fams)}")
    for thr in (120, 80, 40, 20, 10, 5, 1):
        sel = [s for s in sizes if s >= thr]
        print(f"  >={thr:>3} editions: {len(sel):>3} families, {sum(sel):>5} editions")
    print(f"wrote {args.out}  fetch {stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
