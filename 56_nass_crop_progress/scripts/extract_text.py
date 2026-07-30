#!/usr/bin/env python3
"""Stage 2: parse every cached PDF into (week-ending date, cleaned narrative).

Reads only from `.cache/pdf/`, so this is a purely local pass -- re-runnable in minutes across the
whole corpus whenever the column detector or the narrative cleaner changes. That separation is the
point of caching raw bytes in stage 1.

Writes `.cache/text/{ALPHA}.jsonl`, one row per PDF:
    {url, sha1, date, n_pages, raw_chars, narrative_chars, narrative, reject}

`reject` records *why* a PDF yielded nothing usable (`no_pages`, `no_date`, `garbled`), so the
drop buckets can be counted and audited instead of vanishing. Nothing is filtered on length here
-- text floors belong to the build step, where their cost can be measured against emitted records.

Usage:
    python scripts/extract_text.py [--states IA,KS] [--procs 64] [--force]
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import state_sources as ss  # noqa: E402


PKG_ROOT = Path(__file__).resolve().parent.parent
CACHE = PKG_ROOT / ".cache"
PDF_DIR = CACHE / "pdf"
TEXT_DIR = CACHE / "text"

# Reject threshold for prose garbling. p99 across the harvest is 0.061, so 0.25 is ~4x the
# worst real report and still far below anything actually shredded.
GARBLE_MAX = 0.25
# A genuine weekly report states one week-ending date, sometimes two (it prints the prior week
# alongside). Above this it is a compilation or a monthly summary, not a weekly report.
MAX_WEEK_DATES = 3


def _install_ag_vocabulary() -> None:
    """Seed the crop-vs-weather-station discriminator from the real SHORT_DESC vocabulary."""
    try:
        import pickle
        idx = pickle.load(open(CACHE / "series_index.pkl", "rb"))
    except OSError:
        return
    ss.set_ag_vocabulary(ss.ag_vocabulary({sd for (_st, sd) in idx}))


def prose_garble_fraction(text: str) -> float | None:
    """Share of *prose* tokens that look like OCR/encoding debris, or None if undecidable.

    Measures the package's standing "clean born-digital text only" scope call instead of relying on
    hand-curated per-state start years.

    ⚠️ Measured over prose lines only, and this matters. Scoring the whole extracted text instead
    flagged **258 perfectly readable reports** as garbled -- every California 2024 report, 155
    Pennsylvania reports, all of Louisiana's -- because those reports interleave a side-by-side
    table whose cells and axis fragments are exactly the short vowel-less tokens the metric counts.
    The prose in them is clean:
        "According to the National Agricultural Statistics Service in Pennsylvania, there were
         5.5 days suitable for fieldwork for the week ending Sunday, August 28, 2022."

    Restricted to prose lines the distribution is tight and unimodal -- p50 0.009, p95 0.039,
    p99 0.061 across 15,829 harvested reports -- i.e. genuinely shredded text is essentially absent
    from this archive; scanned-image years fail earlier as `no_pages` instead. The threshold is set
    well clear of that (see GARBLE_MAX) so it still catches real debris without discarding a report
    for having a table next to its prose.

    Returns None when there is too little prose to judge (short-but-real reports). Rejecting those
    as "garbled" is what the old `< 40 tokens -> 1.0` guard did, and it is a different failure than
    broken text: length is the build's `min_text_chars` decision, not an extraction verdict.
    """
    lines = [ln for ln in text.splitlines() if ss._looks_like_prose(ln.strip())]
    toks = [t for t in "\n".join(lines).split() if any(c.isalpha() for c in t)]
    if len(toks) < 40:
        return None
    bad = 0
    for t in toks:
        letters = [c for c in t if c.isalpha()]
        if not letters:
            continue
        if len(letters) <= 2 and t.lower() not in {
            "a", "i", "an", "as", "at", "be", "by", "do", "go", "he", "if", "in", "is", "it",
            "me", "my", "no", "of", "on", "or", "so", "to", "up", "us", "we", "am", "hi",
        }:
            bad += 1
            continue
        if not any(c.lower() in "aeiouy" for c in letters) and len(letters) >= 3:
            bad += 1
    return bad / len(toks)


def parse_one(args: tuple[str, str, str]) -> dict:
    # Worker processes are forked per task; the vocabulary is cheap to rebuild and must exist in
    # each worker, since module globals set in the parent don't survive a spawn start method.
    if len(ss._AG_VOCAB) <= len(ss._AG_EXTRA):
        _install_ag_vocabulary()
    alpha, url, sha1 = args
    path = PDF_DIR / alpha / f"{sha1}.pdf"
    out = {"url": url, "sha1": sha1}
    try:
        raw = path.read_bytes()
    except OSError:
        return {**out, "reject": "missing_file"}
    pages = ss.pdf_to_pages_text(raw)
    if not pages:
        return {**out, "reject": "no_pages"}
    full = "\n".join(pages)
    out["n_pages"] = len(pages)
    out["raw_chars"] = len(full)
    # Split "has no text layer at all" from "has text but no parseable date". Both used to land in
    # `no_date`, which hid what the bucket actually was: Illinois contributes 972 scanned-image
    # files (e.g. wc_012180.pdf, week of 1980-01-21, extracts to 1 character) and Mississippi 222.
    # Those need OCR, which is out of scope by an explicit scope call -- whereas a *dated* report
    # whose date merely failed to parse would be recoverable. Keeping them in one bucket makes it
    # impossible to tell how much volume is actually on the table.
    if len(full.strip()) < 200:
        return {**out, "reject": "no_text"}
    date = ss.parse_week_ending(full)
    narrative = ss.clean_narrative(pages)
    out["narrative_chars"] = len(narrative)
    gf = prose_garble_fraction(narrative)
    out["garbled_frac"] = None if gf is None else round(gf, 4)
    if date is None:
        return {**out, "reject": "no_date"}
    out["date"] = date.isoformat()
    # Not a single weekly report: whole-season compilations and monthly summaries both state a
    # valid date and would otherwise pair a season's worth of text with one week's window.
    n_dates = ss.count_week_ending_dates(full)
    out["n_week_dates"] = n_dates
    if n_dates > MAX_WEEK_DATES:
        return {**out, "reject": "multi_week_document"}
    if gf is not None and gf > GARBLE_MAX:
        return {**out, "reject": "garbled", "narrative": narrative[:400]}
    out["narrative"] = narrative
    return out


def run_state(alpha: str, procs: int, force: bool) -> dict:
    idx_path = PDF_DIR / alpha / "index.jsonl"
    if not idx_path.exists():
        return {"alpha": alpha, "n": 0}
    jobs = []
    with open(idx_path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("status") == "ok" and rec.get("sha1"):
                jobs.append((alpha, rec["url"], rec["sha1"]))
    # de-dup (index.jsonl is append-only across resumed runs)
    seen, uniq = set(), []
    for j in jobs:
        if j[2] in seen:
            continue
        seen.add(j[2])
        uniq.append(j)
    jobs = uniq

    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TEXT_DIR / f"{alpha}.jsonl"
    if out_path.exists() and not force:
        have = set()
        with open(out_path) as f:
            for line in f:
                try:
                    have.add(json.loads(line)["sha1"])
                except Exception:
                    continue
        jobs = [j for j in jobs if j[2] not in have]
        mode = "a"
    else:
        mode = "w"
    if not jobs:
        print(f"  {alpha}: nothing to do", file=sys.stderr)
        return {"alpha": alpha, "n": 0}

    n_ok = 0
    rej: dict[str, int] = {}
    with open(out_path, mode) as fh, ProcessPoolExecutor(max_workers=procs) as ex:
        for fut in as_completed([ex.submit(parse_one, j) for j in jobs]):
            try:
                rec = fut.result()
            except Exception as e:
                rec = {"reject": f"crash_{type(e).__name__}"}
            fh.write(json.dumps(rec) + "\n")
            if rec.get("reject"):
                rej[rec["reject"]] = rej.get(rec["reject"], 0) + 1
            else:
                n_ok += 1
    print(f"  {alpha}: {n_ok} parsed clean, rejects={rej}", file=sys.stderr, flush=True)
    return {"alpha": alpha, "n": n_ok, "rejects": rej}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", default=None)
    ap.add_argument("--procs", type=int, default=48)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    states = (args.states.split(",") if args.states
              else sorted(p.name for p in PDF_DIR.iterdir() if p.is_dir()))
    tot = 0
    allrej: dict[str, int] = {}
    for a in states:
        r = run_state(a, args.procs, args.force)
        tot += r.get("n", 0)
        for k, v in (r.get("rejects") or {}).items():
            allrej[k] = allrej.get(k, 0) + v
    print(f"TOTAL parsed clean: {tot}   rejects: {allrej}", file=sys.stderr)


if __name__ == "__main__":
    main()
