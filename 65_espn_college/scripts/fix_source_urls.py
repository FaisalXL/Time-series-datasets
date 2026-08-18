#!/usr/bin/env python3
"""One-time migration: rewrite every record's `source` URL from the SPORT form to the LEAGUE form.

Every record built before 2026-08-13 carries a `source` that does not resolve, because the URL was
assembled from the ESPN *sport* rather than the *league slug*:

    https://www.espn.com/football/boxscore/_/gameId/322430238   -> 404
    https://www.espn.com/basketball/game/_/gameId/323140084     -> 404
    https://www.espn.com/college-football/game/_/gameId/322430238            -> 200
    https://www.espn.com/mens-college-basketball/game/_/gameId/323140084     -> 200

Verified 200 across all four tiers at both ends of the era (CFB/FCS/MCB/WCB, 2012 and 2025 shards).
`scripts/build_cpt_jsonl.py` was fixed at the same time, so this only exists to repair records
already on disk; it is idempotent and safe to re-run.

The new URL is DERIVED FROM EACH RECORD'S OWN `meta` (`espn_league_slug` + `event_id`), never by
string surgery on the old value — the record's own metadata is the truth about which game it is.
A record whose old URL is not one of the two known-broken forms is left untouched and counted as
`unexpected`, so a surprise shape is reported rather than silently rewritten.

Shards are replaced atomically (.part then rename), so an interrupted run leaves whole shards.

Usage:
    python scripts/fix_source_urls.py --dry-run
    python scripts/fix_source_urls.py
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG.parent / "schema"))
from validate import URL_RE                                             # noqa: E402

BROKEN = re.compile(r"^https://www\.espn\.com/(football|basketball)/(boxscore|game)/_/gameId/(\d+)$")


def new_url(meta: dict) -> str:
    return f"https://www.espn.com/{meta['espn_league_slug']}/game/_/gameId/{meta['event_id']}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--shards", default=str(PKG / "output" / "shards"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    shards = sorted(Path(args.shards).glob("*.jsonl"))
    if not shards:
        print(f"no shards in {args.shards}")
        return 1

    tally = collections.Counter()
    per_tier = collections.Counter()
    bad_url = []
    mismatched = []

    for sp in shards:
        out = []
        changed = 0
        with sp.open() as fh:
            for line in fh:
                if not line.strip():
                    continue
                r = json.loads(line)
                m = r["meta"]
                old = r.get("source", "")
                want = new_url(m)
                if old == want:
                    tally["already_correct"] += 1
                    out.append(r)
                    continue
                hit = BROKEN.match(old)
                if not hit:
                    tally["unexpected"] += 1
                    if len(mismatched) < 5:
                        mismatched.append(old)
                    out.append(r)
                    continue
                # the event id must agree, or this is not the same game
                if hit.group(3) != str(m["event_id"]):
                    tally["event_id_disagrees"] += 1
                    mismatched.append(f"{old} vs meta {m['event_id']}")
                    out.append(r)
                    continue
                if not URL_RE.match(want):
                    tally["new_url_invalid"] += 1
                    bad_url.append(want)
                    out.append(r)
                    continue
                r["source"] = want
                tally["fixed"] += 1
                per_tier[m["league"]] += 1
                changed += 1
                out.append(r)

        if changed and not args.dry_run:
            tmp = sp.with_suffix(".jsonl.part")
            with tmp.open("w") as fh:
                for r in out:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            tmp.replace(sp)
        print(f"  {sp.name}: {changed:,} rewritten", flush=True)

    print("\n" + json.dumps({"tally": dict(tally), "fixed_by_tier": dict(per_tier)}, indent=1))
    if mismatched:
        print(f"\n⚠️  {len(mismatched)} record(s) not rewritten -- unexpected shape: {mismatched[:5]}")
    if bad_url:
        print(f"\n❌ {len(bad_url)} constructed URLs fail the schema URL pattern: {bad_url[:3]}")
        return 2
    if args.dry_run:
        print("\n(dry run -- nothing written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
