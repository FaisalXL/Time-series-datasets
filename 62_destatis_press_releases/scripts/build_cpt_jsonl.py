#!/usr/bin/env python3
"""Build CPT world-knowledge records from Destatis (German Federal Statistical Office) releases.

One record = one Destatis ENGLISH press release's own VERBATIM prose + the trailing multi-channel
window of Germany's harmonised index (HICP) that the release reports.

WHAT MAKES THIS SOURCE UNUSUAL: the prose recites SEVERAL CONSECUTIVE MONTHS of its own series in a
single sentence -- "the inflation rate ... was +2.3% in June 2026 ... after having stood at +2.6% in
May 2026 and +2.9% in April 2026" -- so one record carries multiple independently-checkable claims
against different points of the same paired window.

THE CROSS-SOURCE JOIN, AND WHY IT IS NARROW: Destatis's own series need a registered GENESIS key
(keyless REST verified to return HTTP 405), so the series comes from Eurostat's keyless API for
GERMANY's HICP. That is legitimate only for the release's HARMONISED-index claim: the release quotes
the national CPI *and* the HICP and they genuinely differ (June 2026: CPI +2.3%, HICP +2.4%).
So `\\bCPI\\b`-style national figures must never be credited to an HICP channel. Enforced by
per-channel keywords evaluated on the figure's OWN CLAUSE, plus reject_terms.

Usage:
    python scripts/build_cpt_jsonl.py --config config.example.yaml
"""
from __future__ import annotations

import argparse
import difflib
import html as htmllib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent
sys.path.insert(0, str(PKG_ROOT.parent / "schema"))
from emit import emit_record  # noqa: E402

MONTHS = ["", "january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]

_last = [0.0]
_stats: dict = {}


def bump(k: str, n: int = 1) -> None:
    _stats[k] = _stats.get(k, 0) + n


def fetch(url: str, cache: Path, cfg: dict) -> bytes:
    if cache.exists():
        return cache.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    d = cfg["data"]
    gap = float(d.get("min_interval_s", 2.0))
    hdrs = {"User-Agent": d.get("user_agent", "CPT-research")}
    last = None
    for attempt in range(int(d.get("retries", 4))):
        w = gap - (time.time() - _last[0])
        if w > 0:
            time.sleep(w)
        _last[0] = time.time()
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=float(d.get("timeout_s", 120))) as r:
                body = r.read()
            cache.write_bytes(body)
            return body
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 503):
                time.sleep(gap * (attempt + 2))
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(gap * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url} ({last})")


# --- series (Eurostat, keyless) ---------------------------------------------------------------

def load_channel(ch: dict, cfg: dict, cache: Path) -> tuple[dict, str]:
    q = urllib.parse.urlencode({"format": "JSON", "lang": "EN", "geo": cfg["data"]["geo"],
                                "coicop": ch["coicop"], "unit": ch["unit"]})
    url = cfg["data"]["eurostat_url"].format(dataset=ch["dataset"]) + "?" + q
    raw = fetch(url, cache / "series" / f"{ch['dataset']}_{ch['coicop']}_{ch['unit']}.json", cfg)
    d = json.loads(raw.decode("utf-8", "replace"))
    idx = d["dimension"]["time"]["category"]["index"]
    vals = d.get("value", {})
    out = {}
    for period, i in idx.items():
        v = vals.get(str(i))
        if v is not None and re.match(r"^\d{4}-\d{2}$", period):
            out[period] = float(v)
    label = d["dimension"]["coicop"]["category"]["label"].get(ch["coicop"], ch["coicop"])
    return out, label


def window(series: dict, ref: str, n: int) -> tuple[list, list]:
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


# --- release HTML ------------------------------------------------------------------------------

def strip_tags(frag: str) -> str:
    frag = re.sub(r"(?is)<(script|style|noscript|nav|footer|header)[^>]*>.*?</\1>", " ", frag)
    frag = re.sub(r"(?is)<table.*?</table>", " ", frag)      # the release's own table is numbers
    frag = re.sub(r"(?is)<br\s*/?>", "\n", frag)
    frag = re.sub(r"(?is)</(p|li|h[1-6]|div|tr)>", "\n\n", frag)
    frag = re.sub(r"(?s)<[^>]+>", " ", frag)
    frag = htmllib.unescape(frag)
    frag = re.sub(r"[ \t ]+", " ", frag)
    return re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", frag).strip()


CHROME_RE = re.compile(
    r"(?i)^\s*(press release|no\.\s*\d+|contact|further information|methodological notes?|"
    r"more on this topic|share|print|to the top|© statistisches|data licence|"
    r"you can find|for further|see also|download|shopping cart|newsletter)\b")

REF_MONTH_RE = re.compile(
    r"(?i)\b(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{4})\b")


def reference_month(html_doc: str, text: str) -> str | None:
    """Reference month from the release's own title, e.g. 'Inflation rate at +2.3% in June 2026'.

    Taken from the TITLE rather than the URL: the slug (PE25_262_611) carries a sequence number and
    a subject code but no date, so there is nothing in the URL to trust or mis-trust -- unlike
    FHFA #59 where the URL month was the index month + 2.
    """
    m = re.search(r"(?is)<title>(.*?)</title>", html_doc)
    if m:
        mm = REF_MONTH_RE.search(re.sub(r"\s+", " ", m.group(1)))
        if mm:
            return f"{int(mm.group(2)):04d}-{MONTHS.index(mm.group(1).lower()):02d}"
    mm = REF_MONTH_RE.search(text[:1200])
    if mm:
        return f"{int(mm.group(2)):04d}-{MONTHS.index(mm.group(1).lower()):02d}"
    return None


def clean_paragraphs(text: str, min_words: int) -> list[str]:
    """Real prose only.

    Destatis opens with a machine-styled key-value bullet block ("Consumer price index, June 2026:
    +2.3% on the same month a year earlier") which is verbatim but is a data dump, not prose. A
    word-count floor plus terminal punctuation drops it, the same structural rule that removed
    chart captions in ONS #61.
    """
    out = []
    for p in re.split(r"\n\s*\n", text):
        p = re.sub(r"\s+", " ", p).strip()
        if not p or CHROME_RE.match(p):
            continue
        if len(p.split()) < min_words:
            continue
        if not re.search(r"[.!?][\"')’”]?$", p):
            continue
        out.append(p)
    return out


FIG_RE = re.compile(r"([+\-]?\d+\.\d)\s*%")


def n_figures(s: str) -> int:
    return len(FIG_RE.findall(s))


def select_span(paras: list[str], cap: int, mode: str) -> tuple[str, dict]:
    if not paras:
        return "", {"selector": mode, "n_paragraphs": 0}

    def join(a, b):
        return "\n\n".join(paras[a:b])

    if mode == "head":
        hi = 1
        while hi < len(paras) and len(join(0, hi + 1)) <= cap:
            hi += 1
        return join(0, hi), {"selector": "head", "n_paragraphs": hi}
    best = (None, -1, 0, 0)
    for lo in range(len(paras)):
        for hi in range(lo + 1, len(paras) + 1):
            t = join(lo, hi)
            if len(t) > cap:
                break
            score = n_figures(t) * 1000 + len(t)
            if score > best[1]:
                best = (t, score, lo, hi)
    if best[0] is None:
        cut = paras[0][:cap]
        ms = list(re.finditer(r"(?<=[.!?])\s", cut))
        txt = cut[:ms[-1].start() + 1].strip() if ms else cut.strip()
        return txt, {"selector": mode, "n_paragraphs": 1, "sentence_truncated": True}
    return best[0], {"selector": "numeric_density", "n_paragraphs": best[3] - best[2],
                     "span": [best[2], best[3]], "n_figures": n_figures(best[0])}


# --- evidence ----------------------------------------------------------------------------------

def split_clauses(text: str) -> list[str]:
    # decimal points are NOT sentence ends ("+2.3%") -- require terminator + space + capital
    bounds = [0]
    for m in re.finditer(r'(?<=[.!?])\s+(?=[A-Z"‘“(])|\n{2,}', text):
        bounds.append(m.end())
    bounds.append(len(text))
    out = []
    for a, b in zip(bounds, bounds[1:]):
        out.extend(x for x in text[a:b].split(";") if x.strip())
    return out


def has_kw(clause: str, kws: list[str]) -> bool:
    for k in kws:
        if re.search(r"\b" + re.escape(k) + (r"\b" if k[-1].isalpha() else ""), clause, re.I):
            return True
    return False


def clause_months(clause: str, ref: str) -> list[tuple[str, str]]:
    """Months a figure in this clause may refer to -- taken from the clause's OWN words.

    Destatis reliably names its months ("in June 2026", "after having stood at +2.6% in May 2026"),
    so attribution can be pinned instead of guessed.
    """
    named = [f"{int(y):04d}-{MONTHS.index(mn.lower()):02d}"
             for mn, y in REF_MONTH_RE.findall(clause)]
    if named:
        return [("named_month", k) for k in named]
    return [("reference_month", ref)]


def verify(text: str, ref: str, chans: list[dict]) -> tuple[str, list, int, dict]:
    ev, rej = [], {"no_keyword": 0, "reject_term": 0, "value_mismatch": 0}
    total = 0
    for clause in split_clauses(text):
        figs = list(FIG_RE.finditer(clause))
        total += len(figs)
        for m in figs:
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            targets = clause_months(clause, ref)
            hit = False
            saw_kw = False
            blocked = False
            for ch in chans:
                kws = ch.get("keywords") or []
                if kws and not has_kw(clause, kws):
                    continue
                saw_kw = True
                if any(has_kw(clause, [r]) for r in (ch.get("reject_terms") or [])):
                    blocked = True
                    continue
                for label, key in targets:
                    got = ch["_series"].get(key)
                    if got is None:
                        continue
                    if abs(round(got, 1) - val) < 0.05:
                        ev.append({"figure_pct": val, "unit": ch["name"], "month": key,
                                   "month_source": label, "series_value": got,
                                   "clause": re.sub(r"\s+", " ", clause).strip()[:200]})
                        hit = True
                        break
                if hit:
                    break
            if not hit:
                if not saw_kw:
                    rej["no_keyword"] += 1
                elif blocked:
                    rej["reject_term"] += 1
                else:
                    rej["value_mismatch"] += 1
    return ("recites" if ev else "describes"), ev, total, rej


def deep_set(d, dotted, raw):
    cur = d
    ps = dotted.split(".")
    for p in ps[:-1]:
        cur = cur.setdefault(p, {})
    cur[ps[-1]] = yaml.safe_load(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(PKG_ROOT / "config.example.yaml"))
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())
    for ov in args.set:
        k, _, v = ov.partition("=")
        deep_set(cfg, k.strip(), v.strip())

    t, s, o = cfg["text"], cfg["series"], cfg["output"]
    if t.get("abstractive_summary"):
        print("ERROR: abstractive_summary is blocked -- schema/validate.py has no "
              "`llm_summarized` text_quality value; 'generated' would mislabel a grounded "
              "summary as synthetic. See README.", file=sys.stderr)
        return 2

    cache = PKG_ROOT / cfg["data"].get("cache_dir", ".cache")
    chans = []
    for ch in cfg["data"]["channels"]:
        series, label = load_channel(ch, cfg, cache)
        if not series:
            bump("channel_empty")
            continue
        c = dict(ch)
        c["_series"] = series
        c["_label"] = label
        chans.append(c)
    print(f"channels: {len(chans)}  months: "
          f"{min(len(c['_series']) for c in chans)}-{max(len(c['_series']) for c in chans)}")

    records, seen = [], []
    for rel in cfg["data"]["releases"]:
        url = cfg["data"]["release_url"].format(year=rel["year"], month=rel["month"],
                                                slug=rel["slug"])
        try:
            raw = fetch(url, cache / "releases" / f"{rel['slug']}.html", cfg)
        except Exception as e:
            bump("release_fetch_failed")
            print(f"  ! {rel['slug']}: {e}")
            continue
        doc = raw.decode("utf-8", "replace")
        body = strip_tags(doc)
        ref = reference_month(doc, body)
        if not ref:
            bump("no_reference_month")
            continue
        paras = clean_paragraphs(body, int(t.get("min_words", 18)))
        if not paras:
            bump("no_prose")
            continue
        span, sel = select_span(paras, int(t["max_chars"]), t.get("selector", "numeric_density"))
        if len(span) < int(t["min_chars"]):
            bump("too_short")
            continue
        cap = t.get("max_similarity")
        if cap is not None and seen:
            worst = max(difflib.SequenceMatcher(None, span, p).ratio() for p in seen)
            if worst > float(cap):
                bump("near_duplicate_dropped")
                continue
        seen.append(span)

        wins, keys = [], None
        for c in chans:
            vals, k = window(c["_series"], ref, int(s["window_months"]))
            if len(vals) < int(s["min_points"]) or any(v is None for v in vals):
                continue
            wins.append((c, vals))
            keys = k
        if not wins:
            bump("no_windowed_channels")
            continue

        align, ev, nfig, rej = verify(span, ref, [c for c, _ in wins])
        for k2, v2 in rej.items():
            bump(f"evidence_rejected_{k2}", v2)

        records.append(emit_record(
            text=f"{span}\n\n<ts></ts>",
            timeseries=[{"values": [round(v, 4) for v in vals], "unit": c["name"],
                         "freq": s["freq"]} for c, vals in wins],
            timestamps=keys,
            alignment=align,
            license="cc-by-4.0",
            text_source="first_party_official",
            source=url,
            dataset="destatis_press_releases",
            series_id=f"destatis:{rel['slug']}:{ref}",
            domain="macro",
            region="DE",
            period_start=keys[0],
            period_end=keys[-1],
            meta={
                "true_license": "Destatis press releases: 'Reproduction and distribution, also of "
                                "parts, are permitted provided that the source is mentioned.' "
                                "GENESIS data: Data licence Germany attribution 2.0 (DL-DE-BY-2.0). "
                                "Series via Eurostat (permissive reuse w/ attribution). Tagged "
                                "cc-by-4.0 as closest schema fit.",
                "attribution": "Text: © Statistisches Bundesamt (Destatis). Series: Eurostat.",
                "release_slug": rel["slug"],
                "subject_code": rel["slug"].rsplit("_", 1)[-1],
                "reference_month": ref,
                "series_provenance": "Eurostat keyless dissemination API (Germany HICP) -- NOT "
                                     "Destatis GENESIS, which requires a registered key (keyless "
                                     "REST verified HTTP 405). Legitimate only for the release's "
                                     "HARMONISED-index claim; the national CPI figure differs and "
                                     "is guarded against being credited to an HICP channel.",
                "n_channels": len(wins),
                "n_points": len(keys),
                "text_selection": sel,
                "shipped_chars": len(span),
                "n_figures_in_text": nfig,
                "recite_evidence": ev,
                "evidence_rejected": rej,
                "channels": [{"dataset": c["dataset"], "coicop": c["coicop"], "unit": c["name"],
                              "eurostat_label": c["_label"]} for c, _ in wins],
                "vintage_caveat": "Eurostat carries the CURRENT vintage; the release quotes its "
                                  "contemporaneous vintage, and Destatis revises. Eurostat's German "
                                  "HICP also lags (verified: ends 2025-12, updated 2026-02-06), so "
                                  "the newest releases have no pairable series.",
            },
        ))
        bump(align)

    _stats["emitted"] = len(records)
    _stats["evidence_claims"] = sum(len(r["meta"]["recite_evidence"]) for r in records)
    _stats["evidence_per_record"] = round(_stats["evidence_claims"] / max(1, len(records)), 2)
    print(json.dumps(_stats, indent=2))
    if args.dry_run:
        print("(dry run)")
        return 0
    out = PKG_ROOT / o["path"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    (PKG_ROOT / o["run_report"]).write_text(json.dumps(
        {"dataset": "destatis_press_releases", "stats": _stats, "config_snapshot": cfg}, indent=2))
    sp = PKG_ROOT / o["samples_path"]
    sp.parent.mkdir(parents=True, exist_ok=True)
    with sp.open("w") as fh:
        for r in records[:3]:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(records)} records -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
