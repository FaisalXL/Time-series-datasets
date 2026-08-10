#!/usr/bin/env python3
"""Stage 2.5: let the model adjudicate the figures the deterministic gate could not match.

The numeric gate compares values and is precise but narrow. Six times it rejected a sound
summary because the source wrote the same fact differently -- "US$437k" against "$437,000",
"$.8" against "$0.8", "fiscal '22" against "fiscal 2022" -- and each fix was a patch on the
previous patch. Rather than enumerate formats forever, the flagged figures are handed to the
model, which is good at exactly this kind of equivalence.

ONLY THE FLAGGED FIGURES GO TO THE MODEL, so this costs ~5% of a verification pass rather
than 100%.

THE MODEL IS NOT TOLD A CHECK REJECTED ANYTHING. Handing over a verdict and asking for review
produces anchoring -- the model ratifies or contrarily overturns depending on phrasing rather
than evidence. It is asked neutrally which source sentence states the figure.

The answer is a sentence INDEX, not prose, so the citation is checkable: an index outside the
range is a fabricated citation and upholds the rejection. The cited sentence is stored on the
record, so a human auditing the corpus can see the evidence the decision rested on rather
than a bare verdict.

Usage:
    export VLLM_KEY=...
    python3 scripts/build_adjudicate.py --windows .cache/windows_90.jsonl \
        --verdicts .cache/dev_v90.jsonl --out .cache/adjud_90.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import re as _re
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_extract import Endpoint, numbered_window  # noqa: E402
from build_window_records import numeric_fidelity, parse_numbers  # noqa: E402

SYSTEM = ("You check whether a figure appears in a set of source articles. You answer with a "
          "sentence number or null. You output only compact JSON.")

USER = """The numbered sentences below are the complete source articles about {company} for
the period {period}.

{sentences}

QUESTION: does any sentence above state this figure?

    {figure}

It counts as stated even if written differently -- a different unit or scale ("$437k" for
"$437,000"), an abbreviated year ("fiscal '22" for "fiscal 2022"), a rounding of a longer
number ("5.8 million" for "5,823,912"), or one end of a range whose unit is given once.
It does NOT count if the figure is merely similar to a different quantity, or if it would
have to be calculated from other numbers rather than read.

Answer with the number of the single sentence that states it. Answer 0 if no sentence does.

Output this JSON only:
{{"sentence": <integer>}}"""

DERIVE_SYSTEM = ("You identify whether a figure can be computed from figures stated in source "
                 "text. You report the operation and its inputs. You never do the arithmetic "
                 "yourself. You output only compact JSON.")

DERIVE_USER = """The numbered sentences below are the complete source articles about {company}.

{sentences}

This figure appears in a summary of those articles but is not stated in them:

    {figure}

Can it be computed from figures that ARE stated? If so, report the operation and its inputs.
Use exactly one operation name:
  pct_change   - percentage change from an earlier value to a later one
  difference   - one value minus another
  sum          - values added together
  ratio        - one value divided by another
  share_of     - one value as a percentage of another

Report the input numbers exactly as written in the sentences, and the sentence number each
one comes from. Do NOT calculate the result -- only name the operation and its inputs.
If the figure cannot be obtained this way, use "none".

Output this JSON only:
{{"op": "<name>", "operands": [<number>, ...], "sentences": [<integer>, ...]}}"""


def check_derivation(op, operands, target, tol):
    """Recompute the claimed derivation OURSELVES and compare to the figure as written.

    The model names the operation and its inputs; the arithmetic is done here. That is the
    whole point -- a model that reports 33% from $1.88 and $2.50 is checkable, and one that
    reports 43% from the same inputs is caught by the same code path. Trusting the model's
    own result would verify nothing.
    """
    try:
        xs = [float(x) for x in operands]
    except (TypeError, ValueError):
        return False, None
    try:
        if op == "pct_change" and len(xs) == 2 and xs[0]:
            got = (xs[1] - xs[0]) / abs(xs[0]) * 100.0
        elif op == "difference" and len(xs) == 2:
            got = xs[1] - xs[0]
        elif op == "sum" and xs:
            got = sum(xs)
        elif op == "ratio" and len(xs) == 2 and xs[1]:
            got = xs[0] / xs[1]
        elif op == "share_of" and len(xs) == 2 and xs[1]:
            got = xs[0] / xs[1] * 100.0
        else:
            return False, None
    except ZeroDivisionError:
        return False, None
    # allow either sign convention for a change, and the figure's own written precision
    return (abs(got - target) <= max(tol, abs(target) * 0.01)
            or abs(abs(got) - abs(target)) <= max(tol, abs(target) * 0.01)), round(got, 4)

# `0` rather than `null` for "not found" is deliberate. Asked for null, the model replied with
# a bare `null` -- a correct answer, but not a JSON object, so the parser saw no braces, burned
# four retries and recorded a transport-shaped failure on what was actually a clean verdict.
# An integer sentinel keeps every answer the same shape.


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default=".cache/windows_90.jsonl")
    ap.add_argument("--verdicts", default=".cache/dev_v90.jsonl")
    ap.add_argument("--out", default=".cache/adjud_90.jsonl")
    ap.add_argument("--char-cap", type=int, default=24000)
    ap.add_argument("--concurrency", type=int, default=24)
    ap.add_argument("--max-tokens", type=int, default=80)
    args = ap.parse_args()

    key = os.environ.get("VLLM_KEY")
    if not key:
        sys.exit("set VLLM_KEY first")

    def resolve(p):
        q = Path(p)
        return q if q.is_absolute() else (ROOT / q)

    verdicts: Dict[str, dict] = {}
    for line in open(resolve(args.verdicts), encoding="utf-8"):
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            continue
        if v.get("status") == "ok" and v.get("summary"):
            verdicts[f"{v['t']}|{v['d']}"] = v

    # find the records whose summary has a figure the deterministic gate could not place
    jobs: List[dict] = []
    n_sum = 0
    for line in open(resolve(args.windows), encoding="utf-8"):
        w = json.loads(line)
        v = verdicts.get(f"{w['t']}|{w['w_start']}")
        if not v:
            continue
        n_sum += 1
        dates = list(w.get("days", [])) + [w["w_start"], w["w_end"]]
        _n, n_bad, toks = numeric_fidelity(v["summary"], " ".join(w["bodies"]), dates)
        if n_bad:
            jobs.append({"w": w, "tokens": toks})
    print(f"{n_sum:,} summaries; {len(jobs):,} carry a figure the gate could not place "
          f"({100*len(jobs)/max(1,n_sum):.1f}%) -> {sum(len(j['tokens']) for j in jobs)} figures",
          flush=True)
    if not jobs:
        return

    ep = Endpoint(key, 180, 4, args.max_tokens)
    outp = resolve(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    fh = open(outp, "w", encoding="utf-8")
    lock = threading.Lock()
    stats: collections.Counter = collections.Counter()
    t0 = time.time()

    def work(job: dict) -> None:
        w = job["w"]
        sents, _art, block, _cap = numbered_window(w["bodies"], w.get("days", []), args.char_cap)
        results = []
        for tok in job["tokens"]:
            content, status, _el, _pt, _ct = ep.call_raw(
                USER.format(company=w["t"], period=f"{w['w_start']} to {w['w_end']}",
                            sentences=block, figure=tok), SYSTEM)
            if status != "ok":
                results.append({"figure": tok, "status": status, "supported": False})
                stats["call_failed"] += 1
                continue
            # Lenient: the answer is one integer, however the model chose to wrap it.
            m = _re.search(r"-?\d+", content or "")
            idx = int(m.group(0)) if m else 0
            # 0 = model found nothing; an index outside the range is a fabricated citation.
            # Both uphold the rejection.
            ok = isinstance(idx, int) and 1 <= idx <= len(sents)
            # A VALID INDEX IS NOT EVIDENCE. Asked where "883 million" appeared, the model
            # cited a sentence about ETF unit creation containing no such figure -- in range,
            # and worthless. So the cited sentence must itself carry a digit string that
            # overlaps the figure's. Deliberately loose (substring either way) because the
            # whole point is to accept "'22" for "2022" and "436-million" for "436 million";
            # it only has to defeat a citation with no numeric relation at all.
            if ok:
                want = _re.sub(r"\D", "", tok)
                got = "".join(_re.findall(r"\d+", sents[idx - 1]))
                cited_ok = bool(want) and (want in got or any(
                    d in want for d in _re.findall(r"\d+", sents[idx - 1]) if len(d) >= 2))
                if not cited_ok:
                    ok = False
                    stats["citation_unsupported"] += 1
                    idx = 0
            if not ok:
                # SECOND PHASE: the figure may be DERIVED rather than stated -- "33%" computed
                # from "$2.50" against "$1.88". The model names the operation and its inputs;
                # the arithmetic is done here, so a correct derivation is admitted and a wrong
                # one is caught by the same code path. Trusting the model's own result would
                # verify nothing.
                d, dstatus, _e, _p, _c = ep.call(
                    DERIVE_USER.format(company=w["t"], sentences=block, figure=tok),
                    DERIVE_SYSTEM)
                if dstatus == "ok" and isinstance(d, dict) and d.get("op") not in (None, "none"):
                    tgt = next(((v, t2) for _s, v, t2 in parse_numbers(tok)), None)
                    cites = [i for i in (d.get("sentences") or []) if isinstance(i, int)
                             and 1 <= i <= len(sents)]
                    cited_text = " ".join(sents[i - 1] for i in cites)
                    src_vals = {round(v, 6) for _s, v, _t in parse_numbers(cited_text)}
                    operands = d.get("operands") or []
                    inputs_present = bool(cites) and all(
                        any(abs(float(x) - v) <= max(1e-9, abs(v) * 0.005) for v in src_vals)
                        for x in operands if isinstance(x, (int, float)))
                    if tgt and inputs_present:
                        good, got = check_derivation(d["op"], operands, tgt[0], tgt[1])
                        if good:
                            ok = True
                            stats["overturned_derived"] += 1
                            results.append({
                                "figure": tok, "status": "ok", "supported": True,
                                "derived": {"op": d["op"], "operands": operands,
                                            "sentences": cites, "recomputed": got},
                                "sentence": cited_text[:300]})
                            continue
                        stats["upheld_bad_arithmetic"] += 1
                    else:
                        stats["upheld_operands_not_in_source"] += 1
                results.append({"figure": tok, "status": "ok", "supported": False,
                                "sentence_index": idx or None})
                stats["upheld_not_found"] += 1
                continue
            results.append({
                "figure": tok, "status": "ok", "supported": True,
                "sentence_index": idx, "sentence": sents[idx - 1][:300],
            })
            stats["overturned_stated"] += 1
        with lock:
            fh.write(json.dumps({"t": w["t"], "d": w["w_start"], "figures": results},
                                ensure_ascii=False) + "\n")

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        list(ex.map(work, jobs))
    fh.close()

    tot = sum(stats.values())
    print(json.dumps({
        "summaries_seen": n_sum, "records_adjudicated": len(jobs),
        "figures_checked": tot, "verdicts": dict(stats),
        "failure_reasons": dict(ep.fail),
        "overturn_rate_pct": round(100 * (stats["overturned_stated"] + stats["overturned_derived"]) / max(1, tot), 1),
        "elapsed_s": round(time.time() - t0, 1), "out": str(outp),
    }, indent=1))


if __name__ == "__main__":
    main()
