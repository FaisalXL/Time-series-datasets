# CDC FluView → CPT

> **Status: Partial full build** — **313 records** from 558 CSV weeks (2015–16 through 2025–26). Historical CSVs in `data/raw_csv/`; HTML cached under `.cache/html/`.

**What it is:** CDC weekly flu surveillance report (HTML) paired with national ILI + lab indicators (CSV). One record = **one epidemiological week**.

**Scale:** 558 national weeks in CSV (11 seasons). **313 emitted** where live CDC HTML + extractable narrative exist (~56%). Largest gaps: **2020–21** (0 records — pages removed from live CDC), **2021–22** and **2022–23** (sparse/broken archive).

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
