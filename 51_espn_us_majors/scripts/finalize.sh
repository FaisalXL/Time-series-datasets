#!/bin/bash
# Aggregate + strict-validate every shard, in one go, into output/finalize.out.
# Run after the harvests exit. Safe to re-run: it only reads shards and rewrites the report.
cd "$(dirname "$0")/.." || exit 1
PY=/usr/local/anaconda3/bin/python3.11

{
  echo "=== finalize $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  echo
  echo "--- aggregate.py ---"
  $PY scripts/aggregate.py --sample-per-league 4000
  echo
  echo "--- validate.py --strict over every shard ---"
  # Every shard, not a sample: the gate is 100% strict-clean with 0 warnings.
  $PY ../schema/validate.py --strict output/shards/*.jsonl
} 2>&1 | tee output/finalize.out
