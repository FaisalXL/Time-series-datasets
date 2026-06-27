# NOAA Storm Events → CPT

> **Status: Complete** (demo: 50 records). Full export pending server access.

**What it is:** Official NOAA severe-weather reports. One record = one `(episode, state)` — forecaster narrative text + daily injury / damage / event-count series over the same dates.

**Scale:** ~**10k records/year** with narrative (2023 US total). **2010+** recommended (~100% episode narrative). Narrative text essentially **absent before ~1996**.

#### 📄 Text — NCEI episode narrative
| | |
|---|---|
| **What** | Forecaster-written episode/event narrative describing the storm and its impacts |
| **Source** | NCEI Storm Events Database — the `EPISODE_NARRATIVE` / `EVENT_NARRATIVE` free-text fields |
| **Where** | Bulk CSV files at [ncei.noaa.gov/stormevents](https://www.ncei.noaa.gov/stormevents/) (`StormEvents_details-*.csv.gz`) |
| **Format** | Plain-text fields inside the details CSV; concatenated per `(episode, state)` |
| **`text_quality`** | `"real"` (official NWS/forecaster text) |

#### 📈 Time series — daily impact metrics
| | |
|---|---|
| **What** | 3 channels aggregated per day from the event rows of the same episode |
| **Source** | Same NCEI details CSV — numeric fields (`INJURIES_*`, `DAMAGE_*`), counted/summed by date |
| **Cadence** | `1d`, variable length (one episode = 1 to N days; 86% are single-day) |

| Channel (`unit`) | Meaning |
|---|---|
| `injuries/day` | Direct + indirect injuries reported that day |
| `USD/day` | Property + crop damage (USD) that day |
| `events/day` | Count of event rows logged that day |

> **Note:** text and TS are drawn from the *same* NCEI database — the human narrative field vs. the structured numeric fields of the same episode. Natural source-native alignment, not a cross-source join.

**Record shape:**
```json
{
  "text": "North Dakota winter storm, Oct 2023. Heavy snow and blowing snow... Daily impact metrics: <ts></ts>.",
  "timeseries": [
    {"values": [0], "unit": "injuries/day", "freq": "1d"},
    {"values": [0], "unit": "USD/day", "freq": "1d"},
    {"values": [19], "unit": "events/day", "freq": "1d"}
  ],
  "episode_date_range": ["2023-10-25", "2023-10-25"],
  "geography": "NORTH DAKOTA",
  "task_type": "world_knowledge",
  "text_quality": "real"
}
```

**Key issues:**
- **86% of episodes are single-day** — time series length is often 1, not a multi-day trend.
- **Injuries/damage usually zero** — only `events/day` carries signal for most records.
- Event counts = report rows, not unique physical storms.

**Run:** `pip install -r requirements.txt && python scripts/build_cpt_jsonl.py`

**Output:** `output/noaa_storm_events_cpt.jsonl` · **Source:** [NCEI Storm Events](https://www.ncei.noaa.gov/stormevents/)
