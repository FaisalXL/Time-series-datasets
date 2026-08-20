#!/usr/bin/env python3
"""EIA "Today in Energy" -> CPT world-knowledge JSONL.

Text  : the article's own VERBATIM body prose, taken from <div class="tie-article">.
Series: EIA bulk petroleum series (keyless), windowed to the article's publication date.
Align : decided per article by EVIDENCE -- a figure in the prose must match a real value in
        one of the paired channels. Zero matches -> the record is dropped (default).

License: U.S. government public domain (eia.gov/todayinenergy/about.php). No licence gate.
No API key: the Open Data API v2 needs one, the bulk files do not. Same keyless route as #11.

Usage:
    python scripts/build_cpt_jsonl.py --dry-run
    python scripts/build_cpt_jsonl.py
    python scripts/build_cpt_jsonl.py --set output.max_records=null
"""
from __future__ import annotations

import argparse
import html
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

SCALE_WORDS = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}
# number, optional scale word. Captures "13.6 million", "350,000", "13,586", "6.3"
FIGURE_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|trillion)?", re.I
)


# --------------------------------------------------------------------------- config / http
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
    """Cached, paced, retrying HTTP GET."""

    def __init__(self, d: Dict[str, Any]):
        self.cache = ROOT / d["cache_dir"]
        self.cache.mkdir(parents=True, exist_ok=True)
        self.ua = d["user_agent"]
        self.timeout = int(d["timeout_s"])
        self.retries = int(d.get("retries", 3))
        self.min_interval = float(d.get("min_interval_s", 0.0))
        self._last = 0.0

    def _pace(self) -> None:
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def get(self, url: str, cache_name: str, binary: bool = False):
        path = self.cache / cache_name
        if path.exists():
            return path.read_bytes() if binary else path.read_text(encoding="utf-8", errors="replace")
        last: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                self._pace()
                req = urllib.request.Request(url, headers={"User-Agent": self.ua})
                raw = urllib.request.urlopen(req, timeout=self.timeout).read()
                path.write_bytes(raw)
                return raw if binary else raw.decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"GET failed after {self.retries} tries: {url} ({last})")


# --------------------------------------------------------------------------- series (bulk)
def load_bulk_series(fetch: Fetcher, bulk_urls: Dict[str, str], wanted: set) -> Dict[str, Dict[str, Any]]:
    """Stream each bulk zip once, keeping only the series the config asked for."""
    out: Dict[str, Dict[str, Any]] = {}
    for tag, url in bulk_urls.items():
        raw = fetch.get(url, f"bulk_{tag}.zip", binary=True)
        zf = zipfile.ZipFile(io.BytesIO(raw))
        member = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
        with zf.open(member) as fh:
            for line in io.TextIOWrapper(fh, encoding="utf-8"):
                if '"series_id"' not in line:
                    continue
                rec = json.loads(line)
                sid = rec.get("series_id")
                if sid in wanted:
                    pts = [(str(p), v) for p, v in (rec.get("data") or []) if v is not None]
                    pts.sort()
                    out[sid] = {"name": rec.get("name"), "units": rec.get("units"),
                                "freq": rec.get("f"), "points": pts}
    missing = wanted - set(out)
    if missing:
        # Loud, not silent: a mistyped series_id must never degrade into an empty channel.
        raise RuntimeError(f"series_id(s) not found in bulk files: {sorted(missing)}")
    return out


def period_key(period: str) -> str:
    """Normalise an EIA period ('2025', '202605', '20260731') to a sortable string."""
    return period


def window_ending(points: List[Tuple[str, float]], cutoff: str, n: int) -> List[Tuple[str, float]]:
    """Last `n` points at or before `cutoff` (an EIA-style period string)."""
    elig = [p for p in points if p[0] <= cutoff]
    return elig[-n:] if elig else []


# --------------------------------------------------------------------------- article side
def article_ids_for_year(fetch: Fetcher, url_tpl: str, year: int) -> List[str]:
    page = fetch.get(url_tpl.format(year=year), f"archive_{year}.html")
    seen, ids = set(), []
    for m in re.finditer(r"detail\.php\?id=(\d+)", page):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            ids.append(m.group(1))
    return ids


def parse_article(page: str, container: str, text_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    idx = page.find(container)
    if idx < 0:
        return None
    seg = page[idx: idx + 60000]
    seg = re.sub(r"<table.*?</table>", "", seg, flags=re.S)

    m_title = re.search(r"<h1[^>]*>(.*?)</h1>", seg, re.S)
    title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m_title.group(1)))).strip() if m_title else ""
    m_date = re.search(r'class="date"[^>]*>([^<]+)<', seg)
    date_text = m_date.group(1).strip() if m_date else ""

    paras = []
    for raw in re.findall(r"<p[^>]*>(.*?)</p>", seg, re.S):
        p = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw))).strip()
        if len(p) < 60:
            continue
        if any(p.startswith(pre) for pre in text_cfg.get("drop_paragraphs_starting", [])):
            continue
        for pre in text_cfg.get("strip_prefixes", []):
            if p.startswith(pre):
                # Caption is glued to the first sentence of real prose; cut at the first
                # capitalised sentence start after the caption.
                cut = re.search(r"(?<=\s)[A-Z]", p[len(pre):])
                p = p[len(pre) + cut.start():].strip() if cut else ""
        if len(p) >= 60:
            paras.append(p)
    if not paras:
        return None
    return {"title": title, "date_text": date_text, "paragraphs": paras}


MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june",
     "july", "august", "september", "october", "november", "december"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})


def parse_date(text: str) -> Optional[str]:
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text.strip())
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"


def cutoff_for(freq: str, iso_date: str) -> str:
    """EIA period string matching a channel's frequency, at the article's date."""
    y, mth, day = iso_date.split("-")
    if freq == "1y":
        return y
    if freq == "1M":
        return f"{y}{mth}"
    return f"{y}{mth}{day}"      # weekly/daily periods are YYYYMMDD


# --------------------------------------------------------------------------- evidence
def figures_in(text: str) -> List[Tuple[float, int]]:
    """(absolute magnitude, character offset) of every numeric token; scale words honoured."""
    out = []
    for m in FIGURE_RE.finditer(text):
        num, word = m.group(1), m.group(2)
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        out.append((v * SCALE_WORDS[word.lower()] if word else v, m.start()))
    return out


def count_evidence(text: str, channels: List[Dict[str, Any]], values: Dict[str, List[float]],
                   ecfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Figures in the prose that match a paired channel's RECENT values, near that channel's
    own keyword.

    Two guards, both learned the hard way:
      * RECENCY -- only the last `match_window` points are eligible. Matching against all 36
        points x N channels gives ~150 targets; at 1% tolerance a coincidental hit is close to
        free, and a coincidental hit is indistinguishable from a real recite.
      * PROXIMITY -- the figure must sit within `proximity_chars` of one of the CHANNEL's own
        keywords. This is the same cross-channel false-match fix that ONS #61 needed after
        RBNZ #60 shipped one. Without it a hydropower article matched a crude-trade channel.
    """
    tol = float(ecfg["tolerance"])
    mwin = int(ecfg.get("match_window", 8))
    prox = int(ecfg.get("proximity_chars", 120))
    low = text.lower()
    figs = figures_in(text)
    hits = []
    for ch in channels:
        scale = float(ch.get("scale", 1))
        kws = [k.lower() for k in ch.get("keywords", [])]
        spans = [m.start() for k in kws for m in re.finditer(re.escape(k), low)]
        if kws and not spans:
            continue                       # this channel is not discussed at all -> no evidence
        # newest-first: an article's headline claim is about the latest point, so preferring
        # recent values makes the recorded evidence the one a reader would actually check.
        vals = list(reversed([v for v in (values.get(ch["unit"]) or []) if v is not None][-mwin:]))
        best = None
        for v in vals:
            target = abs(v * scale)
            if target == 0:
                continue
            for f, pos in figs:
                if abs(f - target) / target > tol:
                    continue
                if kws and not any(abs(pos - s) <= prox for s in spans):
                    continue               # right number, wrong neighbourhood
                best = {"unit": ch["unit"], "series_value": v, "prose_figure": f}
                break
            if best:
                break
        if best:
            hits.append(best)
    return hits


def score_bundle(text: str, bundle: Dict[str, Any]) -> int:
    low = text.lower()
    return sum(low.count(k.lower()) for k in bundle.get("keywords", []))


# --------------------------------------------------------------------------- build

def iso_period(code) -> str:
    """EIA period code -> ISO-8601 at its native granularity. `2025` and `2025-06` are both
    valid ISO-8601; padding either to `2025-01-01` would assert a day the source never gave."""
    c = str(code)
    if re.fullmatch(r"\d{6}", c):
        return f"{c[:4]}-{c[4:]}"
    if re.fullmatch(r"\d{8}", c):
        return f"{c[:4]}-{c[4:6]}-{c[6:]}"
    return c

def build(cfg: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    d, scfg, tcfg, ecfg, ocfg = cfg["data"], cfg["series"], cfg["text"], cfg["evidence"], cfg["output"]
    if tcfg.get("abstractive_summary"):
        raise SystemExit(
            "text.abstractive_summary is not supported: schema/validate.py allows only "
            "text_quality in {'real','generated'} -- there is no `llm_summarized` value yet."
        )
    fetch = Fetcher(d)

    wanted = {ch["series_id"] for b in d["bundles"] for ch in b["channels"]}
    series = load_bulk_series(fetch, d["bulk_urls"], wanted)
    print(f"[series] loaded {len(series)} channels from bulk", flush=True)

    ids: List[Tuple[int, str]] = []
    for year in range(int(d["last_year"]), int(d["first_year"]) - 1, -1):
        for aid in article_ids_for_year(fetch, d["archive_url"], year):
            ids.append((year, aid))
    print(f"[articles] enumerated {len(ids)} across {d['first_year']}-{d['last_year']}", flush=True)

    stats = {"articles_seen": 0, "no_body": 0, "no_date": 0, "no_bundle": 0,
             "short_series": 0, "no_evidence": 0, "too_short": 0, "emitted": 0}
    cap = ocfg.get("max_records")
    records: List[Dict[str, Any]] = []

    for _year, aid in ids:
        if cap is not None and len(records) >= int(cap):
            break
        stats["articles_seen"] += 1
        try:
            page = fetch.get(d["article_url"].format(article_id=aid), f"article_{aid}.html")
        except RuntimeError:
            stats["no_body"] += 1
            continue
        art = parse_article(page, d["body_container"], tcfg)
        if not art:
            stats["no_body"] += 1
            continue
        iso = parse_date(art["date_text"])
        if not iso:
            stats["no_date"] += 1
            continue

        body = "\n\n".join(art["paragraphs"])
        blob = art["title"] + " " + body
        bundle = max(d["bundles"], key=lambda b: score_bundle(blob, b))
        # The TITLE must also hit the bundle, not just the body. Body-only matching let a
        # hydropower article win `crude_trade` on a passing mention of "exports".
        if score_bundle(blob, bundle) < int(ecfg.get("min_bundle_score", 1)) or (
            ecfg.get("require_title_keyword", False) and score_bundle(art["title"], bundle) == 0
        ):
            stats["no_bundle"] += 1
            continue

        cut = cutoff_for(bundle["freq"], iso)
        chans, values, periods = [], {}, None
        short = False
        for ch in bundle["channels"]:
            pts = window_ending(series[ch["series_id"]]["points"], cut, int(scfg["window"]))
            if len(pts) < int(scfg["min_points"]):
                short = True
                break
            chans.append({"values": [v for _, v in pts], "unit": ch["unit"], "freq": bundle["freq"]})
            values[ch["unit"]] = [v for _, v in pts]
            periods = [p for p, _ in pts]
        if short or not chans:
            stats["short_series"] += 1
            continue

        # trim to the text cap, keeping whole leading paragraphs (100% verbatim)
        kept, total = [], 0
        for p in art["paragraphs"]:
            if total + len(p) > int(tcfg["max_chars"]) and kept:
                break
            kept.append(p)
            total += len(p) + 2
        text_body = "\n\n".join(kept)
        if len(text_body) < int(tcfg["min_chars"]):
            stats["too_short"] += 1
            continue

        ev = count_evidence(text_body, bundle["channels"], values, ecfg)
        if len(ev) < int(ecfg["min_evidence"]):
            if not ecfg.get("allow_describes"):
                stats["no_evidence"] += 1
                continue
            alignment = "describes"
        else:
            alignment = "recites"

        rec = emit_record(
            text=text_body + "\n\n<ts></ts>",
            timeseries=chans,
            alignment=alignment,
            license="public-domain-us-gov",
            source=d["article_url"].format(article_id=aid),
            dataset="eia_today_in_energy",
            series_id=f"{bundle['name']}_{periods[0]}_{periods[-1]}",
            domain="energy",
            region="US",
            # EIA period codes are unpunctuated (`2025`, `202601`, `20260717`). `series_id`
            # keeps them verbatim so existing ids stay stable, but `period_start`/`period_end`
            # are schema date fields and shipped non-ISO for all 132 records -- and with three
            # different shapes inside one package. Punctuate to the granularity the code
            # already has; do NOT pad to a full date, which would invent precision the source
            # does not have (an annual series does not start on 1 January).
            period_start=iso_period(periods[0]),
            period_end=iso_period(periods[-1]),
            meta={
                "article_id": aid,
                "article_title": art["title"],
                "published": iso,
                "bundle": bundle["name"],
                "eia_series": [c["series_id"] for c in bundle["channels"]],
                "bulk_source": list(d["bulk_urls"].values()),
                "evidence": ev,
                "publisher": "U.S. Energy Information Administration",
                "attribution": "Source: U.S. Energy Information Administration, Today in Energy",
            },
        )
        records.append(rec)
        stats["emitted"] += 1

    report = {"stats": stats, "n_bundles": len(d["bundles"]), "config_snapshot": cfg, "dry_run": dry_run}
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


if __name__ == "__main__":
    main()
