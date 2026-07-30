#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from NOAA Fisheries Stock Assessments (Stock SMART).

One record = one (stock, assessment) pair -- Stock SMART's own unit. Text is that
assessment report's own **status narrative** about that stock; series is that stock's own
catch / fishing-mortality / recruitment / abundance history through the assessment's terminal
data year, at annual resolution and often decades deep.

API: NOAA Stock SMART's public JSON API (reverse-engineered from the NOAA-EDAB/stocksmart R
package's data-raw/query_stocksmart_api.R -- no auth, no key):
  - sis_servlet?jsonParam={"method":"searchEntities",...}                -> all stock entities
  - data-export-servlet?jsonParam={"dataType":"Assessment Summary Data",...}
        -> per (stock, assessment): jurisdiction, science center, last data year, and the
           real report-PDF download link(s) (as XLSX hyperlink relationships, not cell text)
  - data-export-servlet?jsonParam={"dataType":"TimeSeriesData","categories":[...],
        "asmtList":"id1,id2,..."} -> wide table, repeating column-blocks per (stock,assessment)
Report PDFs download from apps-st.fisheries.noaa.gov/sis/docServlet?fileAction=download&fileId=N.

Three things this build does differently from the 50-record demo, each because the demo's
assumption was measured and failed (see README "What the full build changed"):

  * **Omnibus detection is structural, not page-count based.** The demo skipped reports over
    80 pages on the theory that long reports are multi-stock. Measured: 107 of 274 sampled
    *single-stock* reports are over 80 pages (max 2,445), while the exact signal is already in
    the source -- 1,410 distinct report files back 3,088 rows, so 1,842 rows (59.7%) share a
    file with a DIFFERENT stock. Page count is replaced by that sharing structure.
  * **The text is the report's own narrative, not the whole PDF.** See scripts/repex.py.
  * **The window starts at the earliest year any channel covers, under a null budget.** The
    demo started every window at the LATEST channel start (max-of-mins), which discarded
    13,676 real year-observations across the universe -- 22% of assessments lost >=10 years
    and the worst lost 103.

Examples:
  python scripts/fetch_reports.py <cands.json>      # optional: warm the PDF cache first
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required. pip install -r requirements.txt") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
import repex  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


# --- config helpers (same conventions as the other packages) ---------------

def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    m = dict(base)
    for k, v in over.items():
        m[k] = deep_merge(m[k], v) if k in m and isinstance(m[k], dict) and isinstance(v, dict) else v
    return m


def coerce(raw: str) -> Any:
    low = raw.strip().lower()
    if low in {"true", "yes"}: return True
    if low in {"false", "no"}: return False
    if low in {"null", "none", "~"}: return None
    if re.fullmatch(r"-?\d+", raw): return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw): return float(raw)
    return raw


def parse_sets(sets: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for it in sets:
        k, v = it.split("=", 1)
        cur = out
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = coerce(v)
    return out


def load_config(path: Path, sets: Sequence[str]) -> Dict[str, Any]:
    cfg = yaml.safe_load(path.read_text())
    return deep_merge(cfg, parse_sets(sets)) if sets else cfg


def rp(s: str) -> Path:
    p = Path(s)
    return p if p.is_absolute() else ROOT / p


# --- HTTP with retry + cache -------------------------------------------------

def http_get(url: str, ua: str, timeout: int, retries: int, backoff: float) -> Optional[bytes]:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(backoff * attempt)
    print(f"  ! fetch failed after {retries} tries: {url[:120]} ({last_err})", file=sys.stderr)
    return None


def cache_path(cache: Path, kind: str, key: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()[:20]
    return cache / kind / f"{h}"


def fetch_cached(url: str, cache: Path, kind: str, d: dict) -> Optional[bytes]:
    fp = cache_path(cache, kind, url)
    if fp.exists():
        return fp.read_bytes()
    raw = http_get(url, d["user_agent"], int(d["timeout_s"]), int(d.get("max_retries", 3)),
                   float(d.get("retry_backoff_s", 3.0)))
    if raw is None:
        return None
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(raw)
    time.sleep(float(d.get("request_delay_s", 0.3)))
    return raw


def api_get(method_or_export: str, query: dict, d: dict, cache: Path, kind: str) -> Optional[bytes]:
    param = urllib.parse.quote(json.dumps(query))
    url = f"{d['api_base']}{method_or_export}?jsonParam={param}"
    return fetch_cached(url, cache, kind, d)


# --- XLSX parsing (stdlib) ---------------------------------------------------

def _col_idx(ref: str) -> int:
    s = "".join(c for c in ref if c.isalpha())
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx_rows(raw: bytes) -> List[List[str]]:
    z = zipfile.ZipFile(io.BytesIO(raw))
    ss: List[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(NS + "si"):
            ss.append("".join(t.text or "" for t in si.iter(NS + "t")))
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows: List[List[str]] = []
    for row in sheet.find(NS + "sheetData").findall(NS + "row"):
        cells: Dict[int, str] = {}
        maxc = -1
        for c in row.findall(NS + "c"):
            ci = _col_idx(c.get("r"))
            v = c.find(NS + "v")
            val = "" if v is None else (ss[int(v.text)] if c.get("t") == "s" else v.text)
            cells[ci] = val
            maxc = max(maxc, ci)
        rows.append([cells.get(i, "") for i in range(maxc + 1)])
    return rows


def read_xlsx_hyperlinks(raw: bytes) -> Dict[str, str]:
    """cell ref (e.g. 'V2') -> target URL, via the worksheet's external relationships."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    rels: Dict[str, str] = {}
    rels_path = "xl/worksheets/_rels/sheet1.xml.rels"
    if rels_path in z.namelist():
        for r in ET.fromstring(z.read(rels_path)):
            rels[r.get("Id")] = r.get("Target")
    out: Dict[str, str] = {}
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    hl = sheet.find(NS + "hyperlinks")
    if hl is not None:
        for h in hl.findall(NS + "hyperlink"):
            rid = h.get(REL_NS + "id")
            if rid in rels:
                out[h.get("ref")] = rels[rid]
    return out


def col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def br_num(s: str) -> Optional[float]:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def id_str(v: Optional[str]) -> str:
    """Normalize a numeric-ID cell ('10455.0', '10455', '') to a clean integer string. NOT
    a naive .rstrip('.0') -- that mangles IDs with trailing zeros before the decimal point
    (e.g. '10400.0'.rstrip('.0') wrongly gives '104')."""
    if not v:
        return ""
    try:
        return str(int(float(v)))
    except ValueError:
        return v.strip()


def file_id(url: str) -> str:
    m = re.search(r"fileId=(\d+)", url)
    return m.group(1) if m else url


# --- API calls ---------------------------------------------------------------

def fetch_entities(d: dict, cache: Path) -> List[Dict[str, str]]:
    q = {"method": "searchEntities", "entName": "%", "scId": "%", "jurId": "%", "fmpId": "%", "ecoId": "%"}
    raw = api_get("sis_servlet", q, d, cache, "entities")
    if not raw:
        return []
    return json.loads(raw)["data"]


def fetch_summary(entity_ids: List[str], d: dict, cache: Path) -> Tuple[List[List[str]], Dict[str, str]]:
    crit = {
        "dataType": "", "scName": "- Science Center -", "ecoName": "- Reg. Ecosystem -",
        "jurName": "- Jurisdiction -", "rgnName": "", "fmpName": "- Fish Mgmt Plan -", "monName": "",
        "entityIdList": ",".join(entity_ids),
        "outputFieldList": "as_year,as_last_data_year,as_files",
        "outputLabelList": "Assessment Year,Last Data Year,Final Assessment Report",
        "entityAttrList": "ent_name,jur_name,sc_name",
        "entityAttrLabelList": "Stock Name,Jurisdiction,Science Center",
        "fileTypeList": "", "DownloadTool_includeNoAsmt": "N", "segIndex": "0",
        "DownloadTool_carryForward": "N", "DownloadTool_ent_name": "",
        "DownloadTool_jur_select": "%", "DownloadTool_fmp_select": "%", "DownloadTool_sc_select": "%",
        "DownloadTool_eco_select": "%", "DownloadTool_fssi_select": "", "startYear": "1800",
        "endYear": "2032", "asmtYears": "",
    }
    q = {"crit": crit, "dataType": "Assessment Summary Data", "dataFormat": "excel"}
    raw = api_get("data-export-servlet", q, d, cache, "summary")
    if not raw:
        return [], {}
    return read_xlsx_rows(raw), read_xlsx_hyperlinks(raw)


def fetch_timeseries_batch(asmt_ids: List[str], d: dict, cache: Path) -> List[List[str]]:
    q = {
        "dataType": "TimeSeriesData", "dataFormat": "excel", "partIndex": "1",
        "categories": ["Catch", "Abundance", "Fmort", "Recruitment"],
        "minYear": "1800", "maxYear": "2032",
        "asmtList": ",".join(asmt_ids),
    }
    raw = api_get("data-export-servlet", q, d, cache, "timeseries")
    if not raw:
        return []
    return read_xlsx_rows(raw)


def parse_timeseries_table(rows: List[List[str]]) -> Dict[str, Dict[str, Any]]:
    """Wide table with repeating column-blocks per (stock,assessment). Returns
    {asmt_id: {"stock_id":..., "channels": {param: {"unit","description","series":{year:val}}}}}"""
    if len(rows) < 8:
        return {}
    ncols = max(len(r) for r in rows)
    out: Dict[str, Dict[str, Any]] = {}
    for col in range(2, ncols):
        stock_id = id_str(rows[1][col] if col < len(rows[1]) else "")
        asmt_id = id_str(rows[2][col] if col < len(rows[2]) else "")
        if not asmt_id:
            continue
        param = rows[5][col] if col < len(rows[5]) else ""
        desc = rows[6][col] if col < len(rows[6]) else ""
        unit = rows[7][col] if col < len(rows[7]) else ""
        series: Dict[int, float] = {}
        for r in rows[8:]:
            if col >= len(r):
                continue
            yv = br_num(r[1] if len(r) > 1 else None)
            val = br_num(r[col])
            if yv is not None and val is not None:
                series[int(yv)] = val
        entry = out.setdefault(asmt_id, {"stock_id": stock_id, "channels": {}})
        if param:
            entry["channels"][param] = {"unit": unit, "description": desc, "series": series}
    return out


# --- window construction ------------------------------------------------------

def choose_window(channels: Sequence[Tuple[str, str, Dict[int, float]]], end_y: int,
                  null_budget: float) -> int:
    """Pick the window start: the EARLIEST year any channel covers whose resulting null
    fraction stays inside the budget.

    The demo build used max-of-mins (the latest channel start), which is null-free but
    silently truncates the deep channel -- a catch series reaching back to 1893 was cut to
    the 1980 start of the biomass series. Union-start with no budget is the other extreme
    (7.0% nulls corpus-wide). Candidate starts are the channel starts themselves, tried
    deepest-first, so the window boundary is always a real source boundary.
    """
    starts = sorted({min(s) for _, _, s in channels if s})
    for st in starts:
        span = end_y - st + 1
        if span <= 0:
            continue
        missing = sum(span - len([y for y in s if st <= y <= end_y]) for _, _, s in channels)
        if missing / (span * len(channels)) <= null_budget:
            return st
    return starts[-1]


# --- alignment measurement ----------------------------------------------------

_TEXT_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+)(?![\w])")

# Non-federal affiliations that appear on some report front matter. The containing document
# is a federal publication (a Council SAFE report or a SEDAR stock assessment report), so
# `public-domain-us-gov` is defensible for the document -- but a chapter co-authored by a
# state or university scientist is the same question #31_usdm_drought had to answer on its
# bylines, so the detection is recorded per record and the call stays reversible by filter
# rather than by rebuild.
_NONFED = {
    "ADFG": r"Alaska Department of Fish and Game|ADF&G",
    "WDFW": r"Washington Department of Fish and Wildlife|\bWDFW\b",
    "ODFW": r"Oregon Department of Fish and Wildlife|\bODFW\b",
    "CDFW": r"California Department of Fish and (?:Wildlife|Game)|\bCDFW\b",
    "FL_FWC": r"Florida Fish and Wildlife|Fish and Wildlife Research Institute|\bFWRI\b",
    "state_marine_agency": r"Department of Marine Resources|Division of Marine Fisheries",
    "university": r"\bUniversity of\b|Institute of Marine Sciences",
}


def nonfederal_affiliations(text: str, window: int = 1500) -> List[str]:
    head = text[:window]
    return sorted(k for k, p in _NONFED.items() if re.search(p, head, re.I))


def text_numbers(text: str) -> List[float]:
    out: List[float] = []
    for m in _TEXT_NUM.finditer(text):
        try:
            out.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return out


def recites_terminal(text_nums: Sequence[float], terminals: Sequence[float],
                     rel_tol: float = 0.005) -> bool:
    """True when the prose quotes a channel's terminal value.

    Guards against coincidence: values that are plausibly a calendar year (1800-2100 and
    integral) are ignored, because every assessment report is dense with years and matching
    one proves nothing. Verified with a permutation control in measure_alignment().
    """
    for v in terminals:
        if v is None or abs(v) < 1e-9:
            continue
        if 1800 <= v <= 2100 and abs(v - round(v)) < 1e-9:
            continue
        tol = max(abs(v) * rel_tol, 5e-4)
        for n in text_nums:
            if abs(n - v) <= tol:
                return True
    return False


# --- pipeline -----------------------------------------------------------------

def build(cfg: Dict[str, Any]) -> Tuple[List[dict], Dict[str, Any]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    cache = rp(d["cache_dir"])
    maxrec = out_cfg.get("max_records")
    min_points = int(d.get("min_points", 8))
    null_budget = float(d.get("null_budget", 0.20))
    allowlist = set(d["jurisdiction_allowlist"])
    min_chars = int(t.get("min_text_chars", 600))
    max_chars = int(t.get("max_text_chars", 24000))
    omnibus_mode = str(d.get("omnibus_mode", "scoped"))

    stat: Dict[str, int] = {
        "entities": 0, "summary_rows": 0,
        "jurisdiction_excluded": 0, "missing_ids": 0, "no_report_link": 0,
        "candidates": 0,
        "report_not_pdf": 0, "no_text_layer": 0,
        "omnibus_unscoped": 0, "omnibus_scoped": 0,
        "short_text": 0, "no_timeseries": 0, "short_window": 0,
        "duplicate_text": 0, "invalid": 0, "emitted": 0,
    }

    entities = fetch_entities(d, cache)
    stat["entities"] = len(entities)
    entity_ids = [e["id"] for e in entities]
    name_by_id = {e["id"]: e["name"] for e in entities}
    print(f"  {len(entities)} stock entities found", file=sys.stderr)

    rows, links = fetch_summary(entity_ids, d, cache)
    if not rows:
        return [], {"stats": stat}
    idx = {h: i for i, h in enumerate(rows[0])}
    stat["summary_rows"] = len(rows) - 1

    candidates: List[dict] = []
    for r_i, r in enumerate(rows[1:], start=2):  # excel row number (1-indexed + header)
        get = lambda k: (r[idx[k]] if k in idx and len(r) > idx[k] else "")
        jur = get("Jurisdiction")
        if jur not in allowlist:
            stat["jurisdiction_excluded"] += 1
            continue
        stock_id, asmt_id = id_str(get("Stock ID")), id_str(get("Assessment ID"))
        if not stock_id or not asmt_id:
            stat["missing_ids"] += 1
            continue
        report_url = None
        for col_name in ("Final Assessment Report 1", "Final Assessment Report 2"):
            if col_name in idx:
                cell = f"{col_letter(idx[col_name])}{r_i}"
                if cell in links:
                    report_url = links[cell]
                    break
        if report_url is None:
            stat["no_report_link"] += 1
            continue
        candidates.append({
            "stock_id": stock_id, "asmt_id": asmt_id, "jurisdiction": jur,
            "stock_name": name_by_id.get(stock_id, get("Stock Name")),
            "science_center": get("Science Center"), "report_url": report_url,
            "last_data_year": br_num(get("Last Data Year")),
            "asmt_year": br_num(get("Assessment Year")),
        })
    stat["candidates"] = len(candidates)

    # Omnibus detection is structural: a report file linked by rows for MORE THAN ONE stock
    # is a multi-stock document. This is exact source metadata, not a page-count heuristic.
    rows_by_file: Dict[str, List[dict]] = {}
    for c in candidates:
        rows_by_file.setdefault(file_id(c["report_url"]), []).append(c)
    for f, group in rows_by_file.items():
        stocks = {c["stock_id"] for c in group}
        for c in group:
            c["is_omnibus"] = len(stocks) > 1
            c["co_stocks"] = sorted({x["stock_name"] for x in group if x["stock_id"] != c["stock_id"]})
    n_omni = sum(1 for c in candidates if c["is_omnibus"])
    print(f"  {len(candidates)} candidates across {len(rows_by_file)} distinct report files "
          f"({n_omni} rows on a multi-stock file)", file=sys.stderr)

    # Time series: fetch in the same batch composition fetch_timeseries.py used, so a warm
    # cache is actually hit (the cache key is the full request URL, which contains the id list).
    ts_by_asmt: Dict[str, Dict[str, Any]] = {}
    batch_file = cache / "ts_batches.json"
    size = int(d.get("ts_batch_size", 50))
    if batch_file.exists():
        batches = json.loads(batch_file.read_text())
    else:
        ids = sorted({c["asmt_id"] for c in candidates}, key=int)
        batches = [ids[i:i + size] for i in range(0, len(ids), size)]
        batch_file.parent.mkdir(parents=True, exist_ok=True)
        batch_file.write_text(json.dumps(batches))
    for i, bt in enumerate(batches, 1):
        ts_by_asmt.update(parse_timeseries_table(fetch_timeseries_batch(bt, d, cache)))
        if i % 20 == 0:
            print(f"    ts batch {i}/{len(batches)}", file=sys.stderr)
    print(f"  {len(ts_by_asmt)} assessments carry series", file=sys.stderr)

    # Newest assessments first -- the deepest, best-formatted reports.
    candidates.sort(key=lambda c: (-(c["asmt_year"] or 0), c["stock_name"], c["asmt_id"]))

    records: List[dict] = []
    seen_text: Dict[str, str] = {}
    pages_cache: Dict[str, List[str]] = {}
    for c in candidates:
        if maxrec is not None and len(records) >= int(maxrec):
            break
        # Checked before any PDF work: in "drop" mode these rows cost nothing, and the
        # omnibus documents are the big ones (up to 2,445 pages).
        if c["is_omnibus"] and omnibus_mode == "drop":
            stat["omnibus_unscoped"] += 1
            continue
        fp = cache_path(cache, "pdf", c["report_url"])
        raw = fetch_cached(c["report_url"], cache, "pdf", d)
        if raw is None or not raw.startswith(b"%PDF"):
            stat["report_not_pdf"] += 1
            continue
        f = file_id(c["report_url"])
        if f not in pages_cache:
            if len(pages_cache) > 12:            # bound memory on 1-2k-page documents
                pages_cache.clear()
            pages_cache[f] = repex.pages(fp)
        pgs = pages_cache[f]
        if not pgs or not any(p.strip() for p in pgs):
            stat["no_text_layer"] += 1
            continue

        scope_pages = pgs
        scope = None
        if c["is_omnibus"]:
            scope = repex.locate_scope(pgs, c["stock_name"], c["co_stocks"])
            if scope is None:
                # Cannot isolate this stock's own chapter. Pairing the whole document -- or a
                # section shared with another stock -- with this stock's series would be
                # boilerplate reuse, so drop the row.
                stat["omnibus_unscoped"] += 1
                continue
            scope_pages = pgs[scope[0]: scope[1] + 1]
            stat["omnibus_scoped"] += 1

        text, sections = repex.narrative(repex.clean_prose(scope_pages), max_chars=max_chars)
        if len(text) < min_chars:
            stat["short_text"] += 1
            continue

        entry = ts_by_asmt.get(c["asmt_id"])
        chans = [(p, ch["unit"], ch["series"]) for p, ch in (entry or {}).get("channels", {}).items()
                 if ch["series"]] if entry else []
        if not chans:
            stat["no_timeseries"] += 1
            continue

        # End the window at the REAL max year the channels reach, not the assessment's
        # "last data year" metadata claim -- that field reflects the freshest raw input
        # (catch/landings), while model-derived channels (Fmort/Abundance) commonly stop 1-3
        # years earlier (the terminal-year problem in stock assessment). Still capped AT
        # last_data_year so a channel never runs past the assessment's stated currency.
        end_y = max(max(s) for _, _, s in chans)
        if c["last_data_year"]:
            end_y = min(end_y, int(c["last_data_year"]))
        chans = [ch for ch in chans if min(ch[2]) <= end_y]
        if not chans:
            stat["short_window"] += 1
            continue
        start_y = choose_window(chans, end_y, null_budget)
        n_points = end_y - start_y + 1
        if n_points < min_points:
            stat["short_window"] += 1
            continue

        timeseries, channel_names, terminals = [], [], []
        for param, unit_raw, series in chans:
            vals = [round(series[y], 4) if y in series else None for y in range(start_y, end_y + 1)]
            if all(v is None for v in vals):
                continue
            unit = (unit_raw or param).strip().lower().replace(" ", "_").replace("/", "_per_") or "value"
            timeseries.append({"values": vals, "unit": f"{param.lower()}_{unit}"[:60], "freq": "1y"})
            channel_names.append(param)
            if vals[-1] is not None:
                terminals.append(vals[-1])
        if not timeseries:
            stat["no_timeseries"] += 1
            continue

        # Texts are deduped globally: an omnibus chapter can be claimed by two assessments of
        # the same stock in one document, and shipping identical prose twice is fake scale.
        key = hashlib.sha1(text.encode()).hexdigest()
        if key in seen_text:
            stat["duplicate_text"] += 1
            continue
        seen_text[key] = c["asmt_id"]

        tnums = text_numbers(text)
        alignment = "recites" if recites_terminal(tnums, terminals) else "describes"
        try:
            rec = emit_record(
                text=f"{text}\n\n<ts></ts>",
                timeseries=timeseries,
                alignment=alignment,
                license="public-domain-us-gov",
                source=c["report_url"],
                dataset="noaa_stock_assessments",
                series_id=f"noaa_stock_{c['stock_id']}_asmt{c['asmt_id']}",
                domain="fisheries",
                region="US",
                period_start=f"{start_y:04d}-01-01",
                # period_end is the window's OWN terminal year, not last_data_year -- those
                # disagreed in 16% of the demo's records, breaking the structural claim.
                period_end=f"{end_y:04d}-12-31",
                meta={
                    "stock_id": c["stock_id"],
                    "stock_name": c["stock_name"],
                    "jurisdiction": c["jurisdiction"],
                    "science_center": c["science_center"],
                    "assessment_id": c["asmt_id"],
                    "assessment_year": c["asmt_year"],
                    "last_data_year": c["last_data_year"],
                    "channels": channel_names,
                    "n_points": n_points,
                    "period_start_year": start_y,
                    "period_end_year": end_y,
                    "sections": sections,
                    "report_pages": len(pgs),
                    "nonfederal_affiliation": nonfederal_affiliations(text) or None,
                    "multi_stock_report": bool(c["is_omnibus"]),
                    "scope_pages": list(scope) if scope else None,
                    "n_stocks_on_report": 1 + len(c["co_stocks"]),
                },
            )
        except ValueError as e:
            print(f"  ! emit_record rejected asmt {c['asmt_id']}: {e}", file=sys.stderr)
            stat["invalid"] += 1
            continue
        records.append(rec)
        stat["emitted"] += 1
        if stat["emitted"] % 100 == 0:
            print(f"  emitted {stat['emitted']}", file=sys.stderr)

    # --- reconcile: the build refuses to report numbers that do not balance -------------
    recon: Dict[str, Any] = {}
    lhs = stat["summary_rows"]
    rhs = (stat["jurisdiction_excluded"] + stat["missing_ids"] + stat["no_report_link"]
           + stat["candidates"])
    recon["rows_balance"] = {"summary_rows": lhs, "sum_of_parts": rhs, "ok": lhs == rhs}
    cand_parts = (stat["emitted"] + stat["report_not_pdf"] + stat["no_text_layer"]
                  + stat["omnibus_unscoped"] + stat["short_text"] + stat["no_timeseries"]
                  + stat["short_window"] + stat["duplicate_text"] + stat["invalid"])
    full_run = maxrec is None
    recon["candidate_balance"] = {"candidates": stat["candidates"], "sum_of_parts": cand_parts,
                                  "ok": (cand_parts == stat["candidates"]) if full_run else None}
    if full_run:
        for name, chk in recon.items():
            if chk["ok"] is False:
                raise SystemExit(f"RECONCILE FAILED ({name}): {chk}")
    return records, {"stats": stat, "reconcile": recon}


def measure_alignment(records: Sequence[dict]) -> Dict[str, Any]:
    """Post-build measurement, reported in run_report.json.

    The `recites` tag gets a permutation control in the #41/#08 style: the same terminal
    values tested against a DIFFERENT record's text. A tag is only meaningful if the true
    rate clears the control rate by a wide margin.
    """
    if not records:
        return {}
    import statistics
    terms, texts = [], []
    for r in records:
        tv = [ch["values"][-1] for ch in r["timeseries"] if ch["values"][-1] is not None]
        terms.append(tv)
        texts.append(text_numbers(r["text"]))
    true_hits = sum(1 for tv, tx in zip(terms, texts) if recites_terminal(tx, tv))
    ctrl_hits = sum(1 for i, tv in enumerate(terms)
                    if recites_terminal(texts[(i + 7) % len(texts)], tv))
    lens = [len(r["text"]) for r in records]
    npts = [r["meta"]["n_points"] for r in records]
    nch = [len(r["timeseries"]) for r in records]
    tot = sum(len(ch["values"]) for r in records for ch in r["timeseries"])
    nul = sum(1 for r in records for ch in r["timeseries"] for v in ch["values"] if v is None)
    # structural checks
    struct_end = sum(1 for r in records
                     if int(r["period_end"][:4]) == r["meta"]["period_end_year"])
    struct_len = sum(1 for r in records
                     if all(len(ch["values"]) == r["meta"]["n_points"] for ch in r["timeseries"]))
    struct_term = sum(1 for r in records
                      if any(ch["values"][-1] is not None for ch in r["timeseries"]))
    return {
        "n": len(records),
        "text_chars": {"min": min(lens), "median": statistics.median(lens), "max": max(lens)},
        "series_points": {"min": min(npts), "median": statistics.median(npts), "max": max(npts),
                          "ge_24": sum(1 for x in npts if x >= 24),
                          "ge_32": sum(1 for x in npts if x >= 32),
                          "ge_43": sum(1 for x in npts if x >= 43)},
        "channels": {"min": min(nch), "median": statistics.median(nch), "max": max(nch)},
        "timesteps": sum(npts), "datapoints": tot,
        "null_pct": round(100.0 * nul / tot, 3),
        "distinct_texts": len({r["text"] for r in records}),
        "distinct_series_id": len({r["series_id"] for r in records}),
        "recites": true_hits, "describes": len(records) - true_hits,
        "recite_rate_pct": round(100.0 * true_hits / len(records), 1),
        "permutation_control_pct": round(100.0 * ctrl_hits / len(records), 1),
        "structural": {
            "period_end_eq_series_terminal_pct": round(100.0 * struct_end / len(records), 2),
            "all_channels_equal_length_pct": round(100.0 * struct_len / len(records), 2),
            "terminal_point_non_null_pct": round(100.0 * struct_term / len(records), 2),
        },
    }


def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    records, extra = build(cfg)
    report = {"stats": extra["stats"], "reconcile": extra.get("reconcile", {}),
              "alignment": measure_alignment(records), "config_snapshot": cfg, "dry_run": dry}

    if dry:
        if records:
            print("\n--- sample record ---")
            r0 = dict(records[0]); r0["text"] = r0["text"][:900] + "…"
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:3500])
        print("\n" + json.dumps({k: report[k] for k in ("stats", "reconcile", "alignment")},
                                indent=2, default=str))
        return report

    out_cfg = cfg["output"]
    op = rp(out_cfg["output_path"]); op.parent.mkdir(parents=True, exist_ok=True)
    with op.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if records and out_cfg.get("samples_path"):
        sp = rp(out_cfg["samples_path"]); sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("w", encoding="utf-8") as fh:
            json.dump(records[:3], fh, ensure_ascii=False, indent=2); fh.write("\n")
    rpath = rp(out_cfg["report_path"]); rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Build NOAA Fisheries Stock Assessments -> CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print("\nDone: {emitted} records from {candidates} candidates "
          "(not_pdf={report_not_pdf}, no_text_layer={no_text_layer}, "
          "omnibus_unscoped={omnibus_unscoped}, short_text={short_text}, "
          "no_timeseries={no_timeseries}, short_window={short_window}, "
          "duplicate_text={duplicate_text}, invalid={invalid})".format(**s), file=sys.stderr)


if __name__ == "__main__":
    main()
