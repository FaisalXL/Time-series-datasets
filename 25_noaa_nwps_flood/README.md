# NOAA NWPS river-flood impacts + USGS gage height → CPT

> **Status: Built** (demo: 50 records). One record = **one flood event at a river gauge** — the
> hourly stage hydrograph around the crest, paired with the National Weather Service flood-category
> definitions and the official impact statement(s) for the stage the river reached. Defu-30 **#25**
> (NOAA NWPS / USGS Flood). New hydrology domain.

**What it is:** For a river gauge, the National Water Prediction Service (NWPS) publishes flood
**categories** (action / minor / moderate / major stage, in feet) and a set of **impact statements**
("At 60 ft: Significant flooding in East End, California and New Richmond…"). USGS NWIS serves the
**gage-height** time series. We detect real flood **events** (the river crossing minor-flood stage),
take a fixed **hourly window around the crest** (Option B — event-anchored), and pair it with the
impact statement(s) for the crest stage. The series *reaches* a stage; the text says what that
stage **means** in the real world.

#### 📈 Time series — hourly river stage
| | |
|---|---|
| **What** | Gage height (ft), one channel, hourly, over a ±10-day window centred on the crest (up to **481** points). |
| **Source** | USGS NWIS instantaneous values (`00065`), pulled per-year (config `usgs_year_start/end`), decimated to hourly buckets (bucket **max** — flood-relevant). Deep sub-daily history (most sites ~2007→). |
| **Datum** | Aligned to the NWPS gauge datum via a per-gauge offset = median(NWPS-observed − USGS) over their recent overlap (typically **< 0.1 ft**; stored in `datum_offset_ft`). This makes the values line up with the NWS flood thresholds/impacts. |
| **Cadence** | `1h` nominal. Explicit `timestamps[]` (UTC) accompany the values; empty hourly buckets are **omitted, not imputed** (honours the team irregular-cadence contract — see `../../docs/sparse_data_problem.md`). |

#### 📄 Text — flood-category definitions + NWS impact statements
| | |
|---|---|
| **What** | A factual event-framing line (crest ft, date, category reached, the gauge's defined flood stages) + the official NWS **impact statement(s)** whose stage falls in `[crest − 2 ft, crest]`. |
| **Source** | NWPS gauge object `flood.categories` + `flood.impacts[]`. |
| **`text_quality`** | `"real"` — the substantive content is official NWS impact text. ⚠️ **but** the one-sentence event framing (crest value/date/category) is derived from the series (see caveat). |

**Record shape** (real — Ohio River at Cincinnati, April 2025; arrays abbreviated):
```json
{
  "text": "Ohio River at Cincinnati (OH) crested at 60.94 ft on April 7, 2025, reaching moderate flood stage. Defined flood stages here: action flood stage 40 ft, minor flood stage 52 ft, moderate flood stage 56 ft, major flood stage 65 ft.\n\nNational Weather Service flood-impact statements for this location:\n- At 59 ft: Some of Route 52 is flooded from Cincinnati to New Richmond...\n- At 60 ft: Significant flooding in East End, California and New Richmond...\n\nHourly river stage (gage height, ft, aligned to the NWS gauge datum) over the 20-day window around the crest at Ohio River at Cincinnati (OH): <ts></ts>",
  "timeseries": [{"values": [27.86, "...", 60.94, "..."], "unit": "stage_ft", "freq": "1h"}],
  "timestamps": ["2025-03-28T21:00:00Z", "..."],
  "task_type": "world_knowledge", "text_quality": "real",
  "gauge_lid": "CCNO1", "usgs_site": "03255000", "gauge_name": "Ohio River at Cincinnati", "state": "OH",
  "flood_stages": {"action": 40.0, "minor": 52.0, "moderate": 56.0, "major": 65.0},
  "crest_ft": 60.94, "crest_time": "2025-04-07T21:00:00Z", "category_reached": "moderate",
  "datum_offset_ft": 0.03, "window_hours": 481,
  "dataset": "noaa_nwps_flood", "license": "Public domain (U.S. Government — NOAA/NWS + USGS)",
  "series_id": "nwps_CCNO1_20250407T21"
}
```

**Scale / harvest** (measured from the NWPS gauge list):
- **12,756** total NWPS gauges. Sampling 60 of them: **~53% carry ≥1 impact statement**, **~21% (≈2,760 gauges) have ≥5** ("rich"). So the text-bearing universe is **~2.7k–6.8k gauges**.
- Each rich gauge floods **repeatedly** over its ~15-year sub-daily record → multiple event records. CCNO1 alone yields ~10 events. Full seed / national harvest is on the order of **~10k–40k event records** under this Option-B design.
- The demo runs a **verified seed of 9 rich-impact gauges** (config `gauges`). Full national enumeration is the documented scale-up.

**Key issues / caveats:**
- **⚠️ Alignment tier — "describes" via threshold semantics, not value-reciting.** Unlike the Fed/EIA sources (where the prose recites the exact series value), here the series *reaches* a crest stage and the text says what that stage **means** (its flood category + real-world impacts at that level). Stronger than co-location, weaker than value-reciting. **Flag for Charon: is this alignment tier in-scope?**
- **⚠️ `text_quality` is a hybrid.** The descriptive content (category definitions + impact statements) is official NWS text (real). The single event-framing sentence (crest ft/date/category) is **templated from the series**. Tagged `"real"` because the substance is NWS text; documented here for honesty.
- **⚠️ Irregular cadence.** River data is sub-daily but gappy (sensor outages). We resample to an hourly grid and **omit empty buckets** (no imputation), carrying explicit `timestamps[]`. Ties directly to the team sparse-data thread (`../../docs/sparse_data_problem.md`, flavour B).
- **⚠️ Datum offset.** NWPS "stage" and USGS "gage height" share a datum for most river gauges (offset < 0.1 ft here) but not universally; we compute and store a per-gauge `datum_offset_ft`. Gauges with no NWPS/USGS overlap default to offset 0 (documented).
- **⚠️ Coverage / enumeration.** The NWPS `/gauges` list endpoint is flaky (intermittent 504s) and its bbox/wfo filters are ignored server-side; the full list (~13 MB, 12,756 gauges) *is* fetchable on retry but carries no impact fields — impact coverage requires per-gauge detail. The demo uses a verified seed; full enumeration = fetch the list once, then per-gauge detail to filter to impact-bearing gauges.

**Run:**
```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full seed run
```

**Output:** `output/noaa_nwps_flood_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl` = first 3. (`.cache/` git-ignored.)

**Sources:** [NWPS API](https://api.water.noaa.gov/nwps/v1/docs/) (flood categories + impacts) · [USGS NWIS](https://waterservices.usgs.gov/) (gage height) — both **U.S. public domain**. See [NOTION_PAGE.md](NOTION_PAGE.md).
