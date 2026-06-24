# Agent brief: CPT world-knowledge datasets

*Read this when switching chats or onboarding a new agent on this repo.*

---

## What we are building

**Continued Pre-Training (CPT) world-knowledge data** — instruction-free records that pair **real official text** with **aligned time series** so a multimodal model learns domain facts from both modalities.

This is **not** SFT. Do not produce Alpaca `instruction` / `input` / `output` triplets unless explicitly asked. The SFT corpus lives elsewhere (~2M rows); check overlap before adding a source (see `PROJECT_CONTEXT.md`).

**Charon's core rule:** natural, source-native alignment — the text must genuinely describe the same phenomenon the numbers represent. No arbitrary sliding-window inflation, no metadata-only rows dressed up as language grounding.

---

## Target record shape

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
| 04 | `04_telecom_ts/` | In progress | ~1.3k samples | `build_cpt_jsonl.py` — **blocked**: GPT-generated anomaly text pending team approval |
| 05 | `05_fnspid/` | In progress | ~2–4M after dedup | `build_cpt_jsonl.py` — demo only; full HF pipeline not built |
| 07 | `07_cdc_fluview/` | **Partial** | 313 / 558 weeks emitted | `build_cpt_jsonl.py` |

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
├── samples/
│   └── example_output.jsonl  # 1–3 pretty-printed records (optional)
└── .cache/                   # gitignored — downloads, HTML cache, etc.
```

**`.gitignore`** (repo level): `**/.cache/`, `**/__pycache__/`, `**/samples/` (optional — samples may or may not be committed).

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

### CDC FluView (`07_cdc_fluview/`) — most recent focus

- **CSV:** historical national weeks in `data/raw_csv/` (ILINet + two NREVSS files, 2015–16 → 2025–26).
- **Text:** scraped from live CDC weekly HTML (legacy `weeklyarchives{season}/weekXX.htm` vs modern `fluview/surveillance/{year}-week-XX.html`).
- **Output:** 313 records; 175 weeks skipped (no HTML on live CDC); 70 skipped (extractor found <200 chars).
- **Gaps:** 2020–21 (0 records — pages off live CDC), 2021–22 / 2022–23 mostly missing. `archive.cdc.gov` was tried and **not** wired in (bot blocking / incomplete); left as documented gap.
- **Open for Charon:** single timestep per week (natural unit) vs multi-week windows.

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

1. Check `defu_30_registry.csv` and SFT overlap in `PROJECT_CONTEXT.md`.
2. Create `datasets/NN_<slug>/` following layout above.
3. Use `01_noaa_storm_events/` or `07_cdc_fluview/` as reference (both use `build_cpt_jsonl.py`).
4. Update `datasets/README.md` index table.
5. Optional: save probe metadata in `reports/NN_probe.json`.

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
