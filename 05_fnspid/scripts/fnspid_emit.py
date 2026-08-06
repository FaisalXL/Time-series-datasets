#!/usr/bin/env python3
"""Schema-v1 record construction for FNSPID, shared by the devset and the full build.

Why this module exists: the shipped `build_cpt_from_hf.py` predates the frozen v1 schema and
hand-rolls its record dict, so `validate.py --strict` returns **0/5000** on its output
(`license` outside vocab, `source` not a URL) and it emits no
`alignment`/`domain`/`region`/`period_*`, scattering dataset keys at top level instead of under
`meta`. Everything that turns a (ticker, date, articles, price window) tuple into a record now
lives here and goes through `schema/emit.py`, so the devset the owner inspects is built by the
exact code path the 1.2M-record run will use.

Three decisions encoded here, each of which was a defect in the previous build:

1. **Truncation is at a sentence boundary.** The old builder did `block[:cap].rstrip()`, which
   is why every shipped record is exactly 3,002 chars — cut mid-word. News is inverted-pyramid
   so a lede-first truncation keeps the substance, but it has to end on a sentence.

2. **`source` is the article's own URL**, not the HuggingFace dataset slug. The `Url` column is
   100% populated with real article URLs, which is both better provenance and the only way to
   satisfy the schema's URL requirement.

3. **`license` is `proprietary-review`, not `cc-by-4.0`.** FNSPID is CC BY-NC 4.0 and the frozen
   v1 enum has no non-commercial slot; `cc-by-4.0` would be a false identifier because the NC
   clause is the entire open question. `proprietary-review` is the honest tag and it means
   "excluded from any release until cleared" (SCHEMA.md §6). Config-driven so it flips in one
   place if the enum gains `cc-by-nc-4.0` or the B8 decision lands.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

HF_DATASET_URL = "https://huggingface.co/datasets/Zihan1004/FNSPID"

# Channel -> (price CSV column, schema unit label, cast)
CHANNEL_SPEC: Dict[str, Tuple[str, str, Any]] = {
    "open":      ("open",      "open_price_usd",      float),
    "high":      ("high",      "high_price_usd",      float),
    "low":       ("low",       "low_price_usd",       float),
    "close":     ("close",     "close_price_usd",     float),
    "adj_close": ("adj close", "adj_close_price_usd", float),
    "volume":    ("volume",    "volume_shares",       int),
}

# Sentence end: . ! ? optionally followed by a quote/bracket, then whitespace.
_SENT_END = re.compile(r'[.!?]["\')\]]?(?=\s)')
_NUM = re.compile(r"\d[\d,]*\.?\d*")

# Abbreviations that end in a period but do NOT end a sentence. This list is not cosmetic:
# FNSPID wires write the ticker straight after the legal suffix, in BOTH a glued form
# ("Delta Air Lines Inc.DAL") and a spaced form ("American Airlines Group Inc. AAL"). A naive
# `(?<=[.!?])\s+(?=[A-Z])` splitter cuts the spaced form in half, so the company name lands in
# one sentence and its ticker in the next. Sentence indices then mean the wrong thing and
# extraction selects the wrong span.
_ABBRS = {
    "inc", "corp", "co", "cos", "ltd", "limited", "llc", "llp", "lp", "plc", "nv", "sa", "ag",
    "gmbh", "bros", "jr", "sr", "dr", "mr", "mrs", "ms", "prof", "rev", "gen", "sen", "rep",
    "st", "ave", "mt", "no", "nos", "fig", "vs", "etc", "eg", "ie", "al", "est", "approx",
    "univ", "dept", "govt", "intl", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
    "sept", "oct", "nov", "dec", "q1", "q2", "q3", "q4",
}
_BOUNDARY = re.compile(r'([.!?])(["\')\]]?)(\s+)(?=[A-Z0-9"\'(])')


def split_sentences(text: str) -> List[str]:
    """Split prose into sentences, without cutting inside `... Inc. AAL ...`.

    Returns sentences with whitespace normalized. Index i in the returned list corresponds to
    sentence number i+1 in the numbered prompt, so the builder can reassemble the model's
    selection verbatim.
    """
    out: List[str] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        pre = text[max(0, m.start(1) - 30):m.start(1)]
        tok = re.split(r"[\s(\[]", pre)[-1].replace(".", "").lower()
        if tok in _ABBRS or len(tok) <= 1:   # abbreviation, or a single initial ("J. P. Morgan")
            continue
        seg = text[start:m.end(2)].strip()
        if seg:
            out.append(seg)
        start = m.end(3)
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def truncate_at_sentence(text: str, cap: int) -> Tuple[str, bool]:
    """Trim `text` to at most `cap` chars, ending on a sentence boundary where possible.

    Returns (text, was_truncated). Falls back to a word boundary if the window contains no
    sentence end (a single very long sentence), and only ever cuts mid-word if a single word
    exceeds the cap.
    """
    if len(text) <= cap:
        return text, False
    window = text[: cap + 1]
    ends = [m.end() for m in _SENT_END.finditer(window)]
    if ends:
        return window[: ends[-1]].rstrip(), True
    sp = window.rfind(" ")
    if sp > 0:
        return window[:sp].rstrip(), True
    return text[:cap].rstrip(), True


def figures_in_series(text: str, values: Sequence[Optional[float]]) -> int:
    """Count distinct numbers in the prose that also appear in this record's own series.

    Used to size the `contextualizes` vs `describes` question for SCHEMA.md §7: news that
    quotes prices the window actually contains is arguably `describes`, not merely
    contemporaneous commentary. Report it alongside a permutation control against another
    ticker's window -- a single loose numeric match has a high coincidence floor, so the bare
    count means nothing on its own.
    """
    have = set()
    for v in values:
        if v is None:
            continue
        have.add(f"{float(v):.2f}")
        have.add(f"{float(v):.1f}")
        have.add(str(int(round(float(v)))))
    hits = set()
    for m in _NUM.finditer(text):
        tok = m.group(0).replace(",", "")
        if tok in have:
            hits.add(tok)
        else:
            try:
                f = float(tok)
            except ValueError:
                continue
            if f"{f:.2f}" in have or f"{f:.1f}" in have:
                hits.add(tok)
    return len(hits)


def build_timeseries(channels: Sequence[str], win_vals: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    series = []
    for name in channels:
        csv_col, unit, _ = CHANNEL_SPEC[name]
        raw = win_vals[csv_col]
        if name == "volume":
            vals: List[Any] = [None if v is None else int(round(float(v))) for v in raw]
        else:
            vals = [None if v is None else round(float(v), 4) for v in raw]
        series.append({"values": vals, "unit": unit, "freq": "1d"})
    return series


def make_record(
    *,
    ticker: str,
    news_date: str,
    article_block: str,
    channels: Sequence[str],
    win_dates: List[str],
    win_vals: Dict[str, List[Any]],
    urls: Sequence[str],
    titles: Sequence[str],
    n_articles_seen: int,
    text_cap: int,
    alignment: str = "contextualizes",
    license: str = "proprietary-review",
    domain: str = "finance",
    region: str = "US",
    text_source: str = "third_party",
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build one v1-compliant FNSPID record. Raises ValueError if it would fail the gate."""
    body, truncated = truncate_at_sentence(article_block.strip(), text_cap)
    # NO GENERATED TEXT: <ts></ts> is appended straight to the real article prose.
    text = f"{body}\n\n<ts></ts>"

    timeseries = build_timeseries(channels, win_vals)
    close_vals = next((s["values"] for s in timeseries if s["unit"] == "close_price_usd"), [])

    source = next((u for u in urls if isinstance(u, str) and u.startswith("http")), HF_DATASET_URL)

    meta: Dict[str, Any] = {
        "ticker": ticker,
        "news_date": news_date,
        "history_days": len(win_dates),
        "channels": list(channels),
        "n_articles_seen": n_articles_seen,
        "n_articles_used": len(urls),
        "article_urls": list(urls)[:5],
        "article_titles": [t for t in titles if t][:5],
        "text_truncated": truncated,
        "text_chars": len(body),
        "figures_matching_own_series": figures_in_series(body, close_vals),
        "upstream_dataset": HF_DATASET_URL,
        "upstream_license": "CC BY-NC 4.0",
    }
    if extra_meta:
        meta.update(extra_meta)

    return emit_record(
        text=text,
        timeseries=timeseries,
        alignment=alignment,
        license=license,
        source=source,
        text_source=text_source,
        dataset="fnspid",
        series_id=f"fnspid_{ticker}_{news_date}",
        domain=domain,
        region=region,
        # The record spans its price window through the day the article was published.
        period_start=win_dates[0] if win_dates else news_date,
        period_end=news_date,
        timestamps=list(win_dates),
        meta=meta,
    )
