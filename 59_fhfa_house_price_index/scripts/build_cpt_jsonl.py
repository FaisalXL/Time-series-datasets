#!/usr/bin/env python3
"""Build CPT world-knowledge records from FHFA House Price Index monthly reports.

One record = a monthly HPI report's own VERBATIM narrative paired with the multi-channel index
series (US + 9 census divisions) it discusses. Text is measured history only -- no forecast
language -- so no WASDE/GAIN-style forecast-caveat is needed.

WHY THE MONTHLY REPORT PAGE, NOT THE "NEWS RELEASE" SLUG URL: the news-release URL is an
unpredictable slugified headline (the same enumeration problem GAIN hit). The
`/reports/house-price-index/{year}/{month}` page carries the SAME narrative under a predictable
URL, so a full run can construct URLs directly.

WHY MONTHLY, NOT QUARTERLY: the quarterly report page's own HTML text is thin (verified: 4
sentences, national-only). The monthly page carries real division-level prose in HTML.

VINTAGE-REVISION RISK: FHFA's repeat-sales index revises its own recent history every release,
and often SAYS SO in the text ("The previously reported 0.1 percent price change in March was
revised upward to 0.2 percent"). This is checked as bonus evidence, not assumed.

Usage:
    python scripts/build_cpt_jsonl.py --config config.example.yaml
"""
from __future__ import annotations

import argparse
import csv
import io
import json
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

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
MONTHS = ["", "January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def fetch(url: str, cache: Path, retries: int = 4) -> bytes:
    if cache.exists():
        return cache.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    # fhfa.gov intermittently 404s on the FIRST hit to a given path then 200s on retry (confirmed
    # non-deterministic across back-to-back requests, 2026-08-03) -- looks like edge/WAF flakiness,
    # not a real missing-page signal, so retry before giving up.
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                blob = r.read()
            cache.write_bytes(blob)
            return blob
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 404 or attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise last


def html_to_text(html: bytes) -> str:
    t = html.decode("utf-8", "ignore")
    t = re.sub(r"<script.*?</script>", " ", t, flags=re.S)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#8217;", "’")
    # Zero-width characters (U+200B/U+FEFF/U+00AD) appear inside FHFA's own markup, sometimes
    # five in a row before the first word, and they break any regex anchored on a word.
    t = re.sub(r"[\u200b\u200c\u200d\ufeff\u00ad]", "", t)
    return re.sub(r"\s+", " ", t).strip()


# Surveyed across all 46 report pages on 2026-08-18 rather than inferred from the recent ones.
# The opening sentence takes three forms and the older half does NOT carry the "U.S." prefix:
#     12x  "U.S. house prices rose ..."          <- the only form the old anchors matched
#     16x  "House prices rose/fell nationwide"   <- silently dropped 18 of 46 reports
#      1x  "Washington, D.C. – U.S. ..."
# and several are prefixed with ZERO-WIDTH SPACE (U+200B), sometimes five of them, which defeats a
# regex anchored at a word boundary. html_to_text strips those now.
_START_ANCHORS = (
    re.compile(r"Washington, D\.C\.\s*[-–]\s*"),
    re.compile(r"(?=(?:U\.S\. )?[Hh]ouse prices (?:rose|fell|were|increased|declined|remained))"),
)


def extract_narrative(html: bytes, tcfg: dict) -> str:
    """Bound to the real narrative paragraph; everything outside is nav/footer boilerplate.

    The dateline prefix ("Washington, D.C. -- ") is NOT present on every release (confirmed:
    March/April 2026 report pages open directly with "U.S. house prices ..."), so multiple start
    anchors are tried in order rather than assuming one fixed opening.
    """
    text = html_to_text(html)
    breadcrumb = text.find("Breadcrumb")
    search_from = breadcrumb if breadcrumb >= 0 else 0
    start = -1
    for pat in _START_ANCHORS:
        m = pat.search(text, search_from)
        if m:
            start = m.end()
            break
    # The END marker is era-dependent too. 2021-2022 releases close the narrative with
    # "Related News Release Attachment(s):" and never contain "Tables and graphs", which stranded
    # 13 further reports after the start anchors were widened. Take the EARLIEST of the known
    # terminators so a page carrying several still cuts at the right place.
    ends = tcfg.get("end_anchors") or [tcfg["end_anchor"]]
    tail = text[max(start, 0):]
    hits = [m.start() for m in
            (re.search(re.escape(a), tail, re.I) for a in ends) if m]
    end = (start if start >= 0 else 0) + min(hits) if hits else -1
    if start < 0 or end < 0 or end <= start:
        return ""
    return text[start:end].strip()


def load_master(cfg: dict, cache: Path) -> dict:
    """Return values[place_name][(year, month)] = index_sa, for the configured series slice."""
    scfg = cfg["series"]
    blob = fetch(cfg["data"]["master_csv"], cache / "hpi_master.csv")
    values: dict[str, dict[tuple[int, int], float]] = {}
    reader = csv.DictReader(io.StringIO(blob.decode("utf-8-sig")))
    places = set(scfg["places"])
    for row in reader:
        if (row["hpi_type"] != scfg["hpi_type"] or row["hpi_flavor"] != scfg["hpi_flavor"]
                or row["frequency"] != scfg["frequency"] or row["level"] != scfg["level"]
                or row["place_name"] not in places):
            continue
        v = row[scfg["value_field"]]
        if not v:
            continue
        values.setdefault(row["place_name"], {})[(int(row["yr"]), int(row["period"]))] = float(v)
    return values


def window_series(values: dict, place: str, year: int, month: int, n: int) -> tuple[list, list]:
    """Trailing n months ending at (year, month), oldest-first."""
    key = (year, month)
    all_keys = sorted(values.get(place, {}).keys())
    if key not in all_keys:
        return [], []
    idx = all_keys.index(key)
    keep = all_keys[max(0, idx - n + 1): idx + 1]
    return keep, [values[place][k] for k in keep]


# ---------------------------------------------------------------------------- text parsing
_DIV_CLAUSE = re.compile(
    r"([+-])\s?(\d+\.\d+) percent in the ([A-Za-z ]+?) [Dd]ivision")


def div_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).title() + " Division"


def parse_national(text: str) -> dict:
    out = {}
    m = re.search(r"\b(rose|fell|up|down|unchanged)\b[^.]{0,60}?(\d+\.\d+) percent from the "
                  r"previous month", text, re.I)
    if m:
        sign = -1 if m.group(1).lower() in ("fell", "down") else 1
        out["mom"] = sign * float(m.group(2))
    elif re.search(r"unchanged in \w+", text, re.I):
        out["mom"] = 0.0
    m = re.search(r"\b(rose|fell)\b\s+(\d+\.\d+) percent from \w+ \d{4} to \w+ \d{4}", text, re.I)
    if m:
        out["yoy"] = (-1 if m.group(1).lower() == "fell" else 1) * float(m.group(2))
    return out


def parse_divisions(text: str) -> dict:
    """Returns {'mom': [(sign,val,div),(sign,val,div)], 'yoy': [...]} for the two 'ranged from
    ... to ...' sentences (monthly changes, then 12-month changes)."""
    out = {}
    for label, needle in (("mom", "monthly home price changes"), ("yoy", "12-month changes")):
        i = text.find(needle)
        if i < 0:
            continue
        window = text[i:i + 220]
        clauses = _DIV_CLAUSE.findall(window)
        if len(clauses) >= 2:
            out[label] = [(1 if s == "+" else -1, float(v), div_key(d))
                          for s, v, d in clauses[:2]]
    return out


def parse_revision(text: str):
    m = re.search(r"previously reported (\d+\.\d+) percent price change in (\w+) was revised "
                  r"(upward|downward) to (\d+\.\d+) percent", text, re.I)
    if not m:
        return None
    orig, month_name, direction, revised = m.groups()
    sign = 1 if direction.lower() == "upward" else -1
    month_num = next((i for i, n in enumerate(MONTHS) if n.lower() == month_name.lower()), None)
    return {"month_name": month_name, "month_num": month_num,
            "originally_reported": float(orig) * (1 if float(orig) >= 0 else 1),
            "revised_to": float(revised), "direction": direction}


# ---------------------------------------------------------------------------- alignment
def detect_alignment(text: str, year: int, month: int, values: dict) -> tuple[str, list, dict]:
    national = parse_national(text)
    divisions = parse_divisions(text)
    evidence, checks = [], {"matched": 0, "total": 0}

    def pct_change(place: str, back: int):
        yks, vs = window_series(values, place, year, month, back + 1)
        if len(vs) < back + 1:
            return None
        return (vs[-1] / vs[-1 - back] - 1) * 100

    if "mom" in national:
        checks["total"] += 1
        real = pct_change("United States", 1)
        if real is not None and abs(real - national["mom"]) < 0.15:
            checks["matched"] += 1
            evidence.append({"place": "United States", "metric": "mom",
                             "claimed": national["mom"], "computed": round(real, 2)})
    if "yoy" in national:
        checks["total"] += 1
        real = pct_change("United States", 12)
        if real is not None and abs(real - national["yoy"]) < 0.15:
            checks["matched"] += 1
            evidence.append({"place": "United States", "metric": "yoy",
                             "claimed": national["yoy"], "computed": round(real, 2)})
    for metric, back in (("mom", 1), ("yoy", 12)):
        for sign, val, div in divisions.get(metric, []):
            checks["total"] += 1
            real = pct_change(div, back)
            if real is not None and abs(real - sign * val) < 0.15:
                checks["matched"] += 1
                evidence.append({"place": div, "metric": metric,
                                 "claimed": sign * val, "computed": round(real, 2)})
    # The checks above verify that percentage changes STATED in the prose match percentage changes
    # COMPUTED from the series. That is strong alignment evidence and it is worth keeping -- but it
    # is not what `recites` means. SCHEMA §7: "the text literally states the numbers that are the
    # series", and this series stores index LEVELS, not the month-over-month percentages the
    # narrative quotes. Tagging `recites` off a derived quantity is the same overclaim ONS #61 made
    # across 71 of its 72 families, and it is what `schema/validate.py` now rejects at construction.
    #
    # So the evidence still gates the record; the TAG additionally requires a raw series value to
    # appear in the prose.
    strong = checks["matched"] >= max(3, checks["total"] - 1)
    alignment = "recites" if (strong and _text_states_a_level(text, values)) else "describes"
    return alignment, evidence, checks


_LEVEL_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def _text_states_a_level(text: str, values: dict) -> bool:
    """Does a raw index level from the paired series literally appear in the prose?

    Same test as schema/validate.py and the team's verify_cpt.py, so the three cannot disagree.
    """
    nums = []
    for m in _LEVEL_NUM.finditer(text or ""):
        try:
            nums.append(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    if not nums:
        return False
    for series in (values or {}).values():
        for v in (series or {}).values() if isinstance(series, dict) else (series or []):
            if v is None or not isinstance(v, (int, float)):
                continue
            for cand in {v, round(v, 1), round(v, 2), round(v)}:
                for t in nums:
                    if cand and abs(t - cand) <= max(0.01 * abs(cand), 0.01):
                        return True
    return False


_SUPERLATIVE = re.compile(r"\b(highest|largest|record[- ]high|all-time high|lowest|smallest|"
                          r"record[- ]low|all-time low)\b", re.I)


def check_superlatives(text: str, values: dict, places: list) -> list:
    """Cheap insurance carried over from #58's finding: a highest/lowest claim must hold against
    the FULL series for that place, not just the shipped window."""
    flags = []
    for m in _SUPERLATIVE.finditer(text):
        window = text[max(0, m.start() - 60): m.end() + 100]
        num = re.search(r"(\d+\.\d+) percent", window)
        if not num:
            continue
        for place in places:
            if place.lower().split()[0] in window.lower() or place == "United States":
                hist = values.get(place, {})
                if not hist:
                    continue
                is_high = bool(re.search(r"highest|largest|record[- ]high|all-time high",
                                         m.group(0), re.I))
                vals = list(hist.values())
                extreme = max(vals) if is_high else min(vals)
                flags.append({"place": place, "claim": m.group(0), "note":
                             "superlative language present; not numerically verified against "
                             "index level (claim is about a % CHANGE, not the index) -- flagged "
                             "for manual review", "series_extreme": extreme})
    return flags


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(PKG_ROOT / "config.example.yaml"))
    ap.add_argument("--set", action="append", default=[])
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    for override in args.set:
        k, v = override.split("=", 1)
        node, *parts = cfg, *k.split(".")
        d = cfg
        for p in parts[:-1]:
            d = d[p]
        d[parts[-1]] = yaml.safe_load(v)

    cache = PKG_ROOT / cfg["data"]["cache_dir"]
    scfg, tcfg = cfg["series"], cfg["text"]
    values = load_master(cfg, cache)

    stats = {"reports": 0, "emitted": 0, "recites": 0, "describes": 0, "no_narrative": 0,
             "short_series": 0, "superlative_flags": 0, "superlative_dropped": 0,
             "checks_matched": [], "checks_total": []}
    records = []

    for rep in cfg["reports"]:
        stats["reports"] += 1
        # URL month = index month + 2 (confirmed 2026-08-03: FHFA publishes with a 2-month lag --
        # e.g. the "2026/6" page's own narrative is about APRIL data, "2026/7" is about MAY).
        url_month = rep["month"] + 2
        url_year = rep["year"] + (1 if url_month > 12 else 0)
        url_month = url_month - 12 if url_month > 12 else url_month
        url = cfg["data"]["report_url"].format(year=url_year, month=url_month)
        html = fetch(url, cache / "reports" / f"{rep['year']}_{rep['month']:02d}.html")
        text = extract_narrative(html, tcfg)
        if len(text) < tcfg["min_chars"]:
            stats["no_narrative"] += 1
            continue

        chans_full = {}
        ok = True
        for place in scfg["places"]:
            yrs, vs = window_series(values, place, rep["year"], rep["month"], scfg["window"])
            if len(vs) < scfg["min_points"]:
                ok = False
                break
            chans_full[place] = (yrs, vs)
        if not ok:
            stats["short_series"] += 1
            continue

        alignment, evidence, checks = detect_alignment(text, rep["year"], rep["month"], values)
        stats["checks_matched"].append(checks["matched"])
        stats["checks_total"].append(checks["total"])

        superlative_flags = check_superlatives(text, values, scfg["places"])
        stats["superlative_flags"] += len(superlative_flags)
        if superlative_flags and tcfg.get("drop_on_superlative_contradiction", True):
            stats["superlative_dropped"] += 1
            continue

        revision = parse_revision(text)
        revision_check = None
        if revision and revision["month_num"]:
            rev_year = rep["year"] if revision["month_num"] < rep["month"] else rep["year"] - 1
            _, vs_prior = window_series(values, "United States", rev_year, revision["month_num"], 13)
            if len(vs_prior) == 13:
                real = (vs_prior[-1] / vs_prior[-2] - 1) * 100
                revision_check = {**revision,
                                  "current_live_value": round(real, 2),
                                  "still_matches_revised_figure": abs(real - revision["revised_to"]) < 0.05}

        ts = [{"values": chans_full[p][1], "unit": re.sub(r"[^a-z0-9]+", "_", p.lower()) + "_index_sa",
               "freq": scfg["freq"]} for p in scfg["places"]]
        month_name = MONTHS[rep["month"]]
        rec = emit_record(
            text=text + "\n\n<ts></ts>",
            timeseries=ts,
            alignment=alignment,
            license="public-domain-us-gov",
            source=url,
            series_id=f"fhfa_hpi_{rep['year']}_{rep['month']:02d}",
            dataset="fhfa_house_price_index",
            domain="housing",
            region="US",
            period_start=f"{chans_full['United States'][0][0][0]}-{chans_full['United States'][0][0][1]:02d}-01",
            period_end=f"{rep['year']}-{rep['month']:02d}-01",
            meta={
                "report_month": f"{rep['year']}-{rep['month']:02d}",
                "report_month_name": f"{month_name} {rep['year']}",
                "published": rep["published"],
                "places": scfg["places"],
                "n_channels": len(scfg["places"]),
                "n_points": scfg["window"],
                "value_field": scfg["value_field"],
                "recite_evidence": evidence,
                "evidence_checks": checks,
                "revision_sentence_check": revision_check,
                "superlative_flags": superlative_flags,
                "series_note": (
                    "trailing 24-month window of the seasonally-adjusted purchase-only index "
                    "(US + 9 census divisions), current vintage (hpi_master.csv at build time). "
                    "MEASURED history only -- no forecast content, unlike WASDE #41 / GAIN #58."),
                "vintage_caveat": (
                    "FHFA's repeat-sales index revises its own recent history every release "
                    "(sometimes stated explicitly in the text itself, see revision_sentence_check). "
                    "A full historical run needs per-release vintage archiving, not live "
                    "master.csv, for older reports."),
            },
        )
        records.append(rec)
        stats["emitted"] += 1
        stats[alignment] += 1

    out = PKG_ROOT / cfg["output"]["path"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    rr = PKG_ROOT / cfg["output"]["run_report"]
    rr.write_text(json.dumps({"dataset": "fhfa_house_price_index", "stats": stats,
                              "config_snapshot": cfg}, indent=2, ensure_ascii=False))
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"\nwrote {len(records)} records -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
