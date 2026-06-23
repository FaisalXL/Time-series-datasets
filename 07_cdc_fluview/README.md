# CDC FluView → CPT

> **Status: In progress** — demo: 36 records (2025–26 season only). Historical scrape 2015+ still TODO.

**What it is:** CDC weekly flu surveillance report (HTML) paired with national ILI + lab indicators (CSV). One record = **one epidemiological week**.

**Scale:** ~**500 records** at full build (10 seasons × ~52 weeks, 2015–2026). Text HTML available from **2015–16 onward** (older archive URLs 404). Demo: **36 weeks** current season.

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
- **Old HTML not on the public archive index** — 2015–2018 pages exist at legacy URLs but may disappear; scrape/cache urgently.
- CSV historical download needed (current local CSVs = one season only).

**Run:** `pip install -r requirements.txt && python scripts/build_cpt_jsonl.py`

**Output:** `output/cdc_fluview_cpt.jsonl` · **Source:** [CDC FluView](https://www.cdc.gov/fluview/) + [FluView Interactive CSVs](https://gis.cdc.gov/grasp/fluview/fluportaldashboard.html)
