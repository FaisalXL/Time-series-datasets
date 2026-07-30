#!/usr/bin/env python3
"""Deep final-inspection pass for 55_noaa_stock_assessments.

Reproduces, on this package's record shape, the three checks the corpus's final-inspection
checklist requires (REVIEW_STATUS.md): reconcile, exhaustion, alignment. Reconcile and
exhaustion are asserted by the build itself (run_report.json); this script measures
**alignment** and series/text health, and is deliberately separate from the builder so it can
be re-run against a shipped output.jsonl without a rebuild.

    python scripts/inspect_alignment.py output/noaa_stock_assessments_cpt.jsonl
"""
from __future__ import annotations

import collections
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_cpt_jsonl import recites_terminal, text_numbers  # noqa: E402

# "increased", "declined" ... as applied to the stock. Used for the direction check on the
# `describes` majority: the prose's stated direction should match the series' own recent move.
_UP = re.compile(r"\b(increas\w+|rose|risen|rising|higher|grew|grown|growth|improv\w+|"
                 r"rebuil\w+|recover\w+|above)\b", re.I)
_DOWN = re.compile(r"\b(declin\w+|decreas\w+|fell|fallen|falling|lower|reduc\w+|"
                   r"depleted|below|dropp\w+)\b", re.I)


def q(xs, name, fmt="{:.0f}"):
    xs = sorted(xs)
    n = len(xs)
    print(f"  {name}: n={n} min={xs[0]} p10={xs[n//10]} p25={xs[n//4]} "
          f"med={fmt.format(statistics.median(xs))} p75={xs[3*n//4]} p90={xs[int(.9*n)]} max={xs[-1]}")


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1
                else "output/noaa_stock_assessments_cpt.jsonl")
    recs = [json.loads(l) for l in path.open()]
    n = len(recs)
    print(f"=== {path} : {n} records ===\n")

    # ---------------- 1. contract / hygiene
    print("-- contract --")
    bad_ts = sum(1 for r in recs if r["text"].count("<ts></ts>") != 1)
    gen = sum(1 for r in recs if r.get("text_quality") != "real")
    print(f"  exactly one <ts></ts>: {n - bad_ts}/{n}")
    print(f"  text_quality real: {n - gen}/{n}   text_source: "
          f"{collections.Counter(r.get('text_source') for r in recs).most_common()}")
    print(f"  distinct series_id: {len({r['series_id'] for r in recs})}/{n}")
    print(f"  distinct texts:     {len({r['text'] for r in recs})}/{n}")
    print(f"  distinct sources:   {len({r['source'] for r in recs})}/{n}")
    eq = sum(1 for r in recs
             if len({len(c['values']) for c in r['timeseries']}) == 1)
    print(f"  all channels equal length: {eq}/{n}")
    # <ts> must follow real prose, not page furniture
    pre_ok = sum(1 for r in recs
                 if re.search(r"[a-z]{3}[^\n]{0,80}[.)]\s*$",
                              r["text"].split("<ts></ts>")[0].strip()))
    print(f"  <ts> follows a sentence-final line: {pre_ok}/{n} ({100*pre_ok/n:.1f}%)")

    # ---------------- 2. series health
    print("\n-- series depth --")
    npts = [r["meta"]["n_points"] for r in recs]
    q(npts, "points per record (= years)")
    for th in (8, 16, 24, 32, 43, 60, 100):
        print(f"     >={th:3d} points: {sum(1 for x in npts if x >= th):5d} "
              f"({100*sum(1 for x in npts if x>=th)/n:5.1f}%)")
    nch = [len(r["timeseries"]) for r in recs]
    print(f"  channels: {dict(sorted(collections.Counter(nch).items()))}")
    tot = sum(len(c["values"]) for r in recs for c in r["timeseries"])
    nul = sum(1 for r in recs for c in r["timeseries"] for v in c["values"] if v is None)
    print(f"  timesteps={sum(npts):,}  datapoints={tot:,}  nulls={100*nul/tot:.2f}%")
    over = sum(1 for r in recs
               if sum(1 for c in r['timeseries'] for v in c['values'] if v is None)
               / sum(len(c['values']) for c in r['timeseries']) > 0.30)
    print(f"  records with >30% nulls: {over}")
    yrs = [(r["meta"]["period_start_year"], r["meta"]["period_end_year"]) for r in recs]
    print(f"  calendar span: {min(a for a, _ in yrs)} -> {max(b for _, b in yrs)}")
    chan = collections.Counter(c for r in recs for c in r["meta"]["channels"])
    print(f"  channel mix: {chan.most_common()}")

    # ---------------- 3. structural alignment
    print("\n-- structural alignment --")
    s_end = sum(1 for r in recs if int(r["period_end"][:4]) == r["meta"]["period_end_year"])
    s_start = sum(1 for r in recs if int(r["period_start"][:4]) == r["meta"]["period_start_year"])
    s_len = sum(1 for r in recs
                if all(len(c["values"]) == r["meta"]["n_points"] for c in r["timeseries"]))
    s_term = sum(1 for r in recs if any(c["values"][-1] is not None for c in r["timeseries"]))
    s_cap = sum(1 for r in recs if not r["meta"]["last_data_year"]
                or r["meta"]["period_end_year"] <= int(r["meta"]["last_data_year"]))
    for lbl, v in [("period_end == series terminal year", s_end),
                   ("period_start == window start year", s_start),
                   ("channel length == meta.n_points", s_len),
                   ("terminal point is non-null in >=1 channel", s_term),
                   ("window never runs past the assessment's last data year", s_cap)]:
        print(f"  {lbl}: {v}/{n} ({100*v/n:.2f}%)")

    # ---------------- 4. measured tier + permutation control
    print("\n-- alignment tier (measured, not asserted) --")
    terms = [[c["values"][-1] for c in r["timeseries"] if c["values"][-1] is not None]
             for r in recs]
    tnums = [text_numbers(r["text"]) for r in recs]
    true_hit = [recites_terminal(tx, tv) for tx, tv in zip(tnums, terms)]
    ctrl = [recites_terminal(tnums[(i + 7) % n], tv) for i, tv in enumerate(terms)]
    tr, cr = 100*sum(true_hit)/n, 100*sum(ctrl)/n
    print(f"  recites {sum(true_hit)} / describes {n-sum(true_hit)}")
    print(f"  true rate {tr:.1f}%  permutation control {cr:.1f}%  lift +{tr-cr:.1f} pp")
    print(f"  declared tier matches measurement: "
          f"{sum(1 for r,h in zip(recs,true_hit) if r['alignment']==('recites' if h else 'describes'))}/{n}")

    # Any-value (not just terminal) recite rate, and the orphan-figure counterpart of #47's
    # check: of the figures the prose quotes, how many are values this record's series holds?
    anyhit = 0
    orph_num = orph_den = 0
    for r, tx in zip(recs, tnums):
        vals = {round(v, 4) for c in r["timeseries"] for v in c["values"] if v is not None}
        if not vals:
            continue
        hit = False
        for x in tx:
            if 1800 <= x <= 2100 and abs(x - round(x)) < 1e-9:
                continue          # calendar years prove nothing
            if abs(x) < 1e-9:
                continue
            orph_den += 1
            if any(abs(x - v) <= max(abs(v) * 0.005, 5e-4) for v in vals):
                hit = True
                orph_num += 1
        anyhit += hit
    print(f"  prose quotes SOME value the record's series holds: {anyhit}/{n} ({100*anyhit/n:.1f}%)")
    if orph_den:
        print(f"  of all non-year figures in the prose, share present in this record's "
              f"series: {orph_num}/{orph_den} ({100*orph_num/orph_den:.1f}%)")

    # ---------------- 5. does the prose report the year the window ends on?
    # An assessment report states its own terminal year constantly ("SSB in 2019 was
    # estimated to be 1,222"). If the window ended on a different year than the report
    # narrates, the pairing would be off by a year -- so this is a semantic check on the
    # structural claim, independent of the recite test.
    print("\n-- terminal year: series vs prose --")
    named = latest = 0
    for r in recs:
        end = r["meta"]["period_end_year"]
        asmt = int(r["meta"]["assessment_year"] or end)
        yrs = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", r["text"])]
        yrs = [y for y in yrs if y <= asmt]          # ignore projection years
        if str(end) in r["text"]:
            named += 1
        if yrs and max(yrs) == end:
            latest += 1
    print(f"  window's terminal year is named in the prose: {named}/{n} ({100*named/n:.1f}%)")
    print(f"  it is also the LATEST non-projection year the prose names: "
          f"{latest}/{n} ({100*latest/n:.1f}%)")

    # ---------------- 5b. per-channel direction, restricted to sentences naming that channel
    print("\n-- stated direction vs that channel's own move (per-channel) --")
    KEYS = {"Catch": r"catch|landing|harvest|removal",
            "Abundance": r"biomass|abundance|spawning stock|SSB",
            "Recruitment": r"recruit",
            "Fmort": r"fishing mortality|exploitation rate|\bF\b"}
    agree = tot_d = 0
    for r in recs:
        sents = re.split(r"(?<=[.!?])\s+", r["text"])
        for ch, name in zip(r["timeseries"], r["meta"]["channels"]):
            pat = KEYS.get(name)
            if not pat:
                continue
            real = [v for v in ch["values"] if v is not None]
            if len(real) < 6:
                continue
            recent, prior = real[-1], statistics.median(real[-6:-1])
            if prior == 0:
                continue
            move = (recent - prior) / abs(prior)
            if abs(move) < 0.10:
                continue
            up = dn = 0
            for s in sents:
                if not re.search(pat, s, re.I):
                    continue
                up += len(_UP.findall(s)); dn += len(_DOWN.findall(s))
            if up == dn:
                continue
            tot_d += 1
            if (move > 0) == (up > dn):
                agree += 1
    if tot_d:
        print(f"  agreement: {agree}/{tot_d} ({100*agree/tot_d:.1f}%)  "
              f"[over channel-year pairs whose move exceeds 10% and whose own sentences "
              f"carry a net direction]")

    # ---------------- 6. text health
    print("\n-- text --")
    lens = [len(r["text"]) for r in recs]
    q(lens, "chars per record")
    print(f"  under 800 chars: {sum(1 for x in lens if x < 800)}")
    secs = collections.Counter(s for r in recs for s in r["meta"]["sections"])
    print(f"  sections used: {secs.most_common(12)}")
    fb = sum(1 for r in recs if r["meta"]["sections"] == ["leading_prose"])
    print(f"  fallback (leading prose only, no canonical heading): {fb}/{n} ({100*fb/n:.1f}%)")
    junk = sum(1 for r in recs if ". . ." in r["text"] or "...." in r["text"])
    print(f"  residual dot-leader junk: {junk}")

    print("\n-- coverage --")
    print(f"  distinct stocks: {len({r['meta']['stock_id'] for r in recs})}")
    print(f"  jurisdictions: {collections.Counter(r['meta']['jurisdiction'] for r in recs).most_common()}")
    print(f"  science centers: {collections.Counter(r['meta']['science_center'] for r in recs).most_common()}")
    ay = collections.Counter(int(r["meta"]["assessment_year"]) for r in recs
                            if r["meta"]["assessment_year"])
    print(f"  assessment years: {min(ay)}-{max(ay)}, "
          f"by decade {dict(sorted(collections.Counter((y//10)*10 for y in ay.elements()).items()))}")


if __name__ == "__main__":
    main()
