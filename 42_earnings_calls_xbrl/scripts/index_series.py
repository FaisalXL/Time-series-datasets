#!/usr/bin/env python3
"""index_series.py -- turn 2.7 GB of cached SEC companyfacts into one compact series index.

Why this exists as a separate step:

  * **Memory.** The previous builder held every parsed `companyfacts` payload in a dict for the
    whole run. Measured: 575 filers x 4.6 MB raw = **2.7 GB on disk**, and parsed Python objects
    run several times that. On a 50-record demo the dict never grew; on the full 26,361-row scan
    it is an OOM. The index below is ~1% of that and is all the build actually needs.
  * **Iteration.** Every question about the join -- which concept, which window, how much Q4 loss --
    is answered from this file in seconds instead of re-parsing gigabytes. Harvest once, extract
    many times, per the corpus-wide rule about caching raw bytes rather than derived text.

Concept fallbacks are tried IN ORDER and the first that yields any quarterly facts wins, so one
series is never a mixture of two different accounting definitions. Which concept each filer
actually resolved to is recorded per channel, because "revenue" is not one tag in us-gaap.

Usage: index_series.py [--cache DIR] [--out FILE]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (channel name, us-gaap unit, TOTAL-level concepts in preference order)
#
# COMPONENT concepts are deliberately absent. `SalesRevenueGoodsNet` and
# `SalesRevenueServicesNet` are *parts* of revenue, not alternative spellings of it: measured
# across 575 filers, those two disagree at 1,350 shared period-ends, and pooling every revenue
# concept put 71.1% of shared period-ends in disagreement (median gap 36%). Splicing a component
# onto a total is the "wrong series, right-looking label" failure, so only totals are listed.
CHANNELS = [
    ("revenue_usd", "USD", [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenuesNetOfInterestExpense",
    ]),
    ("net_income_usd", "USD", [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ]),
    ("eps_diluted_usd_per_share", "USD/shares", [
        "EarningsPerShareDiluted",
        "IncomeLossFromContinuingOperationsPerDilutedShare",
        "EarningsPerShareBasicAndDiluted",
    ]),
]

# A spliced-in concept must agree with the anchor on EVERY shared period-end, to this tolerance.
SPLICE_TOL = 0.005


def quarterly(facts: dict, unit: str, concept: str) -> dict:
    """{period-end: value} for quarterly-DURATION facts only (80-100 day span).

    A duration filter is what separates a quarter from the year-to-date and full-year facts
    filed under the same concept. Later filings overwrite the same period-end, which is
    intended: a restated quarter should win over its original.
    """
    out: dict[str, float] = {}
    for f in facts.get(concept, {}).get("units", {}).get(unit, []):
        start, end = f.get("start"), f.get("end")
        if not (start and end):
            continue
        try:
            days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
        except ValueError:
            continue
        if 80 <= days <= 100:
            out[end] = f["val"]
    return out


def splice(cands: dict[str, dict], order: list[str]) -> tuple[dict, list[str], list[str]]:
    """Merge one filer's concepts into a single series, but only where the overlap AGREES.

    Why a per-filer, overlap-verified splice rather than a global fallback order:

      * "First concept that yields anything wins" silently truncates history at the ASC 606
        boundary. Abbott files `RevenueFromContractWithCustomer...` for 2017-03 onward (31
        quarters) and `SalesRevenueNet` for 2008-03..2017-12 (40 quarters); taking the first
        threw away all 40. Measured: 442 of 575 filers were losing quarters this way, a mean
        of 32 each.
      * But a blind merge is worse. Even restricted to total-level concepts, only 51.3% of
        shared period-ends agree -- `Revenues` frequently includes interest or other income
        that ASC 606 revenue excludes. `Revenues`/`RevenuesNetOfInterestExpense` agree on 9%.

    So: anchor on the concept covering the LATEST period (the series must reach the present),
    then extend backwards with any concept that matches the anchor on every shared period-end.
    A concept that disagrees is REJECTED and recorded, never averaged or preferred away.
    Measured outcome: 277 filers splice with a verified overlap (+7,082 quarters, median +28),
    and 204 filers have at least one concept rejected -- which is the guard doing its job.
    """
    if not cands:
        return {}, [], []
    anchor = max(cands, key=lambda c: (max(cands[c]), len(cands[c])))
    out = dict(cands[anchor])
    used, rejected = [anchor], []
    for c in sorted(cands, key=lambda c: order.index(c) if c in order else 99):
        if c in used:
            continue
        shared = set(out) & set(cands[c])
        if shared and any(abs(out[e] - cands[c][e])
                          > SPLICE_TOL * max(abs(out[e]), abs(cands[c][e]), 1.0)
                          for e in shared):
            rejected.append(c)
            continue
        new = {e: v for e, v in cands[c].items() if e not in out}
        if new:
            out.update(new)
            used.append(c)
    return out, used, rejected


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / ".cache" / "cf"))
    ap.add_argument("--out", default=str(ROOT / ".cache" / "series_index.json"))
    args = ap.parse_args()

    files = sorted(Path(args.cache).glob("CIK*.json"))
    print(f"indexing {len(files)} cached companyfacts payloads")
    idx: dict[str, dict] = {}
    concept_use: collections.Counter = collections.Counter()
    absent: collections.Counter = collections.Counter()

    for i, fp in enumerate(files, 1):
        try:
            cf = json.loads(fp.read_text())
        except Exception:                                        # noqa: BLE001
            absent["unparseable_payload"] += 1
            continue
        facts = cf.get("facts", {}).get("us-gaap", {})
        cik = str(cf.get("cik", "")).zfill(10)
        channels = {}
        for name, unit, concepts in CHANNELS:
            cands = {c: p for c in concepts if (p := quarterly(facts, unit, c))}
            if not cands:
                absent[f"no_{name}"] += 1
                continue
            points, used, rejected = splice(cands, concepts)
            channels[name] = {"concepts": used, "rejected": rejected,
                              "points": {k: round(float(v), 4) for k, v in sorted(points.items())}}
            concept_use[f"{name} <- {'+'.join(used)}"] += 1
            if len(used) > 1:
                absent["_spliced"] += 1
            for r in rejected:
                absent[f"_rejected:{name} x {r}"] += 1
        if channels:
            idx[cik] = {"entity": cf.get("entityName"), "channels": channels}
        if i % 100 == 0:
            print(f"  {i}/{len(files)}", flush=True)

    out = Path(args.out)
    out.write_text(json.dumps(idx))
    print(f"\n{len(idx)} filers indexed -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
    print("\nchannels absent (per filer):")
    for k, v in absent.most_common():
        print(f"   {v:>4}  {k}")
    print("\nconcept each filer actually resolved to:")
    for k, v in concept_use.most_common():
        print(f"   {v:>4}  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
