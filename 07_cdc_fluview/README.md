# CDC FluView → CPT

> **Status: Partial full build** — **313 records** from 558 CSV weeks (2015–16 through 2025–26). Historical CSVs in `data/raw_csv/`; HTML cached under `.cache/html/`.

**What it is:** CDC weekly flu surveillance report (HTML) paired with national ILI + lab indicators (CSV). One record = **one epidemiological week**.

**Scale:** 558 national weeks in CSV (11 seasons). **313 emitted** where live CDC HTML + extractable narrative exist (~56%). Largest gaps: **2020–21** (0 records — pages removed from live CDC), **2021–22** and **2022–23** (sparse/broken archive).

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
| **What** | 15 channels from **three** FluView Interactive datasets for the same epi-week |
| **Source** | [FluView Interactive](https://gis.cdc.gov/grasp/fluview/fluportaldashboard.html) CSVs: ILINet, Clinical Labs, Public Health Labs |
| **Cadence** | `1w`, **single timestep per record** (natural unit is one week, not a window) |

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

**Record shape:**
```json
{
  "text": "Key Points: Seasonal influenza activity is low nationally... During Week 40, 1.6% of ILINet visits were due to ILI... Weekly indicators: <ts></ts>.",
  "timeseries": [
    {"values": [1.61], "unit": "ili_pct_weighted", "freq": "1w"},
    {"values": [41531], "unit": "ili_total_visits", "freq": "1w"},
    {"values": [0.52], "unit": "clinical_pct_positive", "freq": "1w"},
    {"values": [62], "unit": "ph_H1N1", "freq": "1w"}
  ],
  "season": "2025-2026", "week": 40, "task_type": "world_knowledge", "text_quality": "real"
}
```

**Key issues:**
- **Single timestep per record** — rich weekly text but each series has length 1 (natural unit is one week; not a multi-week window).
- **COVID-era HTML gap** — 2020–21 through 2022–23 mostly unavailable at live `cdc.gov` URLs; listed on [past reports](https://www.cdc.gov/fluview/surveillance/past-reports.html) but often only via CDC web archive (not wired into this pipeline).
- **Legacy extractor** — 2015–19 pages sometimes bleed regional table text into Key Points; 70 weeks skipped for short extracted text.

**Run:** `pip install -r requirements.txt && python scripts/build_cpt_jsonl.py`

**Output:** `output/cdc_fluview_cpt.jsonl` · **Source:** [CDC FluView](https://www.cdc.gov/fluview/) + [FluView Interactive CSVs](https://gis.cdc.gov/grasp/fluview/fluportaldashboard.html)
