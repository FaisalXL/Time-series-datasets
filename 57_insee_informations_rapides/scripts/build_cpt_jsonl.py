#!/usr/bin/env python3
"""Build CPT world-knowledge records from INSEE "Informations Rapides" statistical press releases.

One record = one release's own VERBATIM ENGLISH narrative (the reciting prose INSEE writes to
accompany a statistical release) paired with a trailing within-indicator window of the exact
series the narrative recites. Anchor indicator = the Consumer Price Index (the French analogue of
the already-built BLS CPI #08 / StatCan #52 / ABS #53); other INSEE narrative indicators
(industrial production, GDP, household consumption, PPI, ...) are addable as further `Indicator`
config entries -- the discovery/extraction/pairing pipeline is shared.

ENGLISH-ONLY (hard requirement): every record's text is INSEE's own English edition. The pipeline
(a) only ever fetches the `/en/` URL, and (b) runs an explicit English-language guard on the
extracted prose and DROPS any release whose English edition doesn't actually exist (INSEE does not
publish an English edition for every release) -- see `_is_english`. No French text can leak in, and
nothing is translated or synthesized: the text is 100% verbatim source English.

Series: INSEE BDM (Banque de Données Macro-économiques) via its open SDMX endpoint
`https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/{idbank}` -- KEYLESS, no token, returns the raw
index series. (The newer portail-api.insee.fr needs a free OAuth token; the legacy SDMX endpoint
used here does not.)

Text discovery: INSEE's site is a JS SPA (no server-side listing / sitemap / RSS), so release IDs
are enumerated from the Wayback Machine CDX index of `insee.fr/en/statistiques/` (~6,800 English
release IDs archived 2018->), then each candidate is fetched and kept only if its title matches the
target indicator AND its body passes the English guard. For a fast/offline demo, `config.seed_ids`
pins a known ID list and skips CDX.

Usage:
    python scripts/build_cpt_jsonl.py --config config.example.yaml
    python scripts/build_cpt_jsonl.py --config config.example.yaml --set output.max_records=4
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
SDMX = "https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/{idbank}"
INSEE_EN = "https://www.insee.fr/en/statistiques/{id}"
CDX = "https://web.archive.org/cdx/search/cdx"

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"]) if m}

# --- English-language guard --------------------------------------------------------------------
# INSEE serves an English edition only for some releases. The guard keeps ONLY genuinely-English
# prose so no French can ever enter the corpus.
_EN_MARKERS = ("over one month", "year on year", "consumer price", "prices of", " rose by ",
               " increased by ", " fell by ", "compared to", "over one year", "seasonally adjusted")
_FR_MARKERS = ("sur un mois", "sur un an", "les prix", "à la consommation", "en glissement",
               "des prix", "par rapport", "hausse de", "en moyenne", "corrigé des variations")


def _is_english(text: str) -> bool:
    t = text.lower()
    en = sum(t.count(m) for m in _EN_MARKERS)
    fr = sum(t.count(m) for m in _FR_MARKERS)
    return en >= 3 and en > 2 * fr


# --- HTTP --------------------------------------------------------------------------------------
def _http_get(url: str, timeout: int = 30, retries: int = 3) -> bytes | None:
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return resp.read()
                return None
        except urllib.error.HTTPError:
            return None
        except Exception:
            if attempt == retries:
                return None
            time.sleep(2 * (attempt + 1))
    return None


# --- Series ------------------------------------------------------------------------------------
def fetch_series(idbank: str) -> dict[str, float]:
    """{'YYYY-MM': value} for one INSEE BDM idbank, via the keyless SDMX endpoint."""
    raw = _http_get(SDMX.format(idbank=idbank), timeout=40)
    if not raw:
        raise RuntimeError(f"SDMX fetch failed for idbank {idbank}")
    x = raw.decode("utf-8", "replace")
    return {t: float(v) for t, v in
            re.findall(r'TIME_PERIOD="([0-9-]+)"\s+OBS_VALUE="([0-9.-]+)"', x)}


def _window(end_month: str, n: int) -> list[str]:
    y, m = map(int, end_month.split("-"))
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y -= 1
            m = 12
    return list(reversed(out))


# --- Text discovery + extraction ---------------------------------------------------------------
def cdx_enumerate_ids(year_from: int, year_to: int) -> list[str]:
    """Wayback CDX -> unique INSEE English /statistiques/{id} release IDs in a year range."""
    q = (f"{CDX}?url=insee.fr/en/statistiques/&matchType=prefix&output=json"
         f"&collapse=urlkey&filter=statuscode:200&fl=original&limit=100000"
         f"&from={year_from}&to={year_to}")
    raw = _http_get(q, timeout=60)
    if not raw:
        return []
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return []
    ids = []
    seen = set()
    for row in data[1:]:
        m = re.search(r"/statistiques/(\d+)", row[0])
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            ids.append(m.group(1))
    return ids


def fetch_release(rid: str) -> str | None:
    raw = _http_get(INSEE_EN.format(id=rid), timeout=30)
    return raw.decode("utf-8", "replace") if raw else None


def parse_title(h: str) -> str:
    m = re.search(r"<title>(.*?)</title>", h, re.S)
    return unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""


def reference_month(title: str) -> str | None:
    """'In July 2025, consumer prices...' -> '2025-07'. English titles only."""
    m = re.match(r"\s*In ([A-Za-z]+)\s+(\d{4})", title)
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).lower())
    return f"{int(m.group(2)):04d}-{mon:02d}" if mon else None


def extract_narrative(h: str) -> str:
    """Verbatim English narrative paragraphs. Collection STARTS at the first paragraph that looks
    like reciting content -- >=40 chars, contains a digit, and mentions a price/CPI/inflation/index
    keyword -- rather than a fixed 'In {Month}...' opener, because the provisional (flash) releases
    lead with 'Over a year, the CPI should rise by...' instead, and a fixed opener silently dropped
    them. Nav/cookie/methodology/data-link tails are still filtered out."""
    out, started = [], False
    kw = re.compile(r"(consumer price|\bCPI\b|inflation|\bprices?\b|\bindex\b|harmonised)", re.I)
    for p in re.findall(r"<p[^>]*>(.*?)</p>", h, re.S):
        # unescape FIRST so HTML nbsp entities become \xa0, then collapse all whitespace
        # (incl. \xa0) to plain spaces -- normalizes formatting only, no word/number is altered.
        t = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", p))).strip()
        if not t:
            continue
        if not started:
            if len(t) >= 40 and re.search(r"\d", t) and kw.search(t):
                started = True
            else:
                continue
        if re.match(r"^Time series", t):
            continue
        if re.search(r"(©|cookie|newsletter|Read more|methodolog|To cite|Warning:|Publication|"
                     r"available in|Download|Insee$)", t, re.I):
            continue
        if len(t) < 40:
            continue
        if t not in out:
            out.append(t)
    return "\n".join(out)


# --- Build -------------------------------------------------------------------------------------
def build(cfg: dict) -> list[dict]:
    ind = cfg["indicator"]
    title_needle = ind["title_contains"].lower()
    idbanks = ind["idbanks"]                       # {unit: idbank}
    window = ind.get("window_months", 24)
    min_chars = cfg.get("min_text_chars", 300)
    max_records = (cfg.get("output") or {}).get("max_records")

    series = {unit: fetch_series(idbank) for unit, idbank in idbanks.items()}

    seed = cfg.get("seed_ids")
    if seed:
        candidate_ids = [str(s) for s in seed]
        print(f"Using {len(candidate_ids)} seed IDs (CDX discovery skipped).", file=sys.stderr)
    else:
        candidate_ids = cdx_enumerate_ids(cfg.get("year_from", 2018), cfg.get("year_to", 2026))
        print(f"CDX enumerated {len(candidate_ids)} candidate release IDs.", file=sys.stderr)

    by_month: dict[str, dict] = {}   # reference_month -> chosen record fields (prefer definitive)
    report = {"candidates": len(candidate_ids), "not_target": 0, "not_english": 0,
              "no_ref_month": 0, "short_text": 0, "kept": 0}

    for rid in candidate_ids:
        h = fetch_release(rid)
        if not h:
            continue
        title = parse_title(h)
        if title_needle not in title.lower():
            report["not_target"] += 1
            continue
        ref = reference_month(title)
        if not ref:
            report["no_ref_month"] += 1          # non-English or non-standard title
            continue
        narrative = extract_narrative(h)
        if not _is_english(narrative):
            report["not_english"] += 1           # /en/ page without a real English edition
            continue
        if len(narrative) < min_chars:
            report["short_text"] += 1
            continue
        definitive = "over one month" in title.lower()   # richer than the y/y-only flash estimate
        prev = by_month.get(ref)
        if prev and prev["_definitive"] and not definitive:
            continue                              # keep the definitive already held for this month
        by_month[ref] = {"rid": rid, "title": title, "narrative": narrative,
                         "_definitive": definitive}
        print(f"  kept {ref} (release {rid}) def={definitive}", file=sys.stderr)

    records = []
    for ref in sorted(by_month):
        item = by_month[ref]
        win = _window(ref, window)
        timeseries = []
        for unit, idbank in idbanks.items():
            vals = [series[unit].get(m) for m in win]
            if all(v is None for v in vals):
                continue
            timeseries.append({"values": vals, "unit": unit, "freq": "1M"})
        if not timeseries:
            continue
        records.append({
            "text": item["narrative"] + "\n\n<ts></ts>",
            "timeseries": timeseries,
            "task_type": "world_knowledge",
            "text_quality": "real",
            "series_id": f"insee_informations_rapides:{ind['key']}:{ref}",
            "dataset": "insee_informations_rapides",
            "source": INSEE_EN.format(id=item["rid"]),
            "license": "cc-by-4.0",              # closest schema fit for Etalab Open Licence 2.0
            "text_source": "first_party_official",
            "alignment": "recites",
            "domain": "economy",
            "region": "FR",
            "period_start": win[0],
            "period_end": win[-1],
            "meta": {"indicator": ind["key"], "reference_month": ref,
                     "window_months": window, "language": "en",
                     "true_license": "etalab-open-license-2.0",
                     "series_idbanks": idbanks},
        })
    report["kept"] = len(records)
    print(json.dumps(report, indent=2), file=sys.stderr)

    if max_records:
        records = records[-max_records:]         # most recent N (deepest, most-archived)
    return records


def load_config(path, overrides):
    cfg = yaml.safe_load(open(path)) if path else {}
    for kv in overrides:
        key, _, val = kv.partition("=")
        node = cfg
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        try:
            node[parts[-1]] = json.loads(val)
        except Exception:
            node[parts[-1]] = None if val == "null" else val
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(PKG_ROOT / "config.example.yaml"))
    ap.add_argument("--set", action="append", default=[], dest="overrides")
    args = ap.parse_args()
    cfg = load_config(args.config, args.overrides)

    records = build(cfg)
    out_dir = PKG_ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "insee_informations_rapides_cpt.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Wrote {len(records)} records to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
