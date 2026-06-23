# NOAA Storm Events → CPT

> **Status: Complete** (demo: 50 records). Full export pending server access.

**What it is:** Official NOAA severe-weather reports. One record = one `(episode, state)` — forecaster narrative text + daily injury / damage / event-count series over the same dates.

**Scale:** ~**10k records/year** with narrative (2023 US total). **2010+** recommended (~100% episode narrative). Narrative text essentially **absent before ~1996**.

**Record shape:**
```json
{
  "text": "North Dakota winter storm, Oct 2023. Heavy snow and blowing snow... Daily impact metrics: <ts></ts>.",
  "timeseries": [
    {"values": [0], "unit": "injuries/day", "freq": "daily"},
    {"values": [0], "unit": "USD/day", "freq": "daily"},
    {"values": [19], "unit": "events/day", "freq": "daily"}
  ],
  "episode_date_range": ["2023-10-25", "2023-10-25"],
  "geography": "NORTH DAKOTA",
  "task_type": "world_knowledge"
}
```

**Key issues:**
- **86% of episodes are single-day** — time series length is often 1, not a multi-day trend.
- **Injuries/damage usually zero** — only `events/day` carries signal for most records.
- Event counts = report rows, not unique physical storms.

**Run:** `pip install -r requirements.txt && python scripts/build_cpt_jsonl.py`

**Output:** `output/noaa_storm_events_cpt.jsonl` · **Source:** [NCEI Storm Events](https://www.ncei.noaa.gov/stormevents/)
