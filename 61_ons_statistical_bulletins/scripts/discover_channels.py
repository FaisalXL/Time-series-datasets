#!/usr/bin/env python3
"""Discover each bulletin family's channel set from the prose itself, and prove it.

WHY THIS EXISTS. The demo wired CDIDs by hand. That shipped the wrong variant: the retailsales
bulletin quoted an online-sales share of 29.4% while the hand-picked CDID `j4mc` read 28.3% --
a different variant of the same concept -- and the family scored 0.25 evidence/record against
2.95 for CPI. Hand-mapping ~500 families is both infeasible and exactly the step that was
wrong.

WHAT IT DOES INSTEAD. One dataset CSV holds every series in the family (mm23: 4,053 series with
full history). So we let the prose choose: a series becomes a channel only if the bulletin's own
figures match its values, at the months the prose names, repeatedly.

WHY A CONTROL IS MANDATORY. Value equality alone is not evidence. The first cut of this script
matched 259,303 claim-series pairs from 2,815 claims -- and its month-shifted control matched
189,344. It discriminated nothing: percentage data has a coincidence floor near its own signal,
and a dataset holds thousands of variants of one concept. Every run therefore reports
real-vs-control, and a family whose ratio is not >= MIN_RATIO does not ship. On CPI the
surviving rule scores 8.3x.

THE ORDERED-GROUP TEST. "Matched in >= 3 editions" is not a group test -- coincidence clears
that bar easily. What must hold is agreement across several DISTINCT MONTHS: a series that
matches the same concept at five different months is locked to the timeline, not to a number.
This also keeps shallow families eligible, since one bulletin quotes many months (current,
previous, year-ago, "lowest since August 2024").

Usage:
    python scripts/discover_channels.py                     # all families in census.json
    python scripts/discover_channels.py --family consumerpriceinflation --verbose
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import onslib as L                                                        # noqa: E402
from onsfetch import fetch, stats                                         # noqa: E402

PKG = HERE.parent
CONTROL_SHIFTS = (7, -5, 13, -11)      # month offsets; coprime-ish so seasonality can't align
# The gate that matters is PER CHANNEL, not per family. A family-wide ratio sums matches over
# every candidate series in the dataset -- ~5,000 for CPI, almost all of them junk variants -- so
# its denominator is dominated by series that will never ship and it moves for reasons that have
# nothing to do with the channels selected. Each channel is instead required to beat its own
# month-shifted control, and the shipped-set ratio is reported alongside the search-space one.
MIN_SELECTED_RATIO = 3.0               # aggregate over SELECTED channels
MIN_CLAIMS = 5                         # per channel: sanity floor
MIN_MONTHS = 3                         # per channel: distinct months agreeing
ALPHA = 0.001                          # per-channel Bonferroni-corrected significance
MAX_CHANNELS = 20
SAMPLE_EDITIONS = 8


_DS_INDEX = None


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam). Stdlib only; k here is small."""
    if lam <= 0:
        return 0.0 if k > 0 else 1.0
    if k <= 0:
        return 1.0
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def _dataset_index() -> dict:
    """subtopic -> [(uri_path, dataset_id)] for CDID time-series datasets. See
    build_dataset_index.py for why the fallback is needed at all."""
    global _DS_INDEX
    if _DS_INDEX is None:
        _DS_INDEX = collections.defaultdict(list)
        p = PKG / "datasets_index.json"
        if p.exists():
            for r in json.load(open(p)):
                _DS_INDEX[r["subtopic"]].append((r["uri_path"], r["dataset_id"]))
    return _DS_INDEX


def family_datasets(path: str, fam: str, edition: str) -> list[tuple[str, str]]:
    """-> [(dataset_uri_path, dataset_id)] for this family.

    Union of two sources, because neither alone is sufficient:
      (a) the CSVs the bulletin's own relateddata page links -- precise, but for uklabourmarket
          every linked dataset is xlsx-only and the CDID series live elsewhere;
      (b) time-series datasets in the family's own subtopic -- the fallback that recovers those.
    """
    out = []
    c, b = fetch(f"https://www.ons.gov.uk/{path}/bulletins/{fam}/{edition}/relateddata")
    if c == 200:
        d = b.decode("utf8", "replace")
        for u in sorted(set(re.findall(r'href="(/[a-z0-9/]+?/datasets/[a-z0-9]+)"', d)))[:6]:
            c2, b2 = fetch(f"https://www.ons.gov.uk{u}/current")
            if c2 != 200:
                continue
            for m in re.finditer(rf"{re.escape(u)}/current/([a-z0-9]+)\.csv",
                                 b2.decode("utf8", "replace")):
                out.append((u.strip("/"), m.group(1)))
    out.extend(_dataset_index().get(path, []))
    return sorted(set(out))


def edition_claims(path: str, fam: str, edition: str, exclude: set[str]):
    """-> (ref, shape, [(clause_low, ctoks, value, kind, scale, numtext, targets)])"""
    c, b = fetch(f"https://www.ons.gov.uk/{path}/bulletins/{fam}/{edition}")
    if c != 200:
        return None, f"http_{c}", []
    ref, shape = L.coverage_period(L.page_title(b))
    if not ref or "-Q" in ref:
        return ref, shape, []
    out = []
    for sec in L.parse_sections(b):
        if sec["anchor"] in exclude:
            continue
        text = "\n\n".join(L.clean_paragraphs(sec["text"]))
        for _off, clause in L.split_clauses(text):
            cl = [c for c in L.claims(clause) if c[1] in L.TYPED]
            if not cl:
                continue
            low, ctoks = clause.lower(), L.clause_tokens(clause)
            for i, (v, kind, scale, numtext) in enumerate(cl):
                tgt = L.clause_target_months(clause, ref, i, len(cl))
                out.append((low, ctoks, v, kind, scale, numtext, tgt))
    return ref, shape, out


def _score(series, idx, sscale, PT, cl_all, shift=0, units=None, vocab=None):
    """-> {cdid: {claims, months:set, spec}} for series the prose both NAMES and matches."""
    if units is None:
        units = {cd: s.get("unit", "") for cd, s in series.items()}
    out = collections.defaultdict(lambda: {"claims": 0, "months": set(), "spec": 0})
    n_nonident = 0
    for low, ctoks, v, kind, scale, numtext, tgt in cl_all:
        hits = {}                              # cd -> (spec, month), for THIS claim only
        for _lbl, key in tgt:
            k = L.shift_month(key, shift)
            for lo, hi, us in L.probes_for(v, kind, scale, numtext):
                for cd in L.lookup(idx, k, lo, hi):
                    # An unknown unit (None) may match at any scale -- ONS labels £m
                    # series as m/M/'' and £bn as bn, so demanding an exact scale match on an
                    # unparsed unit is what produced 0 matches from 794 claims.
                    if kind in ("money", "count") and sscale[cd] is not None and sscale[cd] != us:
                        continue
                    if not L.unit_compatible(kind, units[cd]):
                        continue
                    ok, spec = L.names_series(PT[cd], ctoks, low, vocab)
                    if not ok:
                        continue
                    if cd not in hits or spec > hits[cd][0]:
                        hits[cd] = (spec, k)
        # A claim matched by many series identifies none of them -- discard it rather than
        # crediting all of them. See onslib.MAX_MULTIPLICITY.
        if len(hits) > L.MAX_MULTIPLICITY:
            n_nonident += 1
            continue
        for cd, (spec, k) in hits.items():
            r = out[cd]
            r["claims"] += 1
            r["months"].add(k)
            r["spec"] = max(r["spec"], spec)
    out["__nonidentifying__"] = {"claims": n_nonident, "months": set(), "spec": 0}
    return out


def discover(path: str, fam: str, editions: list[str], exclude: set[str], verbose=False) -> dict:
    """Full discovery for one family."""
    t0 = time.time()
    rep = {"family": fam, "path": path, "n_editions_total": len(editions)}
    ds = family_datasets(path, fam, editions[0])
    rep["datasets"] = ds
    if not ds:
        rep["status"] = "no_dataset"
        return rep
    series = {}
    for p, i in ds:
        d = L.load_dataset(p, i)
        for cd, s in d.items():
            if s["m"]:                       # monthly-capable series only
                series.setdefault(cd, s)
    rep["n_candidate_series"] = len(series)
    if not series:
        rep["status"] = "no_monthly_series"
        return rep

    # sample editions spread across the family's history, newest first
    step = max(1, len(editions) // SAMPLE_EDITIONS)
    sample = editions[::step][:SAMPLE_EDITIONS] or editions[:1]
    cl_all, refs, shapes = [], [], collections.Counter()
    for ed in sample:
        ref, shape, cl = edition_claims(path, fam, ed, exclude)
        shapes[shape] += 1
        if ref:
            refs.append(ref)
        cl_all.extend(cl)
    rep.update(n_sampled=len(sample), n_claims=len(cl_all),
               period_shapes=dict(shapes), sample=sample)
    if len(cl_all) < MIN_CLAIMS:
        rep["status"] = "too_few_claims"
        return rep

    PT = {cd: L.parse_title(s["title"]) for cd, s in series.items()}
    sscale = {cd: L.unit_scale(s["unit"]) for cd, s in series.items()}
    idx = L.build_index(series, "m")

    vocab = L.prose_vocab(c[1] for c in cl_all)
    rep["prose_vocab_size"] = len(vocab)
    real = _score(series, idx, sscale, PT, cl_all, 0, vocab=vocab)
    ctrl = collections.defaultdict(int)
    for sh in CONTROL_SHIFTS:
        for cd, v in _score(series, idx, sscale, PT, cl_all, sh, vocab=vocab).items():
            ctrl[cd] += v["claims"]
    SENT = "__nonidentifying__"
    rep["nonidentifying_claims"] = real.get(SENT, {}).get("claims", 0)
    real = {k: v for k, v in real.items() if k != SENT}
    ctrl.pop(SENT, None)
    tot_real = sum(v["claims"] for v in real.values())
    tot_ctrl = sum(ctrl.values()) / len(CONTROL_SHIFTS)
    rep["floor"] = {"search_space_real": tot_real,
                    "search_space_control": round(tot_ctrl, 1),
                    "search_space_ratio": round(tot_real / tot_ctrl, 2) if tot_ctrl else None,
                    "control_shifts": list(CONTROL_SHIFTS),
                    "note": "search_space_* covers every candidate series in the dataset, most "
                            "of which never ship; selected_* is the floor for the shipped set"}

    # A FIXED ratio bar cannot work across these datasets, because the number of chances to get
    # lucky differs by orders of magnitude. `uktrade` tests 1,567 monthly series against 991
    # claims and its average series matches the SHIFTED control 4.3 times; `consumerpriceinflation`
    # averages 0.17. So a 6x-over-control rule kept "Trade in Goods: Hong Kong: Total: Exports"
    # for a bulletin that never mentions Hong Kong, while the same rule is barely a constraint on
    # CPI. This is a multiple-comparisons problem, and the control already measures the null: test
    # each channel against Poisson(its own coincidence rate) and Bonferroni-correct by the number
    # of candidate series actually tested.
    n_tested = max(1, len(series))
    lam_avg = tot_ctrl / n_tested
    rep["null_model"] = {"mean_control_per_series": round(lam_avg, 3),
                         "n_series_tested": n_tested,
                         "bonferroni_alpha": ALPHA / n_tested}
    picked = []
    for cd, v in real.items():
        c = ctrl[cd] / len(CONTROL_SHIFTS)
        if v["claims"] < MIN_CLAIMS or len(v["months"]) < MIN_MONTHS:
            continue
        lam = max(c, lam_avg)
        p = poisson_sf(v["claims"], lam)
        if p * n_tested >= ALPHA:
            continue
        picked.append({"cdid": cd, "title": series[cd]["title"], "ons_unit": series[cd]["unit"],
                       "claims": v["claims"], "months": len(v["months"]), "spec": v["spec"],
                       "control": round(c, 2), "null_lambda": round(lam, 3),
                       "p_value": p, "p_bonferroni": min(1.0, p * n_tested),
                       "n_points": len(series[cd]["m"])})
    picked.sort(key=lambda r: (-r["spec"], -r["claims"], -r["months"]))
    picked = picked[:MAX_CHANNELS]
    # Channel labels must be DISTINCT within a record: validate.py warns on a repeated `unit`,
    # and --strict promotes warnings to errors, so a title collision after truncation would
    # fail the gate for every record in the family. Disambiguate with the CDID.
    used = set()
    for r in picked:
        u = unit_label(r["title"], r["ons_unit"], r["cdid"])
        if u in used:
            u = f"{u}__{r['cdid']}"
        used.add(u)
        r["unit"] = u
        # Freeze the requirement discovery actually proved, so the build applies the same rule
        # without re-deriving the family vocabulary (and cannot drift from it).
        pt = PT[r["cdid"]]
        r["require"] = {
            "measure": sorted(pt["measure"]),
            "restriction": sorted(pt["restriction"]),
            "segments": [sorted(w for w in seg if w in vocab) for seg in pt["segments"]
                         if any(w in vocab for w in seg)],
        }
    rep["channels"] = picked
    rep["n_channels"] = len(picked)
    rep["evidence_per_sampled_edition"] = round(
        sum(r["claims"] for r in picked) / max(1, len(sample)), 2)
    sel_real = sum(r["claims"] for r in picked)
    sel_ctrl = sum(r["control"] for r in picked)
    sel_ratio = round(sel_real / sel_ctrl, 2) if sel_ctrl else None
    rep["floor"].update(selected_real=sel_real, selected_control=round(sel_ctrl, 2),
                        selected_ratio=sel_ratio)
    rep["status"] = ("no_channels" if not picked else
                     "ok" if (sel_ratio is None or sel_ratio >= MIN_SELECTED_RATIO)
                     else "below_floor")
    rep["secs"] = round(time.time() - t0, 1)
    if verbose:
        print(json.dumps({k: v for k, v in rep.items() if k != "channels"}, indent=2))
        for r in picked:
            print(f"   {r['cdid']} spec={r['spec']} claims={r['claims']:>3} "
                  f"months={r['months']:>2} ctrl={r['control']:<5} {r['title'][:66]}")
    return rep


_SLUG = re.compile(r"[^a-z0-9]+")


def unit_label(title: str, ons_unit: str, cdid: str) -> str:
    """A readable, distinct channel label. `unit` is the channel's name in the record."""
    t = title.lower()
    t = re.sub(r"\b\d{4}\s*=\s*\d+\b", "", t)
    t = _SLUG.sub("_", t).strip("_")[:56].strip("_")
    u = _SLUG.sub("_", (ons_unit or "").lower()).strip("_")[:18]
    return f"{t}__{u}" if u else (t or f"cdid_{cdid}")


def _job(args):
    path, fam, eds, exclude = args
    try:
        return discover(path, fam, eds, set(exclude))
    except Exception as e:                       # one bad family must not kill the sweep
        return {"family": fam, "path": path, "status": "error", "error": f"{type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--census", default=str(PKG / "census.json"))
    ap.add_argument("--out", default=str(PKG / "channels.json"))
    ap.add_argument("--family", action="append", help="restrict to these families")
    ap.add_argument("--min-editions", type=int, default=1)
    ap.add_argument("--dataset-subtopics-only", action="store_true",
                    help="only families whose subtopic holds a CDID time-series dataset. These "
                         "are exactly what crawl.py --triage warmed, so the sweep runs off cache "
                         "and cannot be throttled part-way through; without it, 104 uncached "
                         "families drove the shared fetch gap from 2s to 10.6s.")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 8))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    census = json.load(open(args.census))
    exclude = ["glossary", "data-sources-and-quality", "related-links",
               "cite-this-statistical-bulletin", "measuring-the-data",
               "strengths-and-limitations", "future-developments", "collaboration",
               "related-publications", "authors", "acknowledgements"]
    subs = None
    if args.dataset_subtopics_only:
        subs = {r["subtopic"] for r in json.load(open(PKG / "datasets_index.json"))}
    jobs = []
    for key, eds in sorted(census.items(), key=lambda kv: -len(kv[1])):
        path, fam = key.split("||")
        if args.family and fam not in args.family:
            continue
        if subs is not None and path not in subs:
            continue
        if len(eds) < args.min_editions:
            continue
        jobs.append((path, fam, [e[0] for e in eds], exclude))
    print(f"discovering {len(jobs)} families with {args.workers} workers")

    if args.family and len(jobs) <= 2:
        out = [discover(p, f, e, set(x), verbose=args.verbose) for p, f, e, x in jobs]
    else:
        out = []
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_job, j): j[1] for j in jobs}
            for i, fu in enumerate(as_completed(futs), 1):
                r = fu.result()
                out.append(r)
                ok = r.get("status")
                print(f"[{i}/{len(jobs)}] {r['family'][:46]:<46} {ok:<18} "
                      f"chan={r.get('n_channels', 0):>3} ratio={(r.get('floor') or {}).get('ratio')}",
                      flush=True)
    Path(args.out).write_text(json.dumps(out, indent=1))
    by = collections.Counter(r.get("status") for r in out)
    print(f"\nstatus: {dict(by)}")
    okf = [r for r in out if r.get("status") == "ok"]
    print(f"shippable families: {len(okf)}  channels: {sum(r['n_channels'] for r in okf)}")
    print(f"wrote {args.out}   fetch {stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
