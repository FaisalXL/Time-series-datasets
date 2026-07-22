# ICS-209-PLUS Wildfire Situation Reports + Daily Incident Series → CPT

> **Status: Built** (demo: 50 records). Full build ≈ **7.4k incidents** at the default `min_reports=3` (run with `output.max_records=null`). Wildfires 1999–2020.
>
> ⚠️ **Time-series length — flagged (we expect longer series).** At the default the daily series is **short: median ~4 reporting days** (mean 5, p90 10), because most wildfires are brief — the length only lives in the tail (large fires). This is the main open question for review. Raising `data.min_reports` trades volume for length (it also acts as a significance filter). Trade-off across the full dataset:
>
> | `min_reports` | records | median window | mean | p90 |
> |---|---|---|---|---|
> | 3 (default) | 7,367 | 4 | 5 | 10 |
> | 7 | 3,477 | 8 | 10 | 17 |
> | 10 | 2,250 | 11 | 13 | 21 |
> | 14 | 1,500 | 15 | 18 | 27 |
> | 20 | 948 | 22 | 24 | 34 |
>
> Sweet spot for a real multi-week trajectory is `min_reports=7–10`.

**What it is:** One record = **one wildfire incident** — the richest situation-report narrative for that fire (the "anchor" report: significant events, current threat, projected activity, weather, planned actions) paired with the incident's **daily time series** — acres burned, percent contained, total personnel — from its first report through the anchor. The narrative *describes* the fire's progression the series quantifies → the "describes" alignment class.

**Fully open:** ICS-209-PLUS (St. Denis et al. 2023, *Scientific Data*) is **CC BY 4.0** — attribution only, no gate.

#### 📄 Text — situation-report narrative
| | |
|---|---|
| **What** | The anchor report's combined free-text: `SIGNIF_EVENTS_SUMMARY`, `CURRENT_THREAT_NARR`, `PROJECTED_ACTIVITY_NARR`, `WEATHER_CONCERNS_NARR`, `PLANNED_ACTIONS`, `STRATEGIC_NARR`, `REMARKS`. Authentic operational prose written by incident command (lowercase, occasional typos → genuinely `"real"`). |
| **Anchor rule** | The daily report with the **longest** combined narrative, chosen among days that still leave ≥ `min_reports` points — so the text and the series' terminal point are the *same* report. |
| **Filter** | Anchor narrative ≥ `text.min_text_chars` (default 300) or the incident is dropped (no synthetic fallback). |

#### 📈 Time series — daily incident metrics
| | |
|---|---|
| **What** | 3 channels, one point per reporting day, first report → anchor |
| **Source** | [ICS-209-PLUS wildfire bundle](https://figshare.com/articles/dataset/All-hazards_dataset_mined_from_the_US_National_Incident_Management_System_1999-2020/19858927) (figshare, CC BY 4.0). The sitrep CSV lives inside the zip; parsed with the **stdlib** (`zipfile`/`csv`). The CSV is contiguous by `INCIDENT_ID`, so the build streams one incident at a time. |
| **Cadence** | `1d` (irregular) — one report per calendar day; days missing any channel are dropped, so gaps are explicit via the `report_dates` array (aligned 1:1 with values, no imputation). |

| Channel (`unit`) | CSV column |
|---|---|
| `acres_burned` (acres) | `ACRES` |
| `percent_contained` (percent) | `PCT_CONTAINED_COMPLETED` |
| `total_personnel` (persons) | `TOTAL_PERSONNEL` |

**Record shape** (real — Donnelly Flats, AK 1999; arrays/text abbreviated):
```json
{
  "text": "the fire made a major run to the north late sunday, forcing the evacuation of fort greely and part of delta junction... the fire jumped the richardson highway and destroyed a residential structure...\n\nDaily situation-report values — acres burned, percent contained, and total personnel — for the Donnelly Flats Fire (AK) across 4 reporting days through 1999-06-14: <ts></ts>",
  "timeseries": [
    {"values": [150.0, 1500.0, 3200.0, 6000.0], "unit": "acres", "freq": "1d"},
    {"values": [0.0, 0.0, 0.0, 0.0], "unit": "percent", "freq": "1d"},
    {"values": [92.0, 119.0, 259.0, 362.0], "unit": "persons", "freq": "1d"}
  ],
  "report_dates": ["1999-06-11", "1999-06-12", "1999-06-13", "1999-06-14"],
  "task_type": "world_knowledge", "text_quality": "real",
  "incident_id": "1999_AK-ARM-B222_DONNELLY FLATS", "incident_name": "Donnelly Flats",
  "poo_state": "AK", "start_year": "1999", "cause": "Human", "discovery_date": "1999-06-11",
  "anchor_report_date": "1999-06-14", "final_acres": 18000.0, "n_reports": 4,
  "dataset": "ics209_wildfire", "license": "CC BY 4.0",
  "source": "figshare.com/articles/19858927 (St. Denis et al. 2023, ICS-209-PLUS, CC BY 4.0)",
  "series_id": "ics209_1999_AK-ARM-B222_DONNELLY FLATS"
}
```

**Key points (and open questions for review):**
- **Alignment = describes.** The narrative recounts the fire's run/threats/containment; the series is that same incident's acreage/containment/personnel arc. Verified on real fires (Donnelly Flats' "major run to the north" ↔ acres 150→6000).
- **⚠️ Record framing is a design choice — flagged for discussion.** We use **one record per incident** (anchor narrative + arc-to-anchor). The alternative is **one record per sitrep** (each report's narrative + arc-to-date), which is ~10× the volume (~80k) but nests overlapping series. Chosen the cleaner per-incident unit for a first pass.
- **⚠️ Short series (the headline caveat)** — median ~4 reporting days at `min_reports=3`; see the trade-off table at the top. We expect longer series, so `min_reports=7–10` (median 8–11) is the likely setting after review.
- **Terminal-value leakage is inherent and intended** (as with BLS CPI / EIA): the anchor report's own metrics = the series' terminal point. The window ends at the anchor — no future values.
- **Complexes / sub-fire merges** (e.g. three fires merging into a "complex") can make early `ACRES` non-monotonic. This v1 doesn't special-case them; `ics209-plus-wf_complex_associations_1999to2020.csv` (in the bundle) can resolve them if needed.
- **Volume:** demo yields ~19% of incidents (dropped: too-few-points, short-narrative); full build ≈ 7–10k records across 1999–2020. Demo (capped 50) covers only 1999 due to file ordering; full build spans all years.

**Run:**
```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (~7–10k)
```

**Output:** `output/ics209_wildfire_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl` = first 3 records. (`.cache/` git-ignored — holds the 48.7 MB wildfire zip.)

**Sources:** [ICS-209-PLUS on figshare](https://figshare.com/articles/dataset/All-hazards_dataset_mined_from_the_US_National_Incident_Management_System_1999-2020/19858927) · [paper](https://www.nature.com/articles/s41597-023-01955-0) · [USFS product page](https://research.fs.usda.gov/firelab/products/dataandtools/ics-209-plus) — **CC BY 4.0** (cite St. Denis, L.A., et al. 2023).
