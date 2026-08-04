#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from a Richmond Fed Fifth District survey.

One record = **(data month x the release's own narrative block)**: that block of the
release's prose, verbatim, paired with a trailing 36-month window of *exactly the
indicators the block names*, ending on the month the release reports.

Two things make this package what it is, and both are measurements rather than choices:

1. **The series is the as-first-published vintage, stitched from each release's own
   table -- the published workbook could not be used.** The release says why in its own
   Technical Notes: "Seasonal adjustment factors are recalculated every July and the
   entire series is revised." Measured over the 102 cached 2018+ releases, the value a
   release printed still equals today's `*_historicaldata.xlsx` in only **27.9%**
   (manufacturing) / **26.5%** (service sector) of cells, and the agreement rate tracks
   the revision schedule exactly (2026: 100%, 2025: ~70%, 2018-23: 11-20%). The banked
   build paired real prose with numbers the prose never quoted in ~72% of cells. Same
   finding as `47_philadelphia_mbos` and `48_dallas_tmos`; third in the family.

   The stitch is checkable: consecutive releases both print two of the same three months,
   and they agree **100% in every release month except July** (19.7% there) -- i.e. exactly
   at the annual re-benchmark, which is the vintage effect itself. See
   `--audit-vintage`.

2. **The release universe is 3x what the banked build reached.** It runs 1997-01 ->
   2026-06 across three site layouts (see `richsrc`), 309/308 release months harvested
   with zero unresolved fetches -- not the 102 months of the live site. The one real gap
   is **2005-01 .. 2008-09**, which Wayback holds in no layout.

Nothing in a record's text is generated: `<ts></ts>` is appended directly to the Bank's
own words. Provenance lives in `meta`.

Usage:
  python scripts/harvest.py                      # fetch the documents first (cached)
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3
  python scripts/build_cpt_jsonl.py --set output.max_records=null
  python scripts/build_cpt_jsonl.py --audit-vintage      # as-published vs workbook
  python scripts/build_cpt_jsonl.py --audit-convention   # data-month convention per era
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import io
import json
import re
import shutil
import statistics
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required. pip install -r requirements.txt") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
import richnarr                                              # noqa: E402
import richtab                                               # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record                                 # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# --- config helpers (same conventions as the sibling packages) --------------

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


def ym_shift(ym: str, back: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m -= back
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


# --- the released workbook, used only for the vintage audit -----------------

def _col_idx(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(raw: bytes) -> List[List[str]]:
    z = zipfile.ZipFile(io.BytesIO(raw))
    ss: List[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(_NS + "si"):
            ss.append("".join(t.text or "" for t in si.iter(_NS + "t")))
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows: List[List[str]] = []
    for row in sheet.find(_NS + "sheetData").findall(_NS + "row"):
        cells: Dict[int, str] = {}
        maxc = -1
        for c in row.findall(_NS + "c"):
            ci = _col_idx(c.get("r"))
            v = c.find(_NS + "v")
            cells[ci] = "" if v is None else (ss[int(v.text)] if c.get("t") == "s" else v.text)
            maxc = max(maxc, ci)
        rows.append([cells.get(i, "") for i in range(maxc + 1)])
    return rows


def load_workbook(cfg: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """The Bank's published (revised) series. NOT the record series -- see the module
    docstring. Kept only so `--audit-vintage` can quantify the drift."""
    fp = rp(cfg["data"]["cache_dir"]) / cfg["data"]["workbook_cache_name"]
    if not fp.exists():
        return {}
    rows = read_xlsx(fp.read_bytes())
    hdr = [h.strip() for h in rows[0]]
    out: Dict[str, Dict[str, float]] = {}
    for r in rows[1:]:
        if not r or not r[0].strip():
            continue
        try:
            n = int(float(r[0]))
        except (ValueError, TypeError):
            continue
        d = dt.date(1899, 12, 30) + dt.timedelta(days=n)
        rec = {}
        for i, col in enumerate(hdr):
            if i == 0 or i >= len(r):
                continue
            try:
                rec[col] = float(r[i])
            except (ValueError, TypeError):
                pass
        out[f"{d.year:04d}-{d.month:02d}"] = rec
    return out


# --- the harvested release documents ---------------------------------------

class Release:
    """One release: its documents, its parsed table, and its narrative blocks."""

    def __init__(self, release: str) -> None:
        self.release = release                # 'YYYY-MM' or 'YYYY-MM-DD'
        self.filename_release = release
        self.docs: Dict[str, Path] = {}
        self.table: Optional[richtab.Table] = None
        self.table_fmt: Optional[str] = None
        self.blocks: List[richnarr.Block] = []
        self.headline: Optional[str] = None
        self.narr_fmt: Optional[str] = None

    @property
    def sort_key(self) -> str:
        return self.release if len(self.release) == 10 else self.release + "-99"


def collect_releases(cfg: Dict[str, Any]) -> Dict[str, Release]:
    cache = rp(cfg["data"]["cache_dir"])
    rels: Dict[str, Release] = {}
    for p in sorted((cache / "docs").glob("*__*")):
        rel, rest = p.name.split("__")
        kind = rest.split(".")[0]
        rels.setdefault(rel, Release(rel)).docs[kind] = p
    # the live-site PDFs the first build cached, keyed by release month
    for p in sorted((cache / "releases").glob("*.pdf")):
        rels.setdefault(p.stem, Release(p.stem)).docs["pdf_live"] = p
    return rels


TABLE_ORDER = ["csv", "pdf", "pdf_live", "tbl_html", "page"]
NARR_ORDER = ["narr_html", "pdf", "pdf_live", "page"]


def load_releases(cfg: Dict[str, Any], stat: collections.Counter) -> Dict[str, Release]:
    """Collect, parse and re-key every release by the date its document prints.

    The re-key matters: correcting `mfg0709.html` from 2009-07 to its real 2002-07 would
    otherwise leave it filed under the old key, so it would still sort as a 2009 release
    and its 2002 values would enter the 2009 vintage.
    """
    rels = collect_releases(cfg)
    for rel in rels.values():
        parse_release(rel, cfg, stat)
    merged: Dict[str, Release] = {}
    for rel in sorted(rels.values(), key=lambda r: (r.release, r.filename_release)):
        cur = merged.get(rel.release)
        if cur is None:
            merged[rel.release] = rel
            continue
        stat["releases_merged_after_date_correction"] += 1
        for k, v in rel.docs.items():
            cur.docs.setdefault(k, v)
        if cur.table is None and rel.table is not None:
            cur.table, cur.table_fmt = rel.table, rel.table_fmt
        if not cur.blocks and rel.blocks:
            cur.blocks, cur.headline, cur.narr_fmt = rel.blocks, rel.headline, rel.narr_fmt
    return merged


def parse_release(rel: Release, cfg: Dict[str, Any], stat: collections.Counter) -> None:
    """Fill in `rel.table` and `rel.blocks` from the best available document.

    The month labels come from whichever format carries them (the PDF and the retired HTML
    table both do; the CSV does not), so a release with only a CSV borrows its months from
    the PDF of the same release when there is one -- and `--audit-convention` measures the
    two against each other on the 99 releases that have both rather than assuming the
    data-month rule, which flips inside the 2005-2007 gap.
    """
    # The release date comes from the document's own dateline, not its filename -- see
    # `richnarr.document_date`. It has to be settled first, because a table whose header
    # prints bare month names ("June  May  April") can only be resolved against it.
    for kind in ("narr_html", "pdf", "pdf_live", "page", "tbl_html"):
        p = rel.docs.get(kind)
        if p is None:
            continue
        try:
            dd = richnarr.document_date(p.read_bytes(),
                                        "pdf" if kind == "pdf_live" else kind)
        except Exception:
            dd = None
        if dd:
            if dd[:7] != rel.filename_release[:7]:
                stat["release_date_corrected_from_document"] += 1
            rel.release = dd
            break
    else:
        stat["release_date_from_filename_only"] += 1

    relmonth = rel.release[:7]
    # Table: among the formats that label their own months, take the one carrying the most
    # indicators -- not the first in a fixed preference order. The formats are not nested:
    # a release's CSV export and its PDF are the same table, but the CSV sometimes lists
    # fewer rows, so a fixed "CSV first" order silently dropped ~11 channels on the six
    # months whose CSV is a spreadsheet dump. Ties break on TABLE_ORDER for determinism.
    labelled: List[Tuple[int, int, richtab.Table, str]] = []
    unlabelled: Optional[Tuple[richtab.Table, str]] = None
    for kind in TABLE_ORDER:
        p = rel.docs.get(kind)
        if p is None:
            continue
        try:
            t = richtab.parse(p.read_bytes(), "pdf" if kind == "pdf_live" else kind, relmonth)
        except Exception:
            stat["table_parse_error"] += 1
            continue
        if not t.cells:
            continue
        if t.months:
            labelled.append((-len(t.cells), TABLE_ORDER.index(kind), t, kind))
        elif unlabelled is None:
            unlabelled = (t, kind)
    if labelled:
        # Merge the renderings rather than choosing one. A release's CSV export and its PDF
        # are two renderings of the *same* table and neither is a superset: the CSV
        # occasionally omits rows the PDF prints and vice versa, so picking one by any rule
        # loses indicators. Merging keeps every row either of them prints, and where both
        # print a cell it is a free cross-check -- `table_cell_conflicts` in the run report
        # counts the disagreements (measured: 0).
        labelled.sort(key=lambda x: (x[0], x[1]))
        base = labelled[0][2]
        cells = dict(base.cells)
        avg = dict(base.avg)
        fmts = [labelled[0][3]]
        for _n, _o, t, kind in labelled[1:]:
            if t.months != base.months:
                stat["table_month_mismatch_between_formats"] += 1
                continue
            used = False
            for key, vals in t.cells.items():
                if key not in cells:
                    cells[key] = vals
                    avg[key] = t.avg.get(key)
                    used = True
                else:
                    for a, b in zip(cells[key], vals):
                        if a is not None and b is not None and abs(a - b) > 0.051:
                            stat["table_cell_conflicts"] += 1
            if used:
                fmts.append(kind)
        rel.table = base._replace(cells=cells, avg=avg)
        rel.table_fmt = "+".join(fmts)
    elif unlabelled is not None:
        # no month labels anywhere for this release: fall back to the era convention,
        # which `--audit-convention` verifies rather than trusts
        t, kind = unlabelled
        off = int(cfg["data"]["data_month_offset_by_era"]
                  ["retired" if relmonth < cfg["data"]["modern_era_start"] else "modern"])
        rel.table = t._replace(months=[ym_shift(relmonth, off + k) for k in range(3)])
        rel.table_fmt = kind + "+era_convention"

    for kind in NARR_ORDER:
        p = rel.docs.get(kind)
        if p is None:
            continue
        k2 = {"narr_html": "narr_html", "page": "page"}.get(kind, "pdf")
        try:
            bl, hl = richnarr.blocks(p.read_bytes(), k2, release=relmonth)
        except Exception:
            stat["narr_parse_error"] += 1
            continue
        if bl:
            rel.blocks, rel.headline, rel.narr_fmt = bl, hl, kind
            break


# --- the as-published vintage ----------------------------------------------

def build_vintage(rels: Dict[str, Release]
                  ) -> Dict[Tuple[str, str], List[Tuple[str, str, float]]]:
    """(channel, half) -> [(data_month, release_sort_key, value)], for every printed cell.

    Every release prints three consecutive months, so a month is printed by up to three
    releases. Which of them a record should use is decided per record by `vintage_as_of`.
    """
    out: Dict[Tuple[str, str], List[Tuple[str, str, float]]] = collections.defaultdict(list)
    for rel in rels.values():
        if rel.table is None or not rel.table.months:
            continue
        for key, vals in rel.table.cells.items():
            for ym, v in zip(rel.table.months, vals):
                if v is not None:
                    out[key].append((ym, rel.sort_key, float(v)))
    for key in out:
        out[key].sort()
    return out


def vintage_as_of(vintage, key: Tuple[str, str], months: List[str], as_of: str
                  ) -> List[Optional[float]]:
    """The window for `key` over `months`, as the numbers stood when `as_of` was published.

    For each month, the value printed by the *latest release at or before* `as_of` that
    printed it. This is a real-time vintage in the ALFRED sense, and it is what makes the
    prose's own comparisons check out: a release restates the two prior months under its
    current seasonal factors, and its sentence "shipments fell to 3 from 16" quotes that
    restatement, not the number first published two months earlier.
    """
    rows = vintage.get(key)
    if not rows:
        return [None] * len(months)
    by_month: Dict[str, List[Tuple[str, float]]] = collections.defaultdict(list)
    for ym, relkey, v in rows:
        by_month[ym].append((relkey, v))
    out: List[Optional[float]] = []
    for ym in months:
        cands = [(rk, v) for rk, v in by_month.get(ym, []) if rk <= as_of]
        out.append(cands[-1][1] if cands else None)
    return out


# --- build ------------------------------------------------------------------

def build(cfg: Dict[str, Any]) -> Tuple[List[dict], Dict[str, Any]]:
    d, t, out_cfg = cfg["data"], cfg["text"], cfg["output"]
    window = int(d["window_months"])
    min_chars = int(t["min_text_chars"])
    max_null = float(d["max_null_fraction"])
    maxrec = out_cfg.get("max_records")

    stat: collections.Counter = collections.Counter()
    rels = load_releases(cfg, stat)
    stat["releases_seen"] = len(rels)
    stat["releases_with_table"] = sum(1 for r in rels.values() if r.table)
    stat["releases_with_blocks"] = sum(1 for r in rels.values() if r.blocks)

    vintage = build_vintage(rels)
    allowed_all = set(vintage)

    records: List[dict] = []
    seen_text: Dict[str, str] = {}
    seen_sid: set = set()
    figs_total = figs_missing = 0
    recites = 0

    for rel in sorted(rels.values(), key=lambda r: r.sort_key):
        if rel.table is None or not rel.table.months:
            stat["drop_release_no_table"] += 1
            stat["drop_blocks_no_table"] += len(rel.blocks)
            continue
        if not rel.blocks:
            stat["drop_release_no_narrative"] += 1
            continue
        data_month = rel.table.months[0]
        allowed = set(rel.table.cells) & allowed_all
        months = [ym_shift(data_month, window - 1 - i) for i in range(window)]

        for b in rel.blocks:
            stat["block_units"] += 1
            named = richnarr.channels_named(b, allowed)
            if not named:
                stat["drop_no_indicator_named"] += 1
                continue
            if len(b.text) < min_chars:
                stat["drop_short_text"] += 1
                continue
            # A channel the block names but whose as-published window is unusable is dropped
            # on its own; the record survives on the rest. Letting any one thin channel veto
            # the whole record meant that *recovering* a table could lose records -- a newly
            # available indicator with no history behind it killed the block it was named in.
            # The cost stays visible rather than hidden: a figure quoted for a dropped channel
            # then counts against `figures_not_in_own_series` in --audit-alignment.
            chans = []
            for key in sorted(named):
                vals = vintage_as_of(vintage, key, months, rel.sort_key)
                if vals[-1] is None:
                    stat["channel_dropped_terminal_null"] += 1
                    continue
                nulls = sum(1 for v in vals if v is None)
                if nulls / float(window) > max_null:
                    stat["channel_dropped_over_null_budget"] += 1
                    continue
                chans.append((key, vals))
            if not chans:
                stat["drop_sparse_or_short_window"] += 1
                continue

            text = f"{b.text}\n\n<ts></ts>"
            timeseries = [{"values": [None if v is None else round(v, 3) for v in vals],
                           "unit": f"{ch}_{half}" if half == "fut" else ch, "freq": "1M"}
                          for (ch, half), vals in chans]
            sid = f"{d['series_id_prefix']}_{data_month}_b{b.ordinal}"
            if sid in seen_sid:
                stat["drop_duplicate_series_id"] += 1
                continue
            if text in seen_text:
                stat["drop_duplicate_text"] += 1
                continue

            # `recites` is measured per record, as an ordered pair (this month's value then
            # last month's, within one clause) rather than any-value-matches -- see
            # richnarr.recites_ordered_pair for why, and --audit-alignment for the control.
            prose_figs = richnarr.figures(b.text)
            named_ch = [(f"{ch}_{half}" if half == "fut" else ch, vals)
                        for (ch, half), vals in chans]
            recited = richnarr.recites_ordered_pair(b.text, named_ch)
            tier = "recites" if recited else "describes"
            recites += tier == "recites"
            allvals = [v for _k, vals in chans for v in vals if v is not None]
            figs_total += len(prose_figs)
            figs_missing += sum(1 for f in richnarr.figures_with_pos(b.text)
                                if not any(richnarr.quotes(f, v) for v in allvals))

            try:
                rec = emit_record(
                    text=text,
                    timeseries=timeseries,
                    alignment=tier,
                    license=d["license_tag"],
                    source=d["source_url"],
                    dataset=d["dataset_name"],
                    series_id=sid,
                    domain="macro_econ",
                    region=d["region"],
                    period_start=f"{months[0]}-01",
                    period_end=f"{data_month}-01",
                    meta={
                        "bank": d["bank"],
                        "survey": d["survey_title"],
                        "district": d.get("district"),
                        "sector": d["domain"],
                        "release_date": rel.release,
                        "data_month": data_month,
                        "section_heading": b.heading,
                        "block_ordinal": b.ordinal,
                        "headline": rel.headline,
                        "n_points": window,
                        "channels": [ts["unit"] for ts in timeseries],
                        "series_vintage": "as-published (stitched from each release's own "
                                          "table, real-time as of this release)",
                        "narrative_format": rel.narr_fmt,
                        "table_format": rel.table_fmt,
                        "figures_in_text": len(prose_figs),
                        "recited_channel": recited,
                    },
                )
            except ValueError as exc:
                stat["drop_invalid"] += 1
                stat[f"invalid::{exc}"] += 1
                continue
            local = local_checks(rec, window)
            if local:
                stat["drop_invalid"] += 1
                stat[f"local::{local[0]}"] += 1
                continue
            records.append(rec)
            seen_text[text] = sid
            seen_sid.add(sid)
            stat["emitted"] += 1
            if maxrec is not None and len(records) >= int(maxrec):
                break
        if maxrec is not None and len(records) >= int(maxrec):
            break

    report: Dict[str, Any] = {
        "stats": dict(stat),
        "reconcile": reconcile(stat),
        "recites": recites,
        "describes": stat["emitted"] - recites,
        "figures_in_text": figs_total,
        "figures_not_in_own_series": figs_missing,
    }
    return records, report


def local_checks(rec: dict, window: int) -> List[str]:
    e = []
    if rec["text"].count("<ts></ts>") != 1:
        e.append("ts token count")
    lens = {len(c["values"]) for c in rec["timeseries"]}
    if lens != {window}:
        e.append(f"channel length {sorted(lens)} != {window}")
    if any(c["values"][-1] is None for c in rec["timeseries"]):
        e.append("terminal point null")
    return e


def reconcile(stat: collections.Counter) -> Dict[str, Any]:
    """The build raises if this does not balance -- the `41`/`08` discipline."""
    units = stat["block_units"] + stat["drop_blocks_no_table"]
    accounted = (stat["emitted"] + stat["drop_no_indicator_named"] + stat["drop_short_text"]
                 + stat["drop_sparse_or_short_window"] + stat["drop_duplicate_series_id"]
                 + stat["drop_duplicate_text"] + stat["drop_invalid"]
                 + stat["drop_blocks_no_table"])
    return {"block_units": units, "accounted": accounted, "balances": units == accounted}


# --- audits ----------------------------------------------------------------

def audit_vintage(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """As-published (each release's own table) vs the current published workbook."""
    wb = load_workbook(cfg)
    if not wb:
        return {"error": "workbook not cached"}
    colmap = cfg["data"]["workbook_columns"]
    stat: collections.Counter = collections.Counter()
    rels = load_releases(cfg, stat)
    peryear: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])
    drift: List[float] = []
    tot = [0, 0]
    for rel in rels.values():
        if rel.table is None or not rel.table.months:
            continue
        for (ch, half), vals in rel.table.cells.items():
            cols = colmap.get(ch)
            col = None if not cols else (cols[0] if half == "cur" else cols[1])
            if not col:
                continue
            for ym, v in zip(rel.table.months, vals):
                w = wb.get(ym, {}).get(col)
                if v is None or w is None:
                    continue
                tot[1] += 1
                peryear[ym[:4]][1] += 1
                if abs(v - w) <= 0.051:
                    tot[0] += 1
                    peryear[ym[:4]][0] += 1
                else:
                    drift.append(abs(v - w))
    return {
        "cells_compared": tot[1],
        "as_published_equals_workbook": tot[0],
        "pct": round(100.0 * tot[0] / max(1, tot[1]), 1),
        "median_abs_drift_where_different": round(statistics.median(drift), 2) if drift else None,
        "max_abs_drift": max(drift) if drift else None,
        "by_data_year": {y: f"{a}/{b}={100*a/b:.0f}%" for y, (a, b) in sorted(peryear.items())},
    }


def audit_overlap(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Cross-release overlap agreement, by release month: the parser's own ground truth
    and the direct measurement of the July re-benchmark."""
    stat: collections.Counter = collections.Counter()
    rels = load_releases(cfg, stat)
    good = sorted((r for r in rels.values() if r.table and r.table.months),
                  key=lambda r: r.sort_key)
    bymon: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])
    for a, b in zip(good, good[1:]):
        eq, n = richtab.overlap_agreement(b.table, a.table)
        bymon[b.release[5:7]][0] += eq
        bymon[b.release[5:7]][1] += n
    avg_ok = avg_n = 0
    for r in good:
        o, n = richtab.check_average(r.table)
        avg_ok += o
        avg_n += n
    return {"by_release_month": {m: f"{a}/{b}={100*a/max(1,b):.1f}%"
                                 for m, (a, b) in sorted(bymon.items())},
            "printed_3month_average_checksum": f"{avg_ok}/{avg_n}",
            "header_label_anomalies": len(richtab.HEADER_ANOMALIES)}


def audit_convention(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Does the release report month M or M-1? Measured, per era, from the table's own
    column headings -- never computed. The rule flips inside the 2005-2007 gap."""
    stat: collections.Counter = collections.Counter()
    rels = load_releases(cfg, stat)
    by_era: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for rel in sorted(rels.values(), key=lambda r: r.sort_key):
        if rel.table is None or not rel.table.months or "era_convention" in (rel.table_fmt or ""):
            continue
        relmonth = rel.release[:7]
        off = 0
        probe = relmonth
        while probe != rel.table.months[0] and off < 6:
            off += 1
            probe = ym_shift(relmonth, off)
        era = "retired(<=2004)" if relmonth < "2005" else "modern(>=2008)"
        by_era[era][f"M-{off}" if probe == rel.table.months[0] else "unresolved"] += 1
    out = {era: dict(c) for era, c in sorted(by_era.items())}
    out["date_corrections"] = stat["release_date_corrected_from_document"]
    out["releases_merged"] = stat["releases_merged_after_date_correction"]
    return out


def audit_alignment(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Is the pairing real? Every claim below is measured on the built file.

    Reported in three layers, because "alignment" means different things:
      * **structural** -- properties the construction guarantees, checked rather than
        asserted (window ends on the reported month, every channel exactly as long as
        `n_points`, terminal point non-null).
      * **quotation** -- of the numbers the prose actually states, how many are values in
        *this record's own* series. This is the check that caught a 15.8% cross-indicator
        contamination in `47_philadelphia_mbos`.
      * **tier, with a permutation control** -- the `recites` test run against *another
        month's* series for the same channel set. Without the control a tag on bounded
        small integers means little: the single-value form of this test fires 23.7% of the
        time on mismatched pairs, so a third of those tags would be coincidence. The
        ordered-pair form is what the package ships.
    """
    import random
    path = rp(cfg["output"]["output_path"])
    if not path.exists():
        return {"error": f"{path} not built yet"}
    recs = [json.loads(l) for l in path.open()]
    n = len(recs)
    out: Dict[str, Any] = {"records": n}

    # -- structural
    out["structural"] = {
        "period_end == data_month": f"{sum(1 for r in recs if r['period_end'][:7] == r['meta']['data_month'])}/{n}",
        "period_start == window start": f"{sum(1 for r in recs if r['period_start'][:7] == ym_shift(r['meta']['data_month'], int(r['meta']['n_points']) - 1))}/{n}",
        "every channel length == n_points": f"{sum(1 for r in recs if all(len(c['values']) == r['meta']['n_points'] for c in r['timeseries']))}/{n}",
        "terminal point non-null": f"{sum(1 for r in recs if all(c['values'][-1] is not None for c in r['timeseries']))}/{n}",
        "exactly one <ts></ts>": f"{sum(1 for r in recs if r['text'].count('<ts></ts>') == 1)}/{n}",
    }

    # -- quotation
    tot = miss = 0
    per_rec_all = 0
    for r in recs:
        body = r["text"].split("\n\n<ts></ts>")[0]
        figs = richnarr.figures_with_pos(body)
        vals = [v for c in r["timeseries"] for v in c["values"] if v is not None]
        bad = 0
        for f in figs:
            tot += 1
            if not any(richnarr.quotes(f, v) for v in vals):
                miss += 1
                bad += 1
        per_rec_all += (bad == 0)
    out["quotation"] = {
        "figures_quoted": tot,
        "not_in_own_series": miss,
        "pct_not_in_own_series": round(100.0 * miss / max(1, tot), 2),
        "records_where_every_figure_is_in_its_own_series": f"{per_rec_all}/{n}",
    }

    # -- tier + permutation control
    random.seed(17)
    by_chanset: Dict[tuple, List[int]] = collections.defaultdict(list)
    for i, r in enumerate(recs):
        by_chanset[tuple(sorted(c["unit"] for c in r["timeseries"]))].append(i)

    def chans_of(r):
        return [(c["unit"], c["values"]) for c in r["timeseries"]]

    pair_true = pair_ctrl = pair_ctrl_n = 0
    single_true = single_ctrl = single_ctrl_n = 0
    for r in recs:
        body = r["text"].split("\n\n<ts></ts>")[0]
        figs = richnarr.figures_with_pos(body)
        pair_true += bool(richnarr.recites_ordered_pair(body, chans_of(r)))
        terms = [c["values"][-1] for c in r["timeseries"]]
        single_true += any(richnarr.quotes(f, t) for f in figs for t in terms)
        peers = [j for j in by_chanset[tuple(sorted(c["unit"] for c in r["timeseries"]))]
                 if recs[j]["meta"]["data_month"] != r["meta"]["data_month"]]
        if not peers:
            continue
        o = recs[random.choice(peers)]
        pair_ctrl += bool(richnarr.recites_ordered_pair(body, chans_of(o)))
        pair_ctrl_n += 1
        oterms = [c["values"][-1] for c in o["timeseries"]]
        single_ctrl += any(richnarr.quotes(f, t) for f in figs for t in oterms)
        single_ctrl_n += 1
    out["tier"] = {
        "shipped_test_ordered_pair": {
            "true": f"{pair_true}/{n} = {100*pair_true/n:.1f}%",
            "permutation_control": f"{pair_ctrl}/{pair_ctrl_n} = {100*pair_ctrl/max(1,pair_ctrl_n):.1f}%",
            "lift_pp": round(100*pair_true/n - 100*pair_ctrl/max(1, pair_ctrl_n), 1),
        },
        "rejected_test_any_terminal_value": {
            "true": f"{single_true}/{n} = {100*single_true/n:.1f}%",
            "permutation_control": f"{single_ctrl}/{single_ctrl_n} = {100*single_ctrl/max(1,single_ctrl_n):.1f}%",
            "lift_pp": round(100*single_true/n - 100*single_ctrl/max(1, single_ctrl_n), 1),
        },
        "tags_in_file": dict(collections.Counter(r["alignment"] for r in recs)),
    }

    # -- stated move reproduces from the series
    pts_ok = pts_n = dir_ok = dir_n = 0
    for r in recs:
        body = r["text"].split("\n\n<ts></ts>")[0]
        deltas = {round(abs(c["values"][-1] - c["values"][-2]), 2)
                  for c in r["timeseries"]
                  if c["values"][-1] is not None and c["values"][-2] is not None}
        for p in richnarr.stated_points_change(body):
            pts_n += 1
            if round(abs(p), 2) in deltas:
                pts_ok += 1
        want = richnarr.stated_direction(body)
        if want is not None:
            signed = [c["values"][-1] - c["values"][-2] for c in r["timeseries"]
                      if c["values"][-1] is not None and c["values"][-2] is not None]
            if signed:
                dir_n += 1
                got = 1 if sum(1 for s in signed if s > 0) >= sum(1 for s in signed if s < 0) else -1
                dir_ok += (got == want)
    out["stated_move_reproduces"] = {
        "points_change_matches_a_channel_delta": f"{pts_ok}/{pts_n} = {100*pts_ok/max(1,pts_n):.1f}%",
        "dominant_direction_matches": f"{dir_ok}/{dir_n} = {100*dir_ok/max(1,dir_n):.1f}%",
    }

    # -- series health
    lens = {len(c["values"]) for r in recs for c in r["timeseries"]}
    nulls = sum(1 for r in recs for c in r["timeseries"] for v in c["values"] if v is None)
    dp = sum(len(c["values"]) for r in recs for c in r["timeseries"])
    nch = sorted(len(r["timeseries"]) for r in recs)
    out["series_health"] = {
        "points_per_channel": sorted(lens),
        "timesteps": sum(len(r["timeseries"][0]["values"]) for r in recs),
        "datapoints": dp,
        "null_pct": round(100.0 * nulls / dp, 2),
        "channels_per_record": {"median": nch[len(nch) // 2], "min": nch[0], "max": nch[-1]},
        "distinct_channels": len({c["unit"] for r in recs for c in r["timeseries"]}),
        "distinct_texts": len({r["text"] for r in recs}),
        "distinct_series_id": len({r["series_id"] for r in recs}),
        "data_month_span": [min(r["meta"]["data_month"] for r in recs),
                            max(r["meta"]["data_month"] for r in recs)],
        "distinct_data_months": len({r["meta"]["data_month"] for r in recs}),
    }
    return out


def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    if not shutil.which("pdftotext"):
        raise SystemExit("pdftotext not found. Install poppler (apt-get install poppler-utils).")
    d, out_cfg = cfg["data"], cfg["output"]
    records, report = build(cfg)
    report.update({"survey": d["survey_title"], "bank": d["bank"],
                   "dataset": d["dataset_name"],
                   "window": f"trailing {d['window_months']} months, as-published vintage",
                   "config_snapshot": cfg, "dry_run": dry})
    if not report["reconcile"]["balances"] and out_cfg.get("max_records") is None:
        raise SystemExit(f"reconcile does not balance: {report['reconcile']}")

    if dry:
        if records:
            print("\n--- sample record ---")
            r0 = dict(records[0])
            r0["text"] = r0["text"][:700] + "…"
            r0["timeseries"] = [{**ts, "values": ts["values"][:6] + ["…"]}
                                for ts in r0["timeseries"][:3]]
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:3000])
        print("\n" + json.dumps({k: v for k, v in report.items()
                                 if k != "config_snapshot"}, indent=2)[:4000])
        return report

    op = rp(out_cfg["output_path"])
    op.parent.mkdir(parents=True, exist_ok=True)
    with op.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if records and out_cfg.get("samples_path"):
        sp = rp(out_cfg["samples_path"])
        sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("w", encoding="utf-8") as fh:
            json.dump(records[:3], fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    rpath = rp(out_cfg["report_path"])
    rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a Richmond Fed Fifth District survey → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--audit-vintage", action="store_true")
    ap.add_argument("--audit-overlap", action="store_true")
    ap.add_argument("--audit-convention", action="store_true")
    ap.add_argument("--audit-alignment", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    if args.audit_vintage:
        print(json.dumps(audit_vintage(cfg), indent=2))
        return
    if args.audit_overlap:
        print(json.dumps(audit_overlap(cfg), indent=2))
        return
    if args.audit_convention:
        print(json.dumps(audit_convention(cfg), indent=2))
        return
    if args.audit_alignment:
        print(json.dumps(audit_alignment(cfg), indent=2))
        return
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s.get('emitted', 0)} records from {s.get('block_units', 0)} block units "
          f"({s.get('releases_with_blocks', 0)} releases). reconcile={rep['reconcile']['balances']}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
