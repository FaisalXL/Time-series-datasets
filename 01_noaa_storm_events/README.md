# NOAA Storm Events → CPT

> **Status: Built full-scale** — **78,618 records, 1996–2026, 78,618/78,618 strict.**

**What it is:** Official NOAA severe-weather reports. One record = one **storm episode**: that
episode's own forecaster narrative + the **trailing 32-day** daily injury / damage / event-count
series for its state, **ending on the episode's last event day**.

**Scale:** **78,618 records** across 31 years (1996–2026), 60 states/territories. ~3.3k/yr in
recent years; 2011 is the peak (3,874).

#### Why the trailing window

The earlier design was one record per `(state, calendar-month)`. It produced genuine 28–31-step
series but had two problems: the text was several unrelated episode narratives concatenated, and
a quiet state-month was ~86% zero days — which is what held this package from finalization.

Anchoring a fixed 32-day window to the **episode's own last event day** (the same pattern as
`11_eia_petroleum_weekly` / `25_noaa_nwps_flood` / `26_ics209_wildfire`) fixes both: the text is
one real narrative about one storm, and the window ends on an active day instead of averaging a
quiet month. Median active days went from ~14% of the window to **47%**, and the episode is the
series' terminal segment in **78,618/78,618 (100%)** — alignment is structural, not incidental.

Other grouping modes remain available: `data.grouping: state_month` (the old unit) and
`episode` (legacy, 1–3-step series).

#### 📄 Text — NCEI episode narrative
| | |
|---|---|
| **What** | Forecaster-written episode/event narrative describing the storm and its impacts |
| **Source** | NCEI Storm Events Database — the `EPISODE_NARRATIVE` / `EVENT_NARRATIVE` free-text fields |
| **Where** | Bulk CSV files at [ncei.noaa.gov/stormevents](https://www.ncei.noaa.gov/stormevents/) (`StormEvents_details-*.csv.gz`) |
| **Format** | Plain-text fields inside the details CSV; **one episode's** narrative, plus up to 3 of its event narratives (capped at `episode_narrative_char_limit`). Median 754 chars. |
| **`text_quality`** | `"real"` (official NWS/forecaster text) |

#### 📈 Time series — daily impact metrics
| | |
|---|---|
| **What** | 3 channels aggregated per day across the trailing 32-day window, over **every** event in that state (not only this episode's) |
| **Source** | Same NCEI details CSV — numeric fields (`INJURIES_*`, `DAMAGE_*`), counted/summed by date |
| **Cadence** | `1d`, **exactly 32 steps, 100% of records**; median 14 active days |

| Channel (`unit`) | Meaning |
|---|---|
| `injuries/day` | Direct + indirect injuries reported that day |
| `USD/day` | Property + crop damage (USD) that day |
| `events/day` | Count of event rows logged that day |

> **Note:** text and TS are drawn from the *same* NCEI database — the human narrative fields vs. the structured numeric fields of the same state. Natural source-native alignment, not a cross-source join.

**Record shape** (real record — Ohio, arctic outbreak ending 1996-02-01; arrays abbreviated).
The `<ts></ts>` tag is appended directly to the real scraped narrative — **no framing/bridging
sentence is generated**; every word before the tag is verbatim NCEI prose:
```json
{
  "text": "Arctic high pressure brought the coldest air of the season to the Ohio Valley. Cincinnati broke its record low on the 4th with a temperature of 11 below zero...\n\n<ts></ts>",
  "timeseries": [
    {"values": [0, 0, "...", 0], "unit": "injuries/day", "freq": "1d"},
    {"values": [0, 0, "...", 1260000], "unit": "USD/day", "freq": "1d"},
    {"values": [0, 47, 0, 5, 1, 37, 11, 0, "...", 19, 0, 5, 0, 0, 36], "unit": "events/day", "freq": "1d"}
  ],
  "task_type": "world_knowledge", "text_quality": "real", "alignment": "describes",
  "license": "public-domain-us-gov", "text_source": "first_party_official",
  "dataset": "noaa_storm_events", "domain": "meteorology", "region": "US",
  "series_id": "2404591_OHIO_1996-02-01",
  "period_start": "1996-01-01", "period_end": "1996-02-01",
  "timestamps": ["1996-01-01", "1996-01-02", "...", "1996-02-01"],
  "source": "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/StormEvents_details-ftp_v1.0_d1996_c20260323.csv.gz",
  "meta": {"geography": "OHIO", "episode_id": "2404591",
           "event_types": ["Cold/Wind Chill"],
           "episode_date_range": ["1996-02-01", "1996-02-01"],
           "episode_n_events": 36, "episode_injuries": 0, "episode_damage_usd": 1260000,
           "episode_share_of_window_events": 0.1417,
           "window_days": 32, "window_active_days": 16,
           "window_n_events": 254, "window_n_episodes": 47,
           "date_range": ["1996-01-01", "1996-02-01"]}
}
```

> The last value of `events/day` (36) is this episode's own event count — the episode is the
> window's terminal segment by construction, in **100%** of records.

**Key issues:**
- **No generated text.** An earlier version appended a templated closing sentence to introduce
  the series before `<ts></ts>`. That sentence was not from NOAA — it was synthesized by the
  build script. Fixed 2026-07-24.
- **Windows overlap, and ~52% of records share a series.** Two episodes ending on the same day
  in the same state produce an identical window (texts still differ — 99.1% of texts are
  distinct). The window *end* is set by the source's own reporting structure, not a stride we
  imposed, which is the line SCHEMA.md §7 draws — but if the leads want every series unique,
  `data.min_days_between_records: 1` gives ~49k records with no shared series.
- **Quiet days are still genuine zeros**, just far fewer of them: `min_active_days_in_window`
  (default 8) drops windows where the state was inactive. Median is 14 active days of 32.
- **Text coverage limits the early years** — episode narratives are ~50–70% of rows for
  1996–2006 and 100% from 2007, so 19,416 records come from 1996–2006 vs 59,202 from 2007+.
- Event counts = report rows, not unique physical storms.
- **URL template rots.** NOAA bumps the `_cYYYYMMDD` compile-date suffix per year; filenames
  are resolved from the NCEI index at run time rather than trusting the template.

**Run:** `pip install -r requirements.txt && python scripts/build_cpt_jsonl.py`

**Output:** `output/noaa_storm_events_cpt.jsonl` · **Source:** [NCEI Storm Events](https://www.ncei.noaa.gov/stormevents/)
