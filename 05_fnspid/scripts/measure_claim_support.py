#!/usr/bin/env python3
"""Measure the NON-NUMERIC hallucination rate, which nothing else in this pipeline checks.

Every guard built so far inspects numbers. A summary sentence carrying no figures -- "the CEO
resigned in May", "the merger was cleared by regulators" -- passes the fidelity gate, the
adjudicator and the derivation checker without any of them looking at it. This measures how
often that matters, so the decision to add a full verification pass (or not) rests on a number
rather than on a guess.

Method, and its limits:

  * Each summary sentence is put to the model on its own, and the model returns the index of
    the source sentence supporting it, or 0. It is NOT told the passage is a summary, still
    less one the same model wrote, because "check your own work" invites ratification.
  * A returned index is then checked here, not trusted: the cited sentence must share content
    words with the claim. A valid index proved worthless before -- asked where "883 million"
    appeared, the model cited a sentence about ETF unit creation -- so the citation has to
    carry evidence, not just exist.
  * Results are split by whether the claim contains a figure. The numeric half is already
    covered by the fidelity gate; the NON-NUMERIC half is the number this script exists for.

This shares the model under test, so it cannot be called an independent audit. It bounds the
problem; it does not certify the corpus.

Usage:
    export VLLM_KEY=...
    python3 scripts/measure_claim_support.py --records output/devset.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_extract import Endpoint, numbered_window  # noqa: E402
from build_window_records import _NUMTOK  # noqa: E402
from fnspid_emit import split_sentences  # noqa: E402

SYSTEM = ("You locate the sentence in a set of source articles that supports a given "
          "statement. You answer with a sentence number. You output only a number.")

USER = """The numbered sentences below are source articles about {company}.

{sentences}

STATEMENT:

    {claim}

Which sentences above support this statement? A sentence supports it if it states the same
fact, even in different words. A statement that combines several facts -- for example listing
two dividend payments, or summarising a period -- is supported by the set of sentences that
together state them.

List up to three sentence numbers, separated by commas, or 0 if no sentence supports it.

Answer with the numbers only."""

_STOP = set("""the a an and or of to in on for with by from as at is are was were be been being
this that these those it its their his her they them we our you your he she which who whom
have has had will would can could may might shall should must not no than then there here
after before during about over under between into out up down off again further more most
some such only own same so too very just also both each few other while where when what how
company companies inc corp said says say new year years quarter""".split())


def content_words(s: str) -> set:
    """Content tokens for the overlap guard, INCLUDING short ones and figures.

    A first version kept only alphabetic tokens of four characters or more. It scored
    "it has a TSR of 29% for the last 3 years" as failing to support a claim about a 29%
    three-year TSR, because `TSR`, `29%` and `3` were all discarded -- the metric threw away
    precisely the tokens carrying the claim.
    """
    toks = set(re.findall(r"[a-zA-Z][a-zA-Z0-9'-]{1,}|\d[\d,.]*%?", s.lower()))
    return {t for t in toks if t not in _STOP and len(t) >= 3}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", default="output/devset.jsonl")
    ap.add_argument("--report", default="output/claim_support_report.json")
    ap.add_argument("--char-cap", type=int, default=24000)
    ap.add_argument("--overlap", type=float, default=0.20,
                    help="min content-word overlap for a citation to count as evidence")
    ap.add_argument("--concurrency", type=int, default=24)
    args = ap.parse_args()

    key = os.environ.get("VLLM_KEY")
    if not key:
        sys.exit("set VLLM_KEY first")

    recs = [json.loads(l) for l in open(ROOT / args.records, encoding="utf-8")]
    recs = [r for r in recs if r["text_quality"] == "generated"]
    wanted = {(r["meta"]["devset_window_len"],
               r["meta"]["ticker"], r["meta"]["window_start"]) for r in recs}
    wins: Dict[tuple, dict] = {}
    for W in sorted({w for w, _t, _d in wanted}):
        for line in open(ROOT / f".cache/windows_{W}.jsonl", encoding="utf-8"):
            x = json.loads(line)
            k = (W, x["t"], x["w_start"])
            if k in wanted:
                wins[k] = x
    print(f"{len(recs)} summarised records; {len(wins)} windows matched", flush=True)

    jobs = []
    for r in recs:
        m = r["meta"]
        k = (m["devset_window_len"], m["ticker"], m["window_start"])
        w = wins.get(k)
        if not w:
            continue
        sents, _a, block, _c = numbered_window(w["bodies"], w.get("days", []), args.char_cap)
        body = r["text"].split("\n\n<ts></ts>")[0]
        for claim in split_sentences(body):
            if len(claim) < 40:
                continue
            jobs.append({"key": k, "claim": claim, "sents": sents, "block": block,
                         "numeric": bool(_NUMTOK.search(claim))})
    print(f"{len(jobs)} claim sentences to check", flush=True)

    ep = Endpoint(key, 180, 4, 24)
    lock = threading.Lock()
    out: List[dict] = []
    t0 = time.time()

    def work(j: dict) -> None:
        content, status, _e, _p, _c = ep.call_raw(
            USER.format(company=j["key"][1], sentences=j["block"], claim=j["claim"]), SYSTEM)
        rec = {"ticker": j["key"][1], "window": j["key"][2], "numeric": j["numeric"],
               "claim": j["claim"][:200], "status": status}
        if status == "ok":
            # aggregation is what a summary is FOR: "two dividend payments during the
            # period" is supported by the union of the sentences stating each one, and a
            # single-citation question structurally cannot confirm it
            idxs = [int(x) for x in re.findall(r"\d+", content or "")][:3]
            idxs = [i for i in idxs if 1 <= i <= len(j["sents"])]
            if idxs:
                cw = content_words(j["claim"])
                cited = " ".join(j["sents"][i - 1] for i in idxs)
                ov = len(cw & content_words(cited)) / max(1, len(cw))
                rec.update({"cited": idxs, "overlap": round(ov, 3),
                            "verdict": "supported" if ov >= args.overlap else "weak_citation",
                            "sentence": cited[:300]})
            else:
                rec["verdict"] = "unsupported"
        else:
            rec["verdict"] = "call_failed"
        with lock:
            out.append(rec)

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        list(ex.map(work, jobs))

    def summarise(rows):
        c = collections.Counter(r["verdict"] for r in rows)
        n = len(rows) or 1
        ovs = [r["overlap"] for r in rows if "overlap" in r]
        return {"claims": len(rows), **{k: v for k, v in c.most_common()},
                "supported_pct": round(100 * c["supported"] / n, 1),
                "overlap_median": round(statistics.median(ovs), 3) if ovs else None}

    report = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "purpose": "bound the non-numeric hallucination rate the other guards cannot see",
        "caveat": ("verifier is the same model that wrote the summaries; this bounds the "
                   "problem, it does not certify the corpus"),
        "overlap_threshold": args.overlap,
        "records": len(recs), "claims": len(out),
        "ALL": summarise(out),
        "NUMERIC_CLAIMS": summarise([r for r in out if r["numeric"]]),
        "NON_NUMERIC_CLAIMS": summarise([r for r in out if not r["numeric"]]),
        "elapsed_s": round(time.time() - t0, 1),
        "unsupported_examples": [
            {"ticker": r["ticker"], "numeric": r["numeric"], "claim": r["claim"]}
            for r in out if r["verdict"] == "unsupported"][:12],
        "weak_citation_examples": [
            {"ticker": r["ticker"], "overlap": r["overlap"], "claim": r["claim"],
             "cited": r.get("sentence")}
            for r in out if r["verdict"] == "weak_citation"][:8],
    }
    json.dump(report, open(ROOT / args.report, "w"), indent=1)
    print(json.dumps({k: report[k] for k in
                      ("records", "claims", "ALL", "NUMERIC_CLAIMS", "NON_NUMERIC_CLAIMS")},
                     indent=1))
    print(f"\nfull report -> {ROOT / args.report}")


if __name__ == "__main__":
    main()
