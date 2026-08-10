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
import bisect
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


_re = __import__("re")
# Matches a number and, optionally, the magnitude word or symbol attached to it. The
# leading-dot alternative is not cosmetic: the wires write "$.8" for eight tenths, and a
# pattern requiring a leading digit sees only "8" -- which is how a correct summary saying
# "$0.8" got scored as an invention.
_NUMTOK = _re.compile(
    r"(?<![\d.])(\d[\d,]*(?:\.\d+)?|\.\d+)\s*"
    # The trailing guard excludes letters and digits but NOT a period: the decimal part is
    # already consumed greedily above, so any period still ahead is sentence punctuation.
    # Excluding it too made "$9.93." at the end of a sentence match nothing at all, and made
    # "$1.5 billion." parse as 1.5 with the magnitude silently dropped -- which is how a
    # stricter-looking gate ended up rejecting six times as many sound summaries.
    r"(%|k|m|mm|bn|b|tn|thousand|million|billion|trillion)?(?![a-z0-9])",
    _re.I)
# "[9]", "[75, 86]" -- the model citing sentence numbers from the prompt, not stating figures
_CITE = _re.compile(r"\[\s*\d+(?:\s*,\s*\d+)*\s*\]")
# separators that can sit between the two ends of a written range: "-", en/em dash, "to",
# optionally with a currency symbol on the second number
_RANGE_SEP = _re.compile(r"\s*(?:-|\u2013|\u2014|to)\s*[$\u20ac\u00a3]?\s*")
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6,
          "b": 1e9, "bn": 1e9, "billion": 1e9, "tn": 1e12, "trillion": 1e12}


def parse_numbers(text: str):
    """Yield (token, value, tolerance) for every number, resolving magnitude words.

    Comparing number STRINGS was the original design and it was wrong in both directions.
    "US$437k" in an article and "$437,000" in a summary are the same fact and never matched;
    so are "5,823,912 shares" and "over 5.8 million". Parsing to a value and carrying a
    tolerance derived from the written precision handles every one of those uniformly,
    instead of accreting a special case per format.

    `tolerance` is half a unit in the last significant place, i.e. exactly the rounding the
    written form implies. "5.8 million" tolerates +/-50,000 and so matches 5,823,912;
    "$9.9 billion" tolerates +/-50,000,000 and still cannot match 1,234,000,000.
    """
    ms = list(_NUMTOK.finditer(text))
    for k, m in enumerate(ms):
        raw, suf = m.group(1), (m.group(2) or "").lower()
        # RANGES SHARE THEIR MAGNITUDE. Guidance is written "$4.09-$4.13 billion", where only
        # the second number carries the word; read literally the first is 4.09 and gets
        # compared against a source figure of 4.09 billion. So a bare number immediately
        # followed by a range separator and a scaled number inherits that scale.
        if not suf and k + 1 < len(ms):
            gap = text[m.end():ms[k + 1].start()]
            nxt = (ms[k + 1].group(2) or "").lower()
            if nxt and nxt != "%" and len(gap) <= 6 and _RANGE_SEP.fullmatch(gap):
                suf = nxt
        digits = raw.replace(",", "")
        try:
            val = float(digits)
        except ValueError:
            continue
        if suf and suf != "%":
            val *= _SCALE.get(suf, 1.0)
        # significant places as WRITTEN: trailing zeros of an integer are not significant
        # ("437,000" is a rounded 437k, not a claim to unit precision)
        if "." in digits:
            dec = len(digits.split(".", 1)[1])
            tol = 0.5 * (10 ** -dec)
        else:
            stripped = digits.rstrip("0")
            tol = 0.5 * (10 ** (len(digits) - len(stripped))) if stripped else 0.5
        if suf and suf != "%":
            tol *= _SCALE.get(suf, 1.0)
        yield m.group(0).strip(), val, max(tol, 1e-9)


def numeric_fidelity(summary: str, source: str, dates=()):
    """Does every number in the summary correspond to one the model was actually shown?

    This is the gate that makes summarisation shippable. The corpus's value is real figures
    against a real series, so a fabricated "$2.3bn" is far worse than a dropped article -- and
    unlike prose drift, a wrong number is cheap to detect.

    IT TOOK THREE PASSES TO GET THE PREMISE RIGHT, and every error was the gate rejecting
    sound summaries rather than letting inventions through:
      * v1 left the trailing period on "2015." so it never matched "2015", and compared only
        against article bodies although the prompt ALSO showed each article's publication
        date -- "August 23, 2015" scored as an invention. ~50% of summaries were rejected.
      * v2 still compared strings, so "US$437k" vs "$437,000" and "$.8" vs "$0.8" failed, as
        did any journalistic rounding of a large figure.
      * This version compares VALUES with a tolerance taken from the written precision, which
        subsumes all of those without a special case for each.

    A match is: some number the model was shown lies within the rounding implied by how the
    summary wrote its own figure. That is deliberately permissive -- it detects invented
    magnitudes, not misattributed ones. A summary that moves a correct figure onto the wrong
    subject passes here and is a job for the human review, not for this check.

    Returns (n_numbers, n_unsupported, [every unsupported token]).
    """
    src = [v for _t, v, _tol in parse_numbers(source)]
    for d in dates:                      # publication dates were shown in the prompt headers
        for part in str(d).split("-"):
            try:
                src.append(float(part))
            except ValueError:
                pass
    src.sort()

    def supported(val: float, tol: float) -> bool:
        i = bisect.bisect_left(src, val - tol)
        return i < len(src) and src[i] <= val + tol

    # Strip the model's own citations first: "[9]", "[75, 86]" point at prompt sentence
    # numbers. They are references, not claims about the world, and scoring them as figures
    # rejected summaries that were entirely sound. The prompt forbids them too.
    bad, total = [], 0
    for tok, val, tol in parse_numbers(_CITE.sub(" ", summary)):
        total += 1
        if not supported(val, tol):
            bad.append(tok)
    return total, len(bad), bad


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
    ap.add_argument("--adjudications", default="",
                    help="stage 2.5 output; overturns figures the value check could not place")
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

    # Stage 2.5 verdicts on figures the value matcher could not place. Absent file = no
    # adjudication, and every flagged figure stands as a rejection.
    adjud: Dict[str, dict] = {}
    if args.adjudications and resolve(args.adjudications).exists():
        for line in open(resolve(args.adjudications), encoding="utf-8"):
            a = json.loads(line)
            adjud[f"{a['t']}|{a['d']}"] = {f["figure"]: f["supported"] for f in a["figures"]}
        print(f"adjudications loaded: {len(adjud):,} records", flush=True)

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
    overturned_total = 0
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
        # A figure the value check could not place is not yet a rejection: stage 2.5 may have
        # found it stated in a form the matcher cannot express ("fiscal '22", "$436-million",
        # "$4.3 mln"). Only figures with no adjudication, or an upheld one, still count.
        adj = adjud.get(f"{w['t']}|{w['w_start']}", {})
        unresolved = [t for t in examples if not adj.get(t)]
        if examples and adj:
            n_overturned = len(examples) - len(unresolved)
            if n_overturned:
                overturned_total += n_overturned
        n_bad = len(unresolved)
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
                        {"numbers": n_num, "unsupported": n_bad, "tokens": examples}
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
        "figures_overturned_by_adjudication": overturned_total,
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
