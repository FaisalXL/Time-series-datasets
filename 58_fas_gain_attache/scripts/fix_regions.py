#!/usr/bin/env python3
"""Recompute `region` on harvested shards from each record's own meta.country_psd.

Exists because the first harvest pass derived region as countryName[:2].upper(), which emitted real
ISO codes for the WRONG countries ('CH' China, 'ME' Mexico, 'NE' New Zealand, 'JA' Japan), and a
later pass still fell back to a bare "Turkey" because pycountry indexes it only as Türkiye. Rather
than re-parse thousands of PDFs to correct one derived field, this rewrites it in place.

Derives ONLY from data already in the record (meta.country_psd), refuses any record whose shape it
does not recognise, is idempotent, and writes atomically -- same discipline as 65's URL migration.

Usage:
    python scripts/fix_regions.py --config config.example.yaml [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from build_cpt_jsonl import region_for  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = yaml.safe_load((PKG_ROOT / a.config).read_text())
    shard_dir = PKG_ROOT / cfg["harvest"]["shard_dir"]

    files = sorted(shard_dir.glob("*.jsonl"))
    changed = unchanged = refused = 0
    for f in files:
        out_lines, touched = [], False
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            country = (rec.get("meta") or {}).get("country_psd")
            if not country:
                refused += 1                  # unknown shape: leave exactly as found
                out_lines.append(line)
                continue
            want = region_for(country)
            if rec.get("region") == want:
                unchanged += 1
                out_lines.append(line)
                continue
            rec["region"] = want
            changed += 1
            touched = True
            out_lines.append(json.dumps(rec, ensure_ascii=False))
        if touched and not a.dry_run:
            tmp = f.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(out_lines) + "\n")
            tmp.replace(f)
    print(f"files={len(files)} changed={changed} already_correct={unchanged} refused={refused}"
          f"{' (dry run)' if a.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
