# NHC HURDAT2 → CPT

> **Status: Complete** (demo: 50 records). Full export pending server access.

**What it is:** Atlantic hurricane best-track (6-hourly wind, pressure, lat/lon, wind radii) paired with real NHC public advisory text. One record = **one storm lifecycle**.

**Scale:** ~**320 storms** (Atlantic 2000–2023, ≥8 obs, with advisory archive) of ~370 qualifying. **87% advisory coverage** overall; **2000–2002 have no online archive** (pre-2003 URL pattern).

#### 📄 Text — NHC public advisories
| | |
|---|---|
| **What** | Official NHC public advisory bulletins — human-written forecaster prose (`BULLETIN` format) |
| **Source** | NHC advisory archive — `nhc.noaa.gov/archive/<year>/<basin><nn>...` |
| **Where** | Fetched per storm from the [NHC archive](https://www.nhc.noaa.gov/) |
| **Format** | Plain-text advisory pages; **3 subsampled per storm** (first / peak / last), each capped 1,500 chars |
| **`text_quality`** | `"real"` (first-party official) · `text_source: "nhc_advisory"` |

#### 📈 Time series — HURDAT2 best-track
| | |
|---|---|
| **What** | 5 channels of storm intensity & position over the storm's life |
| **Source** | HURDAT2 best-track file — [nhc.noaa.gov/data/hurdat](https://www.nhc.noaa.gov/data/hurdat/) |
| **Cadence** | `6h`, variable length (one storm = ~8 to 60+ steps) |

| Channel (`unit`) | Meaning |
|---|---|
| `max_wind_kt` | Max sustained wind (knots) |
| `min_pressure_mb` | Min central pressure (mb; `null` if unrecorded) |
| `lat` | Storm-center latitude (°N) |
| `lon` | Storm-center longitude (°E, negative = W) |
| `r34_max_nm` | Max radius of 34-kt winds (nautical miles) |

> **Note:** text (NHC advisories) and TS (HURDAT2 best-track) are two *independent* NHC products for the same storm — genuine cross-source alignment.

**Record shape:**
```json
{
  "text": "BULLETIN\nTROPICAL STORM ALBERTO ADVISORY NUMBER 10\n...MAXIMUM SUSTAINED WINDS ARE NEAR 60 MPH...\n\nSix-hourly observations across the storm's lifetime: <ts></ts>.",
  "timeseries": [
    {"values": [25, 30, 35, 40, 60, 55, 45], "unit": "max_wind_kt", "freq": "6h"},
    {"values": [1004, 1003, 1002, 997, 995, 996, 1001], "unit": "min_pressure_mb", "freq": "6h"},
    {"values": [20.0, 21.9, 23.6, 25.8, 27.5, 29.5, 31.3], "unit": "lat", "freq": "6h"},
    {"values": [-85.0, -85.7, -87.8, -87.4, -85.4, -83.7, -81.9], "unit": "lon", "freq": "6h"},
    {"values": [0, 0, 100, 200, 200, 150, 0], "unit": "r34_max_nm", "freq": "6h"}
  ],
  "storm_name": "ALBERTO", "storm_id": "AL012006", "task_type": "world_knowledge", "text_quality": "real"
}
```

**Key issues:**
- **Text length:** a major storm has 60–100+ advisories (~40k+ words full). We subsample **3 advisories** (first, peak, last), each capped at 1,500 chars → ~1k tokens/record. Team decision needed on granularity (storm vs. per-advisory records).
- Storms without retrievable advisories are dropped — no synthetic fallback.

**Run:** `pip install -r requirements.txt && python scripts/build_cpt_jsonl.py`

**Output:** `output/nhc_hurdat2_cpt.jsonl` · **Source:** [HURDAT2](https://www.nhc.noaa.gov/data/hurdat/) + [NHC advisory archive](https://www.nhc.noaa.gov/)
