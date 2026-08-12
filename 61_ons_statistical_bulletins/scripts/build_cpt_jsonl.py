#!/usr/bin/env python3
"""Build CPT world-knowledge records from UK ONS statistical bulletins, at full scale.

One record = ONE CONTIGUOUS RUN OF PARAGRAPHS from one analytical section of one bulletin
edition: that run's own VERBATIM prose paired with the trailing multi-channel window of the
indicator series the bulletin family reports.

Three design decisions carry the scale and the quality:

1. SPLIT, DON'T CUT. A bulletin runs ~44,000 chars (~11,000 tokens), 22x the 500-token cap.
   The demo kept only the densest run per section and discarded ~70% of real first-party prose.
   A cap should split the source, not truncate it: the discarded remainder is exactly as real
   and as quotable as the part kept, and truncating a `recites` record orphans the numbers that
   fall after the cut. Sections are chunked into consecutive whole-paragraph runs and every
   chunk carrying figures becomes its own record. Text stays 100% VERBATIM.

2. CHANNELS ARE DISCOVERED, NOT DECLARED. `discover_channels.py` derives each family's channel
   set from the prose's own figures and proves it against a month-shifted control. The demo's
   hand-wired CDIDs shipped the wrong variant for retailsales (j4mc read 28.3% where the
   bulletin quoted 29.4%) and could never have covered the 495 families ONS publishes.

3. THE PERIOD COMES FROM THE DOCUMENT. Edition slugs come in 14 shapes and a slug is not a
   date. Each edition's reference period is read from its own h1 and cross-checked against the
   slug; disagreements are recorded in meta, not silently trusted.

Workers write JSONL shards to disk and return only counts. Returning records through process
IPC would move gigabytes of record dicts for a full build; shards also mean a crashed worker
costs one family, not the run.

Usage:
    python scripts/build_cpt_jsonl.py --dry-run
    python scripts/build_cpt_jsonl.py --set output.max_records_per_family=20   # smoke
    python scripts/build_cpt_jsonl.py                                          # full build
"""
from __future__ import annotations

import argparse
import bisect
import collections
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PKG.parent / "schema"))
import onslib as L                                                        # noqa: E402
from emit import emit_record                                             # noqa: E402
from onsfetch import fetch, stats                                        # noqa: E402

LICENSE_TAG = "cc-by-4.0"
TRUE_LICENSE = ("Open Government Licence v3.0 (OGL v3) -- ONS states OGL v3 is interoperable "
                "with CC BY 4.0; attribution required. Tagged cc-by-4.0 as closest schema fit.")
ATTRIBUTION = ("Source: Office for National Statistics licensed under the Open Government "
               "Licence v.3.0")
VINTAGE_CAVEAT = ("Series are the CURRENT ONS vintage; the bulletin quotes its contemporaneous "
                  "vintage. ONS revises, so an older claim can drift from live data -- the "
                  "measured effect on evidence yield is in README 'Vintage'.")

SUPERLATIVE_RE = re.compile(
    r"(?i)\b(highest|lowest|record high|record low|strongest|weakest|largest|smallest)\b"
    r"[^.]{0,80}?\b(on record|since records began|ever)\b")
SCOPE_QUALIFIER = re.compile(
    r"(?i)\b(national statistic|comparable series|consistent series|on a comparable basis|"
    r"records began in \d{4})\b")
BOUNDED_SUP_RE = re.compile(
    rf"(?i)\b(highest|lowest)\b[^.;]{{0,60}}?\bsince\s+({L.MONTH_ALT})\s+(\d{{4}})"
    r"(?:[^.;]{0,30}?\bwhen it was\s+(-?\d+\.\d+)\s*%)?")


# ============================ evidence =======================================================

def verify_chunk(text: str, ref: str, chans: list[dict]) -> tuple[str, list, int, dict]:
    """Attribute each TYPED figure in `text` to a channel, conservatively.

    A figure becomes evidence only if ALL of these hold:
      1. the clause NAMES the series -- measure token exact, every restriction named, one whole
         concept segment named (`onslib.names_series`);
      2. the channel's value at the month THE CLAUSE ITSELF names rounds to the figure, at the
         precision the figure was quoted to;
      3. no second guess: the first channel to satisfy both wins and the search stops.
    Channels arrive sorted by discovery specificity, so the most precisely-named series is
    tried first and a headline channel cannot pre-empt a sub-series that the clause names more
    exactly. A figure matching nothing is left unattributed and counted: being wrong here is
    worse than being silent, because a polluted evidence array inflates apparent quality.
    """
    ev = []
    rej = {"not_named": 0, "value_mismatch": 0, "no_data_at_month": 0,
           "ambiguous_multi_channel": 0}
    n_typed = 0
    for _off, clause in L.split_clauses(text):
        cl = [c for c in L.claims(clause) if c[1] in L.TYPED]
        if not cl:
            continue
        low, ctoks = clause.lower(), L.clause_tokens(clause)
        n_typed += len(cl)
        for i, (v, kind, scale, numtext) in enumerate(cl):
            targets = L.clause_target_months(clause, ref, i, len(cl))
            named_any = had_data = False
            cands = []
            for ch in chans:
                if not L.unit_compatible(kind, ch.get("ons_unit", "")):
                    continue
                ok, spec = L.names_series(ch["_pt"], ctoks, low)
                if not ok:
                    continue
                named_any = True
                for label, key in targets:
                    got = ch["_series"].get(key)
                    if got is None:
                        continue
                    had_data = True
                    matched = False
                    for lo, hi, us in L.probes_for(v, kind, scale, numtext):
                        if (kind in ("money", "count") and ch["_scale"] is not None
                                and ch["_scale"] != us):
                            continue
                        if lo <= got <= hi:
                            matched = True
                            break
                    if matched:
                        cands.append({"figure": v, "kind": kind, "unit": ch["unit"],
                                      "cdid": ch["cdid"], "month": key, "month_source": label,
                                      "series_value": got, "specificity": spec,
                                      "clause": re.sub(r"\s+", " ", clause).strip()[:220]})
                        break
            # Identifiability, the same rule discovery uses: a figure that fits several channels
            # at once identifies none of them, so it is recorded as ambiguous rather than credited
            # to whichever happened to sort first. Sorting by specificity makes the choice
            # deterministic, not correct.
            uniq = {c["cdid"]: c for c in cands}
            if len(uniq) == 1:
                ev.append(next(iter(uniq.values())))
            elif len(uniq) > 1:
                if len(uniq) <= L.MAX_MULTIPLICITY:
                    best = max(uniq.values(), key=lambda c: c["specificity"])
                    tie = [c for c in uniq.values()
                           if c["specificity"] == best["specificity"]]
                    if len(tie) == 1:
                        best["ambiguous_with"] = sorted(c["unit"] for c in uniq.values()
                                                        if c["cdid"] != best["cdid"])
                        ev.append(best)
                    else:
                        rej["ambiguous_multi_channel"] += 1
                else:
                    rej["ambiguous_multi_channel"] += 1
            elif not named_any:
                rej["not_named"] += 1
            elif not had_data:
                rej["no_data_at_month"] += 1
            else:
                rej["value_mismatch"] += 1
    return ("recites" if ev else "describes"), ev, n_typed, rej


def _enclosing(text: str, idx: int) -> str:
    """The paragraph containing idx -- a superlative's subject is set in an earlier sentence."""
    a = text.rfind("\n\n", 0, idx)
    a = 0 if a < 0 else a + 2
    b = text.find("\n\n", idx)
    return text[a:(len(text) if b < 0 else b)]


def _clause_names(text: str, idx: int, ch: dict, chans: list[dict]) -> bool:
    """Does the claim's OWN clause name this channel, and only this channel?"""
    own = ""
    for off, cl in L.split_clauses(text):
        if off <= idx:
            own = cl
        else:
            break
    if not own:
        return False
    low, ctoks = own.lower(), L.clause_tokens(own)
    named = [c for c in chans if L.names_series(c["_pt"], ctoks, low)[0]]
    return len(named) == 1 and named[0]["cdid"] == ch["cdid"]


def superlative_checks(text: str, cur_month: str, chans: list[dict]) -> tuple[list, bool]:
    """Check "highest/lowest since <Month Year>" (and "on record") against the held history.

    ONS makes BOUNDED superlatives far more often than unbounded ones ("the lowest since August
    2024, when it was 1.3%"), and a bounded claim is fully checkable against 450 months of held
    history -- including the back-reference value. Attribution uses the same naming rule as the
    evidence pass, at paragraph scope because the subject is usually established in an earlier
    sentence; two candidate channels means the claim is reported as ambiguous and never acted
    on. `cur_month` is the window anchor, i.e. the latest month actually held.
    """
    flags, contradicted = [], False
    for m in list(BOUNDED_SUP_RE.finditer(text)) + list(SUPERLATIVE_RE.finditer(text)):
        bounded = m.re is BOUNDED_SUP_RE
        claim = re.sub(r"\s+", " ", text[max(0, m.start() - 80):m.end() + 60]).strip()
        para = _enclosing(text, m.start())
        low, ctoks = para.lower(), L.clause_tokens(para)
        cands = [c for c in chans if L.names_series(c["_pt"], ctoks, low)[0]]
        rec = {"claim": claim, "bounded": bounded}
        if not cands:
            rec["verdict"] = "unchecked_no_named_channel"
            flags.append(rec)
            continue
        if len(cands) > 1:
            rec.update(verdict="ambiguous_multi_channel",
                       candidates=[c["unit"] for c in cands][:6])
            flags.append(rec)
            continue
        ch = cands[0]
        hist = ch["_series"]
        cur = hist.get(cur_month)
        word = m.group(1).lower()
        is_high = word.startswith(("high", "record h", "strong", "larg"))
        # The claim may scope its own comparison to a narrower series than the one we hold.
        # "the largest ever recorded increase in the CPI NATIONAL STATISTIC 12-month rate" is
        # true of the National Statistic series (from 1997) and false of the 449-month history
        # including the earlier modelled estimates. We cannot represent that scope, so we cannot
        # judge it -- and saying "contradicted" would be an accusation the data does not support.
        if SCOPE_QUALIFIER.search(_enclosing(text, m.start())):
            rec.update(verdict="unchecked_scope_qualifier", unit=ch["unit"],
                       note="the claim scopes its comparison to a narrower series than the one "
                            "held (e.g. 'National Statistic'), so it is not checkable here")
            flags.append(rec)
            continue
        if cur is None:
            rec.update(verdict="unchecked_no_data", unit=ch["unit"])
            flags.append(rec)
            continue
        if bounded:
            since = f"{int(m.group(3)):04d}-{L.MONTHS.index(m.group(2).lower()):02d}"
            span = {k: v for k, v in hist.items() if since < k <= cur_month}
            if not span:
                rec.update(verdict="unchecked_no_data", unit=ch["unit"], since=since)
                flags.append(rec)
                continue
            extremum = max(span.values()) if is_high else min(span.values())
            ok = (cur >= extremum - 1e-9) if is_high else (cur <= extremum + 1e-9)
            rec.update(unit=ch["unit"], since=since, current=cur,
                       extremum_over_span=extremum, n_months_in_span=len(span),
                       verdict="consistent" if ok else "contradicted")
            if m.group(4):
                q, at = float(m.group(4)), hist.get(since)
                rec.update(quoted_at_since=q, series_at_since=at,
                           back_reference=("match" if at is not None and abs(at - q) < 0.05
                                           else "mismatch"))
        else:
            # "the largest ever recorded INCREASE in the CPI 12-month inflation rate" is a claim
            # about the month-on-month CHANGE, not the level. Comparing it against the level's
            # own max produced all 4 surviving contradictions in the second full build -- the
            # source was right and the check was asking the wrong question. When the superlative
            # is qualified by a change word, test the first difference instead.
            about_change = re.search(r"(?i)\b(increase|rise|fall|decrease|change|jump|drop)\b",
                                     text[max(0, m.start() - 60):m.end() + 40]) is not None
            ks = sorted(hist)
            if about_change and len(ks) > 1:
                diffs = {ks[i]: hist[ks[i]] - hist[ks[i - 1]] for i in range(1, len(ks))}
                val = diffs.get(cur_month)
                if val is None:
                    rec.update(verdict="unchecked_no_data", unit=ch["unit"], basis="change")
                    flags.append(rec)
                    continue
                hi, lo = max(diffs.values()), min(diffs.values())
                ok = (val >= hi - 1e-9) if is_high else (val <= lo + 1e-9)
                rec.update(unit=ch["unit"], basis="month_on_month_change", current_change=val,
                           change_max=round(hi, 4), change_min=round(lo, 4),
                           n_months=len(diffs), verdict="consistent" if ok else "contradicted")
            else:
                hi, lo = max(hist.values()), min(hist.values())
                ok = (cur >= hi - 1e-9) if is_high else (cur <= lo + 1e-9)
                rec.update(unit=ch["unit"], basis="level", current=cur, series_max=hi,
                           series_min=lo, n_months=len(hist),
                           verdict="consistent" if ok else "contradicted")
        # A contradiction is only a contradiction if the attribution holds. Two independent ways
        # to show it does not -- both of which fired on the smoke build, where all 4 apparent
        # contradictions turned out to be mis-attributions:
        #   * the prose states the value at the earlier month and the series disagrees there
        #     ("lowest since October 2021, when it was 3.1%" against a series reading 3.8), so
        #     the claim is demonstrably about a series we do not hold;
        #   * the claim's own CLAUSE does not unambiguously name the channel, only its paragraph
        #     does. That is enough to flag but never enough to accuse the source, because ONS
        #     references far more series than any channel set holds.
        if rec.get("verdict") == "contradicted":
            if rec.get("back_reference") == "mismatch":
                rec["verdict"] = "attribution_rejected_back_reference"
                rec["note"] = ("the prose's own back-reference value does not match this series "
                               "at that month, so the claim is about a series we do not hold")
            elif not _clause_names(text, m.start(), ch, chans):
                rec["verdict"] = "contradicted_weak_attribution"
                rec["note"] = ("attributed only at paragraph scope -- the claim's own clause "
                               "does not unambiguously name this channel, so this is reported, "
                               "not acted on")
        if rec["verdict"] == "contradicted":
            contradicted = True
        flags.append(rec)
    return flags, contradicted


# ============================ per-family build ================================================

def load_channels(fam_rep: dict) -> list[dict]:
    """Attach live series, parsed titles and sorted month keys to the discovered channels."""
    datasets = {i: L.load_dataset(p, i) for p, i in fam_rep.get("datasets", [])}
    out = []
    for ch in fam_rep.get("channels", []):
        s = next((d[ch["cdid"]] for d in datasets.values() if ch["cdid"] in d), None)
        if not s or not s["m"]:
            continue
        c = dict(ch)
        c["_series"] = s["m"]
        c["_keys"] = sorted(s["m"])
        # The requirement frozen by discovery, which was calibrated against this
        # family's own prose vocabulary. Falling back to a fresh parse would re-introduce the
        # notation tokens (CPNSA, CVM, Liabs) that discovery proved unmatchable.
        c["_pt"] = ch.get("require") or L.parse_title(s["title"])
        c["_scale"] = L.unit_scale(s["unit"])
        out.append(c)
    # most precisely-named channels first, so a headline series cannot pre-empt a sub-series
    out.sort(key=lambda c: (-int(c.get("spec") or 0), -int(c.get("claims") or 0)))
    return out


def anchor_for(chans: list[dict], ref: str) -> tuple[str | None, str]:
    """Where the window ENDS.

    Not always `ref`. A labour-market bulletin published for July reports rolling quarters that
    end in May, so no July point exists yet; anchoring blindly at `ref` made the window empty
    and silently dropped the entire family. So back off to the latest month <= ref at which a
    majority of channels hold data, and record which rule fired. Uses each channel's
    pre-sorted key list -- scanning full histories per edition is 52M dict operations on a full
    build.
    """
    latest = []
    for c in chans:
        ks = c["_keys"]
        i = bisect.bisect_right(ks, ref) - 1
        if i >= 0:
            latest.append(ks[i])
    if not latest:
        return None, "no_data_before_ref"
    latest.sort(reverse=True)
    need = max(1, len(chans) // 2)
    best = latest[min(need, len(latest)) - 1]      # latest month >= need channels reach
    return best, ("ref" if best == ref else "backoff")


def build_family(fam_rep: dict, editions: list, cfg: dict, shard: Path) -> dict:
    tcfg, scfg, ocfg = cfg["text"], cfg["series"], cfg["output"]
    st = collections.Counter()
    fam, path = fam_rep["family"], fam_rep["path"]
    chans = load_channels(fam_rep)
    if not chans:
        return {"family": fam, "records": 0, "counters": {"family_no_channels": 1}}
    excl = set(tcfg["exclude_anchors"])
    cap, min_chars = int(tcfg["max_chars"]), int(tcfg["min_chars"])
    min_figs = int(tcfg["min_figures"])
    wmonths, minpts = int(scfg["window_months"]), int(scfg["min_points"])
    sim_cap = tcfg.get("max_similarity")
    per_fam_cap = ocfg.get("max_records_per_family")
    seen_exact, seen_group = set(), collections.defaultdict(list)
    n = 0
    ev_total = 0
    n_recites = 0
    sims = []
    # Evidence yield bucketed by the EDITION's own year. This measures the vintage question
    # directly and for free: series come from the current vintage, so if that were drifting away
    # from what older bulletins quoted, yield would fall off with edition age. Measuring the
    # effect beats measuring the cause -- comparing vintage CSVs costs 23MB per vintage per
    # family, and on CPI vintage v70 turned out identical to current in 0 of 1,172 points.
    by_year = collections.defaultdict(lambda: {"records": 0, "evidence": 0, "typed": 0})
    fh = shard.open("w")
    try:
        for row in editions:
            slug = row[0]
            url = f"https://www.ons.gov.uk/{path}/bulletins/{fam}/{slug}"
            code, page = fetch(url)
            if code != 200:
                st[f"edition_http_{code}"] += 1
                continue
            title = L.page_title(page)
            ref, shape = L.coverage_period(title)
            if not ref:
                st["no_reference_period"] += 1
                continue
            if "-Q" in ref:
                st["quarterly_period_skipped"] += 1
                continue
            slug_ref = _slug_month(slug)
            # Three-valued on purpose. 7 CPI editions are slugged with their PUBLICATION date
            # (`2015-07-14`) while covering the month before (2015-06) -- the FHFA #59 trap, live
            # in this source. Those slugs encode no reference month at all, which is a different
            # statement from "the slug disagrees", and collapsing the two would report a source
            # inconsistency that does not exist.
            st["slug_encodes_no_month" if slug_ref is None else
               "slug_matches_dateline" if slug_ref == ref else
               "slug_differs_from_dateline"] += 1
            st["editions_parsed"] += 1

            anch, how = anchor_for(chans, ref)
            if not anch:
                st["no_window_anchor"] += 1
                continue
            st[f"anchor_{how}"] += 1
            wins, keys = [], None
            for c in chans:
                vals, ks = L.window(c["_series"], anch, wmonths, "m")
                if sum(v is not None for v in vals) < minpts:
                    continue
                wins.append((c, vals))
                keys = ks
            if not wins:
                st["no_windowed_channels"] += 1
                continue
            live = [c for c, _ in wins]

            for sec in L.parse_sections(page):
                if sec["anchor"] in excl:
                    st["section_excluded_boilerplate"] += 1
                    continue
                paras = L.clean_paragraphs(sec["text"])
                if not paras:
                    st["section_no_prose"] += 1
                    continue
                # A paragraph longer than the cap is divided at sentence bounds, not truncated,
                # so it cannot push a record over the 500-token cap and no sentence is lost.
                split = []
                for p in paras:
                    parts = L.split_long_paragraph(p, cap)
                    if len(parts) > 1:
                        st["long_paragraph_split"] += 1
                    split.extend(parts)
                paras = split
                spans = L.chunk_paragraphs(paras, cap)
                st["chunks_seen"] += len(spans)
                for ci, (lo, hi) in enumerate(spans):
                    span = "\n\n".join(paras[lo:hi])
                    if len(span) < min_chars:
                        st["chunk_too_short"] += 1
                        continue
                    if sum(1 for _o, c2 in L.split_clauses(span)
                           for c3 in L.claims(c2) if c3[1] in L.TYPED) < min_figs:
                        st["chunk_too_few_figures"] += 1
                        continue
                    h = hashlib.sha1(span.encode()).hexdigest()
                    if h in seen_exact:
                        st["exact_duplicate_dropped"] += 1
                        continue
                    if sim_cap is not None:
                        grp = seen_group[(sec["anchor"], ci)]
                        worst = max((difflib.SequenceMatcher(None, span, p).ratio()
                                     for p in grp), default=0.0)
                        if grp:
                            sims.append(round(worst, 3))
                        if worst > float(sim_cap):
                            st["near_duplicate_dropped"] += 1
                            continue
                        grp.append(span)
                        del grp[:-10]         # template reuse is month-to-month adjacent
                    seen_exact.add(h)

                    align, ev, n_typed, rej = verify_chunk(span, ref, live)
                    for k, v in rej.items():
                        st[f"rejected_{k}"] += v
                    flags, contra = superlative_checks(span, anch, live)
                    st["superlative_flags"] += len(flags)
                    st["superlative_contradicted"] += sum(
                        1 for f in flags if f.get("verdict") == "contradicted")
                    if contra and tcfg.get("drop_on_superlative_contradiction"):
                        st["superlative_dropped"] += 1
                        continue
                    rec = emit_record(
                        text=f"{span}\n\n<ts></ts>",
                        timeseries=[{"values": [None if v is None else round(v, 4) for v in vals],
                                     "unit": c["unit"], "freq": scfg["freq"]}
                                    for c, vals in wins],
                        timestamps=keys,
                        alignment=align,
                        license=LICENSE_TAG,
                        text_source="first_party_official",
                        source=url,
                        dataset="ons_statistical_bulletins",
                        series_id=f"ons:{fam}:{slug}:{sec['anchor']}:{ci}",
                        domain="macro",
                        region="GB",
                        period_start=keys[0],
                        period_end=keys[-1],
                        meta={
                            "true_license": TRUE_LICENSE,
                            "attribution": ATTRIBUTION,
                            "bulletin_family": fam,
                            "bulletin_title": title,
                            "edition_slug": slug,
                            "reference_period": ref,
                            "reference_period_source": "document_h1",
                            "reference_period_shape": shape,
                            "slug_derived_month": slug_ref,
                            "slug_agrees_with_dateline": (None if slug_ref is None
                                                          else slug_ref == ref),
                            "window_anchor": anch,
                            "window_anchor_rule": how,
                            "section_anchor": sec["anchor"],
                            "section_title": sec["title"],
                            "chunk_index": ci,
                            "n_chunks_in_section": len(spans),
                            "section_paragraphs": len(paras),
                            "chunk_paragraphs": hi - lo,
                            "n_channels": len(wins),
                            "n_points": len(keys),
                            "n_nulls": sum(1 for _c, vs in wins for v in vs if v is None),
                            "n_typed_figures": n_typed,
                            "n_evidenced": len(ev),
                            "evidenced_channels": sorted({e["unit"] for e in ev}),
                            "recite_evidence": ev,
                            "evidence_rejected": rej,
                            "superlative_flags": flags,
                            "channels": [{"cdid": c["cdid"], "unit": c["unit"],
                                          "ons_title": c.get("title", ""),
                                          "ons_unit": c.get("ons_unit", ""),
                                          "discovery_claims": c.get("claims"),
                                          "discovery_control": c.get("control")}
                                         for c, _ in wins],
                            "channel_discovery": {
                                "method": "value_verified_from_prose",
                                "family_floor": fam_rep.get("floor"),
                                "n_candidate_series": fam_rep.get("n_candidate_series"),
                            },
                            "vintage": {"series_vintage": "current", "caveat": VINTAGE_CAVEAT},
                        },
                    )
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    n += 1
                    ev_total += len(ev)
                    n_recites += 1 if align == "recites" else 0
                    st["recites" if align == "recites" else "describes"] += 1
                    yb = by_year[ref[:4]]
                    yb["records"] += 1
                    yb["evidence"] += len(ev)
                    yb["typed"] += n_typed
                    if per_fam_cap and n >= int(per_fam_cap):
                        raise _CapReached
    except _CapReached:
        st["per_family_cap_hit"] += 1
    finally:
        fh.close()
    return {"family": fam, "records": n, "recites": n_recites, "evidence_claims": ev_total,
            "evidence_per_record": round(ev_total / max(1, n), 2),
            "editions_parsed": st.get("editions_parsed", 0),
            "similarity_p95": (sorted(sims)[int(len(sims) * 0.95)] if sims else None),
            "similarity_max": (max(sims) if sims else None),
            "evidence_by_edition_year": {
                y: {**d, "evidence_per_record": round(d["evidence"] / max(1, d["records"]), 2)}
                for y, d in sorted(by_year.items())},
            "counters": dict(st), "shard": str(shard)}


class _CapReached(Exception):
    pass


def _slug_month(slug: str) -> str | None:
    """Month implied by the slug, or None if the slug does not encode one.

    Handles the abbreviated form too (`aug2017`, `apr2017`): without it, 97 records reported
    `slug_agrees_with_dateline: false` when the slug and the dateline in fact agreed and only
    the parser could not read it. Claiming a disagreement that is not there is worse than
    reporting "could not tell", because it looks like evidence the source is inconsistent.
    """
    m = re.search(rf"({L.MONTH_ALT})(\d{{4}})$", slug)
    if m:
        return f"{int(m.group(2)):04d}-{L.MONTHS.index(m.group(1)):02d}"
    m = re.search(r"([a-z]{3})(\d{4})$", slug)
    if m and m.group(1).upper() in L.ABBR:
        return f"{int(m.group(2)):04d}-{L.ABBR[m.group(1).upper()]:02d}"
    return None


def _job(args):
    rep, eds, cfg, shard = args
    try:
        return build_family(rep, eds, cfg, Path(shard))
    except Exception as e:
        return {"family": rep["family"], "records": 0, "counters": {},
                "error": f"{type(e).__name__}: {e}"}


def deep_set(d: dict, dotted: str, raw: str) -> None:
    cur = d
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = yaml.safe_load(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(PKG / "config.example.yaml"))
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--family", action="append")
    ap.add_argument("--limit-editions", type=int, default=0, help="smoke test: newest N only")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 6))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    for ov in args.set:
        k, _, v = ov.partition("=")
        deep_set(cfg, k.strip(), v.strip())
    if cfg["text"].get("abstractive_summary"):
        print("ERROR: text.abstractive_summary is not shippable -- schema/validate.py allows\n"
              "  text_quality in {'real','generated'} only; there is no `llm_summarized` value,\n"
              "  and 'generated' would conflate a grounded summary of a real document with\n"
              "  fully synthetic text. Get the schema tag signed off first (README).",
              file=sys.stderr)
        return 2

    # The corpus runner (cpt_corpus/run_full.py) drives every package with the same three
    # overrides -- output.max_records / output.output_path / output.report_path -- so honour
    # those names as aliases rather than making this package the one that needs a special case.
    ocfg = cfg["output"]
    if ocfg.get("output_path"):
        ocfg["path"] = ocfg["output_path"]
    if ocfg.get("report_path"):
        ocfg["run_report"] = ocfg["report_path"]
    global_cap = ocfg.get("max_records")

    def _p(key):
        v = cfg["data"][key]
        return Path(v) if os.path.isabs(v) else PKG / v

    census = json.load(open(_p("census")))
    chans = json.load(open(_p("channels")))
    ok = {r["family"]: r for r in chans if r.get("status") == "ok"}
    shard_dir = PKG / "output" / "shards"
    if shard_dir.exists():
        shutil.rmtree(shard_dir)
    shard_dir.mkdir(parents=True, exist_ok=True)

    jobs = []
    for key, eds in sorted(census.items(), key=lambda kv: -len(kv[1])):
        path, fam = key.split("||")
        if fam not in ok or (args.family and fam not in args.family):
            continue
        if args.limit_editions:
            eds = eds[:args.limit_editions]
        jobs.append((ok[fam], eds, cfg, str(shard_dir / f"{fam}.jsonl")))
    print(f"building {len(jobs)} families ({sum(len(j[1]) for j in jobs)} editions) "
          f"with {args.workers} workers")

    t0 = time.time()
    results = []
    if args.workers <= 1 or len(jobs) == 1:
        for j in jobs:
            results.append(_job(j))
            print(f"  {results[-1]['family'][:44]:<44} recs={results[-1]['records']:>5}", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_job, j): j[0]["family"] for j in jobs}
            for i, fu in enumerate(as_completed(futs), 1):
                r = fu.result()
                results.append(r)
                print(f"[{i}/{len(jobs)}] {r['family'][:42]:<42} recs={r['records']:>5} "
                      f"ev/rec={r.get('evidence_per_record')} "
                      f"{'ERR ' + r['error'] if r.get('error') else ''}", flush=True)

    agg = collections.Counter()
    per_fam, errs = {}, {}
    for r in results:
        agg.update(r.get("counters") or {})
        if r.get("error"):
            errs[r["family"]] = r["error"]
        per_fam[r["family"]] = {k: v for k, v in r.items()
                                if k not in ("counters", "shard", "family", "error")}

    gate = float(cfg["output"].get("min_evidence_per_record", 1.0))
    keep, dropped = [], {}
    for r in results:
        if r["records"] and r.get("evidence_per_record", 0) >= gate:
            keep.append(r)
        elif r["records"]:
            dropped[r["family"]] = r.get("evidence_per_record")

    n_kept = sum(r["records"] for r in keep)
    st = {
        "families_built": len([r for r in results if r["records"]]),
        "families_shipped": len(keep),
        "families_below_gate": dropped,
        "records_before_gate": sum(r["records"] for r in results),
        "records_shipped": n_kept,
        "recites": sum(r.get("recites", 0) for r in keep),
        "evidence_claims": sum(r.get("evidence_claims", 0) for r in keep),
        "editions_parsed": sum(r.get("editions_parsed", 0) for r in keep),
        "counters": dict(agg), "errors": errs,
        "elapsed_s": round(time.time() - t0, 1), "fetch": stats(),
    }
    st["evidence_per_record"] = round(st["evidence_claims"] / max(1, n_kept), 2)
    st["recites_share"] = round(st["recites"] / max(1, n_kept), 3)
    print(json.dumps({k: v for k, v in st.items() if k != "counters"}, indent=2)[:3000])
    print("counters:", json.dumps(dict(agg), indent=1)[:2000])
    if args.dry_run:
        print("(dry run -- shards written, nothing concatenated)")
        return 0

    outp = ocfg["path"]
    out = Path(outp) if os.path.isabs(outp) else PKG / outp
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out.open("w") as fh:
        for r in sorted(keep, key=lambda r: -r["records"]):
            with open(r["shard"]) as sh:
                for line in sh:
                    if global_cap and written >= int(global_cap):
                        break
                    fh.write(line)
                    written += 1
    if global_cap:
        st["records_shipped"] = written
        st["global_cap"] = int(global_cap)
    repp = ocfg["run_report"]
    (Path(repp) if os.path.isabs(repp) else PKG / repp).write_text(json.dumps(
        {"dataset": "ons_statistical_bulletins", "stats": st, "per_family": per_fam,
         "config_snapshot": cfg}, indent=1))
    sp = PKG / cfg["output"]["samples_path"]
    sp.parent.mkdir(parents=True, exist_ok=True)
    with out.open() as src, sp.open("w") as dst:
        for i, line in enumerate(src):
            if i >= 5:
                break
            dst.write(line)
    print(f"wrote {written} records -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
