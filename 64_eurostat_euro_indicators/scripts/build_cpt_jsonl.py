#!/usr/bin/env python3
"""Eurostat "Euro indicators" news releases -> CPT world-knowledge JSONL.

Text  : the release's own VERBATIM headline prose (tables stripped, methodology tail cut).
Series: the Eurostat dissemination API (keyless), windowed to the release's reference period.
Align : decided per release by EVIDENCE -- a figure in the prose must match a real value in a
        paired channel, near that channel's own keyword.

Two Eurostat-specific hazards this script exists to handle:
  1. `web/products-euro-indicators` redirects to the EU Login wall; `web/main/news/...` does not.
  2. A geo code a dataset does not carry returns HTTP 200 with an EMPTY `value` object -- a
     silent zero. Euro-area codes differ per dataset (une_rt_m has only EA21; prc_hicp_manr has
     no EA21 at all), so the code is RESOLVED per dataset and an empty result RAISES.

Usage:
    python scripts/build_cpt_jsonl.py --dry-run
    python scripts/build_cpt_jsonl.py
    python scripts/build_cpt_jsonl.py --set output.max_records=null
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

SCALE_WORDS = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
FIGURE_RE = re.compile(r"([-\u2212\u2013]?\d[\d\s,]*(?:\.\d+)?)\s*(thousand|million|billion)?", re.I)


def load_config(path: Path, overrides: List[str]) -> Dict[str, Any]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    for ov in overrides:
        key, _, raw = ov.partition("=")
        node = cfg
        parts = key.split(".")
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = yaml.safe_load(raw)
    return cfg


class Fetcher:
    def __init__(self, d: Dict[str, Any]):
        self.cache = ROOT / d["cache_dir"]
        self.cache.mkdir(parents=True, exist_ok=True)
        self.ua = d["user_agent"]
        self.timeout = int(d["timeout_s"])
        self.retries = int(d.get("retries", 3))
        self.min_interval = float(d.get("min_interval_s", 0.0))
        self._last = 0.0

    def get(self, url: str, cache_name: str) -> str:
        path = self.cache / cache_name
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        last: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                wait = self.min_interval - (time.time() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.time()
                req = urllib.request.Request(url, headers={"User-Agent": self.ua})
                raw = urllib.request.urlopen(req, timeout=self.timeout).read().decode("utf-8", "replace")
                if "ecas.ec.europa.eu" in raw[:2000] or "EU Login" in raw[:3000]:
                    raise RuntimeError("hit the EU Login wall -- wrong URL form")
                path.write_text(raw, encoding="utf-8")
                return raw
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(2.0 * (attempt + 1))
        raise RuntimeError(f"GET failed after {self.retries} tries: {url} ({last})")


# --------------------------------------------------------------------------- series
def api_get(fetch: Fetcher, api_tpl: str, dataset: str, params: Dict[str, str]) -> Dict[str, Any]:
    qs = urllib.parse.urlencode({"format": "JSON", **params})
    url = f"{api_tpl.format(dataset=dataset)}?{qs}"
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{dataset}_{qs}")[:180] + ".json"
    return json.loads(fetch.get(url, key))


def resolve_geo(fetch: Fetcher, api_tpl: str, dataset: str, prefs: List[str]) -> str:
    """First preferred geo code the dataset actually carries.

    Filtering on an absent code returns 200 + an empty `value` object, so the code must be
    resolved against the dataset's own geo dimension rather than assumed.
    """
    d = api_get(fetch, api_tpl, dataset, {"lastTimePeriod": "1"})
    have = set(d["dimension"]["geo"]["category"]["index"])
    for code in prefs:
        if code in have:
            return code
    raise RuntimeError(f"{dataset}: none of {prefs} present; dataset carries {sorted(have)[:12]}")


def fetch_series(fetch: Fetcher, api_tpl: str, dataset: str, params: Dict[str, str]) -> List[Tuple[str, float]]:
    d = api_get(fetch, api_tpl, dataset, params)
    vals = d.get("value") or {}
    if not vals:
        raise RuntimeError(
            f"{dataset} {params}: API returned 200 with an EMPTY value object. This is the "
            "silent-empty trap -- a filter code the dataset does not carry. Refusing to emit."
        )
    idx = d["dimension"]["time"]["category"]["index"]
    inv = {i: t for t, i in idx.items()}
    return [(inv[i], v) for i, v in sorted(((int(k), v) for k, v in vals.items()))]


# --------------------------------------------------------------------------- release text
def parse_release(page: str, tcfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1)))).strip() if m else ""
    title = title.split(" - Eurostat")[0].split(" - Euro indicators")[0].strip()

    # Tables first: ~84% of what a naive <p> scrape returns is table content.
    notab = re.sub(r"<table.*?</table>", "", page, flags=re.S)
    paras: List[str] = []
    for raw in re.findall(r"<p[^>]*>(.*?)</p>", notab, re.S):
        p = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw))).strip()
        if len(p) < 50 or "{" in p or "etrans" in p:
            continue
        if any(p.startswith(s) for s in tcfg.get("stop_markers", [])):
            break                                   # methodology tail begins here
        if any(s in p for s in tcfg.get("drop_paragraphs_containing", [])):
            continue
        paras.append(p)
    return {"title": title, "paragraphs": paras} if paras else None


def reference_period(text: str) -> Optional[str]:
    """Latest 'Month YYYY' named in the prose -> the release's reference month."""
    months = {m: i for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"], 1)}
    found = [(int(y), months[mn]) for mn, y in re.findall(
        r"\b(" + "|".join(months) + r")\s+(\d{4})\b", text)]
    if not found:
        return None
    y, mth = max(found)
    return f"{y:04d}-{mth:02d}"


# --------------------------------------------------------------------------- evidence
def figures_in(text: str) -> List[Tuple[float, int]]:
    out = []
    for m in FIGURE_RE.finditer(text):
        num = m.group(1).replace(",", "").replace(" ", "")
        num = num.replace("\u2212", "-").replace("\u2013", "-")   # minus sign / en-dash
        if not num or num in (".", "-"):
            continue
        try:
            v = float(num)
        except ValueError:
            continue
        w = m.group(2)
        out.append((v * SCALE_WORDS[w.lower()] if w else v, m.start()))
    return out


DOWN_WORDS = ("down", "fell", "decreas", "declin", "lower", "negative", "contract")
UP_WORDS = ("up ", "rose", "increas", "higher", "grew", "growth")


def _clause(text: str, pos: int, span: int) -> str:
    """The lead-in before a figure -- where the modifier that qualifies it lives
    ("...the unemployment rate for women was 6.4%")."""
    start = max(0, pos - span)
    cut = text.rfind(". ", start, pos)
    return text[(cut + 1) if cut > start else start: pos].lower()


def count_evidence(text: str, channels: List[Dict[str, Any]], values: Dict[str, List[float]],
                   ecfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Figure matches, with three guards. Each exists because it caught a real false match.

    * PROXIMITY  -- the figure must sit near the channel's own keywords.
    * MODIFIERS  -- a euro-indicator release discusses far more series than any channel set
      holds ("for women", "for men", "young persons"). If a disqualifying modifier appears in
      the figure's own clause and is NOT part of this channel's identity, the figure is left
      UNATTRIBUTED instead of credited to the headline channel. Without this, "the
      unemployment rate for women was 6.4%" was credited to the euro-area TOTAL rate (6.3),
      because 6.4 also happened to be a real value earlier in the window. ev=8 looked perfect
      and several hits were false.
    * SIGN      -- for channels that legitimately go negative, a "down/fell/decreased" clause
      must not be matched to a positive stored value (and vice versa). The prose carries the
      sign in words, the digits do not.
    """
    tol = float(ecfg["tolerance"])
    mwin = int(ecfg.get("match_window", 6))
    prox = int(ecfg.get("proximity_chars", 140))
    disq = [t.lower() for t in ecfg.get("disqualifying_terms", [])]
    low = text.lower()
    figs = figures_in(text)
    hits = []
    for ch in channels:
        scale = float(ch.get("scale", 1))
        kws = [k.lower() for k in ch.get("keywords", [])]
        identity = " ".join(kws) + " " + ch["unit"].lower()
        spans = [m.start() for k in kws for m in re.finditer(re.escape(k), low)]
        if kws and not spans:
            continue
        vals = list(reversed([v for v in (values.get(ch["unit"]) or []) if v is not None][-mwin:]))
        hit = None
        signed = bool(ch.get("signed"))
        for v in vals:
            target = v * scale if signed else abs(v * scale)
            if target == 0:
                continue
            for f, pos in figs:
                cand = f if signed else abs(f)
                if abs(cand - target) / abs(target) > tol:
                    continue
                if kws and not any(abs(pos - s) <= prox for s in spans):
                    continue
                cl = _clause(text, pos, prox)
                # modifier guard (word-boundary: "men" must not fire inside "women")
                if any(re.search(rf"\b{re.escape(t)}\b", cl) and t not in identity for t in disq):
                    continue
                # sign guard
                if ch.get("signed"):
                    if any(w in cl for w in DOWN_WORDS) and v > 0:
                        continue
                    if any(w in cl for w in UP_WORDS) and v < 0:
                        continue
                hit = {"unit": ch["unit"], "series_value": v, "prose_figure": f}
                break
            if hit:
                break
        if hit:
            hits.append(hit)
    return hits


def score(text: str, keywords: List[str]) -> int:
    low = text.lower()
    return sum(low.count(k.lower()) for k in keywords)


# --------------------------------------------------------------------------- build
def build(cfg: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    d, scfg, tcfg, ecfg, ocfg = cfg["data"], cfg["series"], cfg["text"], cfg["evidence"], cfg["output"]
    if tcfg.get("abstractive_summary"):
        raise SystemExit("text.abstractive_summary unsupported: no `llm_summarized` in the schema vocab.")
    fetch = Fetcher(d)

    listing = fetch.get(d["listing_url"], "listing_p1.html")
    codes: List[str] = []
    for m in re.finditer(r'href="https://ec\.europa\.eu/eurostat/product\?code=([^"]+)"', listing):
        if m.group(1) not in codes:
            codes.append(m.group(1))
    codes += [c for c in d.get("extra_codes", []) if c not in codes]
    total = re.search(r"out of ([\d,]+) results", listing)
    print(f"[listing] {len(codes)} codes on page 1; server reports {total.group(1) if total else '?'} total",
          flush=True)

    # Resolve each family's geo codes + series once.
    fam_series: Dict[str, Dict[str, Any]] = {}
    for fam in d["families"]:
        ds = fam["dataset"]
        ea = resolve_geo(fetch, d["api_url"], ds, d["euro_area_preference"])
        eu = resolve_geo(fetch, d["api_url"], ds, d["eu_preference"])
        chans = {}
        for ch in fam["channels"]:
            geo = ea if ch["geo"] == "EA" else eu
            chans[ch["unit"]] = fetch_series(fetch, d["api_url"], ds, {**ch["filters"], "geo": geo})
        fam_series[fam["name"]] = {"geo_ea": ea, "geo_eu": eu, "channels": chans}
        print(f"[series] {fam['name']:22s} {ds:14s} EA={ea:9s} EU={eu:10s} {len(chans)} channels", flush=True)

    stats = {"codes_seen": 0, "no_body": 0, "no_family": 0, "no_period": 0,
             "short_series": 0, "no_evidence": 0, "too_short": 0, "emitted": 0}
    cap = ocfg.get("max_records")
    records: List[Dict[str, Any]] = []

    for code in codes:
        if cap is not None and len(records) >= int(cap):
            break
        stats["codes_seen"] += 1
        try:
            page = fetch.get(d["release_url"].format(code=code), f"rel_{code}.html")
        except RuntimeError:
            stats["no_body"] += 1
            continue
        rel = parse_release(page, tcfg)
        if not rel:
            stats["no_body"] += 1
            continue
        body = "\n\n".join(rel["paragraphs"])
        blob = rel["title"] + " " + body
        fam = max(d["families"], key=lambda f: score(blob, f["keywords"]))
        if score(rel["title"], fam["keywords"]) == 0:
            stats["no_family"] += 1
            continue
        period = reference_period(body) or reference_period(rel["title"])
        if not period:
            stats["no_period"] += 1
            continue

        fs = fam_series[fam["name"]]
        chans, values, periods = [], {}, None
        short = False
        for ch in fam["channels"]:
            pts = [p for p in fs["channels"][ch["unit"]] if p[0] <= period][-int(scfg["window"]):]
            if len(pts) < int(scfg["min_points"]):
                short = True
                break
            chans.append({"values": [v for _, v in pts], "unit": ch["unit"], "freq": scfg["freq"]})
            values[ch["unit"]] = [v for _, v in pts]
            periods = [p for p, _ in pts]
        if short or not chans:
            stats["short_series"] += 1
            continue

        kept, tot = [], 0
        for p in rel["paragraphs"]:
            if tot + len(p) > int(tcfg["max_chars"]) and kept:
                break
            kept.append(p)
            tot += len(p) + 2
        text_body = "\n\n".join(kept)
        if len(text_body) < int(tcfg["min_chars"]):
            stats["too_short"] += 1
            continue

        ev = count_evidence(text_body, fam["channels"], values, ecfg)
        if len(ev) < int(ecfg["min_evidence"]):
            if not ecfg.get("allow_describes"):
                stats["no_evidence"] += 1
                continue
            alignment = "describes"
        else:
            alignment = "recites"

        records.append(emit_record(
            text=text_body + "\n\n<ts></ts>",
            timeseries=chans,
            alignment=alignment,
            license="cc-by-4.0",
            source=d["release_url"].format(code=code),
            dataset="eurostat_euro_indicators",
            series_id=f"{fam['dataset']}_{periods[0]}_{periods[-1]}",
            domain="macro",
            region="EU",
            period_start=periods[0],
            period_end=periods[-1],
            meta={
                "release_code": code,
                "release_title": rel["title"],
                "reference_period": period,
                "family": fam["name"],
                "eurostat_dataset": fam["dataset"],
                "geo_euro_area": fs["geo_ea"],
                "geo_eu": fs["geo_eu"],
                "evidence": ev,
                "publisher": "Eurostat",
                "attribution": "Source: Eurostat",
            },
        ))
        stats["emitted"] += 1

    report = {"stats": stats, "families": [f["name"] for f in d["families"]],
              "geo_resolved": {k: {"EA": v["geo_ea"], "EU": v["geo_eu"]} for k, v in fam_series.items()},
              "config_snapshot": cfg, "dry_run": dry_run}
    if not dry_run:
        out = ROOT / ocfg["path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        smp = ROOT / ocfg["samples_path"]
        smp.parent.mkdir(parents=True, exist_ok=True)
        with smp.open("w", encoding="utf-8") as fh:
            for r in records[:3]:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        (ROOT / ocfg["run_report"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.example.yaml"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--set", action="append", default=[], dest="overrides")
    args = ap.parse_args()
    report = build(load_config(Path(args.config), args.overrides), args.dry_run)
    print(json.dumps(report["stats"], indent=2))
    print(json.dumps(report["geo_resolved"], indent=2))


if __name__ == "__main__":
    main()
