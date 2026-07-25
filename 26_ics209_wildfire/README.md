# ICS-209-PLUS Wildfire Situation Reports + Daily Incident Series → CPT

> **Status: Built full-scale — 13,352 records** (banked + finalized 2026-07-25). Wildfires 1999–2020.
>
> ✅ **Time-series length — solved.** Every record's series is **>20 points (min 21, median 32; 52% at a full 32)**, up from **median 4** under the original per-incident/anchor design. The fix was re-framing the record unit (below), not filtering: length is now a structural guarantee.

**What it is:** One record = **one situation report** — that report's own free-text narrative (significant events, current threat, projected activity, weather, planned actions) paired with the **trailing window of daily incident metrics ending at that report** — acres burned, percent contained, total personnel. The narrative *describes* the fire state the series quantifies → the "describes" alignment class.

#### Why the record unit changed (2026-07-25)

The original design was one record per *incident*: series = first report → the "anchor" report (longest narrative), text = the anchor narrative. Two problems showed up at full scale:

1. **Series far too short for a patch-based model** — median **4** reporting days (97% ≤20, only 0.9% ≥32), because truncating at the anchor discards the rest of the fire and most wildfires are brief. Raising `min_reports` does not *create* long series, it only deletes short ones (the ≥32-point universe is capped at ~520 incidents at any threshold).
2. It left the fire's post-anchor history unused.

**Now:** one record per *report*, series = the last `window_reports` (32) reporting days **ending at that report**, emitted for every report with ≥ `min_window_reports` (21) of trailing history and a narrative ≥ `min_text_chars`. This is the same **trailing-window** pattern as `11_eia_petroleum_weekly`. It makes the report's own metrics **always** the series' terminal point (alignment is structural, not incidental) and yields **13,352** records instead of 7,194 — with a guaranteed usable length.

**Fully open:** ICS-209-PLUS (St. Denis et al. 2023, *Scientific Data*) is **CC BY 4.0** — attribution only, no gate.

#### 📄 Text — situation-report narrative
| | |
|---|---|
| **What** | The report's combined free-text: `SIGNIF_EVENTS_SUMMARY`, `CURRENT_THREAT_NARR`, `PROJECTED_ACTIVITY_NARR`, `WEATHER_CONCERNS_NARR`, `PLANNED_ACTIONS`, `STRATEGIC_NARR`, `REMARKS`. Authentic operational prose written by incident command (lowercase, occasional typos → genuinely `"real"`). Median **1,448** chars. |
| **Which reports** | Every report with ≥ `min_window_reports` of trailing history — so one fire contributes many records (median 10, max 180), each with its own distinct narrative. |
| **Filter** | Narrative ≥ `text.min_text_chars` (default 300) or that report is skipped (no synthetic fallback). |

#### 📈 Time series — daily incident metrics
| | |
|---|---|
| **What** | 3 channels, one point per reporting day, trailing `window_reports` (32) days ending at the report |
| **Source** | [ICS-209-PLUS wildfire bundle](https://figshare.com/articles/dataset/All-hazards_dataset_mined_from_the_US_National_Incident_Management_System_1999-2020/19858927) (figshare, CC BY 4.0). The sitrep CSV lives inside the zip; parsed with the **stdlib** (`zipfile`/`csv`). The CSV is contiguous by `INCIDENT_ID`, so the build streams one incident at a time. |
| **Cadence** | `1d` (irregular) — one report per calendar day; days missing any channel are dropped, so gaps are explicit via the `report_dates` array (aligned 1:1 with values, no imputation). |

| Channel (`unit`) | CSV column |
|---|---|
| `acres_burned` (acres) | `ACRES` |
| `percent_contained` (percent) | `PCT_CONTAINED_COMPLETED` |
| `total_personnel` (persons) | `TOTAL_PERSONNEL` |

**Record shape** (real — Garden Valley Complex, ID 2002; arrays/text abbreviated):
```json
{
  "text": "currently 4 major fires are being managed by the team. a total of 830 acres in 17 fires are being managed... continue to hold line on four of the major fires. mop-up on the others that have been contained.\n\n<ts></ts>",
  "timeseries": [
    {"values": [3500.0, 5000.0, "...", 775.0, 830.0], "unit": "acres", "freq": "1d"},
    {"values": [2.0, 5.0, "...", 50.0, 70.0], "unit": "percent", "freq": "1d"},
    {"values": [177.0, 230.0, "...", 868.0, 743.0], "unit": "persons", "freq": "1d"}
  ],
  "timestamps": ["2002-06-29", "2002-06-30", "...", "2002-07-31"],
  "task_type": "world_knowledge", "text_quality": "real",
  "incident_id": "2002_ID-BOF-067_GARDEN VALLEY COMPLEX", "incident_name": "Garden Valley Complex",
  "poo_state": "ID", "start_year": "2002", "cause": "Human",
  "report_date": "2002-07-31", "report_acres": 830.0, "report_pct_contained": 70.0,
  "report_total_personnel": 743.0, "n_reports": 32, "incident_reporting_days": 44,
  "dataset": "ics209_wildfire", "license": "cc-by-4.0", "alignment": "describes",
  "series_id": "ics209_2002_ID-BOF-067_GARDEN VALLEY COMPLEX_2002-07-31"
}
```
(The narrative recites "830 acres" — exactly the series' terminal `acres` value.)

**Key points:**
- **Alignment = describes, and it is structural.** The record's text *is* the report whose acres/contained/personnel are the series' **terminal point** — verified on **13,352/13,352 (100%)** of records. A further **11.4%** recite an exact value verbatim in the prose. Weaker than the value-reciting Fed/EIA sources, stronger than co-location.
- **Series overlap is intended** (same as `11_eia_petroleum_weekly`): consecutive reports of one fire share up to 31 of 32 trailing days, but each record's **text is a distinct report**. Set `data.max_records_per_incident: N` to cap a single fire's contribution (picks are spread evenly across the fire).
- **⚠️ Physical-validity filter — `data.max_acres_drop_frac` (0.25).** Burned area cannot shrink, but sub-fire re-scoping inside a *complex* makes reported `ACRES` collapse mid-incident (observed to **−100%**, e.g. Mustang Complex 2012). Windows containing a >25% single-step drop are dropped (**951** windows, 22 whole incidents); small decreases from remapping/GIS correction are normal and kept. Set to `null` to disable.
- **Coverage ceiling is data-bound.** Only **882** of 34,622 incidents have ≥21 reporting days with all 3 channels — most wildfires are simply brief. Reconciliation: 860 used + 22 fully-filtered + 33,575 too-few-points + 165 short-text = 34,622.
- **Terminal-value leakage is inherent and intended** (as with BLS CPI / EIA): the report's own metrics = the series' terminal point. The window ends at the report — no future values.
- **No generated text.** An earlier version appended a templated closing sentence ("Daily situation-report values — … across N reporting days through DATE:") before `<ts></ts>`. That was synthesized by the build script, not ICS-209 text. Fixed 2026-07-24 and **verified 0/13,352 in the shipped bank**: `<ts></ts>` is appended directly to the real narrative.

**Run:**
```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (13,352)
```

**Output:** `output/ics209_wildfire_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl` = first 3 records. (`.cache/` git-ignored — holds the 48.7 MB wildfire zip.)

**Sources:** [ICS-209-PLUS on figshare](https://figshare.com/articles/dataset/All-hazards_dataset_mined_from_the_US_National_Incident_Management_System_1999-2020/19858927) · [paper](https://www.nature.com/articles/s41597-023-01955-0) · [USFS product page](https://research.fs.usda.gov/firelab/products/dataandtools/ics-209-plus) — **CC BY 4.0** (cite St. Denis, L.A., et al. 2023).
