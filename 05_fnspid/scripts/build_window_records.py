#!/usr/bin/env python3
"""Assemble window records: series + ranked extraction -> schema-v1 JSONL.

Pairs with `build_windows.py` (which forms the windows) and `build_extract.py --prompt v2`
(which ranks the sentences). This is the cheap, re-runnable stage: every policy knob lives
here, so changing the token budget or the kept roles costs one CPU pass and zero GPU.

THE BUDGET IS SPENT IN RANK ORDER. v1 assembled the model's chosen sentences in document
order and then cut the tail at 2,240 chars, which discarded model-selected content in 46% of
records. Here the model returns its selection ranked by importance, the budget is filled from
the front of that ranking, and only then are the survivors re-sorted into document order so
the record reads as prose. Nothing is dropped arbitrarily: what falls outside the budget is
what the model itself ranked last.

Usage:
    python3 scripts/build_window_records.py --windows .cache/windows_90.jsonl \
        --verdicts .cache/verdicts_w90.jsonl --out output/window90.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_extract import numbered_window  # noqa: E402
from fnspid_emit import (CHANNEL_SPEC, figures_in_series, make_record,  # noqa: E402
                         truncate_at_sentence)

ERAS = [("2009-2012", "0000", "2013-01-01"), ("2013-2016", "2013-01-01", "2017-01-01"),
        ("2017-2020", "2017-01-01", "2021-01-01"), ("2021-2023", "2021-01-01", "9999")]


def era_of(d: str) -> str:
    for name, lo, hi in ERAS:
        if lo <= d < hi:
            return name
    return "other"


def resolve(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (ROOT / q)


_NUMTOK = __import__("re").compile(r"\d[\d,]*(?:\.\d+)?")


def _norm(tok: str) -> str:
    return tok.replace(",", "").rstrip(".")


def numeric_fidelity(summary: str, source: str, dates=()):
    """Every number in an LLM summary must occur in the source it was shown.

    This is the gate that makes summarisation shippable. The corpus's whole value is real
    figures paired with a real series, so a fabricated "$2.3bn" is far more damaging than a
    dropped article -- and unlike prose drift, a wrong number is cheap to detect.

    THE PREMISE HAD TO BE CHECKED BEFORE THE GATE COULD BE TRUSTED. A first version flagged
    ~50% of summaries, which looked like a damning hallucination rate and was almost entirely
    the checker's fault: it left the trailing period on "2015." so it never matched "2015",
    and it compared only against article bodies while the prompt ALSO showed the model each
    article's publication date. The model writing "August 23, 2015" was then scored as an
    invention. `dates` therefore carries every date the prompt exposed, decomposed into the
    components a writer would actually use.

    Returns (n_numbers, n_unsupported, [examples]).
    """
    src = {_norm(m.group(0)) for m in _NUMTOK.finditer(source)}
    for d in dates:                      # "2015-08-19" -> 2015, 08, 8, 19
        parts = str(d).split("-")
        for p in parts:
            src.add(p)
            src.add(p.lstrip("0") or "0")
        src.add(str(d))
    bad, total = [], 0
    for m in _NUMTOK.finditer(summary):
        tok = _norm(m.group(0))
        total += 1
        if tok in src:
            continue
        try:
            f = float(tok)
        except ValueError:
            continue
        # tolerate a rounded restatement of a source figure: 12.34 -> 12.3
        cands = {f"{f:.1f}", f"{f:.2f}", f"{f:g}"}
        if f == int(f):
            cands.add(str(int(f)))
        if cands & src:
            continue
        bad.append(m.group(0))
    return total, len(bad), bad[:5]


def fill_budget(sents: List[str], ranked: List[int], budget: int) -> List[int]:
    """Take the longest prefix of the model's ranking that fits, then order by document.

    Greedy on the ranking rather than on length: a shorter but less important sentence must
    not displace a more important one, or the ranking stops meaning anything.
    """
    picked: List[int] = []
    used = 0
    for i in ranked:
        need = len(sents[i - 1]) + (1 if picked else 0)
        if used + need > budget:
            continue          # skip and keep going: a later, shorter pick may still fit
        picked.append(i)
        used += need
    return sorted(picked)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.fullscale.yaml")
    ap.add_argument("--windows", default=".cache/windows_90.jsonl")
    ap.add_argument("--verdicts", default=".cache/verdicts_w90.jsonl")
    ap.add_argument("--out", default="output/window90.jsonl")
    ap.add_argument("--report", default="")
    ap.add_argument("--roles", default="primary,secondary")
    ap.add_argument("--text-from", default="extraction", choices=["extraction", "summary"],
                    help="which variant becomes `text`; summary falls back on a fidelity fail")
    ap.add_argument("--floor", type=int, default=300)
    ap.add_argument("--char-cap", type=int, default=24000, help="must match stage 2")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(resolve(args.config), encoding="utf-8"))
    channels = list(cfg["data"]["channels"])
    csv_cols = [CHANNEL_SPEC[c][0] for c in channels]
    budget = int(cfg["text"]["max_chars"])
    keep_roles = {r.strip() for r in args.roles.split(",") if r.strip()}

    verdicts: Dict[str, dict] = {}
    for line in open(resolve(args.verdicts), encoding="utf-8"):
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            continue
        verdicts[f"{v['t']}|{v['d']}"] = v
    print(f"verdicts {len(verdicts):,}  budget {budget} chars  roles {sorted(keep_roles)}",
          flush=True)

    outp = resolve(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    fh = open(outp, "w", encoding="utf-8")

    drops: collections.Counter = collections.Counter()
    roles_seen: collections.Counter = collections.Counter()
    kept = 0
    tc: List[int] = []
    fig: List[int] = []
    ctrl_pool: List[list] = []
    eras: collections.Counter = collections.Counter()
    arts: collections.Counter = collections.Counter()
    ranked_used: List[float] = []
    fid_rates: List[float] = []
    fid_fail: collections.Counter = collections.Counter()
    used_summary = 0
    fell_back = 0
    tickers: set = set()
    t0 = time.time()
    seen = 0

    for line in open(resolve(args.windows), encoding="utf-8"):
        w = json.loads(line)
        seen += 1
        v = verdicts.get(f"{w['t']}|{w['w_start']}")
        if v is None:
            drops["no_verdict"] += 1
            continue
        if v.get("status") != "ok":
            drops["verdict_" + str(v.get("status"))] += 1
            continue
        role = v.get("role")
        roles_seen[role] += 1
        if role not in keep_roles:
            drops["role_" + str(role)] += 1
            continue
        ranked = v.get("sentences") or []
        if not ranked:
            drops["no_sentences_selected"] += 1
            continue

        sents, art_of, _block, _cap = numbered_window(w["bodies"], w.get("days", []),
                                                      args.char_cap)
        ranked = [i for i in ranked if 1 <= i <= len(sents)]
        if not ranked:
            drops["all_indices_invalid"] += 1
            continue
        picks = fill_budget(sents, ranked, budget)
        if not picks:
            drops["nothing_fits_budget"] += 1
            continue
        extractive = " ".join(sents[i - 1] for i in picks)
        used_articles = sorted({art_of[i - 1] for i in picks})
        ranked_used.append(len(picks) / len(ranked))

        # --- choose the record text -------------------------------------------------
        # The extraction is always computed and always stored. Whether it or the summary
        # becomes `text` is a policy flag, so flipping costs one CPU pass and zero GPU.
        summary = (v.get("summary") or "").strip()
        source_text = " ".join(w["bodies"])
        # the prompt showed the model each article's publication date and the period bounds,
        # so those are legitimately quotable and belong in the supported set
        shown_dates = list(w.get("days", [])) + [w["w_start"], w["w_end"]]
        n_num, n_bad, examples = (numeric_fidelity(summary, source_text, shown_dates)
                                  if summary else (0, 0, []))
        summary_ok = bool(summary) and n_bad == 0 and len(summary) >= args.floor
        if not summary:
            fid_fail["no_summary"] += 1
        elif not summary_ok:
            fid_fail["unsupported_number" if n_bad else "too_short"] += 1

        if args.text_from == "summary" and summary_ok:
            body, quality, tsource = summary, "generated", "generated"
            used_summary += 1
            # a summary draws on every article in the window, not just the extracted ones,
            # so provenance has to widen or the record under-reports its own sources
            used_articles = list(range(len(w["bodies"])))
        else:
            body, quality, tsource = extractive, "real", "third_party"
            if args.text_from == "summary":
                fell_back += 1
        if len(body) < args.floor:
            drops["below_floor"] += 1
            continue
        if n_num:
            fid_rates.append(1 - n_bad / n_num)

        wd = w["dates"]
        wv = {c: w["vals"][c] for c in csv_cols}
        try:
            rec = make_record(
                ticker=w["t"], news_date=w["w_end"], article_block=body, channels=channels,
                win_dates=wd, win_vals=wv,
                urls=[w["urls"][i] for i in used_articles if i < len(w["urls"])],
                titles=[w["titles"][i] for i in used_articles if i < len(w["titles"])],
                n_articles_seen=len(w["bodies"]), text_cap=budget,
                text_quality=quality, text_source=tsource,
                extra_meta={
                    "record_shape": "window",
                    "text_from": "summary" if quality == "generated" else "extraction",
                    # both variants ride along, so the schema decision can be revisited
                    # without another GPU pass
                    "extractive_text": extractive if quality == "generated" else None,
                    "summary_text": summary if quality != "generated" else None,
                    "summary_numeric_fidelity": (
                        {"numbers": n_num, "unsupported": n_bad, "examples": examples}
                        if summary else None),
                    "window_start": w["w_start"], "window_end": w["w_end"],
                    "window_trading_days": len(wd),
                    "news_days_in_window": w.get("n_news_days_in_window"),
                    "news_dates": w.get("news_days"),
                    "era": era_of(w["w_start"]),
                    # The text is drawn from INSIDE the window, so text and series overlap in
                    # time by design. Stamped so nobody builds a forecasting eval on this.
                    "lookahead_safe": False,
                    "article_spread_gt5": w.get("article_spread_gt5", 0),
                    "extraction": {
                        "prompt": "extract_v2",
                        "model": "Qwen/Qwen3.6-35B-A3B",
                        "role": role,
                        "relation": v.get("relation"),
                        "confidence": v.get("confidence"),
                        "ranked_sentences": ranked,
                        "sentences_used": picks,
                        "n_sentences_available": len(sents),
                        "budget_chars": budget,
                        "input_capped": bool(v.get("capped")),
                    },
                },
            )
        except ValueError as exc:
            drops[f"emit_error:{str(exc)[:50]}"] += 1
            continue

        rec["meta"]["n_articles_used"] = len(used_articles)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        kept += 1
        tc.append(rec["meta"]["text_chars"])
        fig.append(rec["meta"]["figures_matching_own_series"])
        eras[rec["meta"]["era"]] += 1
        arts[len(used_articles)] += 1
        tickers.add(w["t"])
        if len(ctrl_pool) < 4000:
            close = next((s["values"] for s in rec["timeseries"]
                          if s["unit"] == "close_price_usd"), [])
            ctrl_pool.append([body, close])
        if args.limit and kept >= args.limit:
            break
    fh.close()

    ctrl = []
    for i, (bd, _c) in enumerate(ctrl_pool):
        other = ctrl_pool[(i + len(ctrl_pool) // 2 + 1) % len(ctrl_pool)][1]
        ctrl.append(figures_in_series(bd, other))

    report = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "record_shape": "window", "prompt": "extract_v2",
        "policy": {"roles_kept": sorted(keep_roles), "budget_chars": budget,
                   "floor_chars": args.floor, "text_from": args.text_from},
        "windows_seen": seen, "records_kept": kept,
        "distinct_tickers": len(tickers),
        "roles_observed": dict(roles_seen.most_common()),
        "drops": dict(drops.most_common()),
        "era": dict(eras.most_common()),
        "n_articles_used": dict(sorted(arts.items())),
        "ranked_share_used": round(sum(ranked_used) / len(ranked_used), 3) if ranked_used else None,
        "text_from": args.text_from,
        "records_using_summary": used_summary,
        "records_fell_back_to_extraction": fell_back,
        "summary_numeric_fidelity_mean": round(sum(fid_rates) / len(fid_rates), 4) if fid_rates else None,
        "summary_gate_failures": dict(fid_fail),
        "text_chars": {"median": statistics.median(tc),
                       "p10": sorted(tc)[len(tc) // 10],
                       "p90": sorted(tc)[-max(1, len(tc) // 10)]} if tc else {},
        "figures_matching_own_series_mean": round(sum(fig) / len(fig), 3) if fig else None,
        "PERMUTATION_CONTROL_mean": round(sum(ctrl) / len(ctrl), 3) if ctrl else None,
        "elapsed_s": round(time.time() - t0, 1),
        "output_path": str(outp),
    }
    if args.report:
        json.dump(report, open(resolve(args.report), "w"), indent=1)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
