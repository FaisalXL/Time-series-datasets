#!/usr/bin/env python3
"""One-time: reduce the ~500MB gzipped NASS Quick Stats bulk file to a compact per-state series
index on disk.

The build used to re-read a flat filtered TSV once per state (48 full scans of a multi-hundred-MB
file). This writes a single pickle keyed `(state_alpha, short_desc) -> {date: value}` instead, so
every later build/measurement pass loads the whole series universe in seconds.

Keeps every STATE-level WEEKLY row whose STATISTICCAT is a *current-season* measure, and every
commodity, so that adding a channel or a commodity later is a pure config change with no re-scan.

⚠️ The category set is the fix for a real defect, not a widening for its own sake. The previous
filter was `{PROGRESS, CONDITION, DAYS SUITABLE}`, but NASS files soil moisture under its own
`STATISTICCAT_DESC = "MOISTURE"` -- so all eight `SOIL, {TOPSOIL,SUBSOIL} - MOISTURE ...` channels
matched nothing and were dropped by the builder's silent `all(v is None)` skip. Those are exactly
the channels the README calls "the dense backbone ... what makes each record a genuinely dense
weekly series rather than a mostly-null per-stage cascade". MOISTURE is the 5th-largest category
in the file (369,377 STATE/WEEKLY rows), so the omission was ~a third of the intended payload.
Full category census is in `docs/source_census.md`.

Excluded on purpose: the `", PREVIOUS YEAR"` and `", 5 YEAR AVG"` mirrors of every category
(~1.5M rows). They are real published series and the narrative does recite them, but they are
lagged/averaged restatements of the same underlying measurement rather than independent
phenomena, so carrying them as extra channels would inflate channel counts with derived
duplicates. They are still *used* -- as a decoy set when measuring alignment, since a number in
the prose can match a window value by being last year's figure rather than this week's.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import pickle
import sys
from collections import Counter, defaultdict
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PKG_ROOT / ".cache"
OUT_PICKLE = CACHE_DIR / "series_index.pkl"
# Comparison mirrors of a category, kept out of the channel index (see module docstring) but
# collected separately so alignment measurement can tell "recites this week" from "recites the
# year-ago or 5-year-average figure printed beside it".
_MIRROR_SUFFIXES = (", PREVIOUS YEAR", ", 5 YEAR AVG")
OUT_MIRROR_PICKLE = CACHE_DIR / "series_mirrors.pkl"


def is_current_season_cat(cat: str) -> bool:
    return not cat.endswith(_MIRROR_SUFFIXES)


def main() -> None:
    bulk = sorted(CACHE_DIR.glob("qs.crops_*.txt.gz"))
    if not bulk:
        sys.exit("no qs.crops_*.txt.gz in .cache/ -- download it first")
    bulk_path = bulk[-1]

    # (state, short_desc) -> {date: value}
    index: dict[tuple[str, str], dict[dt.date, float]] = defaultdict(dict)
    mirrors: dict[tuple[str, str], dict[dt.date, float]] = defaultdict(dict)
    cats: Counter = Counter()
    csv.field_size_limit(1 << 24)
    kept = mkept = seen = 0
    with gzip.open(bulk_path, "rt", encoding="utf-8", errors="replace", newline="") as inp:
        reader = csv.reader(inp, delimiter="\t")
        header = next(reader)
        idx = {name: i for i, name in enumerate(header)}
        i_agg, i_freq, i_cat = idx["AGG_LEVEL_DESC"], idx["FREQ_DESC"], idx["STATISTICCAT_DESC"]
        i_state, i_sd, i_we, i_val = (idx["STATE_ALPHA"], idx["SHORT_DESC"],
                                      idx["WEEK_ENDING"], idx["VALUE"])
        for row in reader:
            seen += 1
            if seen % 5_000_000 == 0:
                print(f"  {seen/1e6:.0f}M rows scanned, {kept:,} kept", file=sys.stderr, flush=True)
            try:
                if row[i_agg] != "STATE" or row[i_freq] != "WEEKLY":
                    continue
                we = row[i_we]
                if not we:
                    continue
                d = dt.date.fromisoformat(we)
                v = float(row[i_val].replace(",", ""))
            except (ValueError, IndexError):
                continue
            cat = row[i_cat]
            cats[cat] += 1
            key = (row[i_state], row[i_sd])
            if is_current_season_cat(cat):
                index[key][d] = v
                kept += 1
            else:
                mirrors[key][d] = v
                mkept += 1

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_PICKLE, "wb") as f:
        pickle.dump(dict(index), f, protocol=5)
    with open(OUT_MIRROR_PICKLE, "wb") as f:
        pickle.dump(dict(mirrors), f, protocol=5)
    with open(CACHE_DIR / "category_census.json", "w") as f:
        json.dump(cats.most_common(), f, indent=1)
    print(f"scanned {seen:,} rows; kept {kept:,} current-season observations across "
          f"{len(index):,} (state, channel) pairs -> {OUT_PICKLE}", file=sys.stderr)
    print(f"  plus {mkept:,} comparison-mirror observations ({len(mirrors):,} pairs) "
          f"-> {OUT_MIRROR_PICKLE}", file=sys.stderr)
    print(f"  {len(cats)} distinct STATE/WEEKLY categories -> category_census.json",
          file=sys.stderr)


if __name__ == "__main__":
    main()
