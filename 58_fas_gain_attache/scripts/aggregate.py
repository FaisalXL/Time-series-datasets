#!/usr/bin/env python3
"""Merge harvested shards into one dataset file + a run report over the whole archive.

Streams shard-by-shard: the full harvest is far too large to hold in memory as parsed objects, and
accumulating records in a list was one of the two defects called out on the ESPN packages.

Usage:
    python scripts/aggregate.py --config config.example.yaml
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    a = ap.parse_args()
    cfg = yaml.safe_load((PKG_ROOT / a.config).read_text())
    shard_dir = PKG_ROOT / cfg["harvest"]["shard_dir"]
    out = PKG_ROOT / cfg["output"]["path"]
    out.parent.mkdir(parents=True, exist_ok=True)

    reports = sorted(shard_dir.glob("*.report.json"))
    if not reports:
        print(f"no completed shards in {shard_dir}", file=sys.stderr)
        return 1

    agg = Counter()
    per_group, per_year = Counter(), Counter()
    align, shapes, layouts = Counter(), Counter(), Counter()
    unmapped, unresolved, errors = Counter(), Counter(), Counter()
    n = 0
    seen_ids: set[str] = set()
    dupes = 0
    tmp = out.with_suffix(".jsonl.tmp")
    with tmp.open("w") as fh:
        for rep_path in reports:
            meta = json.loads(rep_path.read_text())
            st = meta["stats"]
            for k, v in st.items():
                if isinstance(v, int):
                    agg[k] += v
            for src, dst in ((st.get("unmapped_labels"), unmapped),
                             (st.get("unresolved_country_names"), unresolved),
                             (st.get("parse_errors"), errors),
                             (st.get("record_shape"), shapes),
                             (st.get("table_layout"), layouts)):
                if isinstance(src, dict):
                    dst.update(src)
            jl = rep_path.with_name(rep_path.name.replace(".report.json", ".jsonl"))
            if not jl.exists():
                continue
            with jl.open() as src_fh:
                for line in src_fh:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    sid = rec.get("series_id")
                    # a report can appear in two shards only if the index changed mid-run; keep
                    # the first and count the rest rather than shipping a duplicate series_id
                    if sid in seen_ids:
                        dupes += 1
                        continue
                    seen_ids.add(sid)
                    align[rec.get("alignment")] += 1
                    m = rec.get("meta") or {}
                    per_group[m.get("psd_group")] += 1
                    per_year[(m.get("published") or "")[:4]] += 1
                    fh.write(line if line.endswith("\n") else line + "\n")
                    n += 1
    tmp.replace(out)

    stats = {"records": n, "shards_completed": len(reports), "duplicate_series_ids_dropped": dupes,
             "reports_seen": agg.get("reports", 0),
             "reports_with_no_table": agg.get("no_table", 0),
             "alignment": dict(align), "record_shape": dict(shapes),
             "table_layout": dict(layouts),
             "per_psd_group": dict(per_group.most_common()),
             "per_year": {k: per_year[k] for k in sorted(per_year) if k},
             "skips": {k: agg[k] for k in sorted(agg) if k in {
                 "no_table", "no_prose", "no_channels", "no_psd_series", "short_series",
                 "no_psd_commodity_match", "unresolved_country", "download_failed",
                 "parse_failed", "superlative_dropped", "wasde_skipped"}},
             "channels_emitted": agg.get("channels_emitted", 0),
             "top_unmapped_labels": dict(unmapped.most_common(30)),
             "unresolved_countries": dict(unresolved.most_common(30)),
             "parse_errors": dict(errors.most_common(15))}
    rr = PKG_ROOT / cfg["output"]["run_report"]
    rr.write_text(json.dumps({"dataset": "fas_gain_attache", "stats": stats}, indent=2,
                             ensure_ascii=False))
    print(json.dumps(stats, indent=2, ensure_ascii=False)[:4000])
    print(f"\nwrote {n} records -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
