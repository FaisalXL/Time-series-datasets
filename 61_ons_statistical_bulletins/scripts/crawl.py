#!/usr/bin/env python3
"""Warm the fetch cache, cheapest-useful-work-first.

ONS hard-429s after a burst of ~5 requests, so at ~3s effective spacing the cache is the whole
schedule: ~7,300 fetches is ~7 hours and the order matters more than the code.

TRIAGE FIRST. A family with no dataset CSV has no series and therefore cannot ship at any
quality -- and the deepest family on the site (deathsregisteredweekly, 273 editions) is exactly
that case. Crawling it first cost 981 seconds for zero shippable records. So:

  --triage : per family, the relateddata page + dataset CSVs + the newest few editions. Enough
             for discover_channels.py to decide whether the family can ship at all.
  --full   : the remaining editions, for families discovery marked ok. Reads channels.json.

Everything is cached and resumable; re-running skips what is already held. A 429 is never
cached, so an interrupted run never bakes in a throttle as "the source has nothing here".
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from onsfetch import fetch, stats                                         # noqa: E402

PKG = HERE.parent


def family_dataset_csvs(path: str, fam: str, edition: str) -> int:
    c, b = fetch(f"https://www.ons.gov.uk/{path}/bulletins/{fam}/{edition}/relateddata")
    if c != 200:
        return 0
    d = b.decode("utf8", "replace")
    n = 0
    for u in sorted(set(re.findall(r'href="(/[a-z0-9/]+?/datasets/[a-z0-9]+)"', d)))[:6]:
        c2, b2 = fetch(f"https://www.ons.gov.uk{u}/current")
        if c2 != 200:
            continue
        for m in re.finditer(rf"{re.escape(u)}/current/([a-z0-9]+)\.csv",
                             b2.decode("utf8", "replace")):
            # The CSV downloads from /file?uri=..., NOT from its own page path -- the page path
            # 404s. Fetching the wrong form made triage report csv=0 for every family that DOES
            # have a dataset (consumerpriceinflation included) and skipped pre-warming the 20MB
            # files that dominate discovery's cost. Same URL form as onslib.load_dataset.
            c3, _ = fetch(f"https://www.ons.gov.uk/file?uri={u}/current/{m.group(1)}.csv")
            n += 1 if c3 == 200 else 0
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=("triage", "full"), required=True)
    ap.add_argument("--census", default=str(PKG / "census.json"))
    ap.add_argument("--channels", default=str(PKG / "channels.json"))
    ap.add_argument("--sample", type=int, default=8, help="triage: newest N editions per family")
    # Depth cutoff. NOT a silent cap: the skipped families and editions are logged and reported,
    # because "we covered everything" and "we covered what was worth 3 hours of throttled
    # fetching" are different claims. At ~3s/request the 319 families with <5 editions cost
    # ~3h of crawl for 9% of all editions, while families with >=5 hold 91%.
    ap.add_argument("--min-editions", type=int, default=1)
    ap.add_argument("--dataset-subtopics-only", action="store_true",
                    help="restrict to subtopics that hold a CDID time-series dataset")
    ap.add_argument("--require-dataset", action="store_true",
                    help="full mode: crawl families that have a CDID dataset (not channels.json)")
    ap.add_argument("--log", default=str(PKG / "output" / "crawl.log"))
    args = ap.parse_args()

    census = json.load(open(args.census))
    order = sorted(census.items(), key=lambda kv: -len(kv[1]))
    n_all_f, n_all_e = len(order), sum(len(v) for _k, v in order)
    if args.min_editions > 1:
        order = [(k, v) for k, v in order if len(v) >= args.min_editions]
    if args.dataset_subtopics_only:
        # A family can only produce channels if CDID series exist for its subject. The 43
        # CSV-bearing time-series datasets sit in 22 subtopics; families outside those have no
        # series to verify against, so triaging them spends throttled requests to prove a
        # negative. 72 of the 176 deep families are in scope, holding 61% of their editions --
        # and the out-of-scope ones are logged, not silently dropped.
        subs = {r["subtopic"] for r in json.load(open(PKG / "datasets_index.json"))}
        keep = [(k, v) for k, v in order if k.split("||")[0] in subs]
        skipped = [k.split("||")[1] for k, _ in order if k.split("||")[0] not in subs]
        order = keep
        print(f"dataset-subtopic filter: kept {len(keep)}, skipped {len(skipped)}")
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    log = open(args.log, "a", buffering=1)

    def say(m):
        log.write(f"[{time.strftime('%H:%M:%S')}] {m}\n")

    if args.mode == "full":
        # Which families to fetch in full. Prefer channels.json when it exists, but the useful
        # pass is usually the other way round: crawl every family that HAS a CDID dataset, then
        # let discovery run on many editions rather than 8. More editions raise a real channel's
        # claim count much faster than they raise its coincidence rate, so significance improves
        # with depth -- that is how recall is won without loosening the test.
        if args.require_dataset:
            sys.path.insert(0, str(HERE))
            from discover_channels import family_datasets
            keep = []
            for k, v in order:
                p, f = k.split("||")
                if family_datasets(p, f, v[0][0]):
                    keep.append((k, v))
            say(f"=== dataset filter: {len(keep)}/{len(order)} families have a CDID dataset")
            order = keep
        else:
            ok = {r["family"] for r in json.load(open(args.channels)) if r.get("status") == "ok"}
            order = [(k, v) for k, v in order if k.split("||")[1] in ok]
        say(f"=== full crawl: {len(order)} shippable families, "
            f"{sum(len(v) for _k, v in order)} editions")
    else:
        kept_e = sum(len(v) for _k, v in order)
        say(f"=== triage: {len(order)}/{n_all_f} families, {kept_e}/{n_all_e} editions "
            f"({100*kept_e/max(n_all_e,1):.0f}%) at min_editions={args.min_editions}; "
            f"SKIPPED {n_all_f-len(order)} families / {n_all_e-kept_e} editions")

    t_start = time.time()
    for i, (key, eds) in enumerate(order, 1):
        path, fam = key.split("||")
        t0 = time.time()
        ncsv = 0
        if args.mode == "triage":
            ncsv = family_dataset_csvs(path, fam, eds[0][0])
            todo = [e[0] for e in eds[:args.sample]]
        else:
            todo = [e[0] for e in eds]
        got = 0
        for slug in todo:
            c, _ = fetch(f"https://www.ons.gov.uk/{path}/bulletins/{fam}/{slug}")
            got += 1 if c == 200 else 0
        say(f"{i:>3}/{len(order)} {fam[:44]:<44} eds={got}/{len(todo)} csv={ncsv} "
            f"{time.time()-t0:.0f}s {stats()}")
    say(f"=== {args.mode} done in {(time.time()-t_start)/60:.0f}min {stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
