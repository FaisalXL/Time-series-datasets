#!/usr/bin/env python3
"""Sharded, resumable full-archive harvest of GAIN reports -> per-shard CPT JSONL.

One shard = (PSD group, publication year). Sharding on the GROUP is not cosmetic: a shard loads
exactly one PSD bulk group into memory, and all nine at once is roughly a gigabyte of dicts.

RESUME CONTRACT: a shard's `.report.json` is written LAST, after its .jsonl is closed. So a shard
counts as done only if its report exists, and an interrupted shard is simply redone -- there is no
state in which a partial .jsonl is mistaken for a finished one.

Usage:
    python scripts/harvest.py --config config.example.yaml                 # everything, resumable
    python scripts/harvest.py --config config.example.yaml --year-from 2011
    python scripts/harvest.py --config config.example.yaml --group livestock
    python scripts/harvest.py --config config.example.yaml --plan          # shard list only
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.parse
import warnings
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import yaml

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import build_cpt_jsonl as B  # noqa: E402

import pdfplumber  # noqa: E402


def parse_report(job: tuple) -> dict:
    """Download (cached) + parse ONE report in a worker PROCESS. Returns serializable pieces only.

    WHY PROCESSES. The harvest is CPU-bound, not IO-bound: pdfplumber's per-page text extraction
    dominates, and it holds the GIL, so raising the thread count from 8 to 24 changed nothing
    (~2.5s/report either way, ~14h for the 2011+ scope). Prefiltering pages by marker text before
    the expensive extract_tables() call was measured at 1.0x on table-bearing reports because
    extract_text() is itself the cost. So the parse moves to processes; only the parse. Channel
    building stays in the parent because it needs the ~300MB PSD group dict, which must not be
    copied into every worker.

    Both prose shapes are extracted here even though only one will be used, because deciding which
    requires the PSD vocabulary that lives in the parent -- and re-opening the PDF later to fetch
    the other shape would cost more than computing both now.
    """
    fn, url, cache_dir, tcfg = job
    import pdfplumber as _pp
    pdf_path = Path(cache_dir) / "reports" / fn
    try:
        B.fetch(url, pdf_path)
    except Exception:                                            # noqa: BLE001
        return {"error": "download_failed"}
    # The download API answers HTTP 200 with a 10-byte '%PDF-1.4\r\n' STUB for reports whose file
    # is gone -- 200 of 18,494, deterministic across retries, so it is the server's real answer and
    # not a transient failure. A further 56 arrive truncated (valid header, no %%EOF). Both were
    # landing in `parse_failed` as PdfminerException, which read as a parser problem rather than
    # as missing source files. Counted separately instead.
    try:
        size = pdf_path.stat().st_size
        with pdf_path.open("rb") as fh:
            head = fh.read(5)
            fh.seek(max(0, size - 2048))
            tail = fh.read()
    except OSError:
        return {"error": "unreadable_cache"}
    if head != b"%PDF" and not head.startswith(b"%PDF"):
        return {"error": "not_a_pdf"}
    if size <= 64:
        return {"error": "empty_pdf_stub_from_api"}
    if b"%%EOF" not in tail:
        return {"error": "truncated_pdf"}

    drop_res = [re.compile(x) for x in tcfg["drop_lines"]]
    try:
        with _pp.open(pdf_path) as pdf:
            tables = B.find_tables(pdf)
            if not tables:
                return {"tables": []}
            prose_by_page = {}
            for tb in tables:
                pg = tb["page"]
                if pg not in prose_by_page:
                    prose_by_page[pg] = B.prose_after_table(pdf, pg, tcfg, drop_res)
            sect_text, sect_head = B.prose_after_any_heading(
                pdf, tcfg["section_headings"], tcfg, drop_res)
    except Exception as e:                                       # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    return {"tables": tables, "prose_by_page": prose_by_page,
            "section": [sect_text, sect_head]}


def report_year(rep: dict) -> str:
    return (rep.get("publishDate") or rep.get("reportDate") or "")[:4]


def records_from(rep: dict, parsed: dict, group: str, psd_vals, psd_units, countries, latest,
                 cfg: dict, stats) -> list[dict]:
    """Turn one parsed report into records. Parent-process side: needs the PSD dicts."""
    tcfg, scfg = cfg["text"], cfg["series"]
    aliases = cfg.get("attribute_aliases", {})
    dedup = cfg.get("dedup", {})

    if parsed.get("error"):
        if parsed["error"] in ("download_failed", "empty_pdf_stub_from_api", "truncated_pdf",
                               "not_a_pdf", "unreadable_cache"):
            stats[parsed["error"]] += 1
        else:
            stats["parse_failed"] += 1
            stats["parse_errors"][parsed["error"]] = \
                stats["parse_errors"].get(parsed["error"], 0) + 1
        return []
    tables = parsed.get("tables") or []
    stats["tables_found"] += len(tables)
    for tb in tables:
        stats["table_layout"][tb.get("found_by", "?")] = \
            stats["table_layout"].get(tb.get("found_by", "?"), 0) + 1
    if not tables:
        stats["no_table"] += 1
        return []

    country = B.resolve_country(rep.get("countryName", "").strip(), countries, latest,
                               cfg.get("country_aliases", {}))
    if country is None:
        stats["unresolved_country"] += 1
        nm = rep.get("countryName", "?")
        stats["unresolved_country_names"][nm] = stats["unresolved_country_names"].get(nm, 0) + 1
        return []

    shape, specs = B.derive_specs(tables, country, psd_vals, tcfg)
    stats["record_shape"][shape] = stats["record_shape"].get(shape, 0) + 1
    if shape == "none":
        stats["no_psd_commodity_match"] += 1
        return []

    by_title = {}
    for tb in tables:
        by_title.setdefault(B.norm_label(tb["title"]), tb)

    url = cfg["data"]["gain_download"].format(
        file_name=urllib.parse.quote_plus(rep["fileName"] + ".pdf"))
    out = []
    for spec in specs:
        stats["candidates"] += 1
        commodities = spec["psd_commodities"]
        if (country in dedup.get("wasde_countries", [])
                and any(c in dedup.get("wasde_commodities", []) for c in commodities)):
            stats["wasde_skipped"] += 1
            continue
        chans, tabs = [], []
        for cm in commodities:
            table = by_title.get(B.norm_label(cm))
            if table is None:
                continue
            tabs.append(table)
            chans.extend(B.build_channels(psd_vals, psd_units, country, cm, table,
                                         aliases, scfg, stats))
        if not tabs or not chans:
            stats["no_channels"] += 1
            continue

        if spec.get("page") is not None:
            text = (parsed.get("prose_by_page") or {}).get(spec["page"], "")
            heading = ""
        else:
            text, heading = parsed.get("section") or ["", ""]
        if len(text) < tcfg["min_chars"]:
            stats["no_prose"] += 1
            continue

        chans_full = chans
        chans, win = B.apply_window(chans, text, scfg)
        alignment, evidence = B.detect_alignment(text, chans)
        flags = B.check_superlatives(text, chans_full, evidence)
        if flags and tcfg.get("drop_on_superlative_contradiction", True):
            stats["superlative_dropped"] += 1
            continue
        years = sorted({y for c in chans for y in c["years"]})
        text_years = sorted({int(y) for y in re.findall(r"\b(19\d{2}|20[0-4]\d)\b", text)})
        described = [y for y in text_years if years[0] <= y <= years[-1]]
        ts = [{"values": c["values"], "unit": c["unit"], "freq": c["freq"]} for c in chans]
        try:
            rec = B.emit_record(
                text=text.rstrip() + "\n\n<ts></ts>",
                timeseries=ts,
                alignment=alignment,
                license="public-domain-us-gov",
                source=url,
                series_id=f"fas_gain_{rep['reportNumber'].lower()}_{spec['slug']}",
                dataset="fas_gain_attache",
                domain="agriculture",
                region=B.region_for(country),
                period_start=f"{years[0]}-01-01",
                period_end=f"{years[-1]}-01-01",
                meta={
                    "report_number": rep["reportNumber"],
                    "report_name": rep.get("reportName"),
                    "report_category": (rep.get("reportCategory") or "").strip(),
                    "post": rep.get("postName"),
                    "country_gain": rep.get("countryName"),
                    "country_psd": country,
                    "published": (rep.get("publishDate") or "")[:10],
                    "psd_group": group,
                    "record_shape": shape,
                    "table_layout": tabs[0].get("found_by"),
                    "prose_heading": heading,
                    "commodities": commodities,
                    "psd_attributes": [c["psd_attribute"] for c in chans],
                    "psd_units": sorted({c["psd_unit"] for c in chans}),
                    "n_channels": len(chans),
                    "market_years": [years[0], years[-1]],
                    "n_points": len(chans[0]["values"]),
                    "splice_year": chans[0]["splice_year"],
                    "report_table_years": tabs[0]["years"],
                    "recite_evidence": evidence,
                    "superlative_flags": flags,
                    "text_years_named": text_years,
                    "series_years_described_pct":
                        round(100 * len(described) / len(years), 1) if years else 0,
                    "window": win,
                    "series_note": (
                        "annual PSD balance sheet, vintage-spliced: PSD Online bulk for settled "
                        f"market years (< {chans[0]['splice_year']}) + this report's OWN table "
                        "values for its table years (the post's own revised column preferred over "
                        "the previous official one). Live PSD has since revised the forecast year "
                        "past both of the report's columns."),
                    "forecast_caveat": (
                        "terminal point(s) are Post's forecast for the coming marketing year, not "
                        "measured history -- same convention as WASDE #41; the text is the "
                        "contemporaneous first-party forecast, so no future-value leakage."),
                    "wasde_overlap": (
                        "WASDE #41 builds only U.S. tables; this is a foreign post, so the series "
                        "are net-new rather than duplicated."),
                },
            )
        except Exception as e:                                   # noqa: BLE001
            stats["emit_rejected"] += 1
            key = f"{type(e).__name__}: {str(e)[:120]}"
            stats["parse_errors"][key] = stats["parse_errors"].get(key, 0) + 1
            continue
        out.append(rec)
        stats["emitted"] += 1
        stats[alignment] += 1
        stats["channels_emitted"] += len(chans)
    return out


_PSD_CACHE: dict[str, tuple] = {}


def psd_for(group: str, cfg: dict):
    """One-entry PSD cache. Loading a group is ~15s and there are 233 shards; without this the
    walk spends about an hour re-parsing the same CSVs. Only one group is held at a time -- all
    nine resident together is roughly a gigabyte of dicts, which is what sharding by group avoids."""
    if group not in _PSD_CACHE:
        _PSD_CACHE.clear()
        vals, units = B.load_psd(group, cfg, PKG_ROOT / cfg["data"]["cache_dir"])
        countries, latest = B.psd_country_index(vals)
        _PSD_CACHE[group] = (vals, units, countries, latest)
    return _PSD_CACHE[group]


def run_shard(group: str, year: str, reps: list, cfg: dict, shard_dir: Path) -> dict:
    rep_path = shard_dir / f"{group}_{year}.report.json"
    out_path = shard_dir / f"{group}_{year}.jsonl"
    if rep_path.exists():
        return json.loads(rep_path.read_text())["stats"]

    stats = defaultdict(int)
    stats.update({"unmapped_labels": {}, "unresolved_country_names": {}, "parse_errors": {},
                  "record_shape": {}, "table_layout": {}, "coverage_pct": [],
                  "reports": len(reps)})
    psd_vals, psd_units, countries, latest = psd_for(group, cfg)
    t0 = time.time()
    records = []
    cache_dir = str(PKG_ROOT / cfg["data"]["cache_dir"])
    jobs = [(r["fileName"] + ".pdf",
             cfg["data"]["gain_download"].format(
                 file_name=urllib.parse.quote_plus(r["fileName"] + ".pdf")),
             cache_dir, cfg["text"]) for r in reps]
    with ProcessPoolExecutor(max_workers=cfg["harvest"]["workers"]) as ex:
        for rep, parsed in zip(reps, ex.map(parse_report, jobs, chunksize=1)):
            records.extend(records_from(rep, parsed, group, psd_vals, psd_units,
                                        countries, latest, cfg, stats))

    tmp = out_path.with_suffix(".jsonl.tmp")
    with tmp.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(out_path)
    stats["elapsed_s"] = round(time.time() - t0, 1)
    stats["records"] = len(records)
    stats.pop("coverage_pct", None)
    # report LAST -- it is the completion marker (see module docstring)
    rep_path.write_text(json.dumps({"group": group, "year": year, "stats": dict(stats)},
                                   indent=2, ensure_ascii=False))
    return dict(stats)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--year-from", type=int, default=None)
    ap.add_argument("--year-to", type=int, default=None)
    ap.add_argument("--group", default=None)
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args()
    cfg = yaml.safe_load((PKG_ROOT / a.config).read_text())

    idx_path = PKG_ROOT / cfg["census"]["index_path"]
    if not idx_path.exists():
        print(f"no report index at {idx_path} -- run scripts/census.py first", file=sys.stderr)
        return 1
    reports = json.loads(idx_path.read_text())["reports"]
    cat2group = cfg["census"]["psd_categories"]

    shards: dict[tuple[str, str], list] = defaultdict(list)
    for r in reports:
        g = cat2group.get((r.get("reportCategory") or "").strip())
        if not g or (a.group and g != a.group):
            continue
        y = report_year(r)
        if not y.isdigit():
            continue
        if a.year_from and int(y) < a.year_from:
            continue
        if a.year_to and int(y) > a.year_to:
            continue
        if not r.get("fileName"):
            continue
        shards[(g, y)].append(r)

    # group-major, newest year first inside a group: keeps psd_for()'s one-entry cache hot
    order = sorted(shards, key=lambda k: (k[0], -int(k[1])))
    total = sum(len(v) for v in shards.values())
    print(f"{len(order)} shards, {total} reports")
    if a.plan:
        for k in order:
            print(f"  {k[0]:14s} {k[1]}  {len(shards[k]):5d}")
        return 0

    shard_dir = PKG_ROOT / cfg["harvest"]["shard_dir"]
    shard_dir.mkdir(parents=True, exist_ok=True)
    agg = Counter()
    done = 0
    for k in order:
        g, y = k
        st = run_shard(g, y, shards[k], cfg, shard_dir)
        done += len(shards[k])
        for kk, vv in st.items():
            if isinstance(vv, int):
                agg[kk] += vv
        print(f"  [{done}/{total}] {g:14s} {y}  reports={st.get('reports',0):5d} "
              f"records={st.get('records',0):5d} no_table={st.get('no_table',0):5d} "
              f"({st.get('elapsed_s',0)}s)", flush=True)
    print("\n=== aggregate ===")
    for kk in sorted(agg):
        print(f"  {kk:26s} {agg[kk]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
