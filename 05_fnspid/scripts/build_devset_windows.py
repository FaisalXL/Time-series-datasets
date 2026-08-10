#!/usr/bin/env python3
"""Select a devset of window records for human audit of the LLM's work.

NOT a random sample. A random 60 would be mostly mid-size windows with clean summaries, which
is exactly the case that needs no review. This spreads across:

  * window length -- 90 and 30 trading days, the open decision
  * era           -- 4 bins over 2009..2023
  * article count -- terciles, so a 1-article window and a 12-article window both appear;
                     the whole claim for summarisation is that it scales with article count,
                     so a devset that only shows thin windows cannot test it
  * the guard     -- records where the numeric-fidelity check REJECTED the summary and fell
                     back to extraction are force-included. Without them the reviewer sees
                     only the cases that passed and cannot judge whether the gate is doing
                     anything.

Usage:
    python3 scripts/build_devset_windows.py --per-length 30
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import statistics
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]


def tercile(n: int, cuts) -> int:
    return 0 if n <= cuts[0] else (1 if n <= cuts[1] else 2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-length", type=int, default=30)
    ap.add_argument("--fallbacks", type=int, default=4, help="force-include N gate rejections")
    ap.add_argument("--out", default="output/devset.jsonl")
    ap.add_argument("--report", default="output/devset_report.json")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    chosen: List[dict] = []
    per_len: Dict[str, Any] = {}
    for W in (90, 30):
        recs = [json.loads(l) for l in
                open(ROOT / f".cache/dev_r{W}.jsonl", encoding="utf-8")]
        for r in recs:
            r["meta"]["devset_window_len"] = W
        counts = sorted(r["meta"]["n_articles_used"] for r in recs)
        cuts = (counts[len(counts) // 3], counts[2 * len(counts) // 3])

        fallbacks = [r for r in recs if r["text_quality"] != "generated"]
        summaries = [r for r in recs if r["text_quality"] == "generated"]
        take_fb = rng.sample(fallbacks, min(len(fallbacks), args.fallbacks))

        cells: Dict[tuple, List[dict]] = collections.defaultdict(list)
        for r in summaries:
            cells[(r["meta"]["era"], tercile(r["meta"]["n_articles_used"], cuts))].append(r)
        for v in cells.values():
            rng.shuffle(v)

        picked = list(take_fb)
        keys = sorted(cells)
        i = 0
        while len(picked) < args.per_length and any(cells[k] for k in keys):
            k = keys[i % len(keys)]
            if cells[k]:
                picked.append(cells[k].pop())
            i += 1
        chosen += picked
        per_len[str(W)] = {
            "pool": len(recs), "selected": len(picked),
            "gate_fallbacks_included": len(take_fb),
            "article_count_cuts": list(cuts),
            "era": dict(collections.Counter(r["meta"]["era"] for r in picked)),
            "articles_per_record_median": statistics.median(
                r["meta"]["n_articles_used"] for r in picked),
            "text_chars_median": statistics.median(r["meta"]["text_chars"] for r in picked),
        }

    outp = ROOT / args.out
    outp.parent.mkdir(parents=True, exist_ok=True)
    with open(outp, "w", encoding="utf-8") as fh:
        for r in chosen:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    fid = [r["meta"]["summary_numeric_fidelity"] for r in chosen
           if r["meta"].get("summary_numeric_fidelity")]
    report = {
        "built_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
        "purpose": "human audit of LLM role/extraction/summary quality on window records",
        "record_shape": "window", "prompt": "extract_v3", "summary_words_cap": 360,
        "text_from": "summary, with extraction fallback on a numeric-fidelity failure",
        "total": len(chosen),
        "by_window_length": per_len,
        "numbers_checked": sum(f["numbers"] for f in fid),
        "numbers_unsupported": sum(f["unsupported"] for f in fid),
        "records_where_gate_forced_extraction": sum(
            1 for r in chosen if r["text_quality"] != "generated"),
        "out": str(outp),
    }
    json.dump(report, open(ROOT / args.report, "w"), indent=1)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
