#!/usr/bin/env python3
"""Build CPT world-knowledge records from UK ONS statistical bulletins.

One record = ONE ANALYTICAL SECTION of one ONS bulletin edition: that section's own VERBATIM
prose paired with the trailing multi-channel window of the exact indicator series the bulletin
family reports.

WHY SECTION-LEVEL, NOT ONE RECORD PER BULLETIN: a single ONS bulletin runs ~44,000 chars
(~11,000 tokens), 22x the 500-token cap. One record per bulletin discards ~95% of real
first-party prose. Each H2 section is a self-contained analytical passage about its own
sub-series, so section-level records multiply scale WITHOUT duplicating prose.

TEXT STAYS 100% VERBATIM. Long sections are cut to the cap by choosing a CONTIGUOUS run of
whole paragraphs in source order -- never by rewriting. Abstractive LLM summarization is
implemented as a guarded hook but disabled: `schema/validate.py` has no `llm_summarized`
text_quality value yet (see README).

Two failure modes this corpus has been bitten by before, both handled up front:
  * Cross-channel false match (killed openFDA/NHTSA/CFPB, caught pre-ship on RBNZ #60):
    a figure is credited to a channel ONLY if that channel's keyword appears near it.
  * URL-slug vs reference-month drift (FHFA #59's URL month = index month + 2): the ONS slug
    is verified to BE the reference month against the bulletin's own prose, not assumed.

Usage:
    python scripts/build_cpt_jsonl.py --config config.example.yaml
"""
from __future__ import annotations

import argparse
import difflib
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent
sys.path.insert(0, str(PKG_ROOT.parent / "schema"))
from emit import emit_record  # noqa: E402

MONTHS = ["", "january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
MONTH_ABBR = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

_last_fetch = [0.0]
_stats: dict = {}


def bump(k: str, n: int = 1) -> None:
    _stats[k] = _stats.get(k, 0) + n


# --- fetch -----------------------------------------------------------------------------------

def fetch(url: str, cache: Path, cfg: dict) -> bytes:
    """Cached GET. ONS returns 429 on back-to-back requests, so live calls are paced."""
    if cache.exists():
        return cache.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    d = cfg["data"]
    gap = float(d.get("min_interval_s", 3.0))
    hdrs = {"User-Agent": d.get("user_agent", "CPT-research"), "Accept": "*/*"}
    last = None
    for attempt in range(int(d.get("retries", 4))):
        wait = gap - (time.time() - _last_fetch[0])
        if wait > 0:
            time.sleep(wait)
        _last_fetch[0] = time.time()
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=float(d.get("timeout_s", 120))) as r:
                body = r.read()
            cache.write_bytes(body)
            return body
        except urllib.error.HTTPError as e:
            last = e
            # 429 = rate limited: back off hard and retry. 404 is a real answer: stop.
            if e.code == 429:
                time.sleep(gap * (attempt + 2))
                continue
            raise
        except Exception as e:  # transient network
            last = e
            time.sleep(gap * (attempt + 1))
    raise RuntimeError(f"fetch failed after retries: {url} ({last})")


# --- series ----------------------------------------------------------------------------------

def load_series(fam: dict, chan: dict, cfg: dict, cache: Path) -> tuple[dict, str, str]:
    """Return ({'YYYY-MM': float}, title, unit_from_ons) for one CDID."""
    url = cfg["data"]["series_url"].format(theme=fam["theme"], subtopic=fam["subtopic"],
                                           cdid=chan["cdid"], dataset=fam["dataset"])
    raw = fetch(url, cache / "series" / f"{fam['dataset']}_{chan['cdid']}.json", cfg)
    d = json.loads(raw.decode("utf-8", "replace"))
    out: dict = {}
    for pt in d.get("months") or []:
        # ONS month labels look like "2026 JUN"
        m = re.match(r"^\s*(\d{4})\s+([A-Z]{3})", str(pt.get("date", "")).upper())
        if not m:
            continue
        mm = MONTH_ABBR.get(m.group(2))
        if not mm:
            continue
        try:
            out[f"{m.group(1)}-{mm:02d}"] = float(pt["value"])
        except (TypeError, ValueError):
            continue
    desc = d.get("description") or {}
    return out, (desc.get("title") or ""), (desc.get("unit") or "")


def window(series: dict, ref: str, n: int) -> tuple[list, list]:
    """Trailing n monthly points ENDING at ref ('YYYY-MM'). Explicit gaps, no imputation."""
    y, m = int(ref[:4]), int(ref[5:7])
    keys = []
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    if keys[-1] not in series:
        return [], []
    return [series.get(k) for k in keys], keys


# --- bulletin HTML ---------------------------------------------------------------------------

def strip_tags(frag: str) -> str:
    frag = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", frag)
    frag = re.sub(r"(?is)<br\s*/?>", "\n", frag)
    frag = re.sub(r"(?is)</(p|li|h[1-6]|div|tr)>", "\n\n", frag)
    frag = re.sub(r"(?s)<[^>]+>", " ", frag)
    frag = htmllib.unescape(frag)
    frag = re.sub(r"[ \t ]+", " ", frag)
    frag = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", frag)
    return frag.strip()


def parse_sections(page: bytes) -> list[dict]:
    """Split an ONS bulletin into its H2 sections.

    Structure (verified live): <div id="{anchor}" class="section__content--markdown">
                                 <section><header><h2><span>N.</span> Title</h2></header> ...
    """
    doc = page.decode("utf-8", "replace")
    out = []
    parts = re.split(r'<div id="([a-z0-9\-]+)"\s+class="section__content--markdown">', doc)
    # parts = [pre, anchor1, body1, anchor2, body2, ...]
    for i in range(1, len(parts) - 1, 2):
        anchor, body = parts[i], parts[i + 1]
        h2 = re.search(r"(?is)<h2[^>]*>(.*?)</h2>", body)
        title = strip_tags(h2.group(1)) if h2 else anchor
        title = re.sub(r"^\d+\.\s*", "", title).strip()
        body_wo_head = re.sub(r"(?is)<header>.*?</header>", " ", body, count=1)
        # tables are numeric dumps, not prose -- drop them from the text side
        body_wo_head = re.sub(r"(?is)<table.*?</table>", " ", body_wo_head)
        # figure/chart furniture ("Download this chart", "Source: ONS") is page chrome
        text = strip_tags(body_wo_head)
        out.append({"anchor": anchor, "title": title, "text": text})
    return out


CHROME_RE = re.compile(
    r"(?i)^\s*(download (this|the) (chart|image|data)|source: |embed code|"
    r"figure \d+[:.]|notes?: |back to table of contents|copy (this )?link|"
    r"view online|hide|show|xlsx|csv)\b")


def clean_paragraphs(text: str) -> list[str]:
    """Keep real prose paragraphs only.

    Chart furniture is the trap here -- an ONS section interleaves figure captions and
    "Download this chart Figure 3: ..." lines with the narrative. RBNZ #60 shipped exactly this
    class of leftover page chrome glued into a sentence before it was caught, so both a prefix
    match AND a structural rule are applied: a real prose paragraph ENDS IN TERMINAL
    PUNCTUATION. Captions, axis labels and sub-headings do not, so they drop out cleanly
    (the cost is losing verbatim sub-headings, which carry no numbers).
    """
    paras = []
    for p in re.split(r"\n\s*\n", text):
        p = re.sub(r"\s+", " ", p).strip()
        if not p or CHROME_RE.match(p):
            continue
        if len(p) < 25:            # stray labels / axis text
            continue
        if not re.search(r"[.!?][\"')’”]?$", p):
            continue               # caption / heading / chart title, not prose
        paras.append(p)
    return paras


FIG_RE = re.compile(r"(-?\d+\.\d)\s*%")


def n_figures(s: str) -> int:
    return len(FIG_RE.findall(s))


# --- text selection (all modes keep text VERBATIM) -------------------------------------------

def select_span(paras: list[str], cap: int, mode: str, cfg: dict, ctx: str) -> tuple[str, dict]:
    """Choose a CONTIGUOUS run of whole paragraphs, in source order, within `cap` chars."""
    if not paras:
        return "", {"selector": mode, "n_paragraphs": 0}

    def join(lo: int, hi: int) -> str:
        return "\n\n".join(paras[lo:hi])

    # candidate runs: every contiguous [lo, hi) that fits the cap
    best = (None, -1, 0, 0)   # (text, score, lo, hi)
    for lo in range(len(paras)):
        for hi in range(lo + 1, len(paras) + 1):
            t = join(lo, hi)
            if len(t) > cap:
                break
            score = n_figures(t) * 1000 + len(t)   # figures first, then use the budget
            if score > best[1]:
                best = (t, score, lo, hi)
    if best[0] is None:
        # even one paragraph exceeds the cap -> keep the first, cut at a sentence boundary
        first = paras[0]
        cut = first[:cap]
        m = list(re.finditer(r"(?<=[.!?])\s", cut))
        text = cut[:m[-1].start() + 1].strip() if m else cut.strip()
        return text, {"selector": mode, "n_paragraphs": 1, "sentence_truncated": True}

    if mode == "llm_extractive":
        chosen = _llm_pick_span(paras, cap, cfg, ctx)
        if chosen is not None:
            lo, hi = chosen
            t = join(lo, hi)
            if t and len(t) <= cap:
                return t, {"selector": "llm_extractive", "n_paragraphs": hi - lo,
                           "span": [lo, hi], "verbatim": True}
            bump("llm_span_rejected")
        bump("llm_fallback_numeric_density")
    elif mode == "head":
        lo = 0
        hi = 1
        while hi < len(paras) and len(join(lo, hi + 1)) <= cap:
            hi += 1
        return join(lo, hi), {"selector": "head", "n_paragraphs": hi - lo}

    return best[0], {"selector": "numeric_density", "n_paragraphs": best[3] - best[2],
                     "span": [best[2], best[3]], "n_figures": n_figures(best[0])}


def _llm_pick_span(paras: list[str], cap: int, cfg: dict, ctx: str):
    """Ask an LLM which contiguous paragraph run to KEEP. Extractive only -- the text it
    selects is shipped verbatim, so text_quality stays "real". Same grounded shape as the
    FNSPID B1 relevance judge. Returns (lo, hi) or None."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        bump("llm_no_api_key")
        return None
    try:
        import anthropic  # optional dependency
    except ImportError:
        bump("llm_sdk_missing")
        return None
    listing = "\n".join(f"[{i}] ({len(p)} chars, {n_figures(p)} figures) {p[:300]}"
                        for i, p in enumerate(paras))
    prompt = (
        "You are selecting which part of a real UK ONS statistical bulletin section to keep "
        "for a text+time-series training corpus. Choose the CONTIGUOUS run of paragraphs that "
        "most directly states measured values and their changes for the indicator series "
        f"({ctx}). The kept text is used VERBATIM and must be at most {cap} characters total.\n"
        "Do not write a summary. Do not paraphrase. Only choose a range.\n"
        f"Paragraphs:\n{listing}\n\n"
        'Reply with ONLY JSON: {"start": <int>, "end": <int>} where end is exclusive.')
    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=cfg["text"].get("llm_model", "claude-sonnet-5"),
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        body = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        m = re.search(r'\{[^}]*"start"\s*:\s*(\d+)[^}]*"end"\s*:\s*(\d+)[^}]*\}', body)
        if not m:
            return None
        lo, hi = int(m.group(1)), int(m.group(2))
        if 0 <= lo < hi <= len(paras):
            bump("llm_span_used")
            return lo, hi
    except Exception:
        bump("llm_call_failed")
    return None


# --- evidence: does the prose actually recite the series? ------------------------------------

def month_name(ref: str) -> str:
    return MONTHS[int(ref[5:7])].capitalize()


def prev_month(ref: str) -> str:
    y, m = int(ref[:4]), int(ref[5:7])
    return f"{y-1:04d}-12" if m == 1 else f"{y:04d}-{m-1:02d}"


def has_kw(clause: str, keywords: list[str]) -> bool:
    """Keyword presence, word-bounded. \\bCPI\\b cannot match inside "CPIH", so CPI and CPIH
    never satisfy each other (the substring trap that bit RBNZ #60 on tradables)."""
    for kw in keywords:
        pat = r"\b" + re.escape(kw) + (r"\b" if kw[-1].isalpha() else "")
        if re.search(pat, clause, re.I):
            return True
    return False


def split_clauses(text: str) -> list[tuple[int, str]]:
    """Split into attribution units: sentences, then semicolon-joined sub-claims.

    ONS chains distinct claims about DIFFERENT series with semicolons -- e.g. "Core CPIH ...
    rose by 2.8% ...; the CPIH goods annual rate slowed from 2.0% to 1.7%". A sentence-level
    window would let the "core" claim and the "goods" claim satisfy each other's keywords, so
    the semicolon split is required, not cosmetic.
    """
    # A naive "[^.!?]+" split treats the decimal point in "2.8%" as a sentence end and shreds
    # every figure (this exact bug produced 0 evidence on the first pass). A sentence break
    # requires terminator + whitespace + an opening capital; paragraph breaks also split.
    bounds = [0]
    for m in re.finditer(r'(?<=[.!?])\s+(?=[A-Z"‘“(])|\n{2,}', text):
        bounds.append(m.end())
    bounds.append(len(text))
    out = []
    for a, b in zip(bounds, bounds[1:]):
        sent = text[a:b]
        pos = 0
        for piece in sent.split(";"):
            out.append((a + pos, piece))
            pos += len(piece) + 1
    return [(o, c) for o, c in out if c.strip()]


MONTH_IN_CLAUSE = re.compile(
    r"(?i)\b(?:12 months to|months to|in|for|during)\s+"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"(?:\s+(\d{4}))?")
PREV_PHRASE = re.compile(r"(?i)\b(?:the previous month|a month earlier|last month)\b")
FROM_TO = re.compile(r"(?i)from\s+-?\d+\.\d\s*%?\s*to\s+-?\d+\.\d\s*%")


def clause_target_months(clause: str, ref: str, fig_idx_in_clause: int, n_figs: int) -> list:
    """Which month(s) a figure inside this clause is allowed to refer to.

    Returns a list of (label, 'YYYY-MM') the figure may legitimately match. Restricting this
    is what stops a figure being silently credited to the wrong month (a false match found in
    the first build: a June CPIH monthly figure matched May's CPI monthly value).
    """
    prev = prev_month(ref)
    named = []
    for m in MONTH_IN_CLAUSE.finditer(clause):
        mi = MONTHS.index(m.group(1).lower())
        yr = int(m.group(2)) if m.group(2) else int(ref[:4])
        named.append(f"{yr:04d}-{mi:02d}")
    # "slowed from 2.0% to 1.7%" -> first figure is the earlier month, second is the reference
    if FROM_TO.search(clause) and n_figs >= 2:
        if fig_idx_in_clause == 0:
            return [("previous_month", prev)]
        if fig_idx_in_clause == 1:
            return [("reference_month", ref)]
    if PREV_PHRASE.search(clause):
        # "rose by 2.8% ..., down from 3.0% the previous month" -> the LAST figure is prev
        if fig_idx_in_clause == n_figs - 1 and n_figs >= 2:
            return [("previous_month", prev)]
        return [("reference_month", ref)]
    if named:
        out = []
        for k in named:
            out.append(("named_month", k))
        # a clause naming exactly one month pins the figure to it
        return out
    return [("reference_month", ref), ("previous_month", prev)]


def verify(text: str, ref: str, chans: list[dict], disq: list[str]) -> tuple[str, list, int, dict]:
    """Match each 'N.N%' figure to a channel, conservatively.

    A figure becomes evidence only if ALL of these hold:
      1. the channel's keyword appears in the figure's OWN clause (not a neighbouring one),
      2. the clause carries no disqualifying modifier that points at a series we do not hold
         ("core", "excluding", "goods", "services", "contribution", ...),
      3. the channel's value at the month the CLAUSE ITSELF names rounds to the figure.
    Anything else is left unattributed and counted in `rejected`. Being wrong here is worse
    than being silent: a polluted evidence array inflates apparent quality.
    """
    ev, rejected = [], {"disqualified_clause": 0, "no_keyword": 0, "value_mismatch": 0}
    n_all = 0
    for c_off, clause in split_clauses(text):
        figs = list(FIG_RE.finditer(clause))
        n_all += len(figs)
        if not figs:
            continue
        blocked = [d for d in disq if re.search(r"\b" + re.escape(d) + r"\b", clause, re.I)]
        for i, m in enumerate(figs):
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            targets = clause_target_months(clause, ref, i, len(figs))
            matched = False
            saw_kw = False
            for ch in chans:
                kws = ch.get("keywords") or []
                if kws and not has_kw(clause, kws):
                    continue
                saw_kw = True
                # a modifier the channel does not cover -> refuse to attribute
                bad = [b for b in blocked if not has_kw(" ".join(kws), [b])]
                if bad:
                    continue
                for label, key in targets:
                    got = ch["_series"].get(key)
                    if got is None:
                        continue
                    if abs(round(got, 1) - val) < 0.05:
                        ev.append({"figure_pct": val, "unit": ch["unit"], "month": key,
                                   "month_source": label, "series_value": got,
                                   "clause": re.sub(r"\s+", " ", clause).strip()[:200]})
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                if blocked and saw_kw:
                    rejected["disqualified_clause"] += 1
                elif not saw_kw:
                    rejected["no_keyword"] += 1
                else:
                    rejected["value_mismatch"] += 1
    align = "recites" if ev else "describes"
    return align, ev, n_all, rejected


SUPERLATIVE_RE = re.compile(
    r"(?i)\b(highest|lowest|record high|record low|strongest|weakest|"
    r"largest|smallest)\b[^.]{0,80}?\b(on record|since records began|ever)\b")

# ONS far more often makes a BOUNDED superlative: "the lowest since August 2024, when it was
# 1.3%". That is fully checkable against 450 months of held history and is the more common
# claim shape, so it gets its own check rather than being ignored.
BOUNDED_SUP_RE = re.compile(
    r"(?i)\b(highest|lowest)\b[^.;]{0,60}?\bsince\s+"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{4})(?:[^.;]{0,30}?\bwhen it was\s+(-?\d+\.\d)\s*%)?")


def enclosing_paragraph(text: str, idx: int) -> str:
    """The paragraph containing `idx`. Used for SUPERLATIVE attribution only.

    Figures are attributed at CLAUSE scope (tight, because a wrong attribution creates false
    evidence). A superlative's subject is normally established in an earlier sentence of the
    same paragraph -- "Food ... prices rose by 1.7% ... The annual rate in June was the lowest
    since August 2024" -- so clause scope leaves every superlative unattributed and the check
    becomes dead code. Paragraph scope fixes that; the ambiguity rule below keeps it safe.
    """
    start = text.rfind("\n\n", 0, idx)
    start = 0 if start < 0 else start + 2
    end = text.find("\n\n", idx)
    return text[start:(len(text) if end < 0 else end)]


def attribute(scope: str, chans: list[dict]) -> list[dict]:
    """Every channel whose keyword appears in `scope`. More than one -> ambiguous."""
    return [ch for ch in chans
            if not (ch.get("keywords") or []) or has_kw(scope, ch["keywords"])]


def _drop_worthy(text: str, idx: int, ch: dict, chans: list[dict], disq: list[str]) -> bool:
    """May a contradiction on this claim DROP the record?

    Only if the claim's OWN CLAUSE identifies the channel unambiguously and carries no
    modifier pointing at a series we do not hold. Paragraph-scope attribution is good enough
    to FLAG but not to discard data: in the first build it produced three false
    "contradictions" -- "housing and household services", "owner occupiers' housing costs" and
    "domestic heating oil" all matched the COICOP-04 keyword "housing" while being different
    series -- and silently dropped a good record. A false drop is worse than a noisy flag
    because nothing downstream can see it.
    """
    clauses = split_clauses(text)
    own = next((c for off, c in reversed(clauses) if off <= idx), "")
    if any(re.search(r"\b" + re.escape(d) + r"\b", own, re.I) for d in disq):
        return False
    kws = ch.get("keywords") or []
    if not kws or not has_kw(own, kws):
        return False
    return len(attribute(own, chans)) == 1


def bounded_superlative_flags(text: str, chans: list[dict], ref: str,
                              disq: list[str] | None = None) -> tuple[list, bool]:
    """Verify "X is the highest/lowest since <Month Year>" against the held history.

    Two independent checks per claim:
      1. extremum -- the reference-month value really is the max/min over (since_month, ref].
      2. back-reference -- if the prose also states the value at that earlier month
         ("when it was 1.3%"), that value must match the series there.
    """
    flags, contradicted = [], False
    for m in BOUNDED_SUP_RE.finditer(text):
        word = m.group(1).lower()
        since = f"{int(m.group(3)):04d}-{MONTHS.index(m.group(2).lower()):02d}"
        quoted = float(m.group(4)) if m.group(4) else None
        claim = re.sub(r"\s+", " ", m.group(0)).strip()
        cands = attribute(enclosing_paragraph(text, m.start()), chans)
        if not cands:
            flags.append({"claim": claim, "since": since, "verdict": "unchecked_no_channel"})
            continue
        if len(cands) > 1:
            # two or more channels could own this claim -> record it, but never drop on it
            flags.append({"claim": claim, "since": since, "verdict": "ambiguous_multi_channel",
                          "candidates": [c["unit"] for c in cands]})
            continue
        ch = cands[0]
        hist = ch["_series"]
        cur = hist.get(ref)
        span = {k: v for k, v in hist.items() if since < k <= ref}
        if cur is None or not span:
            flags.append({"claim": claim, "since": since, "unit": ch["unit"],
                          "verdict": "unchecked_no_data"})
            continue
        extremum = max(span.values()) if word == "highest" else min(span.values())
        ok = cur >= extremum - 1e-9 if word == "highest" else cur <= extremum + 1e-9
        hit = {"claim": claim, "since": since, "unit": ch["unit"], "current": cur,
               "extremum_over_span": extremum, "n_months_in_span": len(span),
               "verdict": "consistent" if ok else "contradicted"}
        if quoted is not None:
            at = hist.get(since)
            hit["quoted_at_since"] = quoted
            hit["series_at_since"] = at
            hit["back_reference"] = ("match" if at is not None
                                     and abs(round(at, 1) - quoted) < 0.05 else "mismatch")
        if not ok:
            if _drop_worthy(text, m.start(), ch, chans, disq or []):
                contradicted = True
            else:
                hit["verdict"] = "contradicted_weak_attribution"
                hit["note"] = ("attributed only at paragraph scope -- the claim's own clause "
                               "does not unambiguously name this channel, so this is reported, "
                               "not acted on")
        flags.append(hit)
    return flags, contradicted


def superlative_flags(text: str, chans: list[dict], ref: str) -> tuple[list, bool]:
    """A verbatim 'highest on record' that its own paired history contradicts is a real risk
    (caught on FAS GAIN #58). Check the claim against the FULL channel history, not the window.
    """
    flags, contradicted = [], False
    clauses = split_clauses(text)
    for m in SUPERLATIVE_RE.finditer(text):
        claim = re.sub(r"\s+", " ", text[max(0, m.start() - 90):m.end() + 60]).strip()
        hit = {"claim": claim, "verdict": "unchecked"}
        # attribute the superlative using its OWN clause, same rule as the figure matcher
        own = next((c for off, c in reversed(clauses) if off <= m.start()), text)
        for ch in chans:
            kws = ch.get("keywords") or []
            if kws and not has_kw(own, kws):
                continue
            hist = ch["_series"]
            cur = hist.get(ref)
            if cur is None:
                continue
            hi = max(hist.values())
            lo = min(hist.values())
            word = m.group(1).lower()
            if word in ("highest", "record high", "strongest", "largest"):
                ok = cur >= hi - 1e-9
            else:
                ok = cur <= lo + 1e-9
            hit = {"claim": claim, "unit": ch["unit"], "current": cur,
                   "series_max": hi, "series_min": lo,
                   "verdict": "consistent" if ok else "contradicted"}
            if not ok:
                contradicted = True
            break
        flags.append(hit)
    return flags, contradicted


# --- main ------------------------------------------------------------------------------------

def deep_set(d: dict, dotted: str, raw: str) -> None:
    cur = d
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = yaml.safe_load(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(PKG_ROOT / "config.example.yaml"))
    ap.add_argument("--set", action="append", default=[],
                    help="override a config key, e.g. --set output.max_records=null")
    ap.add_argument("--dry-run", action="store_true", help="parse + report, write nothing")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    for ov in args.set:
        k, _, v = ov.partition("=")
        deep_set(cfg, k.strip(), v.strip())

    tcfg, scfg, ocfg = cfg["text"], cfg["series"], cfg["output"]
    if tcfg.get("abstractive_summary"):
        print("ERROR: text.abstractive_summary is not shippable yet.\n"
              "  schema/validate.py allows text_quality in {'real','generated'} only -- there\n"
              "  is no `llm_summarized` value, and 'generated' would conflate a grounded\n"
              "  summary of a real document with fully synthetic text. Get the schema tag\n"
              "  signed off first (see README 'The LLM question'), then wire this path.",
              file=sys.stderr)
        return 2

    cache = PKG_ROOT / cfg["data"].get("cache_dir", ".cache")
    cap = int(tcfg["max_chars"])
    min_chars = int(tcfg["min_chars"])
    excl = set(tcfg.get("exclude_anchors") or [])
    min_figs = int(tcfg.get("min_figures", 0))
    max_records = ocfg.get("max_records")

    records: list[dict] = []
    seen_text: dict = {}
    for fam in cfg["data"]["families"]:
        # ---- channels (fetched once per family, reused across editions) ----
        chans = []
        for ch in fam["channels"]:
            series, title, ons_unit = load_series(fam, ch, cfg, cache)
            if not series:
                bump("channel_empty")
                continue
            c = dict(ch)
            c["_series"] = series
            c["_title"] = title
            c["_ons_unit"] = ons_unit
            chans.append(c)
        if not chans:
            bump("family_no_channels")
            continue
        print(f"[{fam['family']}] {len(chans)} channels, "
              f"{min(len(c['_series']) for c in chans)}-{max(len(c['_series']) for c in chans)} months each")

        for edition in fam["editions"]:
            m = re.match(r"^([a-z]+)(\d{4})$", edition)
            if not m or m.group(1) not in MONTHS:
                bump("bad_edition_slug")
                continue
            ref = f"{int(m.group(2)):04d}-{MONTHS.index(m.group(1)):02d}"
            url = cfg["data"]["bulletin_url"].format(theme=fam["theme"], subtopic=fam["subtopic"],
                                                     family=fam["family"], edition=edition)
            try:
                page = fetch(url, cache / "bulletins" / f"{fam['family']}_{edition}.html", cfg)
            except Exception as e:
                bump("bulletin_fetch_failed")
                print(f"  ! {edition}: {e}")
                continue

            # window the channels once per edition
            wins, ts_keys = [], None
            for c in chans:
                vals, keys = window(c["_series"], ref, int(scfg["window_months"]))
                if len(vals) < int(scfg["min_points"]) or any(v is None for v in vals):
                    continue
                wins.append((c, vals))
                ts_keys = keys
            if not wins:
                bump("no_windowed_channels")
                continue

            secs = parse_sections(page)
            bump("sections_seen", len(secs))
            for sec in secs:
                if sec["anchor"] in excl:
                    bump("section_excluded_boilerplate")
                    continue
                paras = clean_paragraphs(sec["text"])
                full = "\n\n".join(paras)
                if n_figures(full) < min_figs:
                    bump("section_too_few_figures")
                    continue
                span, sel_meta = select_span(paras, cap, tcfg.get("selector", "numeric_density"),
                                            cfg, f"{fam['family']} {month_name(ref)} {ref[:4]}")
                if len(span) < min_chars:
                    bump("section_too_short")
                    continue

                align, ev, n_fig, rej = verify(span, ref, [c for c, _ in wins],
                                               tcfg.get("disqualifying_terms") or [])
                for k, v in rej.items():
                    bump(f"evidence_rejected_{k}", v)
                flags, contra = superlative_flags(span, [c for c, _ in wins], ref)
                bflags, bcontra = bounded_superlative_flags(
                    span, [c for c, _ in wins], ref, tcfg.get("disqualifying_terms") or [])
                flags = flags + bflags
                contra = contra or bcontra
                bump("superlative_flags_seen", len(flags))
                if contra and tcfg.get("drop_on_superlative_contradiction"):
                    bump("superlative_dropped")
                    continue

                # ---- near-duplicate gate -------------------------------------------------
                # ONS reuses sentence templates month to month ("The 12-month rate for X was
                # Y% in <month>, down from Z% in <prev>"). Measured on the demo: consecutive
                # editions of the SAME section reach 0.806 similarity -- distinct (the numbers
                # carry the information), but at ~130 editions per family the tail needs a
                # ceiling or the corpus fills with near-copies.
                sim_cap = tcfg.get("max_similarity")
                if sim_cap is not None:
                    prev_same = [p for p in seen_text.get((fam["family"], sec["anchor"]), [])]
                    worst = max((difflib.SequenceMatcher(None, span, p).ratio()
                                 for p in prev_same), default=0.0)
                    if worst > float(sim_cap):
                        bump("near_duplicate_dropped")
                        continue
                    seen_text.setdefault((fam["family"], sec["anchor"]), []).append(span)

                text = f"{span}\n\n<ts></ts>"
                timeseries = [{"values": [round(v, 4) for v in vals],
                               "unit": c["unit"], "freq": scfg["freq"]} for c, vals in wins]
                rec = emit_record(
                    text=text,
                    timeseries=timeseries,
                    timestamps=ts_keys,
                    alignment=align,
                    license="cc-by-4.0",
                    text_source="first_party_official",
                    source=url,
                    dataset="ons_statistical_bulletins",
                    series_id=f"ons:{fam['family']}:{edition}:{sec['anchor']}",
                    domain=fam.get("domain", "macro"),
                    region=fam.get("region", "GB"),
                    period_start=ts_keys[0],
                    period_end=ts_keys[-1],
                    meta={
                        "true_license": "Open Government Licence v3.0 (OGL v3) -- ONS states OGL "
                                        "v3 is interoperable with CC BY 4.0; attribution required. "
                                        "Tagged cc-by-4.0 as closest schema fit.",
                        "attribution": "Source: Office for National Statistics licensed under the "
                                       "Open Government Licence v.3.0",
                        "bulletin_family": fam["family"],
                        "edition": edition,
                        "reference_month": ref,
                        "section_anchor": sec["anchor"],
                        "section_title": sec["title"],
                        "n_channels": len(wins),
                        "n_points": len(ts_keys),
                        "text_selection": sel_meta,
                        "section_full_chars": len(full),
                        "shipped_chars": len(span),
                        "compression_ratio": round(len(full) / max(1, len(span)), 2),
                        "n_figures_in_text": n_fig,
                        "recite_evidence": ev,
                        "evidence_rejected": rej,
                        "superlative_flags": flags,
                        "channels": [{"cdid": c["cdid"], "unit": c["unit"], "ons_title": c["_title"]}
                                     for c, _ in wins],
                        "vintage_caveat": "Series come from the CURRENT ONS vintage; the bulletin "
                                          "quotes its contemporaneous vintage. ONS revises, so a "
                                          "claim can drift from live data (same class as FHFA #59 "
                                          "and ons_awe). A full historical run should archive "
                                          "per-release vintages.",
                    },
                )
                records.append(rec)
                bump("recites" if align == "recites" else "describes")
                if max_records and len(records) >= int(max_records):
                    break
            if max_records and len(records) >= int(max_records):
                break
        if max_records and len(records) >= int(max_records):
            break

    # ---- per-family evidence yield: the per-family ACCEPTANCE GATE ----------------------
    # Measured on this build: consumerpriceinflation 2.95 evidence claims/record (channels are
    # the right variants) vs retailsales 0.25 (two of three CDIDs are the wrong variant of the
    # figure the prose quotes). A family whose ev/rec is near zero has a channel-mapping
    # problem, not a text problem -- fix the CDIDs before scaling that family.
    per_fam: dict = {}
    for r in records:
        f = r["meta"]["bulletin_family"]
        d = per_fam.setdefault(f, {"records": 0, "recites": 0, "evidence_claims": 0})
        d["records"] += 1
        d["recites"] += 1 if r.get("alignment") == "recites" else 0
        d["evidence_claims"] += len(r["meta"]["recite_evidence"])
    for f, d in per_fam.items():
        d["evidence_per_record"] = round(d["evidence_claims"] / max(1, d["records"]), 2)
        d["channels_look_verified"] = d["evidence_per_record"] >= 1.0
    _stats["per_family"] = per_fam
    _stats["emitted"] = len(records)
    report = {"dataset": "ons_statistical_bulletins", "stats": _stats,
              "config_snapshot": {"data": {k: v for k, v in cfg["data"].items()},
                                  "series": scfg, "text": tcfg, "output": ocfg}}
    print(json.dumps(_stats, indent=2))
    if args.dry_run:
        print("(dry run -- nothing written)")
        return 0

    out = PKG_ROOT / ocfg["path"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    (PKG_ROOT / ocfg["run_report"]).write_text(json.dumps(report, indent=2))
    sp = PKG_ROOT / ocfg["samples_path"]
    sp.parent.mkdir(parents=True, exist_ok=True)
    with sp.open("w") as fh:
        for r in records[:3]:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(records)} records -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
