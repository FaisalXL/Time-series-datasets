# NOAA SWPC → CPT (daily)

> **Status: Built full-scale** — **8,385 records, 1995-12→2018, 8,385/8,385 strict.**

**What it is:** Daily space-weather activity reports paired with geomagnetic and solar
measurements. One record = **one SGAS report**: that report's own prose + the **trailing
32 days** of daily indices **ending on the observation day it reports**. Text is the
official NOAA/USAF Solar and Geophysical Activity Summary (SGAS); the series come from the
companion Daily Geomagnetic Data (DGD) and Daily Solar Data (DSD) products.

**Scale:** **8,385 records** across 24 calendar years. Up to 17 channels (median 16),
always 32 daily steps.

#### Why the trailing window

The earlier build made each record **one observation day**: a single 8-point 3-hourly
K-index channel alongside ~15 channels holding *one value each*. That is a scalar snapshot,
not a series, and unusable for a patch-32 model. Anchoring a 32-day daily window to the
report's own observation day (the `11`/`25`/`26` pattern) gives **100% 32-step** series on a
uniform `1d` cadence, with the reported day always the terminal point.

#### Two fixes made during this build

- **Generated text removed.** The old build appended a synthesized sentence
  (`"Geomagnetic K-indices (3-hourly intervals) … for this observation day: <ts></ts>"`)
  to **100% of records** — the same defect that made `26_ics209_wildfire`'s first bank
  unusable. `<ts></ts>` is now appended directly to the real SGAS prose, and the record
  keeps the report's own `JOINT USAF/NOAA …` / `SGAS NUMBER … ISSUED AT …` header instead
  of a dateline the script wrote.
- **Old-layout archive recovered.** The NGDC index files use two layouts. The parser only
  understood the 1997+ one (`YYYY MM DD`, space-separated) and could not see the `*`
  missing-marker at all, so any row with a starred station fell under its token-count floor
  and was dropped whole. That silently lost **all of 1996** and 20 days of 1997
  (`skip_no_dgd` 379 → 0, +394 records).

#### Alignment — `describes`, with a measured terminal point

SGAS section E recites the observation day's own indices
(`10 CM 130  SSN 091  AFR/AP 020/027  X-RAY BACKGROUND B4.4`), so the series' **terminal
point is verifiable against the text**. The report says nothing about the preceding 31 days,
so the tier stays `describes`. Measured agreement across the build (`terminal_recite` in the
run report, also per-record in `meta`):

| Channel stated in section E | Match |
|---|---:|
| `radio_flux_10_7cm_sfu` | **99.7%** |
| `sunspot_number` | **98.9%** |
| `a_index_planetary` | 75.9% |
| `a_index_fredericksburg` | 55.8% |

The two A-index channels drift because section E is labelled *REAL-TIME
PRELIMINARY/ESTIMATED* while DGD carries the later final values — the same as-published-vs-
revised drift `07_cdc_fluview` documents. Not an extraction bug: the column mapping was
regression-checked against the raw DGD file.

**⚠️ Range is bounded by the series, not the text.** SGAS runs 1996→present, but the NGDC
annual DGD/DSD files **stop at 2018** (2019+ return 404), so there is nothing to pair after
that. Extending past 2018 needs a newer index source wired up (~2,700 more records available
on the text side).

**Record shape** (real record, arrays abbreviated). The `<ts></ts>` tag is appended
directly to the real SGAS prose — every word before it is verbatim NOAA/USAF text:

```json
{
  "text": "JOINT USAF/NOAA SOLAR AND GEOPHYSICAL ACTIVITY SUMMARY\nSGAS NUMBER 001 ISSUED AT 0245Z ON 02 JAN 2000\nTHIS REPORT IS COMPILED FROM DATA RECEIVED AT SWO ON 01 JAN\nA.  ENERGETIC EVENTS\n...\nE.  DAILY INDICES: (REAL-TIME PRELIMINARY/ESTIMATED VALUES)\n10 CM 130  SSN 091  AFR/AP 020/027   X-RAY BACKGROUND B4.4\n...\nF.  COMMENTS:  NONE\n\n<ts></ts>",
  "timeseries": [
    {"values": [9, 12, 11, "...", 27],   "unit": "a_index_planetary",             "freq": "1d"},
    {"values": [12, 17, 14, "...", 21],  "unit": "a_index_fredericksburg",        "freq": "1d"},
    {"values": [4, 4, 4, "...", 5],      "unit": "k_index_planetary_daily_max",   "freq": "1d"},
    {"values": ["..."],                  "unit": "radio_flux_10_7cm_sfu",         "freq": "1d"},
    {"values": ["..."],                  "unit": "sunspot_number",                "freq": "1d"}
  ],
  "task_type": "world_knowledge", "text_quality": "real", "alignment": "describes",
  "license": "public-domain-us-gov", "text_source": "first_party_official",
  "dataset": "noaa_swpc", "domain": "space_weather", "region": "global",
  "series_id": "noaa_swpc:daily:2000-01-01",
  "period_start": "1999-12-01", "period_end": "2000-01-01",
  "timestamps": ["1999-12-01", "1999-12-02", "...", "2000-01-01"],
  "meta": {
    "obs_date": "2000-01-01", "sgas_issue": "2000-01-02",
    "n_ts_channels": 17, "window_days": 32, "terminal_date": "2000-01-01",
    "terminal_recite": {
      "radio_flux_10_7cm_sfu": {"stated": 130, "series": 130, "match": true},
      "sunspot_number":        {"stated": 91,  "series": 91,  "match": true}
    }
  }
}
```

> Every channel is `1d` and 32 steps long. A channel is emitted only if it is present on
> **every** day of the window — no imputation, so a partially observed channel is dropped
> rather than filled. Channel count therefore varies (median 16, max 17; 1996 records carry
> fewer because the old-layout DSD has no optical-flare columns).


**Data sources:**

| Modality | Product | URL pattern | Coverage |
| --- | --- | --- | --- |
| Text | SGAS (`yyyymmddSGAS.txt`) | `.../solar_geophysical_activity_summaries/YYYY/MM/` | 1996–present |
| TS (geomagnetic) | DGD (`yyyy_DGD.txt`) | `.../daily_geomagnetic_data/` | **1994–2018** (2019+ → 404) |
| TS (solar) | DSD (`yyyy_DSD.txt`) | `.../daily_solar_data/` | **1994–2018** (2019+ → 404) |

The build range is the **intersection**, so it ends at 2018 — verified by directory listing,
not inferred from a single failed request.

All sources are on the NGDC public archive (`www.ngdc.noaa.gov/stp/space-weather/swpc-products/`).

**Processing:**

```
SGAS (issued date D)  →  obs_date = D - 1 day   ← text, and the window's terminal day
DGD/DSD rows for [obs_date - 31 .. obs_date]    ← the 32-day trailing series
```

SGAS files from 1996–~2002 use ALL CAPS; later years use Title Case. Both are handled.

The DGD/DSD index files come in **two layouts** and both are parsed: 1997+ uses
`YYYY MM DD` with space-separated columns (where a run of missing values can be
concatenated, e.g. `-1-1-1-1`), while 1996 uses `DD Mon YY` with hyphen-separated
K-indices (`2-0-0-1-2-2-2-2`) and only 9 index columns. Missing values appear as either
`-1` or `*`; both map to `None`, and a channel with any missing day in the window is
dropped rather than imputed.

**TS channels (17 max, median 16):**

| Channel | Source | Freq | Unit/Description |
| --- | --- | --- | --- |
| `k_index_planetary_daily_max` | DGD | 1d | Daily max of the 8 3-hourly planetary K-indices |
| `k_index_fredericksburg_daily_max` | DGD | 1d | Daily max of the 8 3-hourly mid-latitude K-indices |
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
