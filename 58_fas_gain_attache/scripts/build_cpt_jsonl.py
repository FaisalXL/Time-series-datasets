#!/usr/bin/env python3
"""Build CPT world-knowledge records from USDA FAS GAIN attaché reports.

One record = a GAIN report's own VERBATIM narrative paired with the multi-channel PSD
(Production, Supply & Distribution) balance sheet(s) that narrative discusses. GAIN is the
country-granular sibling of built WASDE #41: same PSD backbone, but #41 builds only "U.S. ..."
tables, so foreign-post series here are structurally net-new.

THE VINTAGE SPLICE (measured 2026-07-30, the reason this script does not just read PSD live):
    PSD Online bulk carries decades of annual history but always reflects the CURRENT vintage.
    USDA keeps revising recent/forecast years after a report ships. On MX2026-0012 the settled
    years matched the report exactly (8/8 for 2024), but the forecast year had drifted past BOTH
    of the report's own columns (Total Slaughter 2026: report 7,200 official / 7,225 New Post ->
    PSD now 7,550). So:
        series = PSD bulk (settled years)  +  the report's OWN table values (its table years)
    Pairing live PSD naively would silently mismatch exactly the years the prose is about.

FORECAST-NOT-MEASURED: GAIN prose is forecast-DOMINANT -- every Report Highlights / Executive
Summary leads with the coming marketing year. Handled like WASDE #41: the forecast year is the
series' terminal point and the text is the contemporaneous first-party forecast, so there is no
future-value leakage. Stripping the forecast would BREAK alignment, not clean it.

TWO RECORD SHAPES (per-post layout variance is real -- see README):
    per_commodity   livestock-style: prose interleaved after each commodity's table -> 1 rec/commodity
    multi_commodity oilseeds-style: prose organized by TOPIC across all commodities, tables grouped
                    at the end -> 1 rec/section, channels = union. Per-commodity pairing here would
                    re-ship one paragraph under N labels = the "no fake scale" ban in SCHEMA.md.

Text is 100% verbatim source prose + a single bare <ts></ts>; nothing is synthesized.

Usage:
    python scripts/build_cpt_jsonl.py --config config.example.yaml
    python scripts/build_cpt_jsonl.py --config config.example.yaml --set output.max_records=2
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from statistics import median

import pdfplumber
import yaml

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent
sys.path.insert(0, str(PKG_ROOT.parent / "schema"))
from emit import emit_record  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
NUM = r"-?[\d,]+(?:\.\d+)?"
# a PSD table's identity rows, in both the bordered and the borderless rendering
TABLE_MARKERS = ("USDA Official", "New Post", "Market Year Begins")


# ---------------------------------------------------------------------------- fetch / cache
def fetch(url: str, cache: Path) -> bytes:
    if cache.exists():
        return cache.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as r:
        blob = r.read()
    cache.write_bytes(blob)
    return blob


def load_psd(group: str, cfg: dict, cache_dir: Path) -> tuple[dict, dict]:
    """Return (values, units): values[(country, commodity, attribute)][market_year] = float."""
    url = cfg["data"]["psd_zip"].format(group=group)
    blob = fetch(url, cache_dir / f"psd_{group}.zip")
    values: dict[tuple[str, str, str], dict[int, float]] = {}
    units: dict[tuple[str, str, str], str] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        with z.open(name) as fh:
            for row in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig")):
                key = (row["Country_Name"], row["Commodity_Description"],
                       row["Attribute_Description"])
                try:
                    v = float(row["Value"])
                except (TypeError, ValueError):
                    continue
                values.setdefault(key, {})[int(row["Market_Year"])] = v
                units[key] = row["Unit_Description"]
    return values, units


# ---------------------------------------------------------------------------- PSD table parsing
def norm_label(s: str) -> str:
    """'Total Cattle Beg. Stocks (1000 HEAD)' -> 'total cattle beg stocks' (unit dropped)."""
    s = re.sub(r"\([^)]*\)", " ", s)          # drop parenthesised units
    s = re.sub(r"\(.*$", " ", s)               # ... and an unclosed trailing '(1000'
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _to_float(tok: str):
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def parse_tables_bordered(page) -> list[dict]:
    """Family A: ruled tables -> pdfplumber.extract_tables() gives clean cells."""
    out = []
    for t in page.extract_tables():
        if len(t) < 4:
            continue
        head = " ".join(" ".join((c or "") for c in r) for r in t[:4])
        if "USDA Official" not in head:
            continue
        title = (t[0][0] or "").split("\n")[0].strip()
        years = [int(y) for c in t[0][1:] if (y := re.sub(r"\D", "", c or "")) and len(y) == 4]
        rows = {}
        for r in t[3:]:
            label = (r[0] or "").replace("\n", " ").strip()
            if not label:
                continue
            nums = [_to_float(c) for c in r[1:] if (c or "").strip()]
            if len(nums) >= 2:
                rows[label] = nums
        if rows:
            out.append({"title": title, "years": years, "rows": rows})
    return out


def parse_tables_text(page) -> list[dict]:
    """Family B: borderless tables -> parse the raw text lines (label + 6 numeric tokens)."""
    txt = page.extract_text() or ""
    if "New Post" not in txt:
        return []
    lines = txt.split("\n")
    out, cur = [], None
    for ln in lines:
        # a table header line carries the commodity + its marketing-year columns
        m = re.match(r"^([A-Z][A-Za-z,\s]+?)\s+((?:\d{4}/\d{4}\s*){2,}|(?:\d{4}\s+){2,}\d{4})\s*$",
                     ln.strip())
        if m:
            ys = [int(y[:4]) for y in re.findall(r"\d{4}(?:/\d{4})?", m.group(2))]
            cur = {"title": m.group(1).strip(), "years": ys, "rows": {}}
            out.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(rf"^(?P<label>.*?[A-Za-z].*?)\s+(?P<nums>(?:{NUM}\s+){{5}}{NUM})\s*$",
                     ln.strip())
        if m:
            nums = [_to_float(t) for t in m.group("nums").split()]
            if all(n is not None for n in nums):
                cur["rows"][m.group("label").strip()] = nums
    return [t for t in out if t["rows"]]


def find_tables(pdf) -> list[dict]:
    tables = []
    for i, page in enumerate(pdf.pages):
        found = parse_tables_bordered(page) or parse_tables_text(page)
        for t in found:
            t["page"] = i
            tables.append(t)
    return tables


def pick_value(nums: list[float], n_years: int, yi: int):
    """Report tables render (USDA Official, New Post) per year. Prefer New Post -- it is Post's own
    view, which is what the narrative argues -- falling back to Official when New Post is a
    not-yet-published 0. (Ukraine MY2026/27: Official=0, New Post=5450 -> must take New Post.)"""
    if len(nums) >= 2 * n_years:
        official, newpost = nums[2 * yi], nums[2 * yi + 1]
    elif len(nums) >= n_years:
        official = newpost = nums[yi]
    else:
        return None, False
    if newpost == 0 and official not in (0, None):
        return official, True
    return newpost, False


# ---------------------------------------------------------------------------- prose extraction
_PURE_NUM = re.compile(r"^-?[\d,]+(?:\.\d+)?%?$")


def is_tableish(ln: str) -> bool:
    """A table row is mostly numbers; PROSE that recites several numbers is not a table row.

    Counting bare numbers alone misfires badly here -- GAIN's richest reciting sentences ("Post
    estimates MY2026/27 total oilseed area at 8.6 million hectares (ha) ... 1.3 million ha ... 11
    percent higher") carry 3+ numerals and would be discarded as table junk, throwing away exactly
    the prose this corpus exists to pair. So gate on the NUMBER DENSITY, not the count.
    """
    s = ln.strip()
    if not s:
        return False
    if any(m in s for m in TABLE_MARKERS):
        return True
    if re.match(r"^(Table|Figure)\s+\d", s):
        return True
    toks = s.split()
    if not toks:
        return False
    nums = sum(1 for t in toks if _PURE_NUM.match(t))
    return nums >= 3 and nums / len(toks) >= 0.4


def clean_lines(lines: list[str], drop_res: list[re.Pattern]) -> list[str]:
    return [ln for ln in lines if not any(p.search(ln.strip()) for p in drop_res)]


# a paragraph that is really a stray table unit fragment ('(1000 HEAD)', 'HEAD)') or a bare
# subsection year heading ('2025') -- verbatim source, but noise at the edges of a prose run
_EDGE_NOISE = re.compile(r"^(?:\(?\s*\d{0,4}\s*(?:HEAD|MT|MT CWE|CWE|HA|MT/HA)\s*\)?|\d{4})$")


def join_prose(lines: list[str]) -> str:
    """Re-flow PDF-wrapped lines into paragraphs. Verbatim: only line-wrapping is undone.

    A line ending in '-' is joined KEEPING the hyphen: GAIN PDFs wrap at existing hyphenated
    compounds ('higher-\\nquality' -> 'higher-quality'), they do not syllable-hyphenate, so
    dropping it would corrupt the source word.
    """
    if not lines:
        return ""
    widths = [len(l) for l in lines if len(l) > 20] or [80]
    full = median(widths)
    paras, cur = [], []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        short_close = len(s) < 0.72 * full and re.search(r"[.:;?!”\"]$", s)
        heading = len(s) < 50 and not re.search(r"[.:;?!]$", s)
        if heading:                            # always its own paragraph, even if cur is empty
            if cur:
                paras.append(cur)
            paras.append([s])
            cur = []
            continue
        cur.append(s)
        if short_close:
            paras.append(cur)
            cur = []
    if cur:
        paras.append(cur)
    out = []
    for p in paras:
        body = ""
        for s in p:
            if body.endswith("-"):
                body += s                      # keep the hyphen (see docstring)
            elif body:
                body += " " + s
            else:
                body = s
        if body.strip():
            out.append(body.strip())
    while out and _EDGE_NOISE.match(out[0]):
        out.pop(0)
    while out and _EDGE_NOISE.match(out[-1]):
        out.pop()
    return "\n\n".join(out)


def prose_after_table(pdf, page_idx: int, tcfg: dict, drop_res) -> str:
    """Family A: the contiguous prose run that follows this commodity's table."""
    got: list[str] = []
    for pi in range(page_idx, min(page_idx + 3, len(pdf.pages))):
        lines = clean_lines((pdf.pages[pi].extract_text() or "").split("\n"), drop_res)
        start = 0
        if pi == page_idx:                     # skip past the table block on its own page
            last = 0
            for i, ln in enumerate(lines):
                if is_tableish(ln):
                    last = i
            start = last + 1
        gap = 0
        for ln in lines[start:]:
            s = ln.strip()
            if not s:
                continue
            if is_tableish(s) or any(m in s for m in TABLE_MARKERS):
                gap += 1
                if got or gap >= 1:
                    return join_prose(got)[: tcfg["max_chars"]]
                continue
            got.append(s)
            if sum(len(x) for x in got) > tcfg["max_chars"]:
                return join_prose(got)[: tcfg["max_chars"]]
    return join_prose(got)[: tcfg["max_chars"]]


def prose_after_heading(pdf, heading: str, tcfg: dict, drop_res) -> str:
    """Family B: the contiguous prose run under a named section heading."""
    hre = re.compile(heading)
    for pi, page in enumerate(pdf.pages):
        lines = clean_lines((page.extract_text() or "").split("\n"), drop_res)
        for i, ln in enumerate(lines):
            if not hre.match(ln.strip()):
                continue
            got: list[str] = []
            for pj in range(pi, min(pi + 3, len(pdf.pages))):
                block = (lines[i + 1:] if pj == pi
                         else clean_lines((pdf.pages[pj].extract_text() or "").split("\n"), drop_res))
                for s in (x.strip() for x in block):
                    if not s:
                        continue
                    if is_tableish(s):
                        if got:
                            return join_prose(got)[: tcfg["max_chars"]]
                        continue
                    got.append(s)
                    if sum(len(x) for x in got) > tcfg["max_chars"]:
                        return join_prose(got)[: tcfg["max_chars"]]
            return join_prose(got)[: tcfg["max_chars"]]
    return ""


# ---------------------------------------------------------------------------- alignment
# which unit words may legitimately sit next to a number for a given PSD unit -- used to stop a
# value being credited to the wrong channel (an oilseed CRUSH of 1,800 must not claim the prose's
# "1.8 million ha", which is an AREA)
_UNIT_WORDS = {
    "HEAD": ("head",),
    "HA": ("ha", "hectare", "hectares"),
    "MT": ("mt", "mmt", "ton", "tons", "tonne", "tonnes"),
    "CWE": ("mt", "mmt", "cwe", "ton", "tons"),
    "MT/HA": ("mt/ha", "ton", "tons"),
}


def _unit_words(psd_unit: str) -> tuple[str, ...]:
    u = psd_unit.upper()
    if "CWE" in u:
        return _UNIT_WORDS["CWE"]
    if "MT/HA" in u:
        return _UNIT_WORDS["MT/HA"]
    for k in ("HEAD", "HA", "MT"):
        if k in u:
            return _UNIT_WORDS[k]
    return ()


def detect_alignment(text: str, channels: list[dict]) -> tuple[str, list[dict]]:
    """recites only when the prose states a paired value EXACTLY and with a compatible unit.

    Deliberately strict, because a loose matcher manufactures fake alignment -- the failure mode
    that killed openFDA/NHTSA/CFPB from this corpus. Two rules do the work:
      * exact surface forms only (v, or v/1000 for 1000-scaled units). No rounding: 2,865 may NOT
        claim the prose's "2.9 MMT", and 1,259 may NOT claim "1.3 million".
      * a compatible unit word must follow the number, so an MT channel cannot claim a "... ha"
        figure, and numeric boundaries stop "2000" matching inside "20,000".
    Returns auditable evidence (channel, year, value, quoted form).
    """
    evidence = []
    for ch in channels:
        words = _unit_words(ch.get("psd_unit", ""))
        for y, v in list(zip(ch["years"], ch["values"]))[-4:]:   # years the narrative is about
            if v is None or abs(v) < 10:        # tiny values collide with everything
                continue
            forms = {f"{v:g}", f"{v:,.0f}"}
            if "1000" in ch.get("psd_unit", "") and abs(v) >= 100:
                forms.add(f"{v/1000:g}")        # exact only: 8400 -> '8.4', never 2865 -> '2.9'
            hit = None
            for f in sorted(forms, key=len, reverse=True):
                if len(re.sub(r"\D", "", f)) < 2:
                    continue
                for m in re.finditer(rf"(?<![\d.,]){re.escape(f)}(?![\d.,])", text):
                    after = text[m.end():m.end() + 30].lower()
                    if not words or any(w in after for w in words):
                        hit = f
                        break
                if hit:
                    break
            if hit:
                evidence.append({"channel": ch["unit"], "market_year": y,
                                 "value": v, "quoted_as": hit})
                break
    return ("recites" if evidence else "describes"), evidence[:6]


# ---------------------------------------------------------------------------- build
def build_channels(psd_vals, psd_units, country, commodity, table, aliases, scfg, stats):
    """Splice: PSD bulk settled history + the report's own table years (vintage-exact tail)."""
    n_years = len(table["years"]) or 3
    channels, fallbacks = [], 0
    for label, nums in table["rows"].items():
        key_norm = norm_label(label)
        attr = aliases.get(key_norm)
        if attr is None:
            # exact/loose match against this commodity's real PSD attribute vocabulary
            for (c, cm, a) in psd_vals:
                if c == country and cm == commodity and norm_label(a) == key_norm:
                    attr = a
                    break
        if attr is None:
            stats["unmapped_labels"].setdefault(f"{commodity} :: {label}", 0)
            stats["unmapped_labels"][f"{commodity} :: {label}"] += 1
            continue
        key = (country, commodity, attr)
        hist = psd_vals.get(key)
        if not hist:
            stats["no_psd_series"] += 1
            continue
        tail = {}
        for yi, y in enumerate(table["years"]):
            v, fb = pick_value(nums, n_years, yi)
            if v is not None:
                tail[y] = v
                fallbacks += int(fb)
        if not tail:
            continue
        first_tail = min(tail)
        years = sorted([y for y in hist if y < first_tail]) + sorted(tail)
        vals = [hist[y] if y < first_tail else tail[y] for y in years]
        if len(vals) < scfg["min_points"]:
            stats["short_series"] += 1
            continue
        unit = psd_units.get(key, "")
        slug = re.sub(r"[^a-z0-9]+", "_",
                      f"{commodity} {attr} {unit}".lower()).strip("_")
        channels.append({"values": vals, "unit": slug, "freq": scfg["freq"],
                         "psd_attribute": attr, "psd_unit": unit,
                         "years": years, "splice_year": first_tail})
    stats["official_fallbacks"] += fallbacks
    return channels


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(PKG_ROOT / "config.example.yaml"))
    ap.add_argument("--set", action="append", default=[], metavar="dotted.key=value")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    for override in args.set:
        k, v = override.split("=", 1)
        node = cfg
        *parts, leaf = k.split(".")
        for p in parts:
            node = node[p]
        node[leaf] = yaml.safe_load(v)

    cache = PKG_ROOT / cfg["data"]["cache_dir"]
    tcfg, scfg = cfg["text"], cfg["series"]
    drop_res = [re.compile(p) for p in tcfg["drop_lines"]]
    aliases = cfg.get("attribute_aliases", {})
    dedup = cfg.get("dedup", {})
    stats = {"reports": 0, "tables_found": 0, "candidates": 0, "emitted": 0,
             "recites": 0, "describes": 0, "channels_emitted": 0, "no_prose": 0,
             "short_series": 0, "no_psd_series": 0, "no_table": 0, "wasde_skipped": 0,
             "official_fallbacks": 0, "unmapped_labels": {}}
    records, psd_cache = [], {}

    for rep in cfg["reports"]:
        group = rep["psd_group"]
        if group not in psd_cache:
            psd_cache[group] = load_psd(group, cfg, cache)
        psd_vals, psd_units = psd_cache[group]

        url = cfg["data"]["gain_download"].format(
            file_name=urllib.parse.quote_plus(rep["file_name"]))
        pdf_path = cache / "reports" / rep["file_name"]
        fetch(url, pdf_path)
        stats["reports"] += 1

        with pdfplumber.open(pdf_path) as pdf:
            tables = find_tables(pdf)
            stats["tables_found"] += len(tables)
            by_title = {}
            for t in tables:
                by_title.setdefault(norm_label(t["title"]), t)

            specs = (rep.get("commodities") if rep["record_shape"] == "per_commodity"
                     else rep.get("sections", []))
            for spec in specs:
                stats["candidates"] += 1

                if rep["record_shape"] == "per_commodity":
                    commodities = [spec["psd_commodity"]]
                    table_keys = [norm_label(spec["table_title"])]
                else:
                    commodities = spec["psd_commodities"]
                    table_keys = [norm_label(c) for c in commodities]

                if (rep["country"] in dedup.get("wasde_countries", [])
                        and any(c in dedup.get("wasde_commodities", []) for c in commodities)):
                    stats["wasde_skipped"] += 1
                    continue

                chans, tabs = [], []
                for cm, tk in zip(commodities, table_keys):
                    table = by_title.get(tk)
                    if table is None:
                        continue
                    tabs.append(table)
                    keep = spec.get("attributes")
                    ch = build_channels(psd_vals, psd_units, rep["country"], cm, table,
                                        aliases, scfg, stats)
                    if keep:
                        ch = [c for c in ch if c["psd_attribute"] in keep]
                    chans.extend(ch)
                if not tabs:
                    stats["no_table"] += 1
                    continue
                if not chans:
                    continue

                if rep["record_shape"] == "per_commodity":
                    text = prose_after_table(pdf, tabs[0]["page"], tcfg, drop_res)
                else:
                    text = prose_after_heading(pdf, spec["prose_heading"], tcfg, drop_res)
                if len(text) < tcfg["min_chars"]:
                    stats["no_prose"] += 1
                    continue

                alignment, evidence = detect_alignment(text, chans)
                years = sorted({y for c in chans for y in c["years"]})
                ts = [{"values": c["values"], "unit": c["unit"], "freq": c["freq"]} for c in chans]
                rec = emit_record(
                    text=text.rstrip() + "\n\n<ts></ts>",
                    timeseries=ts,
                    alignment=alignment,
                    license="public-domain-us-gov",
                    source=url,
                    series_id=f"fas_gain_{rep['report_number'].lower()}_{spec['slug']}",
                    dataset="fas_gain_attache",
                    domain="agriculture",
                    region=rep["region"],
                    period_start=f"{years[0]}-01-01",
                    period_end=f"{years[-1]}-01-01",
                    meta={
                        "report_number": rep["report_number"],
                        "post": rep["post"],
                        "country": rep["country"],
                        "published": rep["published"],
                        "record_shape": rep["record_shape"],
                        "commodities": commodities,
                        "psd_attributes": [c["psd_attribute"] for c in chans],
                        "psd_units": sorted({c["psd_unit"] for c in chans}),
                        "n_channels": len(chans),
                        "market_years": [years[0], years[-1]],
                        "n_points": len(chans[0]["values"]),
                        "splice_year": chans[0]["splice_year"],
                        "report_table_years": tabs[0]["years"],
                        "recite_evidence": evidence,
                        "series_note": (
                            "annual PSD balance sheet, vintage-spliced: PSD Online bulk for settled "
                            f"market years (< {chans[0]['splice_year']}) + this report's OWN table "
                            "values for its table years (New Post column preferred). Live PSD has "
                            "since revised the forecast year past both of the report's columns."),
                        "forecast_caveat": (
                            "terminal point(s) are Post's forecast for the coming marketing year, "
                            "not measured history -- same convention as WASDE #41; the text is the "
                            "contemporaneous first-party forecast, so no future-value leakage."),
                        "wasde_overlap": (
                            "WASDE #41 builds only U.S. tables; this is a foreign post, so the "
                            "series are net-new rather than duplicated."),
                    },
                )
                records.append(rec)
                stats["emitted"] += 1
                stats[alignment] += 1
                stats["channels_emitted"] += len(chans)
                if cfg["output"]["max_records"] and stats["emitted"] >= cfg["output"]["max_records"]:
                    break

    out = PKG_ROOT / cfg["output"]["path"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    rr = PKG_ROOT / cfg["output"]["run_report"]
    rr.write_text(json.dumps({"dataset": "fas_gain_attache", "stats": stats,
                              "config_snapshot": cfg}, indent=2, ensure_ascii=False))
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"\nwrote {len(records)} records -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
