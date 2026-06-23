# NHC HURDAT2 → CPT World-Knowledge JSONL

> **Status: Complete** — pipeline and demo output ready. Advisory text subsampled (3 per storm); full-scale run and text-length policy pending team decision.

Atlantic and Eastern Pacific tropical cyclone best-track data paired with real NHC public advisory text for continued pre-training (CPT).

---

## What this dataset is

The [NHC HURDAT2](https://www.nhc.noaa.gov/data/hurdat/) (Hurricane Database) is NOAA's official post-season best track for Atlantic (1851–present) and East Pacific (1949–present) tropical cyclones. Each storm is a sequence of 6-hourly synoptic observations: date/time, status (TD/TS/HU/…), latitude/longitude, maximum sustained wind (knots), minimum central pressure (millibars), wind radii, and record identifiers (landfall `L`, peak `P`, etc.).

NHC also maintains a rich **advisory archive** (`nhc.noaa.gov/archive/YYYY/`) with forecaster-written public advisories per storm. That text is first-party, operationally written, and semantically dense — the primary text source for this package.

Tropical cyclones are a strong CPT candidate: high temporal resolution (6-hourly), physically meaningful paired signals (wind, pressure, track, wind extent), long historical record, and natural document structure (one storm life cycle = one story with an intensity evolution curve).

---

## What we are training for

**Objective:** Continued pre-training on world knowledge, not instruction fine-tuning. The model reads real NHC advisory prose with a `<ts></ts>` placeholder; numeric 6-hourly series are stored separately. There is no forecast task framing.

**What the model learns:** The relationship between storm narratives and intensity evolution — e.g. that "extremely dangerous Category 4 hurricane nearing the Louisiana coast" correlates with 150-kt winds, sub-940-mb pressure, and a tightening track over the Gulf.

**Example record** (Hurricane Ida 2021 — see `samples/example_output.jsonl`):

```json
{
  "text": "...EYE OF EXTREMELY DANGEROUS CATEGORY 4 HURRICANE IDA NEARING THE SOUTHEASTERN COAST OF LOUISIANA... MAXIMUM SUSTAINED WINDS...150 MPH... MINIMUM CENTRAL PRESSURE...933 MB...\n\n---\n\n...IDA CONTINUES TO WEAKEN AS IT MOVES INLAND OVER LOUISIANA...\n\nSix-hourly observations across the storm's lifetime — maximum sustained wind (kt), minimum central pressure (mb), track latitude, track longitude, and maximum 34-kt wind radius (nm): <ts></ts>.",
  "timeseries": [
    {"values": [30, 35, 40, 50, 65, 85, 105, 115, 130, 140, 150, 130, 100, 75, 55], "unit": "max_wind_kt", "freq": "6h"},
    {"values": [1007, 1003, 997, 990, 978, 965, 955, 945, 935, 930, 929, 938, 960, 980, 993], "unit": "min_pressure_mb", "freq": "6h"},
    {"values": [17.4, 18.1, 19.0, 21.2, 23.8, 26.4, 28.8, 29.5, 30.2, 31.0, 31.5, 32.0, 32.5, 33.0, 33.5], "unit": "lat", "freq": "6h"},
    {"values": [-86.8, -87.2, -87.8, -88.5, -89.0, -89.5, -90.0, -90.5, -91.0, -91.5, -92.0, -92.5, -93.0, -93.2, -93.5], "unit": "lon", "freq": "6h"},
    {"values": [null, null, 80, 120, 160, 200, 220, 240, 230, 200, 150, null, null, null, null], "unit": "r34_max_nm", "freq": "6h"}
  ],
  "track_date_range": ["2021-08-26T18:00", "2021-09-01T00:00"],
  "storm_name": "IDA",
  "storm_id": "AL092021",
  "basin": "AL",
  "season": 2021,
  "peak_wind_kt": 150,
  "peak_category": 4,
  "made_landfall": true,
  "dataset": "nhc_hurdat2",
  "source": "nhc_hurdat2_best_track",
  "series_id": "AL092021",
  "task_type": "world_knowledge",
  "text_source": "nhc_advisory"
}
```

---

## Advisory text acquisition

**Primary and only text source:** NHC public advisory archive at `https://www.nhc.noaa.gov/archive/{YEAR}/{basin}{number}/` (e.g. `2021/al09/` for AL092021).

For each qualifying storm the script:

1. Fetches the storm's archive index page. If it 404s, times out, or has no `.public.*.shtml` files → **storm is dropped**.
2. Downloads up to 3 public advisories: the **first** (`.001`), the **last**, and — when more than 4 advisories exist — the one whose number is closest to the peak-intensity point in the track.
3. Strips HTML and WMO header lines (`ZCZC`, `TTAA`, `NNNN`), truncates each advisory to 1,500 characters, and concatenates with `\n\n---\n\n`.
4. Appends the configured `ts_intro_sentence` and sets `text_source: "nhc_advisory"`.

**No synthetic text is used.** Storms without retrievable advisory text are excluded entirely.

**Coverage:** Named storms with archive pages are reliably available from approximately **1993 onward**. Earlier storms and unnamed depressions generally have no advisory archive and are dropped. Running with default `season_start: 2000` typically yields advisory coverage for the large majority of named Atlantic storms.

### ⚠️ Open design question: how many advisories to include?

*This decision is pending discussion with the team lead. The current approach is a reasonable first pass but has a known trade-off.*

A major hurricane like Ida generates **~30–50 individual advisory files** (one every 6 hours over 8+ days). Including all of them would produce a text field of 50,000+ characters — impractical. The current script takes a maximum of 3: the **first** advisory (formation language), the **last** (dissipation summary), and the **one closest to peak intensity** (most dramatic moment). The rest are discarded.

**Problem with this:** Taking three scattered advisories and stitching them together with `---` separators creates a document that no human ever wrote or read. It is an artificial construction, not a natural document — which is in tension with Charon's principle that text should be real and natural.

**Alternatives to consider:**

| Option | Approach | Trade-off |
|---|---|---|
| **A — Current (3 advisories)** | First + last + peak | Manageable size, but artificial stitching |
| **B — All advisories, narrative only** | Extract just the prose paragraph from each advisory, concatenate all in order | Natural chronological story; total ~3,000–6,000 chars for a 10-day storm; requires more careful HTML parsing to isolate prose |
| **C — First advisory only** | Just `.001` | One clean, unmodified document; captures formation and initial forecast; misses the storm's evolution |
| **D — Tropical Cyclone Report (TCR)** | Post-season PDF per storm | Highest quality, comprehensive; one natural document per storm; requires PDF parsing; only for named storms |

**Recommendation to discuss:** Option B (all advisories, prose only) is the most natural construction and the most faithful to the "real unmodified text" principle. Option D (TCR PDFs) is the gold standard but requires a PDF pipeline. The current Option A is the easiest to implement but the weakest from a text quality standpoint.

---

## Window design

**One storm = one record.** The window is the storm's full qualifying tropical/subtropical life — all 6-hourly observations where status is TD, TS, HU, SS, or SD. Extratropical (EX), wave (WV), low (LO), and disturbance (DB) stages are excluded from the time series but landfall flags are read from the full track.

This is the natural document unit for tropical cyclones: formation → intensification → peak → landfall (if any) → dissipation. No sliding windows.

Typical series length: **8–80 observations** (2–20 days at 6-hourly resolution), depending on storm duration and `min_qualifying_obs` filter.

---

## The five time series

All arrays have the same length — one value per qualifying 6-hourly observation, in chronological order.

| Order | Unit | Description |
|:---:|---|---|
| 1 | `max_wind_kt` | Maximum sustained wind speed (knots) |
| 2 | `min_pressure_mb` | Minimum central pressure (millibars); `null` when HURDAT2 reports -999 (missing) |
| 3 | `lat` | Storm center latitude (decimal degrees; positive = North) |
| 4 | `lon` | Storm center longitude (decimal degrees; negative = West) |
| 5 | `r34_max_nm` | Maximum 34-kt wind radius across four quadrants (nautical miles); `null` when all quadrants are missing (-999) |

**lat/lon** give the full 6-hourly track of the storm center — the spatial path paired with intensity evolution.

**r34_max_nm** captures gale-force wind extent. Only the 34-kt threshold is included; 50-kt and 64-kt radii are omitted because they are missing for nearly all pre-2004 records and many weaker storms.

Wind and pressure together capture intensity; lat/lon capture track; r34 captures storm size.

---

## Output format

Each line in `output/nhc_hurdat2_cpt.jsonl` is one JSON object:

| Field | Description |
|---|---|
| `text` | Real NHC public advisory text (up to 3 advisories) ending with `<ts></ts>` |
| `timeseries` | Exactly five objects: wind, pressure, lat, lon, r34_max_nm |
| `track_date_range` | `[first_obs, last_obs]` as ISO `YYYY-MM-DDTHH:MM` |
| `storm_name` | Storm name (uppercase), e.g. `"IDA"` |
| `storm_id` | HURDAT2 ID, e.g. `"AL092021"` |
| `basin` | Two-letter basin code: `AL` or `EP` |
| `season` | Calendar year of the storm |
| `peak_wind_kt` | Maximum wind in the qualifying track |
| `peak_category` | Saffir-Simpson category from peak wind (-1=depression, 0=TS, 1–5=hurricane) |
| `made_landfall` | `true` if any track row has record identifier `L` |
| `dataset` | Always `"nhc_hurdat2"` |
| `source` | Always `"nhc_hurdat2_best_track"` |
| `series_id` | Same as `storm_id` |
| `task_type` | Always `"world_knowledge"` |
| `text_source` | Always `"nhc_advisory"` |

---

## Text coverage

The numbers below come from a full advisory-archive scan across all 368 qualifying Atlantic storms in the default 2000–2023 window (≥ 8 qualifying observations). The scan checked each storm's NHC advisory archive directory without downloading advisory content, using the `scripts/advisory_coverage_check.py` tool.

### Overall (2000–2023, ≥ 8 qualifying obs)

| | Count | Share |
|---|---:|---:|
| Storms in full HURDAT2 file (all years, all basins) | 1,973 | — |
| After season filter (2000–2023) + obs filter (≥ 8) | 368 | 100% |
| **With advisory archive** | **319** | **86.7%** |
| Without advisory archive | 49 | 13.3% |

### By era

The NHC archive URL structure changed after 2002. From 2003 onward the per-storm advisory archive is consistently available.

| Era | Qualifying storms | With advisory | Coverage |
|---|---:|---:|---:|
| 2000–2002 | 45 | 0 | 0% |
| 2003–2005 | 60 | 59 | 98% |
| 2006–2023 | 263 | 260 | 99% |
| **Total** | **368** | **319** | **86.7%** |

The 2000–2002 gap is an archiving issue: the NHC's online public-advisory index for those three seasons uses a directory format that does not match the fetch pattern expected by the build script. The actual text may exist in alternative paths, but has not been confirmed. The practical fix is to set `data.season_start: 2003`, which raises effective coverage to **99%** across 323 qualifying storms.

The remaining 6 missing storms post-2002 are one late-season outlier (Zeta 2005, AL312005), one unnamed system (2006), one unnamed system (2011), and one unnamed system (2013) — all depressions or unnamed storms whose archive directories do not exist.

**Detailed year-by-year breakdown** is in `output/advisory_coverage.json`, produced by running `scripts/advisory_coverage_check.py`.

---

## Caveats

1. **Post-analysis best track** — HURDAT2 positions and intensities are refined after the season; they may differ from real-time advisories.
2. **Missing pressure** — Many weaker or older storms have `-999` (missing) pressure; stored as `null` in the values array.
3. **Storms without advisories excluded** — The corpus is biased toward named storms from ~1993 onward; unnamed systems and early-era storms are dropped.
4. **Wind radii sparsity** — `r34_max_nm` is `null` for most pre-2004 storms and many weaker systems.
5. **Separate basin files** — Atlantic and East Pacific are different HURDAT2 files; use `basin: both` to merge.
6. **6-hourly fixed sampling** — Rapid intensification and landfall can occur between synoptic times; series may show large step changes.

---

## Assessment summary

| Field | Value |
|-------|-------|
| **Verdict** | `needs_pairing` — time series native; text requires advisory download |
| **TS source** | NHC HURDAT2 best-track ([data index](https://www.nhc.noaa.gov/data/hurdat/)) |
| **Text source** | NHC public advisory archive (`nhc.noaa.gov/archive/`) |
| **Acquisition** | `txt` (HURDAT2 download + per-storm HTTP advisory fetch) |
| **Window method** | One record per storm; full qualifying tropical/subtropical track |

Full probe metadata: [`assessment.json`](./assessment.json)

---

## Quick start

From this folder:

```bash
cd datasets/02_nhc_hurdat2
pip install -r requirements.txt

# Run with defaults (Atlantic, 2000–2023, ≤50 storms with advisories)
python scripts/build_cpt_jsonl.py --config config.example.yaml

# Use cached HURDAT2 (no re-download)
python scripts/build_cpt_jsonl.py \
  --set data.source=local \
  --set data.local_path=.cache/hurdat2/hurdat2-1851-2023-051124.txt

# Single storm (Hurricane Ida 2021)
python scripts/build_cpt_jsonl.py \
  --set data.source=local \
  --set data.local_path=.cache/hurdat2/hurdat2-1851-2023-051124.txt \
  --set 'data.storm_filter=[AL092021]' \
  --set output.max_records=null

# Recent seasons, both basins, no cap
python scripts/build_cpt_jsonl.py \
  --set data.basin=both \
  --set data.season_start=2010 \
  --set output.max_records=null
```

Outputs:

- `output/nhc_hurdat2_cpt.jsonl` — CPT records (JSONL)
- `output/run_report.json` — storm counts, skip breakdown, config snapshot

---

## Configuration guide

Copy `config.example.yaml` → `config.yaml` for persistent local edits. Every key can be overridden: `--set dotted.path=value`.

| Section | Key | Purpose |
|---------|-----|---------|
| **data** | `source` | `download` or `local` |
| | `basin` | `atlantic`, `east_pacific`, or `both` |
| | `atlantic_url` / `east_pacific_url` | HURDAT2 file URLs |
| | `local_path` | Local `.txt` when `source=local` |
| | `season_start` / `season_end` | Inclusive season year filter |
| | `min_qualifying_obs` | Min TD/TS/HU/SS/SD observations per storm |
| | `storm_filter` | Storm IDs or names; `[]` = all |
| **advisories** | `enabled` | Fetch NHC advisories; if false, all storms dropped |
| | `max_per_storm` | Max advisories concatenated (first, last, peak) |
| | `char_limit_per_advisory` | Truncation limit per advisory file |
| | `timeout_seconds` | HTTP timeout per request |
| **text** | `ts_intro_sentence` | Closing sentence with `<ts></ts>` |
| **output** | `output_path` | JSONL output file |
| | `report_path` | Run statistics JSON |
| | `max_records` | Cap records; `null` = no cap |
| | `indent` | `null` = compact JSONL; integer = pretty-print each record |

---

## Files in this folder

| File | Purpose |
|------|---------|
| `README.md` | This document |
| `assessment.json` | Triage / probe metadata |
| `config.example.yaml` | Documented default configuration |
| `requirements.txt` | Python dependencies (`pyyaml`) |
| `scripts/build_cpt_jsonl.py` | Storm-level CPT JSONL builder |
| `scripts/advisory_coverage_check.py` | Fast parallel advisory-archive coverage scan |
| `samples/example_output.jsonl` | Hand-authored Hurricane Ida example |
| `output/advisory_coverage.json` | Full advisory coverage scan results (by year) |
| `output/` | Generated JSONL + run reports |
