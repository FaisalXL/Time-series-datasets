# TelecomTS → CPT

> **Status: In progress** — do not scale until team approves.

**What it is:** 5G lab testbed KPI windows (128 timesteps @ 100ms) paired with scenario descriptions. One record = one **128-step sample** (normal traffic or injected anomaly).

**Scale:** HF dataset has **~1,260 train samples** in current split. Full corpus is small — not a large-scale source.

#### 📄 Text — scenario description
| | |
|---|---|
| **What** | A short description of the 128-step window — normal-traffic summary, or a troubleshooting narrative for an injected anomaly |
| **Source** | [`AliMaatouk/TelecomTS`](https://huggingface.co/datasets/AliMaatouk/TelecomTS) (HF dataset) |
| **Format** | Text field per sample in the HF parquet/JSON split |
| **`text_quality`** | `"real"` for normal-window descriptions · **`"generated"` for anomaly text (GPT-4) ⚠️ pending team sign-off** · `text_source` tags origin |

#### 📈 Time series — 5G KPI window
| | |
|---|---|
| **What** | 5 radio/throughput KPIs from a 5G lab testbed (one base station) |
| **Source** | Same HF dataset — sensor logs sampled at 100 ms |
| **Cadence** | `100ms`, **fixed 128-step window** (source-imposed, not arbitrary sliding) |

| Channel (`unit`) | Meaning |
|---|---|
| `DL_Throughput_Mbps` | Downlink throughput (Mbps) |
| `UL_Throughput_Mbps` | Uplink throughput (Mbps) |
| `DL_BLER_pct` | Downlink block error rate (%) |
| `UL_BLER_pct` | Uplink block error rate (%) |
| `RSRP_dBm` | Reference signal received power (dBm) |

**Record shape:**
```json
{
  "text": "RSRP was consistently strong at -73 dBm. UL_MCS dropped to 9 during the window... KPI observations: <ts></ts>.",
  "timeseries": [
    {"values": [0.39, 0.51, ...], "unit": "DL_Throughput_Mbps", "freq": "100ms"},
    {"values": [35.5, 34.9, ...], "unit": "UL_Throughput_Mbps", "freq": "100ms"},
    {"values": [0.001, 0.0, ...], "unit": "DL_BLER_pct", "freq": "100ms"},
    {"values": [8.6, 8.7, ...], "unit": "UL_BLER_pct", "freq": "100ms"},
    {"values": [-73, -73, ...], "unit": "RSRP_dBm", "freq": "100ms"}
  ],
  "record_type": "anomaly", "task_type": "world_knowledge", "text_quality": "generated"
}
```

**Key issues:**
- **Lab data, not production network** — university testbed with one base station.
- **Anomaly troubleshooting text is GPT-4 generated** — tagged `text_quality: "generated"`. Conflicts with first-party-text rule for backbone CPT.
- **Most anomalies are synthetically injected** — only jamming used a real physical jammer.
- Fixed 128-step window is source-imposed (not arbitrary sliding).

**Run:** `pip install -r requirements.txt && python scripts/build_cpt_jsonl.py`

**Output:** `output/telecom_ts_cpt.jsonl` · **Source:** [AliMaatouk/TelecomTS](https://huggingface.co/datasets/AliMaatouk/TelecomTS)
