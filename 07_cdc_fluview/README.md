# CDC FluView → CPT

> **Status: Partial full build** — **358 records** from 558 CSV weeks (2015–16 through 2025–26). Historical CSVs in `data/raw_csv/`; HTML cached under `.cache/html/`.

**What it is:** CDC weekly flu surveillance report (HTML) paired with national ILI + lab indicators (CSV). One record = **one epidemiological week's report**, with the TS holding that **season's trajectory to date** (not a single value).

**Scale:** 558 national weeks in CSV (11 seasons). **358 emitted** where live CDC HTML + extractable narrative exist. Largest gaps: **2020–21** (pages removed from live CDC), **2021–22** and **2022–23** (sparse/broken archive).

#### 📄 Text — weekly FluView surveillance report
| | |
|---|---|
| **What** | The "Key Points" / national summary narrative from CDC's weekly influenza surveillance report |
| **Source** | [cdc.gov/fluview](https://www.cdc.gov/fluview/) weekly report HTML (per-record `report_url`) |
| **Format** | Extracted from the live HTML page; `text_source` tags the extractor used |
| **`text_quality`** | `"real"` (official CDC text) |

#### 📈 Time series — national flu indicators (15 channels)
| | |
|---|---|
| **What** | 15 channels from **three** FluView Interactive datasets |
| **Source** | [FluView Interactive](https://gis.cdc.gov/grasp/fluview/fluportaldashboard.html) CSVs: ILINet, Clinical Labs, Public Health Labs |
| **Cadence / window** | `1w`, **season-to-date window** — MMWR week 40 → the report week (1–52 values; median ~22). The weekly report text discusses the season's trajectory, so the multi-week window is what the prose describes. |

| Channel (`unit`) | Source table | Meaning |
|---|---|---|
| `ili_pct_weighted` | ILINet | Weighted % of outpatient visits for ILI |
| `ili_total_visits` | ILINet | Total patient visits reported |
| `age_0_4` | ILINet | ILI visits, ages 0–4 |
| `age_5_24` | ILINet | ILI visits, ages 5–24 |
| `age_25_49` | ILINet | ILI visits, ages 25–49 |
| `age_50_64` | ILINet | ILI visits, ages 50–64 |
| `age_65_plus` | ILINet | ILI visits, ages 65+ |
| `clinical_pct_positive` | Clinical Labs | % specimens positive for flu |
| `clinical_pct_A` | Clinical Labs | % positive for influenza A |
| `clinical_pct_B` | Clinical Labs | % positive for influenza B |
| `ph_H1N1` | Public Health Labs | Specimens typed A(2009 H1N1) |
| `ph_H3` | Public Health Labs | Specimens typed A(H3) |
| `ph_B` | Public Health Labs | Specimens typed B (lineage not determined) |
| `ph_BVic` | Public Health Labs | Specimens typed B/Victoria |
| `ph_BYam` | Public Health Labs | Specimens typed B/Yamagata |

> **Note:** text (CDC report HTML) and TS (FluView Interactive CSVs) are independent CDC products for the same week — genuine cross-source alignment.

**Record shape:** (real record — 2015–16 season, week 47, 8 weeks to date; arrays abbreviated)
```json
{
  "text": "Key Points: During week 47 (November 22-28, 2015), influenza activity increased slightly in the United States but remained low overall... National influenza surveillance indicators for the 2015-2016 season, weekly from the season start (MMWR week 40) through week 47 (8 weeks to date): <ts></ts>.",
  "timeseries": [
    {"values": [1.23, 1.31, 1.37, 1.39, 1.44, 1.50, 1.60, 1.90], "unit": "ili_pct_weighted", "freq": "1w"},
    {"values": [10049, 10715, 11584, 11164, 12423, 12676, 13484, 12158], "unit": "ili_total_visits", "freq": "1w"}
  ],
  "season": "2015-2016", "week": 47, "window_n_weeks": 8, "window_start_week": 40,
  "task_type": "world_knowledge", "text_quality": "real"
}
```
*(15 channels total; all share the same window length. Window = season start → report week.)*

**Key issues:**
- **Season-to-date window** — each record's TS runs from MMWR week 40 to the report week (median ~22 weeks), matching the season trajectory the report text discusses. Season-opening (week-40) records are length-1 by nature (~2.5% of records); raise `min_window_weeks` to drop them if a multi-step floor is wanted.
- **COVID-era HTML gap** — 2020–21 through 2022–23 mostly unavailable at live `cdc.gov` URLs; listed on [past reports](https://www.cdc.gov/fluview/surveillance/past-reports.html) but often only via CDC web archive (not wired into this pipeline).
- **Legacy extractor** — 2015–19 pages sometimes bleed regional table text into Key Points; weeks with short extracted text are skipped.

**Run:** `pip install -r requirements.txt && python scripts/build_cpt_jsonl.py`

**Output:** `output/cdc_fluview_cpt.jsonl` · **Source:** [CDC FluView](https://www.cdc.gov/fluview/) + [FluView Interactive CSVs](https://gis.cdc.gov/grasp/fluview/fluportaldashboard.html)
