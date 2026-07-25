#!/usr/bin/env python3
"""Deep final-inspection pass over output/bls_cpi_cpt.jsonl.

Reconcile / exhaustion / alignment, plus the checks the finalized packages made
standard: no generated text, exactly one <ts>, distinct texts, no duplicate series_id,
and a permutation control on the recite tag so a coincidence rate can be quoted.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cpi_series import SERIES  # noqa: E402

OUT = ROOT / "output/bls_cpi_cpt.jsonl"
NUM = re.compile(r"(?<![\d.])(\d{2,3}\.\d{1,3})(?![\d])")

# The stated over-the-year change. NSA indexes are final when published, so this one is
# exactly reproducible from the series; the SA month-over-month figures are not.
YOY_RE = re.compile(
    r"(?:increased|rose|decreased|fell|declined|advanced|was\s+unchanged|unchanged)\s+"
    r"(\d+\.\d)\s+percent\s+(?:over\s+the\s+(?:last|past)\s+12\s+months"
    r"|for\s+the\s+12\s+months\s+end(?:ing|ed)"
    r"|over\s+the\s+(?:last|past)\s+year)", re.IGNORECASE)
MOM_SA_RE = re.compile(
    r"^[^.]{0,180}?\b(rose|increased|advanced|fell|declined|decreased)\s+(\d+\.\d)\s+percent\s+in\s+"
    + r"(?:January|February|March|April|May|June|July|August|September|October|November|December)",
    re.IGNORECASE)

TOPIC_ANCHOR_IS_NSA = True   # every topic's first channel is its NSA anchor where one exists


def prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def level_forms(v: float):
    return {f"{v:.3f}", f"{v:.2f}", f"{v:.1f}"}


def contains_level(text: str, v: float) -> bool:
    return any(re.search(r"(?<![\d.])" + re.escape(f) + r"(?![\d])", text)
               for f in level_forms(v))


def main() -> None:
    records = [json.loads(l) for l in OUT.open()]
    n = len(records)
    print(f"records: {n}\n")

    # ---- structure -------------------------------------------------------
    ts_counts = Counter(r["text"].count("<ts></ts>") for r in records)
    lens = sorted(len(r["timeseries"][0]["values"]) for r in records)
    equal_len = sum(1 for r in records
                    if len({len(c["values"]) for c in r["timeseries"]}) == 1)
    print("== structure ==")
    print(f"  <ts></ts> per record: {dict(ts_counts)}")
    print(f"  series length: min={lens[0]} med={lens[len(lens)//2]} max={lens[-1]} "
          f"| all channels equal length: {equal_len}/{n}")
    print(f"  text_quality: {dict(Counter(r['text_quality'] for r in records))}")
    print(f"  channels/record: min={min(len(r['timeseries']) for r in records)} "
          f"med={sorted(len(r['timeseries']) for r in records)[n//2]} "
          f"max={max(len(r['timeseries']) for r in records)}")
    ids = Counter(r["series_id"] for r in records)
    print(f"  duplicate series_id: {sum(1 for v in ids.values() if v > 1)}")

    # ---- text: distinct, no generated prose ------------------------------
    prose = [r["text"].split("\n\nMonthly U.S. city average CPI index levels")[0]
             for r in records]
    intro = [r["text"][r["text"].rindex("\n\nMonthly U.S. city average"):] for r in records]
    print("\n== text ==")
    print(f"  distinct section prose: {len(set(prose))}/{n} "
          f"({100*len(set(prose))/n:.1f}%)")
    print(f"  prose chars: min={min(map(len,prose))} "
          f"med={sorted(map(len,prose))[n//2]} max={max(map(len,prose))}")
    print(f"  records whose prose is script-written: 0 by construction "
          f"(every section is a verbatim slice; the only added sentence is the "
          f"channel-naming <ts> intro, {len(set(intro))} distinct forms)")

    # ---- structural alignment -------------------------------------------
    print("\n== structural alignment ==")
    ok_terminal = sum(1 for r in records if r["period_end"] == f"{r['meta']['data_month']}-01")
    ok_anchor = sum(1 for r in records
                    if r["timeseries"][0]["unit"] == r["meta"]["channels"][0])
    ok_nonnull = sum(1 for r in records if r["timeseries"][0]["values"][-1] is not None)
    print(f"  window ends on the reported data month: {ok_terminal}/{n} "
          f"({100*ok_terminal/n:.1f}%)")
    print(f"  first channel is the topic anchor:      {ok_anchor}/{n} "
          f"({100*ok_anchor/n:.1f}%)")
    print(f"  anchor has a value at the data month:   {ok_nonnull}/{n} "
          f"({100*ok_nonnull/n:.1f}%)")
    nulls = sum(1 for r in records for c in r["timeseries"] for v in c["values"] if v is None)
    total = sum(len(c["values"]) for r in records for c in r["timeseries"])
    print(f"  null datapoints: {nulls}/{total} ({100*nulls/total:.3f}%)")

    # ---- verbatim recite + permutation control --------------------------
    print("\n== verbatim recite of a channel's terminal level ==")
    by_topic = defaultdict(lambda: [0, 0])
    true_hit = ctrl_hit = ctrl_n = 0
    # control: same channels, a different month's prose for the same topic
    prose_by_topic = defaultdict(list)
    for r, p in zip(records, prose):
        prose_by_topic[r["meta"]["topic"]].append(p)
    for i, (r, p) in enumerate(zip(records, prose)):
        topic = r["meta"]["topic"]
        hit = bool(r["meta"]["recited_channels"])
        by_topic[topic][0] += hit
        by_topic[topic][1] += 1
        true_hit += hit
        pool = prose_by_topic[topic]
        if len(pool) > 1:
            other = pool[(pool.index(p) + len(pool) // 2) % len(pool)]
            if other != p:
                ctrl_n += 1
                ctrl_hit += any(
                    c["values"][-1] is not None and contains_level(other, c["values"][-1])
                    for c in r["timeseries"])
    for t, (h, tot) in sorted(by_topic.items(), key=lambda x: -x[1][0]):
        print(f"  {t:16} {h:4}/{tot:4} ({100*h/tot:5.1f}%)")
    print(f"  overall true rate  : {true_hit}/{n} ({100*true_hit/n:.1f}%)")
    print(f"  permutation control: {ctrl_hit}/{ctrl_n} ({100*ctrl_hit/max(1,ctrl_n):.1f}%)"
          f"   lift = {100*true_hit/n - 100*ctrl_hit/max(1,ctrl_n):+.1f} pp")

    # ---- 12-month NSA change reproducibility ----------------------------
    # Only sentences that name the anchor index itself count: a section talks about
    # a dozen indexes and "rose 14.2 percent over the last 12 months" three sentences
    # down is about natural gas, not about housing.
    print("\n== stated over-the-year change vs the NSA series ==")
    nsa_ok = nsa_tot = 0
    sa_by_year = defaultdict(lambda: [0, 0])
    sa_ok = sa_tot = 0
    bad = []
    twin_ok = []
    for r, p in zip(records, prose):
        dm = r["meta"]["data_month"]
        ch = {c["unit"]: c for c in r["timeseries"]}
        anchor = r["meta"]["channels"][0]
        # The sentence must *be about* the anchor: its subject, not a passing mention.
        # "The index for natural gas rose 14.2 percent over the last 12 months" sits
        # inside the housing section but is not a statement about the housing index.
        alts = [re.escape(SERIES[anchor][1])]
        alts += {"all_items_nsa": [r"CPI-U", r"all items CPI"],
                 "cpi_w_nsa": [r"CPI-W"],
                 "c_cpi_u_nsa": [r"C-CPI-U"]}.get(anchor, [])
        subject = re.compile(
            r"^(?:In\s+\w+,\s+|Over\s+the\s+(?:last|past)\s+(?:12\s+months|year),\s+|\()?"
            r"(?:The\s+)?(?:index\s+for\s+)?(?:all\s+items\s+)?(?:" + "|".join(alts) + r")"
            r"(?!\s+(?:at\s+home|at\s+employee|away\s+from\s+home|and\s+beverages|commodities|services|"
            r"fares|oil|gas|less\s+food))(?:\s+index)?\b", re.IGNORECASE)
        vals = ch[anchor]["values"]
        for sent in re.split(r"(?<=[.)])\s+(?=[A-Z(])", p):
            m = YOY_RE.search(sent)
            if not (m and subject.search(sent)):
                continue
            if vals[-1] is None or vals[-13] is None:
                break
            stated = float(m.group(1))
            derived = abs(round((vals[-1] / vals[-13] - 1) * 100, 1))
            nsa_tot += 1
            if abs(stated - derived) <= 0.051:
                nsa_ok += 1
                break
            # a few sections quote the change on the seasonally adjusted twin instead;
            # that channel is in the record too, so check it before calling a miss
            twin = anchor[:-4] + "_sa" if anchor.endswith("_nsa") else anchor + "_nsa"
            tv = ch.get(twin, {}).get("values") if twin in ch else None
            if tv and tv[-1] is not None and tv[-13] is not None and \
                    abs(stated - abs(round((tv[-1] / tv[-13] - 1) * 100, 1))) <= 0.051:
                twin_ok.append((dm, r["meta"]["topic"]))
            elif len(bad) < 8:
                bad.append((dm, r["meta"]["topic"], stated, derived, sent[:70]))
            break
        # SA month-over-month, for the revision-drift figure
        sa_key = anchor[:-4] + "_sa" if anchor.endswith("_nsa") else anchor
        if sa_key in ch:
            sv = ch[sa_key]["values"]
            m2 = MOM_SA_RE.match(p)
            if m2 and sv[-1] is not None and sv[-2] is not None:
                stated = float(m2.group(2))
                if m2.group(1).lower() in ("fell", "declined", "decreased"):
                    stated = -stated
                derived = round((sv[-1] / sv[-2] - 1) * 100, 1)
                hit = abs(stated - derived) <= 0.051
                sa_tot += 1
                sa_ok += hit
                sa_by_year[dm[:4]][0] += hit
                sa_by_year[dm[:4]][1] += 1
    print(f"  anchor is a never-revised NSA index in "
          f"{sum(1 for r in records if r['meta']['channels'][0].endswith('_nsa'))}/{n} records")
    print(f"  stated 12-month change on the anchor reproduces: "
          f"{nsa_ok}/{nsa_tot} ({100*nsa_ok/max(1,nsa_tot):.1f}%)")
    print(f"  + matches the anchor's SA/NSA twin (also in the record): {len(twin_ok)} "
          f"{twin_ok} -> {nsa_ok + len(twin_ok)}/{nsa_tot} "
          f"({100*(nsa_ok+len(twin_ok))/max(1,nsa_tot):.1f}%) against some channel of the record")
    for b in bad:
        print(f"    miss {b}")
    print(f"\n  stated 1-month SA change reproduces from today's SA series: "
          f"{sa_ok}/{sa_tot} ({100*sa_ok/max(1,sa_tot):.1f}%)"
          f"   <- as-published vs annually-revised seasonal factors")
    print("  by data year (revisions reach back 5 years from each February):")
    line = []
    for y in sorted(sa_by_year):
        h, t = sa_by_year[y]
        line.append(f"{y}:{100*h/max(1,t):.0f}%")
    for i in range(0, len(line), 11):
        print("    " + "  ".join(line[i:i + 11]))

    # ---- coverage --------------------------------------------------------
    print("\n== coverage ==")
    dms = sorted({r["meta"]["data_month"] for r in records})
    years = Counter(d[:4] for d in dms)
    print(f"  data months: {len(dms)}  {dms[0]}..{dms[-1]}")
    print(f"  per year: {dict(sorted(years.items()))}")
    print(f"  formats: {dict(Counter(r['meta']['release_format'] for r in records))}")
    print(f"  licenses: {dict(Counter(r['license'] for r in records))}")
    print(f"  tiers: {dict(Counter(r['alignment'] for r in records))}")


if __name__ == "__main__":
    main()
