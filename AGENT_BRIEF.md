# Agent brief: CPT world-knowledge datasets

*Last updated: Jun 26 2026. Re-read this when switching chats or onboarding a new agent.*

---

## What we are building

**Continued Pre-Training (CPT) world-knowledge data** — instruction-free records that pair **real official text** with **aligned time series** so a multimodal model learns domain facts from both modalities.

This is **not** SFT. Do not produce Alpaca `instruction` / `input` / `output` triplets unless explicitly asked. The SFT corpus lives elsewhere (~2M rows); check overlap before adding a source (see `PROJECT_CONTEXT.md`).

**Charon's core rule:** natural, source-native alignment — the text must genuinely describe the same phenomenon the numbers represent. No arbitrary sliding-window inflation, no metadata-only rows dressed up as language grounding.

---

## Target record shape

> **Finalized spec: [`schema/SCHEMA.md`](./schema/SCHEMA.md)** — field definitions, qualification rules, alignment taxonomy, license enum, and the `validate.py` gate. The summary below is unchanged but the schema folder is authoritative.

Each line in `output/*_cpt.jsonl` is one JSON object:

```json
{
  "text": "…official narrative prose… exactly one <ts></ts> placeholder…",
  "timeseries": [
    {"values": [1.61], "unit": "ili_pct_weighted", "freq": "1w"},
    {"values": [41531], "unit": "ili_total_visits", "freq": "1w"}
  ],
  "task_type": "world_knowledge",
  "text_quality": "real"
}
```

**Required conventions:**

| Field | Rule |
|-------|------|
| `text` | Natural prose only — no task instructions. Contains **exactly one** `<ts></ts>`. |
| `timeseries` | List of channels; each has `values`, `unit`, `freq` (`daily`, `6h`, `1w`, etc.). |
| `task_type` | Always `"world_knowledge"`. |
| `text_quality` | `"real"` for first-party human/official text. Use `"generated"` only for tagged synthetic text (e.g. TelecomTS anomaly descriptions — team review required). |
| Extra metadata | Dataset-specific keys OK (`season`, `week`, `geography`, `episode_date_range`, `report_url`, …). |

The `<ts></ts>` token marks where the model should consume the paired series during CPT — typically in a short closing sentence, not mid-paragraph.

---

## Current dataset status

Git repo root: `datasets/` (not the parent workspace). Demo/full outputs live in each folder's `output/`.

| # | Folder | Status | ~Scale | Script |
|---|--------|--------|--------|--------|
| 01 | `01_noaa_storm_events/` | **Complete** | ~10k/year (2010+) | `build_cpt_jsonl.py` |
| 02 | `02_nhc_hurdat2/` | **Complete** | ~320 storms (2000–23 w/ text) | `build_cpt_jsonl.py` |
| 04 | `04_telecom_ts/` | **Demo done** | ~1.3k full records | `build_cpt_jsonl.py` — **do not scale**: GPT-generated anomaly text pending team approval |
| 05 | `05_fnspid/` | In progress | ~2–4M after dedup | `build_cpt_jsonl.py` — demo only; full HF pipeline not built |
| 06 | `06_stocknet/` | **Demo done** | ~29k (87 tickers × ~2 yrs) | `build_cpt_jsonl.py` — full run pending server |
| 07 | `07_cdc_fluview/` | **Complete** | 313 / 558 weeks (real ceiling) | `build_cpt_jsonl.py` — 2020–21 gap: CDC removed archive pages |
| 24 | `24_noaa_swpc/` | **Demo done** | ~10,800 daily + ~1,500 weekly | `build_daily_cpt.py` + `build_weekly_cpt.py` — full run pending server |

Index: [`datasets/README.md`](./README.md). Broader project notes: [`../PROJECT_CONTEXT.md`](../PROJECT_CONTEXT.md) (may lag — trust per-dataset READMEs for status).

---

## Standard package layout

```
datasets/NN_<slug>/
├── README.md                 # Status banner, scale, record shape, key issues, run cmd
├── config.example.yaml       # All knobs; overridable via --set key=value
├── requirements.txt
├── scripts/
│   └── build_cpt_jsonl.py    # NOT build_alpaca_json.py
├── data/                     # Raw inputs when not downloaded by script (optional)
├── output/
│   ├── *_cpt.jsonl           # Main deliverable
│   └── run_report.json       # Counts, skips, config snapshot
└── .cache/                   # gitignored — downloads, HTML cache, etc.
```

**`.gitignore`** (repo level): `**/.cache/`, `**/__pycache__/`.

---

## What each build script must do

1. **Load YAML config** — support `--config` and `--set dotted.key=value` overrides.
2. **Pair text + TS at source-native granularity** — episode, storm, week, filing, etc. Prefer the natural unit over fixed sliding windows.
3. **Validate records** — exactly one `<ts></ts>`, expected channel count, non-empty text above `min_text_chars` when configured.
4. **Write JSONL** + **`run_report.json`** with:
   - records emitted / skipped (and why: no text, no TS, short text, …)
   - config snapshot
5. **Demo by default** where useful — `max_records: 50` in config; set `null` for full run.
6. **No API keys** for core path; deterministic where possible.
7. **Document** config keys in README and `--help`.

---

## README template (keep ~25–35 lines)

Every dataset README should start with a **status banner**, then:

1. **What it is** — one record = one what?
2. **Scale** — full-build estimate vs current output count
3. **Record shape** — minimal JSON example
4. **Key issues** — leakage, single-timestep records, missing archives, generated text, open team questions
5. **Run** — install + one command
6. **Output path** + source links

---

## Charon rules (apply to every dataset)

1. **Real-world only** — no semi-synthesized corpora (e.g. CAF-7M is out).
2. **No SFT overlap** — check SFT Notion page before building.
3. **Variable / source-native windows** — shape windows to report structure, not arbitrary stride-1 sliding unless the source imposes fixed epochs.
4. **No fake scale** — don't repeat the same template across inflated windows.
5. **First-party text for backbone** — official reports, advisories, filings. Generated text only with `text_quality: "generated"` and team sign-off.

---

## Active work & known blockers

### NOAA SWPC (`24_noaa_swpc/`) — most recently completed

- **Daily build** (`build_daily_cpt.py`): SGAS text + DGD (K/A-indices) + DSD (solar flux, sunspots, flares). 18 TS channels. Demo: 50 records (year 2000). Full: ~10,800 records (1996–2026).
- **Weekly build** (`build_weekly_cpt.py`): PRF PDF Highlights section + 15-channel TS from embedded tables. Uses `pymupdf` (fitz). Demo: 5 records. Full: ~1,500 records (1997–2026).
- **Quiet-day records kept intentionally** — contrast between quiet (Kp 0–3) and active days is the learning signal.
- Full runs pending server allocation.

### FNSPID (`05_fnspid/`)

- Demo from local Alpaca JSON at `../../FNSPID/` (outside git).
- Full HuggingFace pipeline not built. Open: temporal alignment (news day D vs prices D-7..D-1), ticker noise, dedup.

### TelecomTS (`04_telecom_ts/`)

- Anomaly narrative text is GPT-generated — **do not scale** until team approves.

### Infrastructure

- Full-scale runs across datasets pending **shared storage server** access from Charon.
- User pushes commits manually; do not commit unless asked.

---

## Adding a new dataset

Schema v1 is finalized in [`schema/SCHEMA.md`](./schema/SCHEMA.md) (+ `schema/cpt_record.schema.json` and `schema/validate.py`). **Existing packages need no rework** — they pass the required contract as-is (verified 1786/1786); the standardized optional vocab is a migration item, not a freeze blocker. **New packages must follow the full v1 spec and pass `--strict`.**

### Before building — qualification checklist (SCHEMA.md §8)

1. **Real first-party text** — official reports, advisories, filings. Third-party commentary only as a tagged minority (`text_source: third_party`); generated text only with team sign-off (`text_quality: generated`).
2. **Native alignment** — text and series describe the *same* phenomenon at the *source's own* granularity. Classify it up front: `recites` / `describes` / `contextualizes`.
3. **License is trainable** — public domain or permissive; otherwise flag `proprietary-review` and quarantine.
4. **No SFT overlap** — check `defu_30_registry.csv` and the SFT table in `PROJECT_CONTEXT.md`.
5. **Honest scale** — full-build estimate from real distinct source units, no sliding-window or template inflation.

If a source fails 1–4 it is **out**; if it fails 5 it is **not ready**.

### Building

1. Create `datasets/NN_<slug>/` following the standard layout above; use `01_noaa_storm_events/` or `07_cdc_fluview/` as reference.
2. Emit records with the **v1 optional fields from day one**: `series_id`, URL-form `source`, standard `license` enum, `text_source`, `alignment`, `period_start`/`period_end`. Nest dataset-specific extras under `meta` (do not add new top-level keys).
3. Update the `datasets/README.md` index table.
4. Optional: save probe metadata in `reports/NN_probe.json`.

### After building — validate (required gate for new packages)

```bash
python3 schema/validate.py NN_<slug>/output/ --strict
```

Must pass with **zero errors and zero warnings**. Legacy packages are validated with the default (lenient) mode only:

```bash
python3 schema/validate.py .        # whole repo, required contract only
```

**Slug naming:** `NN_<lowercase_underscore_slug>` — e.g. `08_bls_cpi`, `28_ercot_notices`.

---

## Do not

- Revert CPT packages to Alpaca SFT format without explicit instruction.
- Commit multi-GB raw downloads (document URLs; use `.cache/`).
- Inflate record counts with synthetic sliding windows or duplicate boilerplate text.
- Force-commit or push — user controls git.
- Scale `04_telecom_ts` until generated-text policy is resolved.

---

## Quick commands

```bash
cd datasets/07_cdc_fluview
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --config config.example.yaml
python scripts/build_cpt_jsonl.py --set output.max_records=10   # smoke test
```

---

## Related files outside `datasets/`

| Path | Purpose |
|------|---------|
| `../PROJECT_CONTEXT.md` | Lab background, Defu's 30 list, SFT overlap table |
| `../defu_30_registry.csv` | Dataset shortlist |
| `../FNSPID/` | Raw FNSPID data + Colab probe notebook |
| `../From_News_to_Forecast/` | Reference impl (Alpaca — **not** CPT target format) |
