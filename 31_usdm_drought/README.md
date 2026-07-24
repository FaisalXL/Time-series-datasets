# US Drought Monitor → CPT

> **Status: Built** (demo: 50 records). Full build ~**271 weekly releases** (2021-05-04 → present). Run with `output.max_records=null`.

**What it is:** The weekly US Drought Monitor (USDM) release. One record = **one weekly map (Tuesday "valid" date)** — the official narrative PDF paired with the **full weekly history** of drought-category area coverage (D0–D4, % of contiguous-US land) from the series' common start (2000-01-04) through the release week (an **expanding window** that grows one week per release).

**Scale:** USDM publishes every Tuesday. Narrative PDFs exist from **2021-05-04** onward (~271 weeks to mid-2026); the statistics API goes back to 2000, so each release carries the complete weekly history to date (~1,110 points for the earliest release, ~1,385 for the latest). Demo emits 50; full build ~271.

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
| **Window** | `1w`, **full weekly history** from the common start (2000-01-04) through the release date (oldest → newest, expanding) |

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
  "text": "National Drought Summary – May 4, 2021 ... Drought Intensity Categories\nD1 ... Moderate Drought\nD2 ... Severe Drought\nD3 ... Extreme Drought\nD4 ... Exceptional Drought\nDrought or Dryness Types\nS ... Short-term\nL ... Long-term\nUpdated May 4, 2021\n\n<ts></ts>",
  "timeseries": [
    {"values": [51.00, 61.80, 67.80, "...", 65.64], "unit": "pct_area_d0_abnormally_dry", "freq": "1w"},
    {"values": [23.35, 24.93, 25.91, "...", 46.55], "unit": "pct_area_d1_moderate_drought", "freq": "1w"},
    {"values": [9.45, 9.90, 10.38, "...", 32.24], "unit": "pct_area_d2_severe_drought", "freq": "1w"},
    {"values": [0.00, 0.00, 0.00, "...", 22.56], "unit": "pct_area_d3_extreme_drought", "freq": "1w"},
    {"values": [0.00, 0.00, 0.00, "...", 9.04], "unit": "pct_area_d4_exceptional_drought", "freq": "1w"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "data_week": "2021-05-04", "release_date": "2021-05-04", "series_start": "2000-01-04", "n_points": 1114,
  "statistics_type": "cumulative", "area_of_interest": "CONUS",
  "report_url": "https://droughtmonitor.unl.edu/data/narrativepdf/20210504_nar_usdm.pdf",
  "dataset": "usdm_drought", "source": "droughtmonitor.unl.edu", "series_id": "usdm_2021-05-04"
}
```

**Key issues:**
- **No generated text.** An earlier version of this build appended a templated closing sentence to introduce the D0-D4 series before `<ts></ts>`. That sentence was not from the US Drought Monitor — it was synthesized by the build script. Fixed 2026-07-24: `<ts></ts>` is now appended directly to the real National Drought Summary narrative with nothing generated in between.
- **Cumulative vs marginal** — default is cumulative (`statisticsType=1`); confirm with Charon whether marginal (exclusive per category) is preferred. One-line config flip.
- **CONUS vs Total** — the API returns both per week; we keep `CONUS` (matches the narrative's regional coverage). `Total` (incl. territories) is available via `data.area_of_interest`.
- **Long narratives** (~14k chars median) — the full PDF (national + all regions + outlook) is kept. Early-era (2021) PDFs carry a standard methodology preamble. Could be trimmed to the national Summary if shorter text is wanted.
- **Demo output ≈ 797 KB** for 50 records (narratives are long); lower `output.max_records` for a more GitHub-friendly sample.

**Run:**
```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py                          # demo (50 records)
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (~271)
```

**Output:** `output/usdm_drought_cpt.jsonl` + `output/run_report.json` (`samples/` is gitignored; `.cache/` holds downloaded PDFs + API JSON so reruns are free).

**Sources:** [US Drought Monitor](https://droughtmonitor.unl.edu/) (NDMC / NOAA / USDA) · [USDM statistics API](https://droughtmonitor.unl.edu/DmData/DataDownload/WebServiceInfo.aspx)
