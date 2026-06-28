# CPT World-Knowledge Datasets

Per-dataset packages for instruction-free continued pre-training: natural text with a single `<ts></ts>` placeholder + aligned time series.

**Demo / dev outputs** live in each folder's `output/` (typically 50 records, capped locally). Full runs pending shared storage.

## Dev Set (for review)

Small-scale samples for format inspection and freeze. The five datasets below are the current dev set — open each folder for its README and `output/*.jsonl`:

| # | Dataset | Dev samples | Channels | `freq` | Notes |
|---|---------|------------:|---------:|--------|-------|
| 01 | [NOAA Storm Events](./01_noaa_storm_events/) | 50 | 3 | `1d` | state-month unit → 28–31 daily steps |
| 02 | [NHC HURDAT2](./02_nhc_hurdat2/) | 50 | 5 | `6h` | per-storm track, 8–70 steps |
| 04 | [TelecomTS](./04_telecom_ts/) | 50 | 5 | `100ms` | 128-step window · ⚠️ anomaly text GPT-generated, pending sign-off |
| 06 | [StockNet](./06_stocknet/) | 50 | 5 | `1d` | weekly OHLCV (~5 steps) · tweets are third-party text (`"real"`) — confirm tag |
| 07 | [CDC FluView](./07_cdc_fluview/) | 358 | 15 | `1w` | season-to-date window (1–52 wks, median ~22) |

## Record format (frozen for dev set)

Every line of `output/*_cpt.jsonl` is one JSON object with these required fields:

| Field | Rule |
|-------|------|
| `text` | Natural prose; contains **exactly one** `<ts></ts>`. |
| `timeseries` | List of channels, each `{values, unit, freq}`. |
| `task_type` | Always `"world_knowledge"`. |
| `text_quality` | `"real"` for first-party/official text; `"generated"` for tagged synthetic text. |

**`freq` convention — compact lowercase:** `100ms`, `6h`, `1d`, `1w` (not `daily`/`weekly`). Dataset-specific extras (`geography`, `season`, `report_url`, …) are allowed after the required fields.

## All packages

| # | Dataset | Status | ~Full scale |
|---|---------|--------|-------------|
| 01 | [NOAA Storm Events](./01_noaa_storm_events/) | Complete | ~10k/year (2010+) |
| 02 | [NHC HURDAT2](./02_nhc_hurdat2/) | Complete | ~320 storms (2000–23, w/ text) |
| 04 | [TelecomTS](./04_telecom_ts/) | Demo done | ~1.3k records (small dataset) |
| 05 | [FNSPID](./05_fnspid/) | In progress | ~2–4M (after dedup); full HF pipeline not yet built |
| 06 | [StockNet](./06_stocknet/) | Demo done | ~29k records (87 tickers × ~2 yrs) |
| 07 | [CDC FluView](./07_cdc_fluview/) | Complete (358/558 wks) | season-to-date TS window; CDC removed 2020–21 archive pages |
| 24 | [NOAA SWPC Space Weather](./24_noaa_swpc/) | Demo done | ~10,800 daily (1996–2026) + ~1,500 weekly (1997–2026) |

Each README follows the same layout: what it is → scale → record shape → key issues → how to run.

See [AGENT_BRIEF.md](./AGENT_BRIEF.md) for adding new datasets.
