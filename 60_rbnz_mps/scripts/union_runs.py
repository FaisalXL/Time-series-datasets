#!/usr/bin/env python3
"""union_runs.py -- union RBNZ build outputs across runs, keyed on series_id.

A single pass over Wayback is NOT reproducible: archive.org throttles by refusing connections,
so each run loses a *different* handful of statements to transient failure rather than to
anything about the data. Measured over two runs of the same code and cache:

    run A (2026-08-19): 108 emitted, lost 2026-02 to `pack_fetch_failed`
    run B (2026-08-20): 109 emitted, lost 2023-11 to `throttled`

Neither run is wrong; both are incomplete, and the union is what the archive actually holds.
Later runs win on conflict (a rebuild reflects the current code). Prints exactly which
statements each input contributed, so the union is never a silent merge.

Usage: union_runs.py OUT.jsonl IN1.jsonl IN2.jsonl [...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 4:
        raise SystemExit(__doc__.strip().splitlines()[-1])
    out = Path(sys.argv[1])
    merged: dict[str, dict] = {}
    for src in sys.argv[2:]:
        rows = [json.loads(l) for l in Path(src).open() if l.strip()]
        added = [r["series_id"] for r in rows if r["series_id"] not in merged]
        replaced = [r["series_id"] for r in rows if r["series_id"] in merged]
        for r in rows:
            merged[r["series_id"]] = r
        print(f"{src}: {len(rows)} records -> +{len(added)} new, {len(replaced)} refreshed")
        if added and len(merged) != len(added):
            print(f"    new here: {', '.join(sorted(added))}")
    rows = [merged[k] for k in sorted(merged)]
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"\nunion: {len(rows)} records -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
