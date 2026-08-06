#!/usr/bin/env python3
"""Run the extraction+salience prompt over a batch of devset records.

Reports the role distribution, extract lengths, and throughput. This is the measurement that
decides whether the LLM replaces `require_symbol_in_text`, and what the character floor
should be.

Deliberately conservative on concurrency. The previous endpoint (ds-serv11:8004-8007) died
under a 16-request burst and never recovered, so this ramps and stops on repeated failure.

Usage:
    export VLLM_KEY=...
    python3 scripts/extract_batch.py --kept 30 --rejects 15 --roundups 15 --concurrency 6
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_run import HEADERS, LANES, MODEL, SYSTEM, assemble, render  # noqa: E402
from fnspid_emit import split_sentences  # noqa: E402

NAMES = {}
_np = ROOT / "data" / "ticker_names.json"
if _np.exists():
    NAMES = json.load(open(_np))

# Names harvested from prose sometimes captured only the legal suffix. Reject those so the
# prompt shows the ticker instead of a meaningless COMPANY value.
_BAD_NAME = {"inc", "corp", "corporation", "co", "cos", "ltd", "limited", "llc", "plc", "lp",
             "group", "holdings", "company", "etf", "trust", "systems", "technologies",
             "technology", "international", "industries", "partners", "nv", "sa"}


def company_of(tk: str) -> str:
    nm = (NAMES.get(tk) or "").strip()
    if not nm or len(nm) < 4 or nm.strip(". ").lower() in _BAD_NAME:
        return tk
    return nm


_lock = threading.Lock()
_fail = {"n": 0}


def judge(rec, floor, retries=3):
    tk = rec["meta"]["ticker"]
    body = rec["text"].split("\n\n<ts></ts>")[0]
    sents, user = render(tk, company_of(tk), body)
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
        "max_tokens": 200, "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    key = os.environ["VLLM_KEY"]
    for attempt in range(retries):
        t0 = time.time()
        try:
            req = urllib.request.Request(LANES[0], data=payload, headers=HEADERS(key))
            with urllib.request.urlopen(req, timeout=180) as resp:
                d = json.loads(resp.read())
            m = d["choices"][0]["message"]
            content = m.get("content") or ""
            usage = d.get("usage") or {}
            try:
                v = json.loads(content[content.find("{"):content.rfind("}") + 1])
            except Exception:
                # NEVER keep on a parse error. Retry, then drop.
                if attempt == retries - 1:
                    return {"ticker": tk, "status": "parse_error", "raw": content[:200]}
                continue
            text, bad, ok = assemble(sents, v.get("sentences", []), floor)
            verbatim = all(s in body for s in text.split(" ")[:1]) if text else True
            return {"ticker": tk, "status": "ok",
                    "class": rec["meta"].get("devset_class"),
                    "role": v.get("role"), "confidence": v.get("confidence"),
                    "relation": v.get("relation"),
                    "n_sent_total": len(sents), "n_sent_sel": len(set(v.get("sentences") or [])),
                    "invalid_idx": bad, "extract_chars": len(text),
                    "body_chars": len(body), "floor_pass": ok, "verbatim": verbatim,
                    "latency_s": round(time.time() - t0, 2),
                    "prompt_tok": usage.get("prompt_tokens"),
                    "compl_tok": usage.get("completion_tokens"),
                    "news_date": rec["meta"]["news_date"]}
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
            with _lock:
                _fail["n"] += 1
            if attempt == retries - 1:
                return {"ticker": tk, "status": f"transport_error:{type(e).__name__}"}
            time.sleep(2 * (attempt + 1))
    return {"ticker": tk, "status": "exhausted"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="devset", help="output/<prefix>{,_rejects,_roundups}.jsonl")
    ap.add_argument("--kept", type=int, default=30)
    ap.add_argument("--rejects", type=int, default=15)
    ap.add_argument("--roundups", type=int, default=15)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--floor", type=int, default=300)
    ap.add_argument("--out", default="output/extract_batch.jsonl")
    args = ap.parse_args()
    if not os.environ.get("VLLM_KEY"):
        sys.exit("export VLLM_KEY first")

    jobs = []
    for suffix, n in (("", args.kept), ("_rejects", args.rejects), ("_roundups", args.roundups)):
        if n <= 0:
            continue
        p = ROOT / "output" / f"{args.prefix}{suffix}.jsonl"
        if not p.exists():
            print(f"  [skip] {p.name} not found")
            continue
        recs = [json.loads(l) for l in open(p)]
        jobs += recs[:n]
    print(f"{len(jobs)} records, concurrency {args.concurrency}, floor {args.floor}, "
          f"names {len(NAMES):,}")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        out = list(ex.map(lambda r: judge(r, args.floor), jobs))
    el = time.time() - t0

    outp = ROOT / args.out
    with open(outp, "w") as fh:
        for o in out:
            fh.write(json.dumps(o) + "\n")

    ok = [o for o in out if o["status"] == "ok"]
    print(f"\n{len(ok)}/{len(out)} ok in {el:.0f}s -> {len(ok)/el:.2f} rec/s "
          f"({len(ok)/el*3600/1000:.1f}k/h)   transport failures {_fail['n']}")
    if len(ok) < len(out):
        print("  non-ok:", collections.Counter(o["status"] for o in out if o["status"] != "ok"))
    if not ok:
        return
    print(f"  latency median {statistics.median(o['latency_s'] for o in ok):.2f}s   "
          f"prompt tok median {statistics.median(o['prompt_tok'] for o in ok):.0f}   "
          f"completion tok median {statistics.median(o['compl_tok'] for o in ok):.0f}")
    bad_idx = sum(1 for o in ok if o["invalid_idx"])
    print(f"  records with an invalid sentence index: {bad_idx}")

    print(f"\n{'devset class':<24}{'n':>4}  role distribution")
    print("-" * 78)
    for cls in ("kept", "symbol_filter_reject", "wire_roundup"):
        g = [o for o in ok if o["class"] == cls]
        if not g:
            continue
        dist = collections.Counter(o["role"] for o in g)
        print(f"{cls:<24}{len(g):>4}  " + "  ".join(f"{k}={v}" for k, v in dist.most_common()))

    print(f"\n{'class':<24}{'role':<12}{'n':>4}{'median extract':>16}{'median % body':>15}{'floor pass':>12}")
    print("-" * 83)
    for cls in ("kept", "symbol_filter_reject", "wire_roundup"):
        for role in ("primary", "secondary", "incidental", "absent"):
            g = [o for o in ok if o["class"] == cls and o["role"] == role]
            if not g:
                continue
            ec = [o["extract_chars"] for o in g]
            pb = [100 * o["extract_chars"] / max(1, o["body_chars"]) for o in g]
            fp = sum(1 for o in g if o["floor_pass"])
            print(f"{cls:<24}{role:<12}{len(g):>4}{statistics.median(ec):>16,.0f}"
                  f"{statistics.median(pb):>14.0f}%{fp:>8}/{len(g)}")

    print("\nsample relations:")
    for o in ok[:10]:
        print(f"  [{o['ticker']:<5}] {o['role']:<11} {o['extract_chars']:>5}ch  {o['relation']}")
    print(f"\nwrote {outp}")


if __name__ == "__main__":
    main()
