# Multimodal TS + Text Dataset Curation

This repo holds **per-dataset CPT packages** for the lab's world-knowledge corpus. Each subfolder corresponds to one row from Defu's 30-dataset registry.

**Demo outputs** live in each package's `output/` folder (typically 50 records, `max_records` capped locally). Full-scale runs pending shared storage access.

## Target JSON format

Conversion scripts produce **CPT-format JSONL** — natural text with a `<ts></ts>` placeholder and aligned `timeseries` arrays (not Alpaca instruction/input/output):

```json
{
  "text": "Natural prose describing the phenomenon... <ts></ts>",
  "timeseries": [
    {"values": [1.2, 3.4, 5.6], "unit": "metric_name", "freq": "daily"}
  ],
  "task_type": "world_knowledge",
  "text_source": "dataset_description",
  "text_quality": "real"
}
```

## Folder convention

```
datasets/
  NN_<slug>/
    README.md              # Team-lead-facing summary (dataset, verdict, caveats, usage)
    assessment.json        # Structured probe / triage metadata
    config.example.yaml    # Default config — copy to config.yaml and edit
    requirements.txt       # Script dependencies (if any beyond repo venv)
    scripts/
      build_cpt_jsonl.py   # Configurable CPT export script (small-sample demo by default)
    output/                # Script-generated JSONL + run reports (committed for demos)
```

## Datasets

| Rank | Folder | CPT status | Notes |
|------|--------|------------|-------|
| 1 | [01_noaa_storm_events](./01_noaa_storm_events/) | **Complete** | Demo output in `output/` |
| 2 | [02_nhc_hurdat2](./02_nhc_hurdat2/) | **Complete** | Demo output in `output/` |
| 4 | [04_telecom_ts](./04_telecom_ts/) | **In progress** | Awaiting team review before scale-up |
| 5 | [05_fnspid](./05_fnspid/) | **In progress** | Local 2-ticker demo only |
| 7 | [07_cdc_fluview](./07_cdc_fluview/) | **In progress** | Single season demo only |

## For agents / contributors

See [AGENT_BRIEF.md](./AGENT_BRIEF.md) when adding a new dataset folder.
