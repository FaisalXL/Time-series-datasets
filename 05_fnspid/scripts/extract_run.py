#!/usr/bin/env python3
"""Render and run the FNSPID extraction+salience prompt.

Two modes:
  --show N     Render the exact prompt for devset record N. No network. Use this to tune the
               prompt text in prompts/extract_v1.md.
  --run        Send the prompt to the vLLM lanes and print the model's JSON verdict.

The prompt returns sentence INDICES. This module assembles the text verbatim from the source,
so the record keeps `text_quality: real` and never needs the llm_summarized sign-off.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fnspid_emit import company_of, load_ticker_names  # noqa: E402
from fnspid_emit import split_sentences as _split  # noqa: E402

SYSTEM = ("You classify financial news for a research corpus. You select sentences. You never "
          "write new text. You never predict price movement. You output only compact JSON.")

USER_TMPL = """TICKER: {ticker}
COMPANY: {company}

The article appears below as numbered sentences.

{sentences}

Do three things.

1. Decide the role of {ticker} in this article. Use exactly one value:
   "primary"    - the article reports on {company} itself: its results, guidance, products,
                  management, filings, analyst coverage of it, or its own share move.
   "secondary"  - the article reports on something else, but states a specific effect on
                  {company}. Examples: a supplier or customer event, a named peer comparison,
                  a rate or commodity move that the article ties to this company or its sector,
                  index or ETF membership that the article discusses.
   "incidental" - the article names {company} but gives it no substantive discussion. It
                  appears in a ticker enumeration, a one-line table row, a passing comparison,
                  or promotional boilerplate. List membership alone does not decide this. If
                  the article devotes its own paragraph to {company}, choose "primary" or
                  "secondary" instead.
   "absent"     - the article does not name or discuss {company} at all.

2. List the sentence numbers that discuss {company}. Include a sentence only if it states
   something about {company}. Include a following sentence when it continues the same subject
   through a pronoun such as "the company" or "its". Return an empty list for "absent".
   Order the numbers as they appear. Do not invent numbers.

3. Write the relation in 12 words or fewer. Name the mechanism. Do not restate the headline.

Output this JSON only:
{{"role":"primary|secondary|incidental|absent","sentences":[int,...],"relation":"...","confidence":0.0-1.0}}"""

# v2: one window's worth of coverage, ranked by importance. See prompts/extract_v2.md for the
# reasoning, in particular why the model is still not shown the price series.
USER_TMPL_V2 = """TICKER: {ticker}
COMPANY: {company}
PERIOD: {period_start} to {period_end}

Below is every article published about this company during that period, as numbered
sentences. Each article is introduced by its publication date.

{sentences}

Do three things.

1. Decide the role of {company} across this period's coverage. Use exactly one value:
   "primary"    - at least one article reports on {company} itself: its results, guidance,
                  products, management, filings, analyst coverage of it, or its own share move.
   "secondary"  - no article is about {company}, but at least one states a specific effect on
                  it: a supplier or customer event, a named peer comparison, a rate or
                  commodity move the article ties to this company or its sector.
   "incidental" - {company} is named but never substantively discussed: ticker enumerations,
                  one-line table rows, passing comparisons, promotional boilerplate.
   "absent"     - the coverage does not discuss {company} at all.

2. Rank the sentence numbers that a reader would need to understand what happened to
   {company} during this period. MOST IMPORTANT FIRST. Only the first part of your list will
   be used, so the order carries the decision. Prefer sentences that report a concrete event,
   figure, or decision over ones that restate context. Do not include a sentence that says
   nothing about {company}. Do not invent numbers. Return an empty list for "absent".

3. Write the relation in 12 words or fewer. Name the mechanism. Do not restate a headline.

Output this JSON only:
{{"role":"primary|secondary|incidental|absent","sentences":[int,...],"relation":"...","confidence":0.0-1.0}}"""

SYSTEM_V2 = ("You classify financial news for a research corpus. You select and rank sentences. "
             "You never write new text. You never predict price movement. You output only "
             "compact JSON.")

# v3: one call returns BOTH a coverage-constrained ranked extraction AND a summary, so the
# extractive-vs-summarised decision moves out of the GPU stage entirely. See prompts/extract_v3.md.
USER_TMPL_V3 = """TICKER: {ticker}
COMPANY: {company}
PERIOD: {period_start} to {period_end}

Below is every article published about this company during that period, as numbered
sentences. Each article is introduced by its publication date.

{sentences}

Do four things.

1. Decide the role of {company} across this period's coverage. Use exactly one value:
   "primary"    - at least one article reports on {company} itself: its results, guidance,
                  products, management, filings, analyst coverage of it, or its own share move.
   "secondary"  - no article is about {company}, but at least one states a specific effect on
                  it: a supplier or customer event, a named peer comparison, a rate or
                  commodity move the article ties to this company or its sector.
   "incidental" - {company} is named but never substantively discussed.
   "absent"     - the coverage does not discuss {company} at all.

2. Rank sentence numbers a reader needs to understand what happened to {company} in this
   period, MOST IMPORTANT FIRST. COVER AS MANY DISTINCT EVENTS AS YOU CAN: take at most two
   sentences from any one article before moving to another article, and prefer a new event
   over a second detail about an event you already covered. Prefer concrete events, figures
   and decisions over restated context. Do not invent numbers. Empty list for "absent".

3. Write a factual summary of what happened to {company} during this period, in at most 220
   words. Rules: state only facts present in the articles above; every number you write must
   appear verbatim in those articles; cover the distinct events across ALL the articles, not
   just the largest one; name dates where the articles give them; do not predict or
   characterise future price movement; do not add analysis of your own.

4. Write the relation in 12 words or fewer. Name the mechanism.

Output this JSON only:
{{"role":"primary|secondary|incidental|absent","sentences":[int,...],"summary":"...","relation":"...","confidence":0.0-1.0}}"""

SYSTEM_V3 = ("You classify financial news for a research corpus. You select and rank sentences, "
             "and you write strictly factual summaries grounded only in the text you are given. "
             "You never predict price movement. You output only compact JSON.")

# Endpoint updated 2026-08-06. The ds-serv11 lanes (8004-8007) died and never recovered; this
# is the replacement gateway. Single endpoint, HTTPS, FP8 weights, max_model_len 32768.
LANES = ["https://enigmalab.dev/llm/qwen36/v1/chat/completions"]
MODEL = "Qwen/Qwen3.6-35B-A3B"


def HEADERS(key: str) -> dict:
    # The gateway runs a WAF that returns 403 for the default `Python-urllib/3.x` user-agent.
    # curl passes, urllib fails, with an identical request otherwise. Send an explicit UA.
    return {"Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "fnspid-builder/1.0"}


def split_sentences(body: str):
    return _split(body)


def render(ticker: str, company: str, body: str):
    sents = split_sentences(body)
    numbered = "\n".join(f"[{i}] {s}" for i, s in enumerate(sents, 1))
    return sents, USER_TMPL.format(ticker=ticker, company=company or ticker, sentences=numbered)


def assemble(sents, idxs, floor: int):
    """Rebuild text verbatim from the model's indices. Returns (text, dropped_indices)."""
    keep, bad = [], []
    for i in idxs:
        if isinstance(i, int) and 1 <= i <= len(sents):
            keep.append(i)
        else:
            bad.append(i)
    keep = sorted(set(keep))
    text = " ".join(sents[i - 1] for i in keep)
    return text, bad, len(text) >= floor


def call(user: str, lane: int = 0, max_tokens: int = 160):
    key = os.environ.get("VLLM_KEY")
    if not key:
        sys.exit("set VLLM_KEY first: export VLLM_KEY=...")
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens, "temperature": 0.0,
        # REQUIRED: this is a thinking model. With thinking on it returns content: null.
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(LANES[lane % len(LANES)], data=body, headers=HEADERS(key))
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            d = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Surface the body. A bare "HTTP 403" hides a WAF rule; the body names it.
        raise SystemExit(f"HTTP {e.code} from gateway: {e.read()[:400]!r}")
    m = d["choices"][0]["message"]
    return m.get("content"), m.get("reasoning"), d.get("usage")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="output/devset.jsonl")
    ap.add_argument("--index", type=int, default=0, help="record index in --file")
    ap.add_argument("--ticker", help="pick the first record with this ticker instead")
    ap.add_argument("--run", action="store_true", help="send to the LLM")
    ap.add_argument("--lane", type=int, default=0)
    ap.add_argument("--floor", type=int, default=300)
    args = ap.parse_args()

    recs = [json.loads(l) for l in open(ROOT / args.file)]
    if args.ticker:
        recs = [r for r in recs if r["meta"]["ticker"] == args.ticker] or recs
        rec = recs[0]
    else:
        rec = recs[args.index]

    tk = rec["meta"]["ticker"]
    body = rec["text"].split("\n\n<ts></ts>")[0]
    nm = company_of(tk, load_ticker_names(ROOT / "data" / "ticker_names.json"))
    sents, user = render(tk, nm, body)

    print("=" * 96)
    print(f"RECORD  {tk}  {rec['meta']['news_date']}   {len(body)} chars, {len(sents)} sentences")
    print(f"        series: {len(rec['timeseries'])} channels x {rec['meta']['history_days']} "
          f"trading days, {rec['timestamps'][0]} .. {rec['timestamps'][-1]}")
    print(f"        title: {(rec['meta']['article_titles'] or [''])[0][:80]}")
    print("=" * 96)
    print("---------- SYSTEM ----------")
    print(SYSTEM)
    print("\n---------- USER ----------")
    print(user)
    print("=" * 96)

    if args.run:
        content, reasoning, usage = call(user, args.lane)
        print("---------- MODEL OUTPUT ----------")
        print("content  :", repr(content))
        print("reasoning:", repr((reasoning or "")[:200]))
        print("usage    :", usage)
        if content:
            try:
                v = json.loads(content[content.find("{"):content.rfind("}") + 1])
            except Exception as e:
                print("PARSE FAILED ->", e, "  DROP the record, never keep on parse error")
                return
            print("parsed   :", v)
            text, bad, ok = assemble(sents, v.get("sentences", []), args.floor)
            print(f"\nassembled {len(text)} chars from sentences {sorted(set(v.get('sentences',[])))}")
            if bad:
                print(f"  invalid indices dropped: {bad}")
            print(f"  floor {args.floor}: {'PASS' if ok else 'FAIL -> drop record'}")
            print(f"  verbatim check: {'PASS' if all(s in body for s in text.split('  ') if s) else 'CHECK'}")
            print(f"\n---------- ASSEMBLED TEXT ----------\n{text}")
    else:
        print("(no --run: lanes were down. Add --run once a lane answers.)")


if __name__ == "__main__":
    main()
