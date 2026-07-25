#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from NOAA Fisheries Stock Assessments (Stock SMART).

One record = one (stock, assessment) pair. Text = that assessment's own real final report PDF
(scientific narrative describing stock trends: biomass, recruitment, exploitation history).
Series = that stock's own historical catch/fishing-mortality/recruitment/abundance time
series through the assessment's last data year -- often DECADES deep (e.g. Acadian redfish:
71 real years, 1934-2004), far beyond the 24-32-period baseline other packages in this project
target. alignment=describes (reports narrate biological trends/dynamics, not a pure recite-
every-number style). text_quality "real".

API: NOAA Stock SMART's public JSON API (reverse-engineered from the NOAA-EDAB/stocksmart R
package's data-raw/query_stocksmart_api.R -- no auth, no key):
  - sis_servlet?jsonParam={"method":"searchEntities",...}                -> all stock entities
  - data-export-servlet?jsonParam={"dataType":"Assessment Summary Data",...}
        -> per (stock, assessment): jurisdiction, science center, last data year, and the
           real report-PDF download link(s) (as XLSX hyperlink relationships, not cell text)
  - data-export-servlet?jsonParam={"dataType":"TimeSeriesData","categories":[...],
        "asmtList":"id1,id2,..."} -> wide table, repeating column-blocks per (stock,assessment)
Report PDFs download from apps-st.fisheries.noaa.gov/sis/docServlet?fileAction=download&fileId=N.

License filter (NOT a blanket public-domain claim): jurisdictions confirmed to be either
internationally co-published (Atlantic HMS -- ICCAT-linked; IPHC -- a real US/Canada joint
commission) or state-level (ASMFC -- an interstate compact, not a federal NOAA jurisdiction)
are excluded via an allowlist built from the REAL jurisdiction distribution (3,527 real
stock-assessment rows fetched and counted 2026-07-25), not assumed. See NOTION_PAGE.md.

Examples:
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

try:
    import pdfplumber
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pdfplumber required. pip install -r requirements.txt") from exc

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
    rows = read_xlsx_rows(raw)
    links = read_xlsx_hyperlinks(raw)
    return rows, links


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


# --- pipeline -----------------------------------------------------------------

def build(cfg: Dict[str, Any]) -> Tuple[List[dict], Dict[str, int]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    cache = rp(d["cache_dir"])
    maxrec = out_cfg.get("max_records")
    min_points = int(d.get("min_points", 8))
    allowlist = set(d["jurisdiction_allowlist"])

    stat = {"entities": 0, "summary_rows": 0, "after_jurisdiction_filter": 0,
            "no_report_link": 0, "report_fetch_failed": 0, "report_too_large": 0,
            "short_text": 0, "no_timeseries": 0, "short_window": 0, "emitted": 0, "invalid": 0}

    entities = fetch_entities(d, cache)
    stat["entities"] = len(entities)
    entity_ids = [e["id"] for e in entities]
    name_by_id = {e["id"]: e["name"] for e in entities}
    print(f"  {len(entities)} stock entities found", file=sys.stderr)

    rows, links = fetch_summary(entity_ids, d, cache)
    if not rows:
        return [], stat
    hdr = rows[0]
    idx = {h: i for i, h in enumerate(hdr)}
    stat["summary_rows"] = len(rows) - 1

    # candidate (stock, assessment) rows, filtered by jurisdiction + must have >=1 report link
    candidates = []
    for r_i, r in enumerate(rows[1:], start=2):  # excel row number (1-indexed + header)
        if len(r) <= idx["Jurisdiction"]:
            continue
        jur = r[idx["Jurisdiction"]]
        if jur not in allowlist:
            continue
        stock_id = id_str(r[idx["Stock ID"]] if r[idx["Stock ID"]] else "")
        asmt_id = id_str(r[idx["Assessment ID"]] if len(r) > idx["Assessment ID"] and r[idx["Assessment ID"]] else "")
        if not stock_id or not asmt_id:
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
        last_data_year = br_num(r[idx["Last Data Year"]]) if "Last Data Year" in idx and len(r) > idx["Last Data Year"] else None
        asmt_year = br_num(r[idx["Assessment Year"]]) if "Assessment Year" in idx and len(r) > idx["Assessment Year"] else None
        candidates.append({
            "stock_id": stock_id, "asmt_id": asmt_id, "jurisdiction": jur,
            "stock_name": name_by_id.get(stock_id, r[idx["Stock Name"]] if "Stock Name" in idx else ""),
            "science_center": r[idx["Science Center"]] if "Science Center" in idx and len(r) > idx["Science Center"] else "",
            "report_url": report_url, "last_data_year": last_data_year, "asmt_year": asmt_year,
        })
    stat["after_jurisdiction_filter"] = len(candidates)
    print(f"  {len(candidates)} candidate (stock, assessment) rows after jurisdiction filter "
          f"+ report-link requirement", file=sys.stderr)

    # newest assessments first (more likely well-formed reports / longest real history)
    candidates.sort(key=lambda c: (c["asmt_year"] or 0), reverse=True)

    records: List[dict] = []
    ts_batch = int(d.get("ts_batch_size", 50))
    i = 0
    while i < len(candidates):
        if maxrec is not None and len(records) >= int(maxrec):
            break
        batch = candidates[i: i + ts_batch]
        i += ts_batch
        ts_rows = fetch_timeseries_batch([c["asmt_id"] for c in batch], d, cache)
        ts_by_asmt = parse_timeseries_table(ts_rows)

        for c in batch:
            if maxrec is not None and len(records) >= int(maxrec):
                break
            pdf_bytes = fetch_cached(c["report_url"], cache, "pdf", d)
            if pdf_bytes is None or not pdf_bytes.startswith(b"%PDF"):
                stat["report_fetch_failed"] += 1
                continue
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    npages = len(pdf.pages)
                    if npages > int(d.get("max_report_pages", 80)):
                        # Large reports are almost always multi-stock omnibus documents (e.g.
                        # the 362-page "Review of Ocean Salmon Fisheries") -- pairing the WHOLE
                        # document with one stock's series dilutes alignment (most of the text
                        # is about other stocks). Prefer tighter, single/few-stock reports.
                        stat["report_too_large"] += 1
                        continue
                    text = "\n".join((p.extract_text() or "") for p in pdf.pages).strip()
            except Exception as e:  # noqa: BLE001
                print(f"  ! pdf parse failed asmt {c['asmt_id']}: {e}", file=sys.stderr)
                stat["report_fetch_failed"] += 1
                continue
            if len(text) < int(t.get("min_text_chars", 400)):
                stat["short_text"] += 1
                continue

            entry = ts_by_asmt.get(c["asmt_id"])
            if not entry or not entry["channels"]:
                stat["no_timeseries"] += 1
                continue

            # Collect each channel's own real series first, then align ALL of them to a
            # SHARED [common_start, end_y] span -- schema requires channels sharing a freq to
            # share a length (index-aligned); trimming each channel to its own independent
            # span (the original approach) produced mismatched lengths and failed validation.
            raw_channels = [(p, ch["unit"], ch["series"]) for p, ch in entry["channels"].items() if ch["series"]]
            if not raw_channels:
                stat["no_timeseries"] += 1
                continue
            # End the window at the REAL max year actually achieved by the channels present in
            # this record, not the assessment's overall "last_data_year" metadata claim -- that
            # field commonly reflects the freshest raw input (e.g. catch/landings), while
            # model-derived channels like Fmort/Abundance often stop 1-3 years earlier (the
            # well-known "terminal year" problem in stock assessment: the most recent years'
            # fishing-mortality and biomass estimates aren't reliable until more data
            # accumulates). Forcing the window to last_data_year regardless just padded ~30% of
            # records with avoidable trailing nulls. Still cap AT last_data_year (never let a
            # channel run past the assessment's own stated currency), just don't pad up to it.
            end_y = max(max(s) for _, _, s in raw_channels)
            if c["last_data_year"]:
                end_y = min(end_y, int(c["last_data_year"]))
            common_start = max(min(s) for _, _, s in raw_channels)
            if common_start > end_y:
                stat["short_window"] += 1
                continue

            timeseries = []
            channel_names = []
            for param, unit_raw, series in raw_channels:
                vals = [round(series[y], 4) if y in series else None for y in range(common_start, end_y + 1)]
                if all(v is None for v in vals):
                    continue
                unit = (unit_raw or param).strip().lower().replace(" ", "_").replace("/", "_per_") or "value"
                timeseries.append({"values": vals, "unit": f"{param.lower()}_{unit}"[:60], "freq": "1y"})
                channel_names.append(param)
            if len(timeseries) == 0:
                stat["no_timeseries"] += 1
                continue
            max_len = end_y - common_start + 1
            if max_len < min_points:
                stat["short_window"] += 1
                continue

            full_text = f"{text}\n\n<ts></ts>"
            period_start_year = common_start
            try:
                rec = emit_record(
                    text=full_text,
                    timeseries=timeseries,
                    alignment="describes",
                    license="public-domain-us-gov",
                    source=c["report_url"],
                    dataset="noaa_stock_assessments",
                    series_id=f"noaa_stock_{c['stock_id']}_asmt{c['asmt_id']}",
                    domain="fisheries",
                    region="US",
                    period_start=f"{period_start_year:04d}-01-01",
                    period_end=f"{int(c['last_data_year']):04d}-01-01" if c["last_data_year"] else None,
                    meta={
                        "stock_id": c["stock_id"],
                        "stock_name": c["stock_name"],
                        "jurisdiction": c["jurisdiction"],
                        "science_center": c["science_center"],
                        "assessment_id": c["asmt_id"],
                        "assessment_year": c["asmt_year"],
                        "last_data_year": c["last_data_year"],
                        "channels": channel_names,
                        "n_points": max_len,
                    },
                )
            except ValueError as e:
                print(f"  ! emit_record rejected asmt {c['asmt_id']}: {e}", file=sys.stderr)
                stat["invalid"] += 1
                continue
            records.append(rec)
            stat["emitted"] += 1
            print(f"  emitted stock {c['stock_id']} ({c['stock_name'][:40]}) asmt {c['asmt_id']} "
                  f"({max_len} pts)", file=sys.stderr)

    return records, stat


def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    records, stats = build(cfg)
    report = {"stats": stats, "config_snapshot": cfg, "dry_run": dry}

    if dry:
        if records:
            print("\n--- sample record ---")
            r0 = dict(records[0]); r0["text"] = r0["text"][:700] + "…"
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:3000])
        print("\n" + json.dumps(stats, indent=2))
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
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
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
    print(f"\nDone: {s['emitted']} records (entities={s['entities']}, "
          f"after_jurisdiction_filter={s['after_jurisdiction_filter']}, "
          f"no_report_link={s['no_report_link']}, report_fetch_failed={s['report_fetch_failed']}, "
          f"report_too_large={s['report_too_large']}, short_text={s['short_text']}, "
          f"no_timeseries={s['no_timeseries']}, short_window={s['short_window']}, "
          f"invalid={s['invalid']}).", file=sys.stderr)


if __name__ == "__main__":
    main()
