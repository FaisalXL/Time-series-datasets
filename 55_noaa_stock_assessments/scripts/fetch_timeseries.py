#!/usr/bin/env python3
"""Bulk-fetch every candidate assessment's TimeSeriesData export into the builder's cache.

Same reason as fetch_reports.py: keep the network phase restartable and separate from
extraction work. Batches follow the servlet's own 50-id limit, and the cache key is the
full request URL, so the builder's own batching must use the SAME batch composition to
hit these files warm. That is why the batch list is written to
`.cache/ts_batches.json` -- the builder reads it instead of re-deriving batches from a
sort order that could drift.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("b", ROOT / "scripts" / "build_cpt_jsonl.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def main() -> None:
    cands = json.loads(Path(sys.argv[1]).read_text())
    cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    d = cfg["data"]
    cache = ROOT / d["cache_dir"]
    ids = sorted({r["asmt_id"] for r in cands}, key=int)
    size = int(d.get("ts_batch_size", 50))
    batches = [ids[i:i + size] for i in range(0, len(ids), size)]
    (cache / "ts_batches.json").parent.mkdir(parents=True, exist_ok=True)
    (cache / "ts_batches.json").write_text(json.dumps(batches))
    print(f"{len(ids)} assessment ids -> {len(batches)} batches", flush=True)
    got = 0
    for i, bt in enumerate(batches, 1):
        rows = b.fetch_timeseries_batch(bt, d, cache)
        parsed = b.parse_timeseries_table(rows)
        got += len(parsed)
        print(f"  batch {i}/{len(batches)}: {len(rows)} rows, {len(parsed)} assessments parsed", flush=True)
    print(f"done: {got} assessments carry series", flush=True)


if __name__ == "__main__":
    main()
