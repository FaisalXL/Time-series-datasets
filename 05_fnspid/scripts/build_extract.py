#!/usr/bin/env python3
"""Stage 2 of the full-scale FNSPID build: the LLM extraction + salience pass.

Reads stage 1's candidates, asks the model for each `(ticker, day)` which sentences discuss
the company and what role the company plays, and writes one verdict line per pair. It writes
NO record text -- only sentence indices and a role. Stage 3 assembles the prose verbatim.

This is the stage that replaces `require_symbol_in_text` as the keep/drop gate. That filter
rejects 33.5% of pairs, of which a measured 19.3% are primary or secondary -- ~76k good
records -- and it rejects 1-2 char tickers structurally (they are 0.5% of its accepts and
20.2% of its rejects). So the symbol flag is carried through as a recorded feature and the
role verdict decides.

Resumable by design. On restart it reads the verdicts already written, skips those pairs and
appends. A 20 h pass that dies at hour 19 resumes at hour 19, and it never re-reads the 23 GB
wire, because stage 1 already reduced it.

NEVER KEEPS ON A PARSE ERROR. A transport failure, a 500, or unparsable JSON is written with
a non-ok status and no role, so stage 3 drops it. The old config's `keep_on_parse_error: true`
against a thinking model that returns `content: null` would have kept 100% of records -- the
filter would have appeared to run and done nothing.

Usage:
    export VLLM_KEY=...
    python3 scripts/build_extract.py --concurrency 48
    python3 scripts/build_extract.py --limit 60 --out .cache/verdicts_sample.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_run import (HEADERS, LANES, MODEL, SYSTEM, SYSTEM_V2, SYSTEM_V3,  # noqa: E402
                         USER_TMPL, USER_TMPL_V2, USER_TMPL_V3)
from fnspid_emit import company_of, load_ticker_names, split_sentences  # noqa: E402

ROLES = {"primary", "secondary", "incidental", "absent"}


def numbered_window(bodies: List[str], days: List[str], char_cap: int):
    """Number sentences across a window's articles, each introduced by its publication date.

    Returns (sentences, article_index_per_sentence, rendered_block, capped). The rendered
    block carries the date headers; `sentences` carries only the prose, so an index means the
    same thing on both sides of the call.
    """
    sents: List[str] = []
    art: List[int] = []
    lines: List[str] = []
    used = 0
    capped = False
    for ai, body in enumerate(bodies):
        day = days[ai] if ai < len(days) else ""
        header = f"\n--- article {ai + 1}, published {day} ---"
        for s in split_sentences(body):
            if used + len(s) > char_cap and sents:
                capped = True
                return sents, art, "\n".join(lines), capped
            if header:
                lines.append(header)
                header = ""
            sents.append(s)
            art.append(ai)
            lines.append(f"[{len(sents)}] {s}")
            used += len(s) + 1
    return sents, art, "\n".join(lines), capped


def numbered_sentences(bodies: List[str], char_cap: int) -> Tuple[List[str], List[int], bool]:
    """Split every article into sentences and number them across the whole group.

    Returns (sentences, article_index_per_sentence, capped). Article boundaries are kept so
    stage 3 can report which articles actually contributed text, instead of the old builder's
    `n_articles_used: 5` for a record the truncation had cut back to article 1.

    The cap is lede-first: news is inverted-pyramid, so dropping the tail of a very long wire
    dump costs less than dropping the opening. p90 of concatenated article text is 14,180
    chars, so a 12k cap touches roughly the top decile.
    """
    sents: List[str] = []
    art: List[int] = []
    used = 0
    capped = False
    for ai, body in enumerate(bodies):
        for s in split_sentences(body):
            if used + len(s) > char_cap and sents:
                capped = True
                return sents, art, capped
            sents.append(s)
            art.append(ai)
            used += len(s) + 1
    return sents, art, capped


def render(ticker: str, company: str, sents: List[str]) -> str:
    numbered = "\n".join(f"[{i}] {s}" for i, s in enumerate(sents, 1))
    return USER_TMPL.format(ticker=ticker, company=company or ticker, sentences=numbered)


class Endpoint:
    """Thin client with retry/backoff and a circuit breaker on sustained failure."""

    def __init__(self, key: str, timeout: int, retries: int, max_tokens: int):
        self.key, self.timeout, self.retries, self.max_tokens = key, timeout, retries, max_tokens
        self.lock = threading.Lock()
        self.fail = collections.Counter()
        self.consec = 0
        self.tripped = False

    def call(self, user: str, system: str = SYSTEM) -> Tuple[Optional[dict], str, float, int, int]:
        payload = json.dumps({
            "model": MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": self.max_tokens, "temperature": 0.0,
            # REQUIRED: thinking model. With thinking on it returns content: null.
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode()
        last = "unknown"
        for attempt in range(self.retries):
            if self.tripped:
                return None, "circuit_open", 0.0, 0, 0
            t0 = time.time()
            try:
                req = urllib.request.Request(LANES[0], data=payload, headers=HEADERS(self.key))
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    d = json.loads(resp.read())
                el = time.time() - t0
                ch = (d.get("choices") or [{}])[0]
                content = (ch.get("message") or {}).get("content") or ""
                usage = d.get("usage") or {}
                pt, ct = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
                if ch.get("finish_reason") == "length":
                    last = "truncated_output"
                    continue                       # index list overran max_tokens; retry
                lo, hi = content.find("{"), content.rfind("}")
                if lo < 0 or hi <= lo:
                    last = "no_json"
                    continue
                try:
                    v = json.loads(content[lo:hi + 1])
                except json.JSONDecodeError:
                    last = "bad_json"
                    continue
                with self.lock:
                    self.consec = 0
                return v, "ok", el, pt, ct
            except urllib.error.HTTPError as e:
                last = f"http_{e.code}"
            except Exception as e:                 # timeout, reset, DNS, malformed frame
                last = type(e).__name__
            with self.lock:
                self.fail[last] += 1
                self.consec += 1
                if self.consec >= 80 and not self.tripped:
                    self.tripped = True
                    print(f"\n!! circuit breaker: {self.consec} consecutive failures "
                          f"({last}). Stopping cleanly; rerun to resume.", flush=True)
            time.sleep(min(30.0, (2 ** attempt) + random.random()))
        return None, last, 0.0, 0, 0


def stream_candidates(path: Path, done: set, limit: int, stride: int) -> Iterator[dict]:
    n = 0
    for i, line in enumerate(open(path, encoding="utf-8")):
        if stride > 1 and i % stride:
            continue
        c = json.loads(line)
        # window records key on the window start; v1 pair records key on the news date
        if f"{c['t']}|{c.get('w_start') or c['d']}" in done:
            continue
        yield c
        n += 1
        if limit and n >= limit:
            return


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default=".cache/candidates.jsonl")
    ap.add_argument("--out", default=".cache/verdicts.jsonl")
    ap.add_argument("--report", default="output/extract_report.json")
    ap.add_argument("--concurrency", type=int, default=48)
    ap.add_argument("--char-cap", type=int, default=12000, help="max chars of numbered sentences")
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="stop after N new pairs")
    ap.add_argument("--stride", type=int, default=1, help="take every Nth candidate (spread sample)")
    ap.add_argument("--batch", type=int, default=4000)
    ap.add_argument("--summary-words", type=int, default=220,
                    help="v3 summary length cap; 220 leaves ~59% of the char budget unused")
    ap.add_argument("--prompt", default="v2", choices=["v2", "v3"],
                    help="v2 = ranked extraction only; v3 = extraction + summary in one call")
    args = ap.parse_args()

    key = os.environ.get("VLLM_KEY")
    if not key:
        sys.exit("set VLLM_KEY first:  export VLLM_KEY=...   (see llm-api.txt, never commit it)")

    cand = ROOT / args.candidates if not Path(args.candidates).is_absolute() else Path(args.candidates)
    outp = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)

    done = set()
    if outp.exists():
        for line in open(outp, encoding="utf-8"):
            try:
                v = json.loads(line)
                done.add(f"{v['t']}|{v['d']}")
            except json.JSONDecodeError:
                continue                            # truncated last line from a hard kill
        print(f"resuming: {len(done):,} pairs already have a verdict")

    names = load_ticker_names(ROOT / "data" / "ticker_names.json")
    ep = Endpoint(key, args.timeout, args.retries, args.max_tokens)
    print(f"endpoint {LANES[0]}\nmodel {MODEL}  concurrency {args.concurrency}  "
          f"char_cap {args.char_cap}  names {len(names):,}", flush=True)

    stats = collections.Counter()
    lat: List[float] = []
    ptoks: List[int] = []
    t0 = time.time()
    n_new = 0
    fh = open(outp, "a", encoding="utf-8")
    wlock = threading.Lock()

    def work(c: dict) -> None:
        nonlocal n_new
        window = "w_start" in c
        key = c["w_start"] if window else c["d"]
        if window:
            sents, art, block, capped = numbered_window(c["bodies"], c.get("days", []),
                                                        args.char_cap)
            tmpl, system = ((USER_TMPL_V3, SYSTEM_V3) if args.prompt == "v3"
                            else (USER_TMPL_V2, SYSTEM_V2))
            fmt = dict(ticker=c["t"], company=company_of(c["t"], names),
                       period_start=c["w_start"], period_end=c["w_end"], sentences=block)
            if args.prompt == "v3":
                fmt["max_words"] = args.summary_words
            user = tmpl.format(**fmt)
        else:
            sents, art, capped = numbered_sentences(c["bodies"], args.char_cap)
            user = render(c["t"], company_of(c["t"], names), sents)
            system = SYSTEM
        if not sents:
            rec = {"t": c["t"], "d": key, "status": "no_sentences"}
        else:
            v, status, el, pt, ct = ep.call(user, system)
            rec = {"t": c["t"], "d": key, "status": status, "n_sents": len(sents),
                   "sent_chars": sum(len(s) for s in sents), "capped": capped,
                   "n_articles": len(c["bodies"])}
            if status == "ok":
                role = str(v.get("role", "")).strip().lower()
                idxs = [i for i in (v.get("sentences") or []) if isinstance(i, int)]
                bad = [i for i in idxs if not (1 <= i <= len(sents))]
                # ORDER IS THE PAYLOAD in v2: the model ranks by importance and the assembler
                # fills the token budget from the front. Sorting here would throw the ranking
                # away and silently restore the blind truncation v2 exists to remove. Dedup
                # keeps the FIRST occurrence, i.e. the model's highest placement.
                ranked: List[int] = []
                for i in idxs:
                    if 1 <= i <= len(sents) and i not in ranked:
                        ranked.append(i)
                summary = str(v.get("summary") or "").strip()
                rec.update({
                    "role": role if role in ROLES else "invalid_role",
                    "sentences": ranked,
                    # stored raw and UNVALIDATED here; stage 3 runs the numeric-fidelity gate
                    # and decides whether this or the extraction becomes the record text
                    "summary": summary,
                    "summary_chars": len(summary),
                    "invalid_idx": len(bad),
                    "relation": str(v.get("relation", ""))[:160],
                    "confidence": v.get("confidence"),
                    "latency_s": round(el, 3), "prompt_tok": pt, "compl_tok": ct,
                })
                with wlock:
                    lat.append(el)
                    ptoks.append(pt)
        with wlock:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats[rec["status"]] += 1
            if rec.get("role"):
                stats["role:" + rec["role"]] += 1
            n_new += 1
            if n_new % 2000 == 0:
                fh.flush()
                el_t = time.time() - t0
                print(f"  {n_new:>9,} done  {el_t:>6.0f}s  {n_new/el_t:>6.1f}/s  "
                      f"ok {stats['ok']:,}  fails {sum(ep.fail.values()):,}", flush=True)

    batch: List[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for c in stream_candidates(cand, done, args.limit, args.stride):
            batch.append(c)
            if len(batch) >= args.batch:
                list(ex.map(work, batch))
                batch = []
                if ep.tripped:
                    break
        if batch and not ep.tripped:
            list(ex.map(work, batch))
    fh.close()
    el = time.time() - t0

    report = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": LANES[0], "model": MODEL,
        "concurrency": args.concurrency, "char_cap": args.char_cap,
        "pairs_new": n_new, "pairs_total_with_verdict": len(done) + n_new,
        "elapsed_s": round(el, 1),
        "rate_per_s": round(n_new / el, 2) if el else None,
        "status": {k: v for k, v in stats.items() if not k.startswith("role:")},
        "roles": {k[5:]: v for k, v in stats.items() if k.startswith("role:")},
        "transport_failures": dict(ep.fail),
        "circuit_tripped": ep.tripped,
        "latency_s": {"median": round(statistics.median(lat), 3),
                      "p90": round(sorted(lat)[int(len(lat) * .9)], 3)} if lat else {},
        "prompt_tokens": {"median": statistics.median(ptoks),
                          "p90": sorted(ptoks)[int(len(ptoks) * .9)]} if ptoks else {},
    }
    rp = ROOT / args.report if not Path(args.report).is_absolute() else Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    json.dump(report, open(rp, "w"), indent=1)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
