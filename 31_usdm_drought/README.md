# US Drought Monitor → CPT

> **Status: Built** (demo: 50 records). Full build ~**269 weekly releases** (2021-05-04 → present). Run with `output.max_records=null`.

**What it is:** The weekly US Drought Monitor (USDM) release. One record = **one weekly map (Tuesday "valid" date)** — the official narrative PDF paired with a **12-week trailing window** of drought-category area coverage (D0–D4, % of contiguous-US land).

**Scale:** USDM publishes every Tuesday. Narrative PDFs exist from **2021-05-04** onward (~269 weeks to mid-2026); the statistics API goes back to 2000, so every narrative has a complete 12-week window. Demo emits 50; full build ~269.

#### 📄 Text — weekly narrative PDF
| | |
|---|---|
| **What** | The official "National Drought Summary" — national overview, per-region breakdown (Northeast, Southeast, South, Midwest, High Plains, West, Caribbean, Pacific), and a "Looking Ahead" forecast. Genuine analytical prose explaining *why* categories changed (precipitation, snowpack, streamflow, soil moisture). |
| **Source** | `droughtmonitor.unl.edu/data/narrativepdf/{YYYYMMDD}_nar_usdm.pdf` (the Tuesday valid date) |
| **Format** | PDF → text via `pdfplumber`; trailing "Author(s)" credits stripped. ~10k–22k chars. |
| **`text_quality`** | `"real"` (official NDMC/NOAA/USDA author rotation) |

#### 📈 Time series — drought-category area coverage (5 channels)
| | |
|---|---|
| **What** | % of CONUS land area in each drought category, weekly |
| **Source** | `usdmdataservices.unl.edu/api/USStatistics/GetDroughtSeverityStatisticsByAreaPercent` (JSON; filtered to `areaOfInterest = CONUS`) |
| **Window** | `1w`, **12 trailing weeks** ending the release date (oldest → newest) |

| Channel (`unit`) | Meaning |
|---|---|
| `pct_area_d0_abnormally_dry` | % area D0 or worse (Abnormally Dry) |
| `pct_area_d1_moderate_drought` | % area D1 or worse (Moderate) |
| `pct_area_d2_severe_drought` | % area D2 or worse (Severe) |
| `pct_area_d3_extreme_drought` | % area D3 or worse (Extreme) |
| `pct_area_d4_exceptional_drought` | % area D4 (Exceptional) |

Values are **cumulative** (`statisticsType=1`: D0 ≥ D1 ≥ … ≥ D4, each includes the more-severe categories). Set `data.statistics_type=2` for marginal (exclusive) values.

> **Note:** the narrative (NDMC) and the area statistics (USDM analysis) are independent USDM products keyed on the same valid week — source-native alignment. The prose discusses the same drought conditions the percentages quantify.

**Record shape:** (real record — 2021-05-04, arrays abbreviated)
```json
{
  "text": "National Drought Summary – May 4, 2021 ... US drought-category coverage (percent of CONUS area, D0 abnormally dry through D4 exceptional drought) for the 12 weeks ending 2021-05-04: <ts></ts>",
  "timeseries": [
    {"values": [64.46, 63.90, 61.67, "...", 65.64], "unit": "pct_area_d0_abnormally_dry", "freq": "1w"},
    {"values": [45.19, 45.56, 46.58, "...", 46.55], "unit": "pct_area_d1_moderate_drought", "freq": "1w"},
    {"values": [30.17, 30.85, 30.93, "...", 32.24], "unit": "pct_area_d2_severe_drought", "freq": "1w"},
    {"values": [19.02, 18.66, 18.62, "...", 22.56], "unit": "pct_area_d3_extreme_drought", "freq": "1w"},
    {"values": [8.14, 8.44, 8.50, "...", 9.04], "unit": "pct_area_d4_exceptional_drought", "freq": "1w"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "data_week": "2021-05-04", "release_date": "2021-05-04", "window_weeks": 12,
  "statistics_type": "cumulative", "area_of_interest": "CONUS",
  "report_url": "https://droughtmonitor.unl.edu/data/narrativepdf/20210504_nar_usdm.pdf",
  "dataset": "usdm_drought", "source": "droughtmonitor.unl.edu", "series_id": "usdm_2021-05-04"
}
```

**Key issues:**
- **Cumulative vs marginal** — default is cumulative (`statisticsType=1`); confirm with Charon whether marginal (exclusive per category) is preferred. One-line config flip.
- **CONUS vs Total** — the API returns both per week; we keep `CONUS` (matches the narrative's regional coverage). `Total` (incl. territories) is available via `data.area_of_interest`.
- **Long narratives** (~14k chars median) — the full PDF (national + all regions + outlook) is kept. Early-era (2021) PDFs carry a standard methodology preamble. Could be trimmed to the national Summary if shorter text is wanted.
- **Demo output ≈ 797 KB** for 50 records (narratives are long); lower `output.max_records` for a more GitHub-friendly sample.

**Run:**
```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py                          # demo (50 records)
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (~269)
```

**Output:** `output/usdm_drought_cpt.jsonl` + `output/run_report.json` (`samples/` is gitignored; `.cache/` holds downloaded PDFs + API JSON so reruns are free).

**Sources:** [US Drought Monitor](https://droughtmonitor.unl.edu/) (NDMC / NOAA / USDA) · [USDM statistics API](https://droughtmonitor.unl.edu/DmData/DataDownload/WebServiceInfo.aspx)
