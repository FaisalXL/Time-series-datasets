#!/usr/bin/env python3
"""LLM pass: split each two-innings match report into per-innings sentence indices.

Contract, rationale and the measurement that scoped it to two-innings formats:
`prompts/attribute_v1.md`. The model returns INDICES ONLY and never sees the time series,
so the builder assembles verbatim (`text_quality: real`) and the permutation control in
`build_cpt_jsonl.py` stays meaningful.

Resumable: one cached JSON per match under `.cache/attrib/`. Re-running picks up only what
is missing, so this can be run repeatedly while the ESPN fetch is still filling in.

  python scripts/attribute.py                      # all cached reports
  python scripts/attribute.py --limit 200          # smoke test
  python scripts/attribute.py --workers 24
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cricket_lib import (ROOT, TWO_INNINGS, innings_table, load_report,  # noqa: E402
                         parse_info, sentences)

SYSTEM = ("You segment cricket match reports for a research corpus. You select sentence "
          "indices. You never write new text. You never invent facts. You output only "
          "compact JSON.")

USER = """Assign each sentence to the innings it narrates.

Rules:
- Return sentence INDICES only, never text.
- A sentence describing a team's batting (their scoring, partnerships, collapse, chase) belongs to the innings that team BATTED in.
- A sentence describing bowling figures belongs to the innings the bowler was bowling IN — that is the innings where the opposing team batted.
- Sentences about the whole match, the toss, the result, conditions, the series, or a player's career go in "shared".
- Every index must appear exactly once across all buckets. Do not omit any.
- Order the indices inside each bucket by importance: most substantive first.

MATCH: {match}
INNINGS:
{innings}

SENTENCES:
{sentences}

Output JSON only, exactly this shape:
{{"innings": {{{ex}}}, "shared": [ints]}}"""

_lock = threading.Lock()
_stat: Dict[str, int] = {"done": 0, "cached": 0, "ok": 0, "bad_json": 0, "error": 0,
                         "repair_dup": 0, "repair_missing": 0, "repair_oob": 0}


def _client(cfg):
    from openai import OpenAI
    l = cfg["llm"]
    key = os.environ.get(l.get("api_key_env", "VLLM_KEY"), "")
    if not key:
        raise SystemExit(
            f"Set ${l.get('api_key_env', 'VLLM_KEY')} (the endpoint key is not committed).")
    return OpenAI(base_url=l["base_url"], api_key=key)


def _ints(v, n: int) -> List[int]:
    out = []
    for x in (v or []):
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n:
            out.append(i)
    return out


def _oob(v, n: int) -> int:
    """Genuinely unusable indices: non-numeric, or outside the sentence range.

    A JSON string index ("3") is NOT out of bounds — it coerces cleanly — and counting it
    as a repair would inflate the report with a defect that never existed.
    """
    c = 0
    for x in (v or []):
        try:
            i = int(x)
        except (TypeError, ValueError):
            c += 1
            continue
        if not (0 <= i < n):
            c += 1
    return c


def attribute_one(client, cfg, mid: str, info: dict, table: list, text: str) -> Optional[dict]:
    """-> {"innings": {inn: [idx...]}, "shared": [idx...], "repairs": {...}} | None."""
    l = cfg["llm"]
    sents = sentences(text)
    n = len(sents)
    if n < 3:
        return None
    roster = [(t["innings"], t["batting_team"], t["bowling_team"], t["overs"]) for t in table]
    body = USER.format(
        match=f"{(info.get('match_type') or '?').upper()} · {info.get('event') or 'n/a'} · {info.get('date') or '?'}",
        innings="\n".join(f"  innings {i}: {b} batting vs {bw} ({o} overs)"
                          for i, b, bw, o in roster),
        ex=", ".join(f'"{i}": [ints]' for i, _, _, _ in roster),
        sentences="\n".join(f"[{k}] {s}" for k, s in enumerate(sents)))

    got = None
    for attempt in range(int(l.get("retries", 3))):
        try:
            r = client.chat.completions.create(
                model=l["model"], max_tokens=int(l.get("max_tokens", 2048)), temperature=0,
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": body}],
                extra_body={"chat_template_kwargs": {"enable_thinking": False}})
            raw = re.sub(r"^```(?:json)?|```$", "", r.choices[0].message.content.strip(),
                         flags=re.M).strip()
            got = json.loads(raw)
            break
        except json.JSONDecodeError:
            with _lock:
                _stat["bad_json"] += 1
            time.sleep(1.0 * (attempt + 1))
        except Exception:
            with _lock:
                _stat["error"] += 1
            time.sleep(2.0 * (attempt + 1))
    if got is None:
        return None

    # --- repair: never trust the model's bookkeeping (see prompts/attribute_v1.md) ---
    raw_buckets = got.get("innings") or {}
    valid = [i for i, _, _, _ in roster]
    n = len(sents)
    oob = 0
    per_bucket: Dict[str, List[int]] = {}
    claims: Dict[int, List[str]] = {}
    for i in valid:
        cand = raw_buckets.get(i, raw_buckets.get(str(i), []))
        oob += _oob(cand, n)
        per_bucket[i] = _ints(cand, n)
        for x in per_bucket[i]:
            claims.setdefault(x, []).append(i)
    for k in raw_buckets:
        if str(k) not in {str(v) for v in valid}:
            oob += 1                                # bucket for an innings not in this match

    # A sentence claimed by MORE THAN ONE innings is a match-level sentence, not evidence
    # that the first innings owns it. Giving it to whichever bucket happened to come first
    # would silently bias every contested sentence toward innings 1; it goes to `shared`
    # instead, so both records carry it.
    contested = {x for x, who in claims.items() if len(who) > 1}
    dup = len(contested)
    buckets = {i: [x for x in per_bucket[i] if x not in contested] for i in valid}
    assigned = {x for v in buckets.values() for x in v}

    # shared order is an importance ranking (the budgeter drops from the tail): the model's
    # own shared list first, then contested, then prose it forgot to assign at all.
    shared = [x for x in _ints(got.get("shared"), n) if x not in assigned]
    seen = assigned | set(shared)
    shared += [x for x in sorted(contested) if x not in seen]
    seen |= contested
    missing = [i for i in range(n) if i not in seen]
    shared += missing

    with _lock:
        _stat["repair_dup"] += dup
        _stat["repair_missing"] += len(missing)
        _stat["repair_oob"] += oob
    return {"innings": buckets, "shared": shared, "n_sents": len(sents),
            "repairs": {"dup": dup, "missing": len(missing), "oob": oob}}


def main() -> None:
    ap = argparse.ArgumentParser(description="LLM innings attribution (cached, resumable)")
    ap.add_argument("--config", type=Path, default=ROOT / "config.example.yaml")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args()

    cfg = yaml.safe_load(args.config.read_text())
    d = cfg["data"]
    cache = ROOT / d["cache_dir"]
    outdir = cache / "attrib"
    outdir.mkdir(parents=True, exist_ok=True)
    workers = args.workers or int(cfg["llm"].get("workers", 16))
    min_chars = int(cfg["text"]["min_report_chars"])

    zp = cache / "all_csv2.zip"
    if not zp.exists():
        raise SystemExit(f"missing {zp} — run the builder once to download it")
    z = zipfile.ZipFile(zp)
    names = set(z.namelist())
    mids = sorted(n[:-4] for n in names
                  if n.endswith(".csv") and not n.endswith("_info.csv"))

    # Only matches whose report is ALREADY cached. This pass must never fetch: it is meant
    # to run alongside the ESPN harvest, and calling the fetching loader here would issue a
    # second, slower request stream for every match the harvest has not reached yet.
    espn_dir = cache / "espn"
    todo = []
    for mid in mids:
        if (outdir / f"{mid}.json").exists():
            _stat["cached"] += 1
            continue
        if f"{mid}_info.csv" not in names or not (espn_dir / f"{mid}.json").exists():
            continue
        info = parse_info(z.read(f"{mid}_info.csv"))
        if (info.get("match_type") or "").upper() not in TWO_INNINGS:
            continue
        text, _ = load_report(mid, d, cache)
        if not text or len(text) < min_chars:
            continue
        todo.append((mid, info))
        if args.limit and len(todo) >= args.limit:
            break

    print(f"{len(todo)} to attribute ({_stat['cached']} already cached), {workers} workers",
          file=sys.stderr)
    if not todo:
        return
    client = _client(cfg)
    t0 = time.time()

    def work(item):
        mid, info = item
        try:
            text, _ = load_report(mid, d, cache)
            table = innings_table(z.read(f"{mid}.csv"))
            res = attribute_one(client, cfg, mid, info, table, text)
            if res:
                (outdir / f"{mid}.json").write_text(json.dumps(res))
                with _lock:
                    _stat["ok"] += 1
        except Exception:
            with _lock:
                _stat["error"] += 1
        with _lock:
            _stat["done"] += 1
            if _stat["done"] % 200 == 0:
                el = time.time() - t0
                rate = _stat["done"] / el
                print(f"  {_stat['done']}/{len(todo)} {rate:.1f}/s "
                      f"eta {(len(todo)-_stat['done'])/max(rate,1e-9)/60:.0f}m {dict(_stat)}",
                      file=sys.stderr, flush=True)

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, todo))

    print(f"\nDONE in {(time.time()-t0)/60:.1f}m: {dict(_stat)}", file=sys.stderr)
    (ROOT / "output" / "attribution_report.json").parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "output" / "attribution_report.json").write_text(
        json.dumps({"stats": _stat, "model": cfg["llm"]["model"],
                    "prompt": "prompts/attribute_v1.md"}, indent=2) + "\n")


if __name__ == "__main__":
    main()
