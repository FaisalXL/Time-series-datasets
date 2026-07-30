# USDA NASS State Crop Progress and Condition

**Status: full build, 25,600 records, 25,600/25,600 strict-clean (0 errors, 0 warnings).**
Domain: agriculture (US). Alignment: **mixed, measured per record** against a permutation control
(12,862 `recites` / 12,738 `describes`). License: public domain (US federal work, 17 U.S.C. §105),
with one flagged licensing question (§6).

| | |
|---|--:|
| records | **25,600** |
| states | **42** (48 wired − 6 New England, §5) |
| seasons covered | **1982–2026** (45 distinct) |
| window | 52 reported weeks, every record |
| timesteps | 1,331,200 |
| real datapoints | **39,738,810** (48.9% null, per-record `meta.null_frac`) |
| channels / record | median 63, max 104 |
| narrative chars | median 3,755 |
| duplicate `series_id` | 0 |
| series duplication | 20.5× (below `26_ics209` 32×, `11_eia` 52×) |

Grown from a **96-record demo**. For scale: this one package carries slightly more real datapoints
(39.7M) than the entire rest of the corpus did before it (37.9M), so it is worth a deliberate look
at training-mix weighting.

**Source reconciliation** — 39,933 archived PDFs discovered → 39,915 fetched (99.95%; the 18 misses
are 16 genuine 404s and 2 non-PDFs at source) → 33,504 parsed to a clean dated report
(4,926 no parseable date, 1,292 no text layer, 172 corrupt, 20 not-a-weekly-report, 1 garbled)
→ 25,600 records (3,746 dated to a week with no series, 48 short window, 68 too sparse, 7 text-length
gates). Every stage balances.

One record = **one state's one real weekly report**: that report's own narrative (Agricultural
Summary + the state weather narrative; letterhead, per-station weather tables, column headers and
third-party copyright lines stripped) paired with a **trailing 52-reported-week window** of that
state's crop progress / condition / soil-moisture / fieldwork channels, ending at the report's own
week. Multi-commodity means multi-channel in one record, so commodities enrich channels but do not
multiply the record count.

**Distinct from USDA WASDE (#41):** WASDE is monthly national supply/demand balance-sheet
forecasts; this is weekly, state-level, physical planting/harvest-progress narration — a different
NASS report family.

---

## 1. What changed in this pass, and why

This package existed as a 96-record demo whose `output/` held only `run_report.json`. Taking it to
full scope surfaced four defects, three of which had been *documented as fixed or as facts about
the source*. They are listed first because each one changes a number the previous README asserted.

### 1.1 The dense backbone was never in the data ⚠️

The README's headline density claim — *"the soil-moisture + full-condition channels are the dense
backbone … those 9 channels are 84-100% dense and give every record a continuous weekly
trajectory … overall null fraction dropped 57.8% → 36.2%"* — **was not true of the committed
code.** NASS files soil moisture under its own `STATISTICCAT_DESC = "MOISTURE"`, and the builder
filtered the bulk file to `{PROGRESS, CONDITION, DAYS SUITABLE}`. All eight
`SOIL, {TOPSOIL,SUBSOIL} - MOISTURE …` channels therefore matched nothing and were silently
dropped by the builder's own `all(v is None)` skip.

`MOISTURE` is the **5th-largest category in the file (369,377 STATE/WEEKLY rows)**. The committed
sample confirms the omission directly: `samples/example_output.jsonl` carries **zero**
soil-moisture channels and only good/excellent of the 5-way condition rating.

Restored (plus `PASTURELAND`, see §3), the backbone is **98.7% dense** across the text-available
era, against ~26% for the cascading growth-stage channels. The category vocabulary is now enumerated
exhaustively — all 52 STATE/WEEKLY categories — in [`docs/source_census.md`](docs/source_census.md),
so the filter's premise is recorded rather than assumed.

### 1.2 Wayback throttling was being recorded as missing data ⚠️

`web.archive.org` throttles at the TCP level (`[Errno 111] Connection refused`) rather than with
HTTP 429, and the inherited fetcher returned immediately on `HTTPError` with a 3-retry budget. Under
a concurrent fan-out this produced **81 of 88 Iowa PDFs marked as permanent failures** — downstream,
indistinguishable from "the archive does not have these."

The same defect in the discovery path is the origin of several "facts" in the previous README:

| Recorded as | Actually |
|---|---|
| `AR: 0 candidates` | 794 |
| AZ / FL / NV — "~0 corn", deferred | **1,225 / 1,354 / 210 archived PDFs** |
| "the remaining 9 were transient Wayback query failures" | same root cause, still unfixed |
| `ID: 0` | 650 (the one prefix query holding them had errored) |

Replaced with a single global AIMD token bucket over a keep-alive session (`scripts/polite_fetch.py`):
Iowa went to **1130/1130, zero failures**. Two structural rules matter as much as the limiter:
`ok` for a state's discovery now requires **every** folder-variant query to answer (the old `any_ok`
let one empty variant mask an errored one), and only `ok`/`http_404`/`http_403` count as final, so a
resumed run retries throttle-failures instead of baking them in.

### 1.3 The two-column fix had inverted into a false negative ⚠️

The column detector's "consistent gutter width" test — added to kill a Kentucky false positive —
rejected *genuine* two-column pages, which then fell back to row-order extraction that interleaves
the columns mid-sentence:

> …eager to plant their crop for the upcoming crop year. Warm **Provided by Harry Hillaker, State
> Climatologist**

Replaced with a projection-profile gutter finder keyed on the physical fact the earlier scouting had
already measured (a printed gutter is a continuous 15–18pt band): find the widest contiguous band no
**text row** crosses, require ≥10pt and text on both sides. Per-*row* rather than per-word is the
point — a real gutter is still crossed by the page's few full-width headings and tables, so a
word-level "empty band" test finds nothing (Iowa 2012-04-01: never drops below 4 crossing words of
830, but shows an unambiguous 12pt row-level band).

### 1.4 The boilerplate filter was deleting a quarter of the narrative ⚠️

`_is_tabular_line` matched table markers as substrings, so it fired on ordinary prose:
`TEMPERATURE` inside *"Temperatures for the week as a whole averaged…"*, `PRECIPITATION` inside
*"The statewide average precipitation was 0.17 inches"*. These are weather narratives — the filter
was eating its own subject matter.

Measured on a 60-report Iowa sample: **24.6% of prose-looking lines dropped**, 560 of them on marker
hits alone. Now a marker only counts when the line does not read as prose. Residual loss **5.7%**,
all of it correct (addresses, unit-header rows, a reversed postal stamp, third-party copyright
lines). Median narrative **4,416 → 4,982 chars**.

Related: weather-station rows and crop-progress rows are both dot-leader shaped
(`Lowden ..... 77 36 52 11` vs `Oats planted ..... 56 57 55 64`), and only the second is
recitation. They are told apart with a vocabulary **derived from the SHORT_DESCs being paired
against**, restricted to commodity head words — including the rating classes put "red" in the
vocabulary and let the weather station `Red Oak ..... 86 41 63 19` through.

### 1.5 Three date-parser gaps, each costing a whole state's archive

Every report is joined to its series by the week-ending date parsed from its own text, so a pattern
the parser doesn't know is a state that silently disappears. Auditing the `no_date` bucket found
**55% of it still mentioned a week-ending phrase** — i.e. most of it was parser gaps, not undated
documents:

| Form found | Example | Cost |
|---|---|---|
| day-of-week between keyword and date | `Week Ending Sunday, May 15, 2011` | **all 283 Louisiana reports** |
| dash-separated numeric | `Week Ending 12-3-51` | Nebraska's historical era |
| year omitted entirely | `WEEK ENDING JULY 16` | Ohio 198→3; the depth the README recorded as unrecoverable for Wisconsin 2001-2014 |
| abbreviated month name | `Week Ending: Aug 13, 2006` | matched the pattern, then failed the month lookup |

The year-less form is recovered by taking the 4-digit years the document itself states and keeping
the one that makes the date a **Sunday** — these weeks end on Sunday, and a given month/day is a
Sunday only about one year in seven, so a unique candidate almost always exists. Where it doesn't,
the report is dropped rather than guessed: a wrong year silently mis-joins the record to another
season's series, which is worse than losing it. A two-digit year now pivots on "not in the future"
(a fixed `<70 → 2000s` rule read Nebraska's `12-3-51` as 2051).

Net: `no_date` **3,064 → 1,890**, clean parses **17,375 → 18,553**. Re-auditing the remainder, the
week-ending mention rate fell 55% → 16%, and those are genuine OCR corruption in Nebraska's
historical scans (`10/6/_7`, `Jialy 23, 1995`, `FEB, l&, 1955`) — correctly rejected.

### 1.6 A garble metric I introduced was itself a false positive ⚠️

Replacing the hand-curated per-state clean-text floors with a measured garble check (§5) initially
scored the **whole** extracted text, and flagged **258 perfectly readable reports** — every
California 2024 report, 155 Pennsylvania reports, all of Louisiana's. Those reports interleave a
side-by-side table whose cells are exactly the short vowel-less tokens the metric counts; their
prose is clean. Scored over **prose lines only**, the distribution is tight and unimodal (p50 0.009,
p95 0.039, p99 0.061 across 15,829 reports) — genuinely shredded text is essentially absent, because
scanned-image years fail earlier with no text layer at all. Garbled rejects: **258 → 1**.

Two related accounting fixes: `no_text` (no text layer — 1,252 scanned images, e.g. Illinois
`wc_012180.pdf`, week of 1980-01-21, extracting to 1 character) is now split from `no_date` (has
text, no parseable date), so the OCR-only opportunity is visible rather than mixed into a bucket
that looks like a parser failure. And documents that are **not a single weekly report** are rejected:
whole-season compilations (Wisconsin `cw2002.pdf`, 80,532 chars) and monthly summaries (Idaho
`Monthly_Feb_2016.pdf`) both carry a valid week-ending date, so nothing upstream caught them, but
their text spans a season while their window ends at one week. Exactly **12 of 19,058 reports**
(0.06%) exceed the 20k-char gate and every one is of those two kinds; p99.9 of real reports is
13,918 chars.

---

## 2. Why a trailing window (and why 52)

The package inherited an **expanding within-season** window — the shape the corpus's windowed-series
policy rejected on `11_eia` — without a measurement. Both were built and compared on Iowa:

| | expanding within-season | **trailing 52 wk** |
|---|--:|--:|
| records | 360 | **773** (2.15×) |
| reports discarded to `min_window_weeks` | **413 (53%)** | 0 |
| window weeks (min/median/max) | 20 / 27.5 / 39 | **52 / 52 / 52** |
| null fraction (median) | 0.412 | 0.470 |
| series duplication (emitted ÷ unique) | 7.08× | 24.71× |
| ordered-group alignment vs control | 97.8% vs 0.28% | 96.8% vs 0.13% |

Trailing wins on volume and on window uniformity, at higher duplication. Note the expanding window's
real problem here is **not** the one the policy flagged: bounded by a season it duplicates *less*
than a trailing window, but it forces a `min_window_weeks` floor that throws away **53% of all
reports** — far worse than the 19% the previous README inferred from the demo.

**Why 52.** Record count is completely **flat** in this parameter (measured 26 → 104 weeks: 773
records at every setting), so length trades only against duplication. Season length runs **34
reported weeks (New England) to 52–53 (Florida)**, so 52 is the *shortest* window spanning a full
annual crop cycle in every state, and it reaches the year-ago week the narrative compares against
(Iowa: lag 28–39 reported weeks, median 35). Duplication of 24.7× sits below the shipped
`26_ics209` (32×) and `11_eia` (52×). Off-season weeks don't exist in the source, so the window is
irregular in clock time and every record carries an explicit `timestamps` array (SCHEMA.md §3.3).

---

## 3. Channels — selected from the data, not hand-written

The old `_corn_channels()` / `_wheat_channels()` pair hard-coded one commodity per state, and was
the stated reason nine states were unbuildable ("they need a new crop-stage channel set, not just a
config entry"). `scripts/channels.py` derives channels from the series index instead, so a state
needs no per-commodity code at all.

- **Universal backbone** (crop-agnostic, surveyed nearly every week): `days_suitable_per_week`,
  8 × topsoil/subsoil moisture, 5 × `pastureland_condition_*`. **`PASTURELAND` is the single
  largest series family in the file** (215,813 STATE/WEEKLY observations, all 48 states) and the
  previous build used none of it.
- **Commodity channels**: every commodity with ≥`min_commodity_weeks` of weekly history in that
  state, contributing its PROGRESS growth stages (ordered by when each stage actually peaks, so the
  channel list reads as the crop's real cascade) plus its 5-way CONDITION rating.

Median **83.5 channels per state**, versus 21 configured before (of which 8 were dead). Iowa: 53.
Growth-stage sparsity is real and expected — each stage is surveyed only during its own window, so
a channel is null outside it; the backbone is what makes every week a real series.

**`max_commodities` is capped at 8, and that cap was measured rather than assumed.** The median
state has 11 commodities clearing the history bar (New York has 24), so the cap does bind. Raising
it 8 → 16 changes essentially nothing: records 2,906 → 2,905, **median channels 69 → 69** (only a
few states have more than 8, so the median record is untouched), null fraction 0.524 → 0.533,
`recites` 2,323 → 2,324, alignment lift 79.8 → 79.9 pp. The extra commodities are marginal crops
the narrative rarely mentions; they add sparsity and no signal. `min_commodity_weeks` is inert at
any value from 50 to 300 for the same reason — the commodity cap binds first.

Going the *other* way is a real dial, and it is a **density-vs-coverage decision for the owner**,
not a bug — the same shape as #55's 400-char text floor:

| `max_commodities` | median channels | null fraction | `recites` | alignment lift |
|--:|--:|--:|--:|--:|
| 2 | 36 | **0.425** | 2,302 | 61.3 pp |
| 4 | 54 | 0.493 | 2,318 | 61.8 pp |
| 6 | 65 | 0.510 | 2,321 | 61.9 pp |
| **8** (default) | 66 | 0.519 | 2,323 | 61.9 pp |

Alignment is flat across the whole range, so the choice is purely how much real published data each
record carries against how dense it is. The default keeps the most data; the dense 14-channel
backbone is ~98% dense regardless, and every record records its own `meta.null_frac`, so a
density-filtered subset can be taken at training time without a rebuild.

---

## 4. Alignment — measured, with a control

Loose "does this value appear in the text" matching is near-useless here: values are 0–100
percentages and a report prints dozens of numbers, so **an unrelated report's prose already matches
50.4%** of any week's values. Alignment is therefore tested on something coincidence cannot produce:
whether a **rating group's** values for the report's own week appear **in order, within ~260
characters** — the form NASS actually recites them in ("Topsoil moisture rated 8 percent very short,
28 percent short, 60 percent adequate, and 4 percent surplus").

Measured on the shipped 25,600 records:

| Test | True | Permuted control | Lift |
|---|--:|--:|--:|
| ordered rating group (decides the tier) | **50.2%** | **0.27%** | **+50.0 pp** |
| loose any-value match, report's own week | 67.8% | ~43% | +25 pp |

The control of **0.27%** is the number that matters: when a group is tagged as recited, it is
essentially never a coincidence. On a single reciting state the rate is far higher (Iowa 96.8% true
vs 0.13% control, +96.6 pp) — the corpus-wide 50.2% reflects that half the states write qualitative
narratives instead, and those are tagged `describes`, not counted as recitation.

Every record is tagged from its own prose (`recites` if any group matches, else `describes`) and
carries the matched groups in `meta.recited_groups` — the per-record pattern #41 and #55 settled on.
The +96.6 pp lift is the strongest alignment evidence in the corpus so far (#55: +26.4 pp).

**The tier split is real and varies enormously by state**, which is why it is tagged per record
rather than asserted per package: Kansas 98% `recites` (827/846), Iowa 96%, Illinois 94% — but **California
0% of 753** and Colorado 4%, because those field offices write a *qualitative, region-by-region* narrative
instead of reciting the rating percentages ("In the Sacramento Valley, rice harvest continued";
"Producers in the San Luis Valley experienced ideal conditions … harvest of potatoes"). Those are
genuine, specific descriptions of the series — not extraction failures, checked by reading them.

`describes` records were then verified against the schema's own condition for the tier ("qualifies
if the description is specific to *this* series, not boilerplate"), each tier against its own
permutation control:

| Tier | n | Own-week value match | Permuted control | Lift |
|---|--:|--:|--:|--:|
| `recites` | 12,862 | 83.4% | 46.6% | **+36.9 pp** |
| `describes` | 12,738 | 52.1% | 38.1% | **+13.9 pp** |

So the `describes` half is week-specific well above chance — weaker signal, correctly labelled,
and qualifying on the same basis as `26_ics209` and `25_nwps`.

### 4.1 How much of the window does the text actually cover?

Stated precisely rather than implied. **Recitation-by-lag profile** (Iowa, share of each week's
values appearing in the prose, against the 50% coincidence floor):

| lag behind report week | 0 | 1 | 2 | 3–51 |
|---|--:|--:|--:|--:|
| recited (Iowa, single state) | **88.8%** | 57.6% | 53.5% | ~50–52% (at floor) |
| recited (all 25,600 records) | **67.8%** | 48.8% | 44.5% | ~43–44% (at floor) |

**The text covers its own week strongly, the previous week weakly, and nothing beyond that.** The
remaining ~50 weeks are historical context, not described content. This is the same terminal-point
structure the corpus ships elsewhere (`26_ics209` 100% terminal-point, `41_wasde` anchor channel,
`11_eia`/`25_nwps`/`31_usdm` trailing windows), and it is why the window length is justified by the
source's own annual cycle rather than by claimed textual coverage. Year-ago values, which the prose
does cite in sentences, are **not** recited above the coincidence floor as a group (0.508 vs 0.50),
so year-over-year grounding is not claimed.

---

## 5. Scope: 48 states, and the six that are excluded

**48 states emit records.** The nine previously deferred were re-examined and split:

- **AZ, FL, NV — now included.** The blocker was the hand-written channel sets, removed in §3. They
  report large weekly series (AZ upland cotton 1,660 weeks, FL peanuts 966, NV pastureland 944) and
  real archives (1,225 / 1,354 / 210 PDFs). The "~0 corn" reading conflated a throttled probe
  (§1.2) with a fact about the source.
- **CT, MA, ME, NH, RI, VT — investigated, still excluded, on alignment rather than availability.**
  The recorded blocker ("per-state Wayback folders are empty") is true but not the real one: the
  shared regional office has **979 archived reports**, and Quick Stats carries per-STATE series for
  all six. They are simply not about each other. The regional prose states **regional** aggregates
  ("there were 5.7 days available for field work across New England. Pasture condition was rated
  12% poor, 20% fair, 58% good, and 10% excellent"), and **no New England aggregate series exists** —
  checked across the whole file, WEEKLY rows exist only at `STATE` (3,352,511), `NATIONAL` (120,649)
  and `REGION : SUB-STATE` (6,713, entirely Colorado potato districts). Per-state content in the
  report is table-only and thin (`Maine 5 5 5 Fair/Good`). Pairing regional prose with one state's
  window would be grounding in name only. The reports stay harvested so a future pass can revisit.

**Per-state clean-text floors are gone.** The hand-curated `clean_text_start_year` values (Iowa
2003, Illinois 2018, …) were conservative guesses at each archive's scanned-image boundary and cost
real records. `extract_text.py` measures garbling per document instead, so each state reaches as far
back as its text actually parses clean.

---

## 6. Open licensing question — non-federal narrative ⚠️

`license: public-domain-us-gov` rests on 17 U.S.C. §105, which covers **federal** works. In many
states the weather narrative inside the federal NASS publication is contributed by the **State**
Climatologist or a state department of agriculture (Iowa: *"Provided by Harry Hillaker, State
Climatologist, Iowa Department of Agriculture & Land Stewardship"*) — a state employee, and state
works are not covered by §105. **620 of 773 Iowa records (80%)** carry such an attribution.

Flagged per record as `meta.nonfederal_narrative`, not silently included and not silently dropped —
the same handling as `31_usdm_drought`'s non-federal lead-byline hold and `55`'s
`nonfederal_affiliation`. **This is a decision for the owner, not an engineering question.**

Separately: **AWIS, Inc.** (a commercial weather vendor) prepares the per-station weather tables in
a large share of reports — 382 of 846 Iowa reports contain an AWIS copyright line. Those tables and
their copyright lines are stripped, so the exposure is removed rather than flagged; the narrative
prose is not AWIS-authored.

---

## 7. Record shape

```json
{
  "text": "Another hot, dry week without significant precipitation in most areas of the state caused Iowa crop conditions to decline… There were 6.8 days suitable for fieldwork statewide during the past week. Topsoil moisture levels declined to 74 percent very short, 23 percent short, 3 percent adequate, and 0 percent surplus…\n\n<ts></ts>",
  "timeseries": [
    {"values": ["…", 6.0, 6.9, 6.7, 6.8], "unit": "days_suitable_per_week", "freq": "1w"},
    {"values": ["…", 28.0, 48.0, 58.0, 74.0], "unit": "topsoil_moisture_pct_very_short", "freq": "1w"},
    {"values": ["…", 26.0, 52.0, 74.0, 85.0], "unit": "soybeans_pct_blooming", "freq": "1w"}
  ],
  "timestamps": ["2011-04-03", "…", "2012-07-22"],
  "task_type": "world_knowledge",
  "text_quality": "real",
  "series_id": "nass_crop_progress:IA:2012-07-22",
  "dataset": "nass_crop_progress",
  "source": "https://www.nass.usda.gov/Statistics_by_State/Iowa/…/2012/…pdf",
  "license": "public-domain-us-gov",
  "text_source": "first_party_official",
  "alignment": "recites",
  "domain": "agriculture",
  "region": "US-IA",
  "period_start": "2011-04-03",
  "period_end": "2012-07-22",
  "meta": {
    "state": "IA", "commodities": ["soybeans", "corn", "oats", "hay"],
    "season_year": 2012, "week_ending": "2012-07-22", "window_weeks": 52,
    "n_channels": 46, "null_frac": 0.4645,
    "recited_groups": ["topsoil_moisture", "subsoil_moisture", "corn_condition",
                       "pastureland_condition", "soybeans_condition"],
    "nonfederal_narrative": true
  }
}
```

The 2012 drought is legible in that window — topsoil very-short runs 15 → 26 → 28 → 48 → 58 → 74
while adequate collapses 53 → 3 — and the prose's stated 6.8 / 74 / 23 / 3 / 0 are exactly the
channel terminals. Full real examples: `samples/example_output.jsonl`.

---

## 8. Pipeline

Three stages, split so the ~40k Wayback fetches are paid **once** and every design question
downstream is a local loop of minutes. The previous single-pass build re-paid the whole network cost
on every iteration, which is what made this "a server-side run, budget like `25_noaa_nwps_flood`".

```bash
pip install -r requirements.txt

# 1. network, once (resumable; raw PDFs cached, not extracted text)
python scripts/harvest_text.py discover
python scripts/harvest_text.py fetch          # ~40k PDFs, ~3-4 req/s adaptive
python scripts/harvest_text.py status

# 2. local
python scripts/prep_series.py                 # bulk Quick Stats -> .cache/series_index.pkl
python scripts/extract_text.py --procs 48     # cached PDFs -> .cache/text/

# 3. build + validate
python scripts/build_corpus.py --config config.example.yaml \
       --out output/nass_crop_progress_cpt.jsonl --report output/run_report.json
python ../schema/validate.py output/ --strict

# design comparison (runs both window strategies, emits nothing)
python scripts/build_corpus.py --measure --states IA,KS,TX
```

Raw bytes are cached rather than extracted text on purpose: the gutter detector and the narrative
cleaner are exactly the parts that needed another pass (§1.3, §1.4), and re-extracting from local
disk is minutes instead of hours.

Sources: [NASS state Crop Progress hub](https://www.nass.usda.gov/Publications/State_Crop_Progress_and_Condition/),
[Quick Stats bulk datasets](https://www.nass.usda.gov/datasets/). Historical reports are **not** on
the live site any more (spot-checked across the archive: 404 for everything but the most recent
files), so the Wayback Machine is the sole route and its rate limit is the build's binding cost.
