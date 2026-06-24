# CPT World-Knowledge Datasets

Per-dataset packages for instruction-free continued pre-training: natural text with `<ts></ts>` + aligned time series.

**Demo outputs** are in each folder's `output/` (typically 50 records, capped locally). Full runs pending shared storage.

| # | Dataset | Status | ~Full scale |
|---|---------|--------|-------------|
| 01 | [NOAA Storm Events](./01_noaa_storm_events/) | Complete | ~10k/year (2010+) |
| 02 | [NHC HURDAT2](./02_nhc_hurdat2/) | Complete | ~320 storms (2000–23, w/ text) |
| 04 | [TelecomTS](./04_telecom_ts/) | In progress | ~1.3k samples (small) |
| 05 | [FNSPID](./05_fnspid/) | In progress | ~2–4M (after dedup) |
| 07 | [CDC FluView](./07_cdc_fluview/) | Partial (~313/558 wks) | ~500 weeks (2015+) |

Each README follows the same layout: what it is → scale → record shape → key issues → how to run.

See [AGENT_BRIEF.md](./AGENT_BRIEF.md) for adding new datasets.
