# Agent brief: add a dataset folder

Use this when the triage chat has assessed a new row from `defu_30_registry.csv` and you need to materialize a team-lead-ready package.

## Inputs you should receive

From the triage assessment (example: `reports/01_probe.json`):

- Rank, dataset name, domain, priority
- `verdict`: `ready_to_use` | `needs_pairing` | `not_good_to_use` | `blocked`
- TS source URL, text source, acquisition type, pairing method
- Confidence, effort estimate, probe notes, caveats
- Optional hand-authored sample JSON

## Deliverables (required)

1. **`datasets/NN_<slug>/README.md`** — polished, English, for the Chinese team lead:
   - What the dataset is (1 short paragraph)
   - Assessment summary table (verdict, confidence, effort)
   - Sources (TS + text) with links
   - How pairing works (plain language)
   - Caveats / leakage / license notes
   - Quick start (install, copy config, run script)
   - Example output JSON (embedded or linked)

2. **`assessment.json`** — copy structured probe metadata from triage

3. **`config.example.yaml`** — **maximize configurability**:
   - Windowing: `history_length`, `forecast_horizon`, `step`
   - Data filters: date range, geography, event types, max rows
   - Aggregation: metric, frequency
   - Text: which fields, max narratives, char limits, dedupe
   - Prompt templates (instruction / input) as format strings
   - Output: path, max_samples, indent
   - Every knob should also be overridable via CLI (`--set key=value` or individual flags)

4. **`scripts/build_alpaca_json.py`**:
   - Loads YAML config; CLI overrides config values
   - Runs on **small sample by default** (demo for team lead)
   - Writes Alpaca JSON + `output/run_report.json` (counts, validation, config snapshot)
   - No API keys; deterministic only
   - Document all config keys in `--help` and README

5. **`samples/example_output.json`** — at least one realistic record

6. Update **`datasets/README.md`** index table

## Do not

- Fully automate all 30 datasets identically — each source differs
- Commit multi-GB downloads (use `output/.gitkeep`, document download URLs)
- Hardcode lookback/horizon without config + CLI override

## Slug naming

`NN_<lowercase_underscore_slug>` e.g. `01_noaa_storm_events`, `05_fnspid`, `28_ercot_notices`
