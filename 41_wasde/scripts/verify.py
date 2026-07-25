#!/usr/bin/env python3
"""Final-inspection pass for 41_wasde.

Checks the three things the CPT checklist asks for, plus a permutation control on
the recite claim (a bare "does this number appear in the prose" test fires by luck
often enough that the raw rate means little on its own).
"""
from __future__ import annotations

import collections
import json
import re
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

# reuse the builder's own recite matcher so we measure what it claims, not a lookalike
import importlib.util

_spec = importlib.util.spec_from_file_location("wb", ROOT / "scripts" / "build_cpt_jsonl.py")
_wb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wb)
endpoint_recited = _wb.endpoint_recited

# the narrative heading each commodity's block must open with
PROSE_START = {
    "wheat": "WHEAT:", "corn": "COARSE GRAINS:", "soybean": "OILSEEDS:",
    "rice": "RICE:", "cotton": "COTTON:", "sugar": "SUGAR:",
}


def load(path: Path):
    return [json.loads(l) for l in path.open()]


def main():
    path = ROOT / "output" / "wasde_cpt.jsonl"
    recs = load(path)
    n = len(recs)
    print(f"=== 41_wasde final inspection — {n} records ===\n")

    # ---- 1. structure -------------------------------------------------------
    lens, chans, ts_count = [], [], collections.Counter()
    for r in recs:
        ts = r["timeseries"]
        ts_count[r["text"].count("<ts>")] += 1
        chans.append(len(ts))
        lens.append(len(ts[0]["values"]))
        assert all(len(c["values"]) == lens[-1] for c in ts), r["series_id"]

    print("[structure]")
    print(f"  series length      min {min(lens)} / med {int(st.median(lens))} / max {max(lens)}"
          f"   (all equal: {len(set(lens)) == 1})")
    print(f"  channels/record    min {min(chans)} / med {int(st.median(chans))} / max {max(chans)}")
    print(f"  <ts> per record    {dict(ts_count)}")
    print(f"  datapoints         {sum(l * c for l, c in zip(lens, chans)):,}")
    ids = [r["series_id"] for r in recs]
    dups = [k for k, v in collections.Counter(ids).items() if v > 1]
    print(f"  duplicate series_id {len(dups)}")
    texts = [r["text"] for r in recs]
    print(f"  distinct texts     {len(set(texts))}/{n} ({len(set(texts))/n:.1%})")

    # ---- 2. text belongs to its commodity -----------------------------------
    print("\n[text <-> commodity]")
    bad_head = [r for r in recs
                if not r["text"].lstrip().startswith(PROSE_START[r["meta"]["commodity"]])]
    print(f"  opens with its own narrative heading  {n-len(bad_head)}/{n} "
          f"({(n-len(bad_head))/n:.2%})")
    for r in bad_head[:5]:
        print(f"    ! {r['series_id']}: {r['text'][:70]!r}")

    # bleed: does the block run past its own section into the next commodity's?
    others = {c: [v for k, v in PROSE_START.items() if k != c] for c in PROSE_START}
    bleed = [r for r in recs
             if any(m in r["text"] for m in others[r["meta"]["commodity"]])]
    print(f"  contains another section's heading    {len(bleed)} ({len(bleed)/n:.2%})")

    # ---- 3. structural alignment -------------------------------------------
    print("\n[structural alignment]")
    pe_ok = sum(1 for r in recs if r["period_end"][:7] == r["meta"]["report_month"])
    ps_ok = sum(1 for r in recs if r["period_start"][:7] == r["meta"]["vintage_months"][0])
    win_ok = sum(1 for r in recs if r["meta"]["vintage_months"][-1] == r["meta"]["report_month"])
    len_ok = sum(1 for r in recs if len(r["meta"]["vintage_months"]) == len(r["timeseries"][0]["values"]))
    mono = sum(1 for r in recs if r["meta"]["vintage_months"] == sorted(r["meta"]["vintage_months"]))
    for label, v in [("period_end == report month", pe_ok),
                     ("period_start == window's first month", ps_ok),
                     ("window TERMINAL month == report month", win_ok),
                     ("vintage_months length == series length", len_ok),
                     ("window months strictly chronological", mono)]:
        print(f"  {label:42s} {v}/{n} ({v/n:.2%})")

    # ---- 4. recite claim, with a permutation control ------------------------
    # A record is tagged `recites` when ANY channel endpoint appears in the prose.
    # To know whether that is signal, re-run the same test against a DIFFERENT
    # record's prose (same commodity, so vocabulary and magnitudes match). The gap
    # between the two rates is the real alignment evidence.
    print("\n[recite claim vs permutation control]")
    by_com = collections.defaultdict(list)
    for r in recs:
        by_com[r["meta"]["commodity"]].append(r)

    def anchor_ep(r):
        a = [c for c in r["timeseries"] if "ending_stocks" in c["unit"]]
        return a[0]["values"][-1] if a else None

    def any_ep(r, txt):
        return any(endpoint_recited(txt, c["values"][-1]) for c in r["timeseries"])

    def anch(r, txt):
        e = anchor_ep(r)
        return e is not None and endpoint_recited(txt, e)

    for label, rule in [("any channel (rejected)", any_ep), ("ANCHOR = ending stocks (used)", anch)]:
        rows = []
        for com, rs in sorted(by_com.items()):
            t = c = 0
            for i, r in enumerate(rs):
                if rule(r, r["text"]):
                    t += 1
                # control: same series, a DIFFERENT month's prose for the SAME commodity
                if rule(r, rs[(i + len(rs) // 2) % len(rs)]["text"]):
                    c += 1
            rows.append((com, len(rs), t / len(rs), c / len(rs)))
        tot_t = sum(t * k for _, k, t, _ in rows) / n
        tot_c = sum(c * k for _, k, _, c in rows) / n
        print(f"\n  -- {label}")
        print(f"  {'commodity':10s} {'n':>5s} {'own prose':>10s} {'other prose':>12s} {'lift':>8s}")
        for com, k, t, c in rows:
            print(f"  {com:10s} {k:5d} {t:10.1%} {c:12.1%} {t-c:+8.1%}")
        print(f"  {'ALL':10s} {n:5d} {tot_t:10.1%} {tot_c:12.1%} {tot_t-tot_c:+8.1%}")

    # ---- 5. which channel is actually recited -------------------------------
    print("\n[per-channel recite rate — which line the prose states]")
    per_ch = collections.Counter()
    per_ch_n = collections.Counter()
    for r in recs:
        for c in r["timeseries"]:
            attr = c["unit"].split("_", 1)[1].rsplit("_", 1)[0]
            per_ch_n[attr] += 1
            if endpoint_recited(r["text"], c["values"][-1]):
                per_ch[attr] += 1
    for attr, k in sorted(per_ch_n.items(), key=lambda x: -per_ch[x[0]] / x[1]):
        print(f"  {attr:22s} {per_ch[attr]:5d}/{k:5d}  {per_ch[attr]/k:6.1%}")

    # ---- 6. tier tagging matches the data -----------------------------------
    print("\n[declared alignment tier]")
    tier = collections.Counter(r["alignment"] for r in recs)
    print(f"  {dict(tier)}")
    mism = sum(1 for r in recs if (r["alignment"] == "recites") != anch(r, r["text"]))
    print(f"  tag reproducible from text+series (anchor rule): {n-mism}/{n} ({(n-mism)/n:.2%})")

    # ---- 7. coverage --------------------------------------------------------
    print("\n[coverage]")
    ym = sorted({r["meta"]["report_month"] for r in recs})
    print(f"  report months {len(ym)}  {ym[0]} -> {ym[-1]}")
    fmt = collections.Counter(r["meta"]["source_format"] for r in recs)
    print(f"  source_format {dict(fmt)}")
    print(f"  new-crop resets/record: med {int(st.median([r['meta']['new_crop_resets'] for r in recs]))}")
    tl = [len(r["text"]) for r in recs]
    print(f"  text chars: min {min(tl)} / med {int(st.median(tl))} / max {max(tl)}")


if __name__ == "__main__":
    main()
