# Copernicus C3S Monthly Climate Bulletin → CPT

> **Status: Built** — **117 records** (temperature 62 + sea ice 55; **2021 → May 2026**), all validated, 0 NaN, uniform 2 channels/record, consistent 1991-2020 baseline. Covers two Copernicus file-naming eras (current `C3S_Bulletin_`* + mid-era `ts_*_anomaly_*`).

**What it is:** The Copernicus Climate Change Service (C3S) monthly Climate Bulletin, built as **two record types** (one per theme, one record per month per theme):

- **Temperature** — the "Surface air temperature" narrative + global & European anomalies (both vs 1991–2020).
- **Sea ice** — the "Sea ice cover" narrative + Arctic & Antarctic extent anomalies.

*(Hydrological variables were evaluated and excluded — the prose there is mostly about precipitation & soil moisture, but only relative humidity is published as a downloadable series, so the text↔series alignment would be broken. See Key issues.)*

**Scale:** 117 records span **2021 → present** (temperature from 2021-01, sea ice from 2021-06). Bulletins exist back to ~~2017, but only the **1991-2020-baseline** era (~~2021+) is included so anomalies are directly comparable across records; pre-2021 bulletins used a different baseline and file layout (see Key issues). Each record carries the **full available history through its release** (an expanding window): the ERA5 temperature series run back to 1940/1979 and sea ice to 1979, so every record recites its series in full rather than a fixed trailing slice.

### 🔎 Examples

- **Temperature** — text: [Surface air temperature for May 2026](https://climate.copernicus.eu/surface-air-temperature-may-2026) · time series (CSV): [global monthly anomaly](https://climate.copernicus.eu/sites/default/files/2026-06/C3S_Bulletin_temp_202605_Fig1b_timeseries_anomalies_ref1991-2020_global_allmonths_DATA.csv)
- **Sea ice** — text: [Sea ice cover for May 2026](https://climate.copernicus.eu/sea-ice-cover-may-2026) · time series (CSV): [Arctic extent anomaly](https://climate.copernicus.eu/sites/default/files/2026-06/C3S_Bulletin_seaice_202605_Fig1_Arctic_monthly_extent_anomalies_May_DATA.csv)



#### 📄 Text — bulletin narrative (per theme)


|                |                                                                                                                                                                                                                                   |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **What**       | Genuine ECMWF/C3S analytical prose — global/European rankings, exact anomaly values, regional breakdowns, seasonal (boreal-spring) and SST/El-Niño sections (temperature); Arctic/Antarctic rankings + regional detail (sea ice). |
| **Source**     | `climate.copernicus.eu/surface-air-temperature-{month}-{year}` and `.../sea-ice-cover-{month}-{year}`                                                                                                                             |
| **Format**     | HTML → tag-stripped main content; figure captions ("Data source: ERA5. Credit: C3S/ECMWF"), nav and boilerplate removed. Temp ~4k–11k chars; sea ice ~3k–4.6k chars.                                                              |
| `text_quality` | `"real"`                                                                                                                                                                                                                          |




#### 📈 Time series — ERA5 anomalies (per theme)


|                   |                                                                                                                                                        |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **What / source** | Per-bulletin CSVs, discovered from each page's own hrefs (robust to Copernicus filename/folder changes). Built from ERA5 reanalysis.                   |
| **Temperature**   | `freq 1m`, **expanding window** — the full continuous monthly series from its start through the release month                                          |
| **Sea ice**       | `freq 1y`, **this-calendar-month-across-years** (e.g. every May 1979→report year) — matches the ranking prose ("ranked fourth lowest *for the month*") |



| Record type | Channel (`unit`)                       | Meaning                                        |
| ----------- | -------------------------------------- | ---------------------------------------------- |
| temperature | `global_sat_anomaly_degc_1991_2020`    | Global surface-air-temp anomaly vs 1991–2020   |
| temperature | `europe_sat_anomaly_degc_1991_2020`    | European-land anomaly vs 1991–2020             |
| sea_ice     | `arctic_sie_anomaly_mkm2_1991_2020`    | Arctic sea-ice extent anomaly (million km²)    |
| sea_ice     | `antarctic_sie_anomaly_mkm2_1991_2020` | Antarctic sea-ice extent anomaly (million km²) |


> **Note:** for each theme the narrative directly describes its own series (rankings, anomaly values) — tight, source-native alignment.

**Record shape — temperature** (real, May 2026; arrays abbreviated):

```json
{
  "text": "May 2026 was the second-warmest May globally. ... The global average temperature for May 2026 was 15.81°C, 0.55°C above the 1991-2020 average ... Global and European monthly surface air temperature anomalies (ERA5, degrees C vs 1991-2020) covering the full series from 1940-01 through 2026-05: <ts></ts>",
  "timeseries": [
    {"values": [-0.9455, -0.8582, "...", 0.5503], "unit": "global_sat_anomaly_degc_1991_2020", "freq": "1m"},
    {"values": [-6.873, -4.6792, "...", 0.5994], "unit": "europe_sat_anomaly_degc_1991_2020", "freq": "1m"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "theme": "temperature", "data_month": "2026-05", "series_start": "1940-01", "n_points": 1037,
  "report_url": "https://climate.copernicus.eu/surface-air-temperature-may-2026",
  "dataset": "copernicus_climate_bulletin", "source": "climate.copernicus.eu", "series_id": "c3s_temperature_2026-05"
}
```

**Record shape — sea ice** (real, May 2026; arrays abbreviated, annual):

```json
{
  "text": "Arctic sea ice extent ranked fourth lowest for May ... Antarctic sea ice extent ranked seventh lowest for May ... Arctic and Antarctic May sea-ice extent anomalies (million sq km, vs 1991-2020) for each May through 2026: <ts></ts>",
  "timeseries": [
    {"values": ["...", -0.1212, -0.2797, -0.5754], "unit": "arctic_sie_anomaly_mkm2_1991_2020", "freq": "1y"},
    {"values": ["...", -0.9000, -0.9946, -0.9238], "unit": "antarctic_sie_anomaly_mkm2_1991_2020", "freq": "1y"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "theme": "sea_ice", "data_month": "2026-05", "calendar_month": "May", "n_years": 49,
  "report_url": "https://climate.copernicus.eu/sea-ice-cover-may-2026",
  "dataset": "copernicus_climate_bulletin", "source": "climate.copernicus.eu", "series_id": "c3s_sea_ice_2026-05"
}
```

**Key issues:**

- **Two cadences by design:** temperature is monthly (`1m`, expanding full-history window); sea ice is annual (`1y`, this-month-across-years, full history to the report year) because that's the only clean per-bulletin series *and* it's what the ranking prose describes.
- **Hydrological excluded** — precip/soil-moisture (the bulk of that theme's prose) are maps-only; only relative humidity is a downloadable series, so the pairing would be mismatched. Revisit only if precip/soil-moisture series are sourced from the Climate Data Store (heavier, separate pipeline).
- **Mild leakage** — the prose states the latest anomaly values, which are the final TS points (standard text-describes-TS; flag for Charon if a stricter variant is wanted).
- **Two file-naming eras handled; pre-2021 excluded on purpose** — Copernicus renamed its bulletin CSVs several times. The builder matches **both** current (`C3S_Bulletin_*_allmonths_DATA.csv` / `C3S_Bulletin_seaice_`*) and mid-era (`ts_1month_anomaly_Global_*_1991-2020`, `ts_{Month}_anomaly_{Arctic,Antarctic}_OSI-SAF_sie_*_1991-2020`) files, discovered from each page's own hrefs. **Pre-2021** bulletins used a **1981-2010 baseline** and different layout — excluded so all 117 records share one comparable 1991-2020 baseline (adding them would need per-record baseline tagging). Dropped the current-only pre-industrial channel so temperature is a uniform 2 channels across eras. `pdf`-free — HTML + CSV only.
- **Overlap with NOAA** (entry 6 in `new_datasets.md`) — same global-temperature phenomenon; distinct methodology (ERA5 reanalysis, 1991–2020 baseline, European channel, sea-ice). Keep both tagged, or pick one.

**Run:**

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=4   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (24 records)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (~260)
python scripts/build_cpt_jsonl.py --set data.themes=[temperature]        # one theme only
```

**Output:** `output/copernicus_climate_bulletin_cpt.jsonl` + `output/run_report.json` (`samples/` gitignored; `.cache/` holds page HTML + CSVs so reruns are free).

**Sources:** [C3S Climate Bulletin](https://climate.copernicus.eu/climate-bulletin) (ERA5 reanalysis) · License: Copernicus, free reuse with attribution.