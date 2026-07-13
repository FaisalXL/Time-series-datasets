# CPT World-Knowledge Datasets

Per-dataset packages for instruction-free continued pre-training: natural text with a single `<ts></ts>` placeholder + aligned time series.

**Demo / dev outputs** live in each folder's `output/` (typically 50 records, capped locally). Full runs pending shared storage. **19 packages built.**

## Dev Set (for review)

Small-scale samples for format inspection and freeze. Open each folder for its README and `output/*.jsonl`:

| # | Dataset | Dev samples | Channels | `freq` | Notes |
|---|---------|------------:|---------:|--------|-------|
| 01 | [NOAA Storm Events](./01_noaa_storm_events/) | 50 | 3 | `1d` | state-month unit → 28–31 daily steps |
| 02 | [NHC HURDAT2](./02_nhc_hurdat2/) | 50 | 5 | `6h` | per-storm track, 8–70 steps |
| 04 | [TelecomTS](./04_telecom_ts/) | 50 | 5 | `100ms` | 128-step window · ⚠️ anomaly text GPT-generated, pending sign-off |
| 05 | [FNSPID](./05_fnspid/) | 75 | 6 | `1d` | 30-day OHLCV window · raw + relevance-filtered files (see folder README) |
| 06 | [StockNet](./06_stocknet/) | 50 | 5 | `1d` | weekly OHLCV (~5 steps) · tweets are third-party text (`"real"`) — confirm tag |
| 07 | [CDC FluView](./07_cdc_fluview/) | 358 | 15 | `1w` | season-to-date window (1–52 wks, median ~22) |
| 08 | [BLS CPI Releases](./08_bls_cpi/) | 50 | 5 | `1M` | CPI release recites the index = the series (all-items SA/NSA, core, food, energy) |
| 11 | [EIA Weekly Petroleum](./11_eia_petroleum_weekly/) | 50 | 6 | `1W` | WPSR "Highlights" recite crude/gasoline/distillate stocks, refinery inputs/util, imports = the series · 52-wk window · **public domain** |
| 24 | [NOAA SWPC Space Weather](./24_noaa_swpc/) | 364 | 18 | `1d`/`3h` | geomagnetic alert/forecast text + Kp / A-index / particle-flux channels |
| 25 | [NOAA NWPS + USGS Flood](./25_noaa_nwps_flood/) | 50 | 1 | `1h` | per-event hourly river-stage hydrograph + NWS flood-category defs & impact statements for the crest stage · ⚠️ "describes" via threshold semantics · public domain |
| 26 | [ICS-209-PLUS Wildfire](./26_ics209_wildfire/) | 50 | 3 | `1d` | fire situation-report narrative + daily acres/containment/personnel arc · CC BY 4.0 |
| 31 | [US Drought Monitor](./31_usdm_drought/) | 50 | 5 | `1w` | 12-week window · D0–D4 % CONUS area · narrative PDF + stats API |
| 35 | [Copernicus Climate Bulletin](./35_copernicus_climate_bulletin/) | 117 | 2 / 2 | `1m` / `1y` | 2 record types: temperature (global+Europe, 12-mo) + sea ice (Arctic+Antarctic, this-month-across-years) |
| 42 | [Earnings Calls + SEC XBRL](./42_earnings_calls_xbrl/) | 50 | 3 | `1q` | transcript recites revenue/net-income/EPS = the XBRL series (12-quarter window) |
| 45 | [Cricket Report + Per-Over](./45_cricket_report_overseries/) | 50 | 4 | `1over` | ESPNcricinfo match report + per-over runs/wickets/cumulative/run-rate · ⚠️ report text copyrighted, redistribution pending sign-off |
| 47 | [Philadelphia Fed MBOS](./47_philadelphia_mbos/) | 50 | 7 | `1M` | MBOS release narrative recites the diffusion indices (24-mo window) · public domain · sibling Fed surveys = separate packages |
| 48 | [Dallas Fed TMOS](./48_dallas_tmos/) | 50 | 7 | `1M` | Texas Mfg Outlook release recites diffusion indices (24-mo window) · PDF 2007–20 + HTML 2024→ (⚠ 2021–23 gap) · public domain |
| 49 | [Richmond Fed Manufacturing](./49_richmond_manufacturing/) | 50 | 7 | `1M` | Fifth District Mfg release recites composite + sub-indices (24-mo window) · chart-heavy PDF, text ~2018→ · public domain |
| 50 | [Richmond Fed Service Sector](./50_richmond_nonmanufacturing/) | 50 | 6 | `1M` | Fifth District Service-Sector (non-mfg) release recites revenues/demand/employment/wages (24-mo window) · service twin of #49 · public domain |

## Record format (frozen for dev set)

Every line of `output/*_cpt.jsonl` is one JSON object with these required fields:

| Field | Rule |
|-------|------|
| `text` | Natural prose; contains **exactly one** `<ts></ts>`. |
| `timeseries` | List of channels, each `{values, unit, freq}`. |
| `task_type` | Always `"world_knowledge"`. |
| `text_quality` | `"real"` for first-party/official text; `"generated"` for tagged synthetic text. |

**`freq` convention — compact:** interval + unit, e.g. `100ms`, `3h`, `6h`, `1d`, `1w`/`1W`, `1M`, `1q`, `1y`, `1over`. (Case varies by package where it disambiguates, e.g. `1M` month vs `100ms`.) Dataset-specific extras (`geography`, `season`, `report_url`, …) are allowed after the required fields.

## All packages

Estimated **full-scale datapoints** = CPT records at `output.max_records=null` (demos are capped at the counts above). Figures are estimates; the two big drivers are NOAA Storm Events and FNSPID.

| # | Dataset | Status | Est. datapoints (full) |
|---|---------|--------|-----------------------:|
| 01 | [NOAA Storm Events](./01_noaa_storm_events/) | Complete | **~150k** (~10k/yr, 2010+) |
| 02 | [NHC HURDAT2](./02_nhc_hurdat2/) | Complete | **~320** storms (2000–23, w/ text) |
| 04 | [TelecomTS](./04_telecom_ts/) | Demo done | **~1.3k** (small dataset) |
| 05 | [FNSPID](./05_fnspid/) | Built (full HF pipeline) | **~146k** candidates (5k raw / 2.7k filtered sampled; scales to millions) |
| 06 | [StockNet](./06_stocknet/) | Demo done | **~29k** (87 tickers × ~2 yrs) |
| 07 | [CDC FluView](./07_cdc_fluview/) | Complete (358/558 wks) | **~558** weekly reports |
| 08 | [BLS CPI Releases](./08_bls_cpi/) | Built (demo 50) | **~389** monthly releases (1994+, PDF+HTML) |
| 11 | [EIA Weekly Petroleum](./11_eia_petroleum_weekly/) | Built (demo 50) | **~779** weekly reports (2011→); series to 1982; **public domain** |
| 24 | [NOAA SWPC Space Weather](./24_noaa_swpc/) | Demo done | **~12.3k** (~10.8k daily 1996+ / ~1.5k weekly 1997+) |
| 25 | [NOAA NWPS + USGS Flood](./25_noaa_nwps_flood/) | Built (demo 50) | **~10k–40k** flood events (~2,760 rich-impact gauges of 12,756; each floods repeatedly over ~15 yr); **public domain**; ⚠️ alignment = "describes" via threshold semantics (Charon sign-off) |
| 26 | [ICS-209-PLUS Wildfire](./26_ics209_wildfire/) | Built (demo 50) | **~7–10k** wildfire incidents (1999–2020); **CC BY 4.0**; one record/incident (per-sitrep alt ≈ 80k) |
| 31 | [US Drought Monitor](./31_usdm_drought/) | Built (demo 50) | **~269** weekly releases (2021-05→) |
| 35 | [Copernicus Climate Bulletin](./35_copernicus_climate_bulletin/) | Built (117) | **~120** (monthly, 2021→; grows) |
| 42 | [Earnings Calls + SEC XBRL](./42_earnings_calls_xbrl/) | Built (demo 50) | **~25k+** (transcript × 12-q fundamentals); ⚠️ confirm SEC EDGAR overlap before scaling |
| 45 | [Cricket Report + Per-Over](./45_cricket_report_overseries/) | Built (demo 50) | **~1.9k** IPL innings w/ report (~30k+ all formats); ⚠️ ESPN text redistribution pending sign-off |
| 47 | [Philadelphia Fed MBOS](./47_philadelphia_mbos/) | Built (demo 50) | **~190** monthly releases (2010→); public domain; ⚠️ FRED-overlap sign-off. First of the Fed regional surveys (siblings = separate packages, ~2–4k combined) |
| 48 | [Dallas Fed TMOS](./48_dallas_tmos/) | Built (demo 50) | **~195** monthly releases (2007–20 PDF + 2024→ HTML; ⚠️ 2021–23 gap); public domain; ⚠️ FRED-overlap sign-off |
| 49 | [Richmond Fed Manufacturing](./49_richmond_manufacturing/) | Built (demo 50) | **~100** monthly releases (text ~2018→; series to 1993); public domain; ⚠️ chart-heavy PDF extraction + FRED-overlap sign-off |
| 50 | [Richmond Fed Service Sector](./50_richmond_nonmanufacturing/) | Built (demo 50) | **~100** monthly releases (text ~2018→; series to 1993); public domain; service-sector twin of #49 (shared plumbing); ⚠️ FRED-overlap sign-off |

**Rough total ≈ 400k+ datapoints** across the built set (excluding license-gated Cricket), dominated by NOAA Storm Events (~150k), FNSPID (~146k), and the NWPS flood harvest (~10–40k); the remaining packages contribute ~80k combined.

Each README follows the same layout: what it is → scale → record shape → key issues → how to run. Packages with a `NOTION_PAGE.md` (e.g. 11, 45) carry the review write-up.

See [AGENT_BRIEF.md](./AGENT_BRIEF.md) for adding new datasets.
