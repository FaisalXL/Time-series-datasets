# NOAA Storm Events → CPT World-Knowledge JSONL

> **Status: Complete** — pipeline and demo output ready. Full multi-year export pending server access (`max_records: 50` locally).

Official U.S. severe-weather episodes paired with daily impact time series and forecaster narratives for continued pre-training (CPT).

---

## What this dataset is

The [NOAA NCEI Storm Events Database](https://www.ncei.noaa.gov/stormevents/) is the authoritative U.S. record of hazardous weather — tornadoes, hail, floods, winter storms, and more. Each CSV row is one reported event with structured fields (begin/end time, state, county, event type, injuries, deaths, property damage) and **official NOAA prose**: `EPISODE_NARRATIVE` describes the broader meteorological episode; `EVENT_NARRATIVE` describes the individual report.

Recent years contain 60k–80k detail rows. Events sharing an `EPISODE_ID` belong to the same meteorological episode. That grouping gives a natural document boundary: forecaster text describing what happened, plus daily counts of injuries, damage, and event reports over the same calendar span.

This makes NOAA Storm Events a strong CPT candidate — both modalities come from the same first-party source, aligned by episode, without synthetic task framing.

---

## What we are training for

**Objective:** Continued pre-training on world knowledge, not instruction fine-tuning. The model reads natural documents where a time series appears inline via a `<ts></ts>` placeholder. There is no "predict the next N days" prompt; the full episode (text + all daily values) is one training document.

**Window design:** One record = one `(EPISODE_ID, STATE)` span. All event rows in that episode for that state are aggregated into three daily series over `[first_event_date, last_event_date]` inclusive. Days with no reports are zero-filled. This is **not** a sliding window — episodes are the natural meteorological unit.

**Why these three metrics:**

| Series | Rationale |
|--------|-----------|
| injuries/day | Human impact; sparse but high signal when non-zero |
| USD/day (property) | Economic impact; parses NOAA categorical damage strings |
| events/day | Activity intensity; always defined, complements sparse injury/damage |

The narrative text and time series describe the same episode from different angles: prose explains the meteorology and specific impacts; the numbers quantify daily totals across the episode.

**Example record** (hand-authored illustrative example — shows a 4-day multi-day episode; see [Notes for team lead](#notes-for-team-lead) for how this compares to real 2023 output):

```json
{
  "text": "Oklahoma tornado outbreak, May 20–23 2023. Multiple rounds of supercell thunderstorms produced tornadoes across central Oklahoma amid strong low-level moisture and shear. An EF1 tornado near Norman had peak winds of 95 mph and a path length of 2.3 miles. Daily impact metrics for this episode: <ts></ts>.",
  "timeseries": [
    {"values": [0, 3, 2, 0],       "unit": "injuries/day",     "freq": "daily"},
    {"values": [0, 150000, 500000, 0], "unit": "USD/day",       "freq": "daily"},
    {"values": [0, 1, 2, 0],       "unit": "events/day",        "freq": "daily"}
  ],
  "episode_date_range": ["2023-05-20", "2023-05-23"],
  "geography": "OKLAHOMA",
  "event_types": ["Tornado", "Hail"],
  "dataset": "noaa_storm_events",
  "source": "ncei_storm_events_db",
  "series_id": "AL20230520_OKLAHOMA",
  "task_type": "world_knowledge"
}
```

---

## Output format

Each line in `output/noaa_storm_events_cpt.jsonl` is one JSON object:

| Field | Description |
|-------|-------------|
| `text` | Natural prose from `EPISODE_NARRATIVE` + up to 3 `EVENT_NARRATIVE`s, ending with a sentence containing exactly one `<ts></ts>` placeholder |
| `timeseries` | One to three daily series (see below); currently always three objects, with a proposed change to omit all-zero series |
| `episode_date_range` | `[first_event_date, last_event_date]` as ISO `YYYY-MM-DD` strings |
| `geography` | State name (uppercase), e.g. `"OKLAHOMA"` |
| `event_types` | Sorted list of distinct event types in the episode |
| `dataset` | Always `"noaa_storm_events"` |
| `source` | Always `"ncei_storm_events_db"` |
| `series_id` | `{EPISODE_ID}_{STATE}`, or `{first_date}_{STATE}_{event_type_slug}` when `EPISODE_ID` is missing |
| `task_type` | Always `"world_knowledge"` |

**The three `timeseries` objects (fixed order when present):**

1. **injuries/day** — `INJURIES_DIRECT + INJURIES_INDIRECT` summed per calendar day
2. **USD/day** — `DAMAGE_PROPERTY` parsed to integer dollars (`50K` → 50000, `1.5M` → 1500000)
3. **events/day** — count of event rows per calendar day

All included `values` arrays have the same length: number of inclusive calendar days in `episode_date_range`. The script currently emits all three series even when injuries and damage are all zeros; see [Notes for team lead](#notes-for-team-lead) for why that may change.

---

## Notes for team lead

This section documents two structural properties of the 2023 NOAA export that show up immediately in `output/noaa_storm_events_cpt.jsonl`. Neither is a script bug — both reflect how NOAA records severe weather. Worth aligning on before scaling up.

### 1. Single-day episodes are expected (~86% of records)

NOAA assigns an `EPISODE_ID` to group events from the same meteorological cause, but many grouped events all fall on the **same calendar day**. A supercell that drops 53 hail reports across Nebraska on July 4 is one episode, all on one date.

The episode window is `[min(BEGIN_DATE_TIME), max(BEGIN_DATE_TIME)]` per `(EPISODE_ID, STATE)`. When every row shares a date, the time series has length 1 — e.g. `{"values": [53], "unit": "events/day"}`.

**2023 qualifying episodes by calendar span** (all states, `require_episode_narrative: true`):

| Episode span | Count | Share |
|---:|---:|---:|
| 1 day | 8,955 | 86.4% |
| 2 days | 898 | 8.7% |
| 3 days | 239 | 2.3% |
| 4 days | 111 | 1.1% |
| 5 days | 114 | 1.1% |
| 6 days | 41 | 0.4% |
| 8 days | 1 | 0.0% |
| **Total** | **10,359** | **100%** |

The hand-authored sample in `samples/example_output.jsonl` shows a 4-day Oklahoma outbreak to illustrate the multi-day format. It is **not** representative of the median record. The demo output (`max_records: 50`) takes the first 50 qualifying episodes in processing order; in a recent run that was 43 single-day, 6 two-day, and 1 three-day.

**CPT implication:** A single-value series is acceptable for world-knowledge pre-training. The model reads "one day, 53 storm events, these narrative conditions" and learns a real association — e.g. that a July 4 QLCS outbreak in Nebraska with 80 mph gusts and 2.75-inch hail produced 53 reported events. CPT is not trend forecasting; a scalar paired with rich narrative is legitimate world knowledge.

**Recommendation:** No change required for single-day episodes. To bias demo output toward multi-day examples, use `min_episode_days: 2` (or higher) in config.

### 2. Two of three series are usually all zeros (injuries and damage are sparse)

The script currently emits three `timeseries` objects per record. In practice, most episodes look like this:

```json
"timeseries": [
  {"values": [0], "unit": "injuries/day", "freq": "daily"},
  {"values": [0], "unit": "USD/day",       "freq": "daily"},
  {"values": [19], "unit": "events/day",    "freq": "daily"}
]
```

Only `events/day` carries signal; injuries and damage are zero for the vast majority of episodes. This is a **real data distribution**, not a parsing error: most individual NOAA episodes cause zero recorded direct injuries and zero recorded property damage. Injuries and deaths are rare per episode; damage is sparse too.

**CPT implication:** Emitting three slots where two are always `[0, 0, …]` means the model repeatedly sees `narrative → [0, 0, N]`, which could teach that storm events typically cause no injuries or damage — true at the median, wrong at the tail (tornado outbreaks, major floods).

**Proposed change (not yet implemented):** Only include a series in `timeseries` if it has at least one non-zero value. Examples:

Typical single-day hailstorm — one series:

```json
"timeseries": [
  {"values": [17], "unit": "events/day", "freq": "daily"}
]
```

Tornado outbreak with casualties — three series:

```json
"timeseries": [
  {"values": [3],      "unit": "injuries/day", "freq": "daily"},
  {"values": [150000], "unit": "USD/day",       "freq": "daily"},
  {"values": [4],      "unit": "events/day",     "freq": "daily"}
]
```

This gives a more honest picture: injuries and damage are the exception; when they appear, they mean something.

### 3. Text coverage and volume (how much is actually useful?)

We audited **all 77 annual detail files (1950–2026)** — 4,047,254 event rows total. Full per-year numbers are in [`output/text_coverage_audit.json`](./output/text_coverage_audit.json). Key finding: **your memory of ~1980 is correct** — the narrative columns exist but are completely empty.

#### Full historical corpus (1950–2026)

| Metric | Value |
|---|---:|
| Total event rows | 4,047,254 |
| Rows with `EPISODE_NARRATIVE` | 3,089,050 (76.3%) |
| Rows with `EVENT_NARRATIVE` | 2,246,514 (55.5%) |
| Unique episode narratives (deduped) | 276,160 |
| Episode narrative characters (deduped) | ~102 million |
| Event narrative characters (all rows) | ~329 million |
| **Combined narrative text** | **~431 million chars (~86M words / ~108M tokens)** |

**Narrative coverage by decade:**

| Decade | Event rows | Episode narr. | Event narr. | Episode chars | Event chars |
|---|---:|---:|---:|---:|---:|
| 1950s | 22,382 | 0.0% | 0.0% | 0 | 0 |
| 1960s | 50,090 | 0.0% | 0.0% | 0 | 0 |
| 1970s | 78,184 | 0.0% | 0.0% | ~2K | ~2K |
| **1980s** | **150,334** | **0.0%** | **0.0%** | **0** | **0** |
| 1990s | 539,262 | 43.8% | 23.3% | 24.1M | 16.7M |
| 2000s | 1,110,316 | 68.1% | 44.8% | 58.8M | 68.8M |
| 2010s | 1,254,760 | 100.0% | 74.2% | 74.9M | 137.4M |
| 2020s* | 841,926 | 100.0% | 82.3% | 46.1M | 106.3M |

\*2020s includes 2020–2026 YTD.

**When did text actually start?**

| Year | Rows | Episode narr. | Event narr. | Notes |
|---|---:|---:|---:|---|
| 1980 | 6,136 | 0.0% | 0.0% | Columns present, all empty — no usable text |
| 1990 | 10,945 | 0.0% | 0.0% | Still no narratives |
| 1993–1995 | — | sparse | ~58% (1995) | Event narratives appear first |
| 1996 | 48,534 | 71.2% | 0.2% | Episode narratives arrive; messy transition year |
| 2010+ | — | **100%** | ~74–85% | Modern reliable era |
| 2023 | 75,593 | 100.0% | 82.5% | Current script demo year |

**84 years (1950–1992) have zero narrative text.** The columns `EPISODE_NARRATIVE` and `EVENT_NARRATIVE` exist in the schema from the start, but NOAA did not populate them until the mid-1990s.

#### Practical subsets for CPT

| Subset | Rows | % of all rows | Episode narr. | Combined text | ~Tokens |
|---|---:|---:|---:|---:|---:|
| **All years (1950–2026)** | 4,047,254 | 100% | 76.3% | ~431M chars | ~108M |
| 1996+ (post-transition) | 3,582,764 | 88.5% | 86.2% | ~525M chars† | ~131M |
| **2010+ (recommended)** | 2,096,686 | 51.8% | **100%** | ~365M chars | ~91M |
| 2015+ | 1,444,958 | 35.7% | 100% | ~252M chars | ~63M |
| 2023 only | 75,593 | 1.9% | 100% | ~13.4M chars‡ | ~3.4M |

†Per-year dedupe only; summing decades slightly overcounts episode text.  
‡Episode chars deduped within year + all event chars for that year.

**For tomorrow's discussion — the headline numbers:**

1. **~4M event rows** in the full NOAA database, but only **~3.1M rows (76%)** have any episode narrative and qualify for CPT with the current filter.
2. **Pre-1996 data is essentially textless** for our purposes — 1980 has 6,136 rows and zero characters of narrative.
3. **The reliable text era starts ~2010** — 2.1M rows, 100% episode narrative coverage, ~91M tokens of forecaster prose.
4. At 2023 density (~10k CPT records/year from ~76k rows), the **2010–2023 window implies on the order of ~130k CPT episode records** and **~15–20M tokens of assembled text** (rough estimate; actual record count depends on episode grouping).

#### 2023 detail (assembled CPT `text` field)

The 2023-only analysis from the build script (10,359 episode records):

| Text field | Rows/episodes | Share |
|---|---:|---:|
| `EPISODE_NARRATIVE` (row level) | 75,593 | 100.0% |
| `EVENT_NARRATIVE` (row level) | 62,352 | 82.5% |
| Episodes with event narratives | 9,107 / 10,359 | 87.9% |

**Assembled `text` length** (after dedupe, truncation, up to 3 event narratives):

| Stat | Value |
|---|---:|
| Median length | 544 chars (~89 words) |
| Mean length | 658 chars (~107 words) |
| Episodes ≥ 200 chars | 94.9% |
| Episodes < 200 chars (thin one-liners) | 5.1% |

Thin example: *"Non thunderstorm winds on 10/13. Daily impact metrics for this episode: \<ts\>\</ts\>."*

**CPT implication:** Text is a strong modality, but **only for modern years**. Pre-1996 rows have time series metadata (dates, types, damage, injuries) but no prose to pair with. For CPT, recommend **`years: [2010, …]`** as the production window. Within that window, the main quality issues are thin one-liners (~5%) and sparse injury/damage series — not missing narratives.

**Recommendation:** Keep `require_episode_narrative: true`. Set `years` to 2010+ for production builds. Consider `min_text_chars: 200` to drop one-liner episodes. Re-run audit anytime: numbers above come from `output/text_coverage_audit.json`.

### Summary for decision

| What is happening | Should we change it? |
|---|---|
| Single-day episodes (~86% of 2023 episodes) | **No** — real NOAA structure; acceptable for CPT |
| Two of three series are all zeros (injuries/damage sparsity) | **Yes** — proposed: omit all-zero series from `timeseries` |
| Text coverage high in 2023 (100% episode narratives) | **No** — but pre-1996 is textless; use 2010+ for production |
| ~5% of episodes have thin text (<200 chars) | **Optional** — add `min_text_chars` quality filter |
| ~12% of episodes lack event narratives | **No** — episode narrative alone is sufficient backbone |
| Demo output skews single-day (first 50 episodes, uncapped order) | **Optional** — use `min_episode_days` or sort/filter for reviewer demos |
| Hand-authored sample shows 4-day episode | **No change** — sample illustrates target format, not median record |

---

## Window design

Episodes (`EPISODE_ID`) are the natural unit for severe-weather storytelling. A forecaster writes one episode narrative covering a multi-day outbreak; event rows within it share that context. Aggregating by episode preserves that structure.

Because the same physical episode can produce reports in multiple states, records are split per `(EPISODE_ID, STATE)`. A four-state derecho may yield four records with different geographies and daily arrays but related narratives.

Record count scales with the number of qualifying episodes, not with sliding-window combinatorics. Filtering (state, event type, narrative requirement) controls diversity and quality.

---

## Caveats

1. **Damage strings** — `DAMAGE_PROPERTY` uses categorical tokens (`50K`, `1.5M`, blank). The script parses these to integer USD; unparseable values become 0.
2. **Multi-state episodes** — handled by splitting one record per state within the same `EPISODE_ID`.
3. **Missing narratives in old years** — 1950–1992 have zero text (columns exist, empty). 1980 confirmed: 6,136 rows, 0% narratives. Reliable text starts ~2010. See [text coverage](#3-text-coverage-and-volume-how-much-is-actually-useful).
4. **Sparse impact series** — injuries and property damage are zero for most episodes; only `events/day` consistently has signal. See [Notes for team lead](#notes-for-team-lead).
5. **Single-day episodes dominate** — ~86% of 2023 episodes span one calendar day; multi-day records exist but are the minority.
6. **Report counts ≠ unique storms** — multiple CSV rows can refer to one physical event; `events/day` is a report count.
7. **No leakage concern in CPT format** — unlike forecast fine-tuning, the full episode text and all daily values are presented together as one document. There is no held-out future window to leak into the input.

---

## Assessment summary

| Field | Value |
|-------|-------|
| **Verdict** | `needs_pairing` — both modalities exist natively; conversion script required |
| **TS source** | NOAA Storm Events details CSV ([FTP index](https://www.ncei.noaa.gov/stormevents/ftp.jsp)) |
| **Text source** | Same CSV — `EPISODE_NARRATIVE` + `EVENT_NARRATIVE` per event/episode |
| **Acquisition** | `csv` (direct HTTP download or local file) |
| **Window method** | One record per `(EPISODE_ID, STATE)` episode span; daily aggregation over episode date range |

Full probe metadata: [`assessment.json`](./assessment.json)

---

## Quick start

From this folder (uses project `.venv` if present):

```bash
cd datasets/01_noaa_storm_events
pip install -r requirements.txt

# Run with defaults (2023, ≤50 records)
python scripts/build_cpt_jsonl.py --config config.example.yaml

# Dry-run: download + parse + report, no output files
python scripts/build_cpt_jsonl.py --dry-run

# Oklahoma tornado/hail episodes only, no record cap
python scripts/build_cpt_jsonl.py \
  --set data.state_filter=[OKLAHOMA] \
  --set data.event_type_filter=[Tornado,Hail] \
  --set output.max_records=null

# Multi-year export
python scripts/build_cpt_jsonl.py --set data.years=[2021,2022,2023]

# Longer episodes only (multi-day demo)
python scripts/build_cpt_jsonl.py --set data.min_episode_days=2 --set output.max_records=50

# Longer episode narratives, smaller demo cap
python scripts/build_cpt_jsonl.py \
  --set text.episode_narrative_char_limit=2000 \
  --set output.max_records=10
```

Outputs:

- `output/noaa_storm_events_cpt.jsonl` — CPT records (JSONL)
- `output/run_report.json` — episode counts, skip breakdown, config snapshot

---

## Configuration guide

Copy `config.example.yaml` → `config.yaml` for persistent local edits. Every key can be overridden: `--set dotted.path=value`.

| Section | Key | Purpose |
|---------|-----|---------|
| **data** | `source` | `download` or `local` |
| | `download_url_template` | Annual NOAA CSV URL; `{year}` substituted |
| | `local_path` | Local `.csv`/`.csv.gz` when `source=local` |
| | `years` | List of years to load |
| | `state_filter` | Uppercase state names; `[]` = all |
| | `event_type_filter` | Event types to keep; `[]` = all |
| | `require_episode_narrative` | Skip episodes with empty episode narrative |
| | `min_episode_days` | Minimum inclusive calendar span |
| | `min_episode_events` | Minimum event row count after filters |
| **text** | `max_event_narratives` | Max `EVENT_NARRATIVE`s appended (deduplicated) |
| | `event_narrative_char_limit` | Per-event narrative truncation |
| | `episode_narrative_char_limit` | Episode narrative truncation |
| | `ts_intro_sentence` | Closing sentence with `<ts></ts>` placeholder |
| **output** | `output_path` | JSONL output file |
| | `report_path` | Run statistics JSON |
| | `max_records` | Cap records written; `null` = no cap |
| | `indent` | `null` = compact one-line records; integer = pretty-print each record |

---

## Files in this folder

| File | Purpose |
|------|---------|
| `README.md` | This document |
| `assessment.json` | Triage / probe metadata |
| `config.example.yaml` | Documented default configuration |
| `requirements.txt` | Python dependencies (`pyyaml`) |
| `scripts/build_cpt_jsonl.py` | Episode-driven CPT JSONL builder |
| `samples/example_output.jsonl` | Hand-authored example record |
| `output/` | Generated JSONL + run reports |
| `output/text_coverage_audit.json` | Full 1950–2026 narrative coverage audit (per-year + decade stats) |
