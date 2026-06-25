# NOAA SWPC → CPT (daily)

> **Status: Demo** — 50 sample records. Full build: ~10,500–10,800 records (1996–2026).

**What it is:** Daily space weather activity reports paired with geomagnetic and solar measurements. One record = **one observation day**. Window size: **1 day** (sub-daily K-indices are 3-hourly, 8 values/day). Text source is the official NOAA/USAF Solar and Geophysical Activity Summary (SGAS). Time series come from two companion products: Daily Geomagnetic Data (DGD) and Daily Solar Data (DSD).

**Scale:** ~10,800 records from 1996-01-01 through 2026. Full range set via `data.start_date` / `data.end_date` in config.

**Record shape:**

```json
{
  "text": "Joint USAF/NOAA Solar and Geophysical Activity Summary for January 1, 2000:\nA.  ENERGETIC EVENTS\nBEGIN  MAX  END  RGN   LOC   XRAY  OP 245MHZ 10CM   SWEEP\n2013 2016 2020  8814 N11E32 B8.0  SF 120\nB.  PROTON EVENTS:  NONE\nC.  GEOMAGNETIC ACTIVITY SUMMARY: THE GEOMAGNETIC FIELD WAS AT UNSETTLED TO MINOR STORM LEVELS...\n... [sections D–F] ...\nGeomagnetic K-indices (3-hourly intervals), daily A-indices, and solar measurements for this observation day: <ts></ts>",
  "timeseries": [
    {"values": [5, 5, 3, 3, 3, 2, 3, 2], "unit": "kp_fredericksburg",            "freq": "3h"},
    {"values": [4, 3, 6, 6, 6, 4, 4, 3], "unit": "kp_college",                   "freq": "3h"},
    {"values": [4, 5, 4, 4, 5, 3, 3, 2], "unit": "kp_planetary",                 "freq": "3h"},
    {"values": [21],                      "unit": "a_index_fredericksburg",       "freq": "1d"},
    {"values": [44],                      "unit": "a_index_college",              "freq": "1d"},
    {"values": [27],                      "unit": "a_index_planetary",            "freq": "1d"},
    {"values": [130],                     "unit": "radio_flux_10_7cm_sfu",        "freq": "1d"},
    {"values": [69],                      "unit": "sunspot_number",               "freq": "1d"},
    {"values": [540],                     "unit": "sunspot_area_millionths_hemis","freq": "1d"},
    {"values": [0],                       "unit": "new_sunspot_regions",          "freq": "1d"},
    {"values": [3],                       "unit": "c_flare_count",               "freq": "1d"},
    {"values": [0],                       "unit": "m_flare_count",               "freq": "1d"},
    {"values": [0],                       "unit": "x_flare_count",               "freq": "1d"},
    {"values": [1],                       "unit": "optical_s_flare_count",       "freq": "1d"},
    {"values": [0],                       "unit": "optical_1_flare_count",       "freq": "1d"},
    {"values": [0],                       "unit": "optical_2_flare_count",       "freq": "1d"},
    {"values": [0],                       "unit": "optical_3_flare_count",       "freq": "1d"},
    {"values": [5.7e-07],                 "unit": "xray_background_flux_wm2",    "freq": "1d"}
  ],
  "task_type": "world_knowledge",
  "text_quality": "real",
  "obs_date": "2000-01-01",
  "sgas_issue": "2000-01-02"
}
```

**Data sources:**

| Modality | Product | URL pattern | Coverage |
| --- | --- | --- | --- |
| Text | SGAS (`yyyymmddSGAS.txt`) | `.../solar_geophysical_activity_summaries/YYYY/MM/` | 1996–2026 |
| TS (geomagnetic) | DGD (`yyyy_DGD.txt`) | `.../daily_geomagnetic_data/` | 1994–2026 |
| TS (solar) | DSD (`yyyy_DSD.txt`) | `.../daily_solar_data/` | 1994–2026 |

All sources are on the NGDC public archive (`www.ngdc.noaa.gov/stp/space-weather/swpc-products/`).

**Processing:**

```
SGAS (issued date D)  →  obs_date = D - 1 day  ─┐
DGD (row for obs_date)                            ├─ join on obs_date → 1 record
DSD (row for obs_date)                            ─┘
```

SGAS files from 1996–~2002 use ALL CAPS; later years use Title Case. Both are handled. K-index values sometimes appear concatenated without spaces (e.g. `3 2-1 2`) in the DGD source — the parser uses regex integer extraction to handle this.

**TS channels (18 total):**

| Channel | Source | Freq | Unit/Description |
| --- | --- | --- | --- |
| `kp_fredericksburg` | DGD | 3h | K-index, middle latitude (length 8) |
| `kp_college` | DGD | 3h | K-index, high latitude (length 8) |
| `kp_planetary` | DGD | 3h | Estimated planetary K-index (length 8) |
| `a_index_fredericksburg` | DGD | 1d | Daily A-index, middle latitude |
| `a_index_college` | DGD | 1d | Daily A-index, high latitude |
| `a_index_planetary` | DGD | 1d | Daily planetary A-index |
| `radio_flux_10_7cm_sfu` | DSD | 1d | Solar radio flux at 10.7 cm (SFU) |
| `sunspot_number` | DSD | 1d | SESC daily sunspot number |
| `sunspot_area_millionths_hemis` | DSD | 1d | Total sunspot area (millionths of hemisphere) |
| `new_sunspot_regions` | DSD | 1d | New active regions that day |
| `c_flare_count` | DSD | 1d | Count of C-class X-ray flares |
| `m_flare_count` | DSD | 1d | Count of M-class X-ray flares |
| `x_flare_count` | DSD | 1d | Count of X-class X-ray flares |
| `optical_s_flare_count` | DSD | 1d | Optical sub-flare count |
| `optical_1_flare_count` | DSD | 1d | Optical class-1 flare count |
| `optical_2_flare_count` | DSD | 1d | Optical class-2 flare count |
| `optical_3_flare_count` | DSD | 1d | Optical class-3 flare count |
| `xray_background_flux_wm2` | DSD | 1d | Background X-ray flux (W/m²), converted from letter+number class |

**Key issues:**

- **Length-1 vs length-8 TS mixing** — K-index channels have 8 values (freq `3h`); all DSD and A-index channels have 1 value (freq `1d`). Mixed-length TS per record is valid per the CPT schema (each channel carries its own `freq`), but unusual compared to other datasets in this project. Confirm with Charon.
- **SGAS date offset** — SGAS issued on date D reports obs_date D−1 ("data received at SWO on XX"). The script extracts the obs date from the header text and falls back to issue_date−1 if the pattern is not found.
- **Missing DGD data** — The DGD source sometimes has `-1` values (missing) across all columns for a given day. Records where planetary K-indices are fully missing are filtered out (`min_ts_channels: 3`).
- **Stanford Solar Mean Field omitted** — This DSD column has systematic -999 gaps across many years and is excluded.
- **DGD concatenation bug in source** — Some DGD lines store `-1` values concatenated without whitespace (e.g. `3 2-1`). The parser handles this with `re.findall(r"-?\d+")` rather than `split()`.
- **Solar cycle variation** — Active periods (solar max ~2000, ~2014) will have much richer section A event logs than solar minimum years. This creates natural text length variation across records.
- **Quiet-day records are intentionally included** — During solar minimum (notably 2008–2009, 2019–2020), many SGAS reports show no energetic events, minimal geomagnetic activity, and a one-line section C ("The geomagnetic field was quiet."). These records are kept rather than filtered. The contrast between a quiet day (K-indices all 0–3, no flares, sparse text) and an active day (K-indices 6–9, multiple M/X flares, multi-paragraph narrative) is itself the learning signal — filtering quiet days would bias the dataset toward storm periods and remove the baseline the model needs.

**Run:**

```bash
cd datasets/24_noaa_swpc
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/build_daily_cpt.py                                            # 50-record demo
python scripts/build_daily_cpt.py --set data.start_date=1996-01-01 \
                                   --set data.end_date=2026-01-01 \
                                   --set output.max_records=null             # full build
```

**Output:** `output/noaa_swpc_daily_cpt.jsonl` · `output/run_report_daily.json`

**Sources:** NOAA/USAF (public domain) · NGDC archive `ngdc.noaa.gov/stp/space-weather/swpc-products/`

---

# NOAA SWPC → CPT (weekly, PRF PDFs)

> **Status: Demo** — 5 sample records. Full build: ~1,500 records (1997–2026).

**What it is:** Weekly space weather reports parsed from the NOAA Preliminary Report and Forecast (PRF) PDFs. One record = **one PRF week**. Window size: **7 days** (all channels length 7, freq `1d`). Text comes from the "Space Weather Highlights" section of the official NOAA/USAF weekly report — typically 400–800 words of expert narrative on solar regions, flares, CMEs, proton events, and geomagnetic storm conditions. Time series come from the embedded Daily Solar Data and Daily Geomagnetic Data tables.

**Scale:** ~52 PRFs/year, 1997–2026 → ~1,500 records.

**Record shape:**

```json
{
  "text": "Space Weather Highlights for the week of December 27–January 2, 2000:\nSolar activity ranged from low to moderate levels. Activity was at moderate levels during 27-28 December as Region 8806 produced three M-class flares...\n[~500 words] ...\nDaily solar flux, sunspot activity, X-ray flux, flare counts, and geomagnetic indices for each day of this observation week: <ts></ts>",
  "timeseries": [
    {"values": [162, 150, 144, 136, 130, 130, 133], "unit": "radio_flux_10_7cm_sfu",         "freq": "1d"},
    {"values": [109,  77, 123,  88,  91,  69,  69], "unit": "sunspot_number",                "freq": "1d"},
    {"values": [1450,1130,1030, 530, 530, 540, 460], "unit": "sunspot_area_millionths_hemis", "freq": "1d"},
    {"values": [  7,   5,   3,   3,   2,   3,   1], "unit": "c_flare_count",                 "freq": "1d"},
    {"values": [  2,   1,   0,   0,   0,   0,   0], "unit": "m_flare_count",                 "freq": "1d"},
    {"values": [  0,   0,   0,   0,   0,   0,   0], "unit": "x_flare_count",                 "freq": "1d"},
    {"values": [7.4e-07, 7.6e-07, 4.6e-07, ...],    "unit": "xray_background_flux_wm2",      "freq": "1d"},
    {"values": [  6,   7,   7,   8,  27,  27,  14], "unit": "a_index_planetary",             "freq": "1d"},
    {"values": [  6,   8,   5,   7,  20,  21,  13], "unit": "a_index_fredericksburg",        "freq": "1d"},
    {"values": [  3,   3,   3,   3,   5,   5,   4], "unit": "kp_daily_max_planetary",        "freq": "1d"},
    {"values": [  4,   4,   3,   3,   5,   5,   3], "unit": "kp_daily_max_fredericksburg",   "freq": "1d"}
  ],
  "task_type": "world_knowledge",
  "text_quality": "real",
  "week_start": "1999-12-27",
  "week_end":   "2000-01-02",
  "prf_id": "1270"
}
```

**Data source:** PRF PDFs, NGDC archive `.../weekly_reports/PRFs_of_SGD/YYYY/MM/prfXXXX.pdf`

**Processing:**

```
PRF PDF (all pages)
  │
  ├── Page 1 (or 1-2 in newer format): "Space Weather Highlights" text
  │     Stop at "Space Weather Outlook" (forward-looking — excluded to prevent leakage)
  │
  └── Page 2 (or 3 in newer format): Data tables
        Daily Solar Data    → 11 TS channels × 7 days
        Daily Geomagnetic Data → 4 TS channels × 7 days
        (Daily Particle Data table skipped — variable column count across eras)
```

**TS channels (15 total, all length 7, freq `1d`):**

| Channel | Source | Description |
| --- | --- | --- |
| `radio_flux_10_7cm_sfu` | Solar Data | Solar radio flux (SFU) |
| `sunspot_number` | Solar Data | SESC daily sunspot number |
| `sunspot_area_millionths_hemis` | Solar Data | Total sunspot area |
| `c_flare_count` | Solar Data | C-class X-ray flare count |
| `m_flare_count` | Solar Data | M-class X-ray flare count |
| `x_flare_count` | Solar Data | X-class X-ray flare count |
| `optical_s_flare_count` | Solar Data | Optical sub-flare count |
| `optical_1/2/3_flare_count` | Solar Data | Optical class-1/2/3 counts |
| `xray_background_flux_wm2` | Solar Data | Background X-ray flux (W/m²) |
| `a_index_planetary` | Geomag Data | Planetary daily A-index |
| `a_index_fredericksburg` | Geomag Data | Fredericksburg daily A-index |
| `kp_daily_max_planetary` | Geomag Data | Daily max Kp (planetary) |
| `kp_daily_max_fredericksburg` | Geomag Data | Daily max Kp (Fredericksburg) |

**Key issues:**

- **Format changed ~2020** — Older PRFs (pre-2020) fit Highlights + tables on pages 1-2. Newer PRFs (post-2020) span Highlights across pages 1-2 and move tables to page 3. The script detects both formats with regex section-finding rather than fixed page numbers.
- **PDF parser** — Uses `pymupdf` (fitz) for all PDFs. `pdfplumber` fails on newer PRF formats (missing MediaBox). Both old and new formats produce equivalent text extraction with fitz.
- **PRF number discontinuities** — The archive has occasional missing files (~5-10% of weeks). These are silently skipped.
- **Leakage guard** — The "Space Weather Outlook" section (forward-looking forecasts) is explicitly excluded. Only the "Space Weather Highlights" narrative (past events) is used as text.
- **Cross-year weeks** — Weeks spanning December/January are handled correctly. The date parsing tries both `year` and `year - 1` when assigning months to days.
- **Daily Particle Data skipped** — Column count varies by era (instrument changes on successive GOES satellites). Excluded to avoid misaligned channels.

**Run:**

```bash
cd datasets/24_noaa_swpc
source .venv/bin/activate   # created by daily build — reuse it
python scripts/build_weekly_cpt.py                                           # 5-record demo
python scripts/build_weekly_cpt.py --set data.year_start=1997 \
                                    --set data.year_end=2026 \
                                    --set output.max_records=null            # full build
```

**Output:** `output/noaa_swpc_weekly_cpt.jsonl` · `output/run_report_weekly.json`
