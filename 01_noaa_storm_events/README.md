# NOAA Storm Events → CPT

> **Status: Complete** (demo: 50 records). Full export pending server access.

**What it is:** Official NOAA severe-weather reports. One record = one `(state, calendar-month)` — that month's forecaster narratives for the state + **daily** injury / damage / event-count series spanning the whole month.

**Scale:** ~**700 state-months/year** with narrative (2023: 695). **2010+** recommended (~100% episode narrative). Narrative text essentially **absent before ~1996**. *(An `episode`-grouping mode is also available via `data.grouping: episode` for per-episode records.)*

#### 📄 Text — NCEI episode narrative
| | |
|---|---|
| **What** | Forecaster-written episode/event narrative describing the storm and its impacts |
| **Source** | NCEI Storm Events Database — the `EPISODE_NARRATIVE` / `EVENT_NARRATIVE` free-text fields |
| **Where** | Bulk CSV files at [ncei.noaa.gov/stormevents](https://www.ncei.noaa.gov/stormevents/) (`StormEvents_details-*.csv.gz`) |
| **Format** | Plain-text fields inside the details CSV; all of a state-month's distinct narratives concatenated (capped at `month_narrative_char_limit`) |
| **`text_quality`** | `"real"` (official NWS/forecaster text) |

#### 📈 Time series — daily impact metrics
| | |
|---|---|
| **What** | 3 channels aggregated per day across the whole calendar month |
| **Source** | Same NCEI details CSV — numeric fields (`INJURIES_*`, `DAMAGE_*`), counted/summed by date |
| **Cadence** | `1d`, **28–31 steps** (full month; quiet days are genuine zeros) |

| Channel (`unit`) | Meaning |
|---|---|
| `injuries/day` | Direct + indirect injuries reported that day |
| `USD/day` | Property + crop damage (USD) that day |
| `events/day` | Count of event rows logged that day |

> **Note:** text and TS are drawn from the *same* NCEI database — the human narrative fields vs. the structured numeric fields of the same state-month. Natural source-native alignment, not a cross-source join.

**Record shape** (real record — North Dakota, Oct 2023, 7 episodes / 84 events; arrays abbreviated).
The `<ts></ts>` tag is appended directly to the real scraped narrative — **no framing/bridging
sentence is generated**; every word before the tag is verbatim NCEI prose:
```json
{
  "text": "In late October, a winter storm dumped heavy snow in eastern North Dakota and northwestern Minnesota over a period of 2 days...Public reports 10 inches of storm total snowfall 1 W Petersburg.\n\n<ts></ts>",
  "timeseries": [
    {"values": [0, 0, "...", 0], "unit": "injuries/day", "freq": "1d"},
    {"values": [0, 0, "...", 0], "unit": "USD/day", "freq": "1d"},
    {"values": [7, 0, 3, 0, "...", 18, "...", 30, 22, 0, 0, 0, 4, 0], "unit": "events/day", "freq": "1d"}
  ],
  "task_type": "world_knowledge", "text_quality": "real", "alignment": "describes",
  "license": "public-domain-us-gov", "text_source": "first_party_official",
  "dataset": "noaa_storm_events", "domain": "meteorology", "region": "US",
  "series_id": "NORTH_DAKOTA_2023-10", "period_start": "2023-10-01", "period_end": "2023-10-31",
  "source": "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/StormEvents_details-ftp_v1.0_d2023_c20260323.csv.gz",
  "meta": {"month": "2023-10", "geography": "NORTH DAKOTA",
           "event_types": ["Drought", "Hail", "Heavy Snow", "High Wind", "Winter Weather"],
           "n_episodes": 7, "n_events": 84,
           "date_range": ["2023-10-01", "2023-10-31"]}
}
```

**Key issues:**
- **No generated text.** An earlier version of this build appended a templated closing sentence to
  introduce the series before `<ts></ts>`. That sentence was not from NOAA — it was synthesized by
  the build script. Fixed 2026-07-24: `<ts></ts>` is now appended directly to the real scraped
  narrative with nothing generated in between.
- **State-month unit** gives genuine multi-step daily series (28–31 steps). Trade-off: text is several episode narratives concatenated, so alignment is "the month's storms → daily totals" rather than one narrative to one event.
- **Quiet days are zeros** — a month with sparse activity has many zero days; the narrative only describes the active days. This is real, not padding.
- Event counts = report rows, not unique physical storms.

**Run:** `pip install -r requirements.txt && python scripts/build_cpt_jsonl.py`

**Output:** `output/noaa_storm_events_cpt.jsonl` · **Source:** [NCEI Storm Events](https://www.ncei.noaa.gov/stormevents/)
