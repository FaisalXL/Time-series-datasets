# CDC FluView → CPT World-Knowledge JSONL

> **Status: In progress** — demo output for 2025–2026 season only (~50 records). Historical CSV download and HTML archive scrape (2015+) still TODO.

Weekly US influenza surveillance: CDC FluView HTML narratives paired with national ILINet and NREVSS laboratory indicators for continued pre-training (CPT).

---

## What this dataset is

[CDC FluView](https://www.cdc.gov/fluview/) is the US Centers for Disease Control and Prevention's **weekly influenza surveillance report**. Each report summarizes national flu activity for one epidemiological week: outpatient illness visits, virologic test positivity, hospitalizations, mortality, and pediatric deaths.

This package produces **one CPT record per week** where:

- **Text** — prose extracted from the CDC FluView weekly HTML report
- **Time series** — 15 single-week national indicators merged from three surveillance CSVs (ILINet, clinical labs, public health labs)

The local demo run processes the **2025–2026 season** (Week 40 2025 through Week 22 2026) using CSV files the user has already downloaded. The script is written to handle **all seasons from 2015–2016 onward** once historical CSVs are dropped into `csv_dir`.

---

## What we are training for

**Objective:** Continued pre-training on world knowledge, not instruction fine-tuning. The model reads natural CDC surveillance prose with a `<ts></ts>` placeholder; 15 weekly indicator values are stored separately. There is no forecast prompt.

**What the model learns:** How official public-health surveillance language ("ILI above baseline," "H3N2 predominance," "FluSurv-NET hospitalization rate") co-occurs with weekly flu indicators — outpatient visits, lab positivity, subtype counts, and mortality signals.

**Example record (schematic):**

```json
{
  "text": "Key Points: Seasonal influenza activity is elevated... Nationally, during Week 52, 8.2% of patient visits... Weekly ILI, virologic, hospitalization, and mortality indicators for this surveillance week: <ts></ts>.",
  "timeseries": [
    {"values": [8.28], "unit": "ili_pct_weighted", "freq": "1w"},
    {"values": [215461], "unit": "ili_total_visits", "freq": "1w"},
    {"values": [40575], "unit": "age_0_4", "freq": "1w"},
    {"values": [71907], "unit": "age_5_24", "freq": "1w"},
    {"values": [53733], "unit": "age_25_49", "freq": "1w"},
    {"values": [21221], "unit": "age_50_64", "freq": "1w"},
    {"values": [28025], "unit": "age_65_plus", "freq": "1w"},
    {"values": [31.71], "unit": "clinical_pct_positive", "freq": "1w"},
    {"values": [29.98], "unit": "clinical_pct_A", "freq": "1w"},
    {"values": [1.72], "unit": "clinical_pct_B", "freq": "1w"},
    {"values": [333], "unit": "ph_H1N1", "freq": "1w"},
    {"values": [4298], "unit": "ph_H3", "freq": "1w"},
    {"values": [93], "unit": "ph_B", "freq": "1w"},
    {"values": [53], "unit": "ph_BVic", "freq": "1w"},
    {"values": [0], "unit": "ph_BYam", "freq": "1w"}
  ],
  "season": "2025-2026",
  "year": 2025,
  "week": 52,
  "week_ending_date": "2025-12-27",
  "report_url": "https://www.cdc.gov/fluview/surveillance/2025-week-52.html",
  "dataset": "cdc_fluview",
  "source": "cdc.gov/fluview",
  "series_id": "fluview_2025_w52",
  "task_type": "world_knowledge",
  "text_source": "cdc_fluview_weekly_report",
  "text_quality": "real"
}
```

---

## Data sources

### Weekly HTML reports (text)

Two URL patterns depending on season:

| Pattern | Seasons | URL format |
|---------|---------|------------|
| **A** | 2015–2016 through 2018–2019 | `https://www.cdc.gov/flu/weekly/weeklyarchives{season}/week{WW}.htm` |
| **B** | 2019–2020 onward | `https://www.cdc.gov/fluview/surveillance/{year}-week-{WW}.html` |

For Pattern B, `{year}` is the calendar year from the CSV `YEAR` column (e.g. Week 1 of season 2025–2026 → `2026-week-01.html`).

HTML is cached under `.cache/html/{season}/week{WW}.html`. Downloads use a 1-second delay between requests.

### Surveillance CSVs (time series)

Three files in `data.csv_dir` (configurable):

| File | Contents |
|------|----------|
| `ILINet.csv` | Weighted/unweighted ILI %, age-group visit counts, ILI total |
| `ICL_NREVSS_Clinical_Labs.csv` | Clinical lab specimens, % positive, % A, % B |
| `ICL_NREVSS_Public_Health_Labs.csv` | Public health lab counts by subtype (H1N1, H3, B, BVic, BYam) |

All three are filtered to `REGION TYPE == "National"` and joined on `(YEAR, WEEK)`. Records are emitted only when all three files have data for that week.

---

## Record structure

- **One record = one epidemiological week** (snapshot, not a rolling window)
- **15 timeseries channels**, each with **exactly one value** (`freq: "1w"`)
- **Exactly one** `<ts></ts>` in the `text` field (closing intro sentence from config)
- Missing/suppressed CSV values (`X`) → `null` in the values array

---

## Text extraction

The script extracts these narrative sections from each HTML page (in order), skipping tables, chart labels, navigation, and boilerplate:

1. **Key Points** — bullet-point summary (or "Synopsis" on older pages)
2. **Virologic surveillance** — first paragraph starting with "Nationally" describing circulating viruses and clinical-lab positivity
3. **Outpatient ILI** — first paragraph starting with "Nationally, during Week X, Y% of patient visits..."
4. **Hospitalization** — FluSurv-NET paragraph with total hospitalizations, weekly rate per 100,000, and age-group breakdown
5. **Mortality** — NCHS paragraph stating % of deaths due to influenza
6. **Pediatric deaths** — weekly pediatric death count paragraph (if present)

Missing sections are skipped silently. Records with fewer than `text.min_text_chars` (default 200) of extracted narrative are dropped.

---

## Known caveats

- **Pre-2015–16 archives** are not covered by the documented URL patterns; older seasons may need alternate acquisition.
- **Some weeks lack HTML pages** (404 or timeout) even when CSV data exists — those weeks are skipped with a warning.
- **Single-week snapshots** — all `timeseries` arrays have length 1; this is not a multi-week window dataset.
- **ILI is syndromic**, not lab-confirmed influenza — ILINet captures fever + cough/sore throat from any respiratory pathogen.
- **Text–data alignment** is same-week by construction, but HTML narrative may reference cumulative season totals alongside weekly figures.

---

## How to run

```bash
cd datasets/07_cdc_fluview
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py
```

Outputs:

- `output/cdc_fluview_cpt.jsonl` — CPT records
- `output/run_report.json` — run statistics and config snapshot
- `samples/example_output.jsonl` — first 3 emitted records

### Configuration

| Section | Key | Purpose |
|---------|-----|---------|
| **data** | `csv_dir` | Directory with ILINet + NREVSS CSV files |
| | `seasons` | List of flu seasons to process |
| | `html_cache_dir` | Local HTML cache path |
| | `request_delay_s` | Delay between CDC HTTP requests |
| | `timeout_s` | HTTP timeout per request |
| **text** | `min_text_chars` | Minimum extracted narrative length |
| | `ts_intro_sentence` | Closing sentence with `<ts></ts>` |
| **output** | `max_records` | Cap records (`null` = all weeks) |
| | `output_path` / `report_path` / `samples_path` | Output files |

```bash
# Full season (no cap) once historical CSVs are available
python scripts/build_cpt_jsonl.py --set output.max_records=null

# Single prior season
python scripts/build_cpt_jsonl.py --set data.seasons=[2024-2025]
```

---

## Files in this folder

| File | Purpose |
|------|---------|
| `README.md` | This document |
| `assessment.json` | Triage / probe metadata |
| `config.example.yaml` | Default configuration |
| `requirements.txt` | Python dependencies |
| `scripts/build_cpt_jsonl.py` | HTML + CSV → CPT JSONL builder |
| `samples/example_output.jsonl` | First 3 records from latest run |
| `data/` | Optional local CSV drop directory |
| `.cache/html/` | Downloaded weekly HTML cache |
| `output/` | Generated JSONL + run reports |
