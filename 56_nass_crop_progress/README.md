# USDA NASS State Crop Progress and Condition

**Status: Built — demo, 96 records (8 per state × 12 states), 0 rejected.** Passes
`schema/validate.py --strict` clean (96/96, 0 errors/0 warnings). **Window length: min 20,
median 31, max 38 real weeks.** Domain: agriculture (US). Original 7 states scouted
2026-07-25/26 (Iowa, Kansas, Minnesota, Indiana, Nebraska, Illinois, Ohio); 5 more added
2026-07-26 (Wisconsin, Missouri, Michigan, Pennsylvania, Kentucky).

**All-state rollout (2026-07-29): 39 states now wired in `STATE_CONFIGS`** (the committed
`output/` demo is still the 12-state build; the full 39-state corpus is a server-side run). An
all-state archive scout confirmed the data supports scale: **38,953 real archived weekly-report
PDFs across 48 states.** Record-count ceiling (a record = one state-week; multi-commodity =
multi-channel in one record, so commodities enrich channels but don't multiply the count):
- **Realistic, as-configured — 39 wired states at their current conservative floors: ~8.6k
  healthy long-series records** (window ≥ 20 wk), ~18k counting short early-season windows too.
  This is what the server `max_records: null` run yields today.
- **Full-scope max — all 48 states + floors pushed to each archive's true start: ~11.5–13.5k
  healthy** (~25–27k all-weeks). Reaching it needs the 9 deferred states (a cotton/peanut
  channel set) and a per-state garbled-boundary check to extend each floor backward.

The rollout also forced a real discovery-layer generalization (whole-folder `discover_all`).

One record = **one state's one real weekly report** during the growing season: that report's
own narrative (Agricultural Summary + state-climatologist weather narrative; letterhead and
per-station raw weather tables stripped) paired with an **expanding within-season window** of
that state's primary-commodity progress/condition/fieldwork channels, from the season's first
reported week through the current week. Alignment = `recites` (the narrative states the exact
percentages that are the series — e.g. "Soybeans blooming reached 22 percent, 3 days ahead of
last year" ↔ the series' own terminal value).

**Distinct from the already-built USDA WASDE (#41)**: WASDE is monthly national supply/demand
balance-sheet forecasts; this is **weekly, state-level**, physical planting/harvest-progress
narration — a different USDA/NASS report family entirely.

## Key facts

- **Series: NASS Quick Stats bulk flat file** (`nass.usda.gov/datasets/qs.crops_*.txt.gz`,
  ~1GB gzipped, **no API key**) — filtered once to state-level weekly PROGRESS/CONDITION/
  DAYS SUITABLE rows and cached in `.cache/`. Real weekly data for the channels used here goes
  back to **1979** for most states — far deeper than any state's real *text* archive, so text
  availability (not series availability) is the binding constraint on record depth.
- **Text: fetched live + via the Wayback Machine**, per state. Every state runs its own NASS
  field office with its own idiosyncratic archive — different base folders, different filename
  conventions across 2-4 naming eras per state (confirmed: no universal template). Discovery
  doesn't try to solve each state's week-numbering scheme; it lists every real (status-200) PDF
  Wayback ever captured under the state's hub folder for a given year, fetches each, and parses
  the report's own embedded "week ending" line for the ground-truth date.
- **Scope is deliberately "clean born-digital text only"** (explicit call, 2026-07-26): states
  and years that are scanned-image or OCR-garbled are excluded rather than OCR'd, to avoid any
  risk of non-verbatim "recited" text. Real per-state clean-text start year (scouted, not
  guessed): Indiana 1997, Iowa/Kansas/Nebraska 2003, Michigan 2004, Kentucky 2005, Missouri 2006,
  Minnesota 2007, Pennsylvania 2008, Ohio/Wisconsin 2015, Illinois 2018. (Nebraska has real files
  to 1947 and Illinois to 1980, but pre-2003/pre-2018 respectively are garbled-OCR /
  scanned-image and are out of scope for this build. Wisconsin has real files to 2001, but
  2001-2014 reports omit the year from their "week ending" line — see the date-parsing note
  below — so those weeks currently fail the series join rather than being misdated.)
- **Long-series selection, fixed after Faisal caught it in the raw output.** The first demo cut
  emitted the *earliest* qualifying week each season (right at `min_window_weeks`) — 30/56
  records had windows under 16 weeks despite full seasons running 30-45 weeks. Root cause: the
  per-season emission loop walked chronologically and stopped as soon as the per-state cap was
  hit, so a capped run always got the shortest windows a season could offer, never the longest.
  Fixed by processing each season's weeks **latest-first** (so a capped run keeps the richest
  trailing history instead) and raising `min_window_weeks` 8 → 20. Now: min 20, median 31,
  max 38 weeks across the current 96-record demo.
- **Two-column PDF layout, fixed, not skipped.** Older-era reports (roughly pre-2015) are
  typeset in two newspaper-style columns; naive extraction interleaves them mid-sentence
  ("Corn Planting Nearing Completion increased from 19 percent complete..." — two unrelated
  sentences glued together). Fixed with a column-gutter detector (find the vertical line
  crossed by the fewest word bounding boxes; reconstruct left column fully, then right) rather
  than skipping or OCR'ing these years.
- **False-positive two-column detection, fixed during the 5-state rollout (2026-07-26).**
  Scouting Missouri and Kentucky independently surfaced the same bug: an ordinary single-column,
  fully-justified page can coincidentally have a low-crossing vertical line too (just wherever a
  word-gap happens to fall), which the original detector mistook for a real column gutter and
  split into two "columns" that read as broken sentence fragments (confirmed on ~25-30% of
  Missouri's sampled weeks). Fixed by requiring the gutter to also have a **consistent width**
  across the rows that straddle it — a real printed gutter is a fixed physical margin (e.g. a
  tight 15-18pt cluster across dozens of rows on a real 2003 Iowa two-column page), while the
  false positive's "gap" is scattered (2-360pt, no cluster, on the Kentucky page that motivated
  the fix). Verified the fix doesn't regress the original real two-column Iowa file.
- **Kentucky's archive nests PDFs under `cw{yy}/` year-subfolders, not `{year}/`.** Every other
  state's discovery assumes a plain 4-digit year folder; Kentucky's real archive (clean back to
  2005) uses a 2-digit `cw05/`, `cw06/`, ... convention instead, which returned zero candidates
  under the shared assumption. Added a per-state `year_folder_fmt` override
  (`StateConfig.year_folder_fmt`, default `"{year}"`, Kentucky's `"cw{yy:02d}"`) rather than
  special-casing Kentucky in the shared discovery function.
- **Wayback CDX/fetch retries bumped 1 → 3 (with backoff).** All 5 states scouted in the second
  rollout independently hit transient `web.archive.org` connection failures/503s under normal
  sequential use — confirmed recoverable on retry, not state-specific. Without this, a real
  year's archive can silently look empty and get skipped.
- **Channels per record** (corn states, 21 channels, `freq: "1w"`):
  - **Growth stages** (7, cascading — see the null note below): `corn_pct_{planted,emerged,
    silking,dough,dented,mature,harvested}`.
  - **Condition** (5, full rating): `corn_condition_pct_{very_poor,poor,fair,good,excellent}`.
  - **Dense weekly backbone** (9): `days_suitable_per_week`, `topsoil_moisture_pct_{very_short,
    short,adequate,surplus}`, `subsoil_moisture_pct_{very_short,short,adequate,surplus}`.

  Kansas (winter wheat) swaps the 7 stage channels for `winter_wheat_pct_{planted,emerged,
  jointing,headed,coloring,mature,harvested}` and uses the `winter_wheat_condition_pct_*` set;
  the 9-channel dense backbone (fieldwork + soil moisture) is identical, since moisture is a
  state-level field measure, not crop-specific.
- **The soil-moisture + full-condition channels are the "dense backbone" (added 2026-07-29).**
  Originally each record carried only the growth-stage channels plus good/excellent condition,
  which made it mostly-null by construction (each stage is surveyed only during its own
  few-week window). NASS actually surveys **topsoil/subsoil moisture and days-suitable every
  week of the season** and the weekly narrative recites them verbatim ("Topsoil moisture was
  rated 1 percent very short, 8 percent short, 78 percent adequate, and 13 percent surplus") —
  those 9 channels are 84-100% dense and give every record a continuous weekly trajectory.
  Completing the 5-way condition rating (adding very-poor/poor/fair to the existing
  good/excellent) adds a further ~68%-dense band. Overall null fraction dropped **57.8% → 36.2%**
  across the demo; the growth-stage channels remain sparse on purpose (see next bullet). These
  channels were already in the cached bulk file — no new fetching.
- **Per-channel nulls in the *growth-stage* channels are real and expected — verified against
  the raw source, not a bug.** Each growth-stage channel is a cascading S-curve: NASS stops
  surveying "percent planted" once it saturates near 100% (e.g. Iowa corn 2003: real rows only
  exist 04-13→06-01, ending at 99% — no more rows published after), then "percent emerged" takes
  over, then "silking," etc. Any single stage channel is null outside its own real reporting
  window — but the dense backbone above means every week still has a real weekly series;
  confirmed directly against `.cache/progress_condition_state_weekly.tsv`, not inferred. (The
  only genuinely thin weeks left are ~0.5% of record-weeks at the very start/end of a season,
  where NASS published only fieldwork-days before planting or after harvest — a real source gap.)
- **Best-effort extraction, occasional residual fragment** (same documented tier as #49/#50
  Richmond Fed): a handful of records carry a short embedded district-breakdown table remnant
  (e.g. "Seedbed, primary preparation completed 96 96 96") or a stray masthead/letterhead
  fragment (a phrasing variant the masthead filter — tuned mostly on Iowa's own letterhead —
  didn't catch for another state, e.g. one Minnesota record still opens with its field-office
  attribution line). Not fabricated text — genuine source content, just noisier than the
  surrounding narrative in a minority of records. New examples from the 5-state rollout: (1)
  Michigan's corn-progress numbers live in a dot-leader table rather than a prose sentence some
  weeks (`Corn Planted .......... 87`, still verbatim and still cross-checked against the
  series, just table-shaped) and a rotated chart-axis label sometimes leaks in as one character
  per line; (2) Kentucky's table-row filter occasionally drops the "Corn Planted"/"Corn Emerged"
  table row specifically (short label + few digits trips the same digit-density heuristic
  meant for weather-station rows) — usually redundant with a prose sentence stating the same
  number, but not always; (3) Pennsylvania's ~2015-2025 template has a source-PDF character-
  interleaving defect in its masthead phone/fax line that a few fragments leak through.
- **License: public domain** (US federal government work, 17 U.S.C. §105) — no gate.

## Record shape

```json
{
  "text": "Soybean Harvest Over 80 Percent Complete\nAgricultural Summary: Last week was another big week for soybean harvest as 28 percent of the state's soybeans were harvested...\n\n<ts></ts>",
  "timeseries": [
    {"values": [null, 3.0, 28.0, 56.0, "... cascading stage, null outside its window ..."], "unit": "corn_pct_planted", "freq": "1w"},
    {"values": [null, null, null, 0.0, "..."], "unit": "corn_pct_emerged", "freq": "1w"},
    {"values": [null, null, null, null, "... condition, ~68% dense ..."], "unit": "corn_condition_pct_good", "freq": "1w"},
    {"values": [3.7, 2.0, 3.2, 4.3, "... days-suitable, 100% dense ..."], "unit": "days_suitable_per_week", "freq": "1w"},
    {"values": [52.0, 58.0, 70.0, 78.0, "... topsoil moisture, 100% dense weekly backbone ..."], "unit": "topsoil_moisture_pct_adequate", "freq": "1w"}
  ],
  "task_type": "world_knowledge",
  "text_quality": "real",
  "series_id": "nass_crop_progress:IA:2003:w28",
  "dataset": "nass_crop_progress",
  "license": "public-domain-us-gov",
  "text_source": "first_party_official",
  "alignment": "recites",
  "domain": "agriculture",
  "region": "US-IA",
  "meta": {"state": "Iowa", "commodity": "corn", "season_year": 2003, "week_index": 28, "window_weeks": 28}
}
```

(Full real example, not truncated: `samples/example_output.jsonl`.)

## Key open issues

- **Ceiling — GROUND-TRUTH from an all-state archive count (2026-07-29), not a projection.**
  A record is one **state-week** (multi-commodity = multi-channel in one record, WASDE-style, so
  commodities enrich channels but do NOT multiply the count). An all-state scout counted the real
  archived weekly-report PDFs per state: **38,953 across all 48 states** (30 addable states =
  25,749; 12 already-wired = 13,204; 6 New England = 0). Applying honest haircuts (≈80% parse to a
  clean dated report that joins the series, ≈90% survive the clean-text/garbled-tail scope):
  - **Healthy long-series records** (window ≥ 20 wk, the quality bar): **~11.5–13.5k** — the
    ground-truth count and the earlier per-week projection agree.
  - **All weeks incl. short early-season windows**: **~25–27k** (matches the original scouting
    estimate, which counted every week).

  The binding constraint is clean-text availability per state (series go back to 1981; text is the
  limit). The ~11.5–13.5k / ~25–27k figures are the **full-scope max** (all 48 states + floors
  pushed to each archive's true start). **As currently configured — the 39 wired states at their
  conservative confirmed-clean floors — the realistic yield is ~8.6k healthy (~18k all-weeks)**;
  that's what a server `max_records: null` run produces today. The `output/` demo caps at 8/state
  to show breadth.
- **39 of 48 real-program states wired** (12 originally + 27 in the all-state rollout). Adding a
  sibling state is mostly a `StateConfig` entry in `scripts/state_sources.py` (folder, commodity/
  stage channel list, clean-text start year) — the discovery/fetch/extract/pairing pipeline is
  shared. **9 states deliberately NOT wired**, documented at the end of `STATE_CONFIGS`: AZ, FL, NV
  (cotton/peanut/hay-primary — ~0 corn, only thin/no winter wheat; need a new crop-stage channel
  set, not just a config entry) and the 6 New England states (CT/MA/ME/NH/RI/VT — crop progress
  is published through a shared *regional* office, so per-state Wayback folders are empty).
- **Layout-agnostic discovery (`discover_all`), added in the all-state rollout.** The original
  per-year `{prefix}/{year}/` query only found reports nested under a 4-digit year folder — an
  all-state ground round revealed ~half the states organize recent reports differently: flat in the
  folder root (Texas `.../Crop_Progress_&_Condition/txcw4111.pdf`), `prevCW/{year}/` (Texas older),
  `{year}_PDF/` (Georgia 2007), or `cw{yy}/` (Kentucky). The build now discovers the WHOLE folder
  once and buckets each report by its OWN parsed week-ending date, so the on-disk layout is
  irrelevant. This lifted the ground round from 18/39 → 30/39 states emitting (the remaining 9 were
  transient Wayback query failures during the parallel run, not missing data — every one has a
  confirmed archive in the scout; e.g. Kentucky isolated-tests clean at 674 candidates).
- **Clean-text start years for the 27 rollout states are conservative confirmed-clean floors.**
  Each is the earliest year a fetched sample actually parsed to clean dated prose; the archived-PDF
  counts show most archives run deeper, but the earlier years were not all confirmed clean (Wayback
  flakiness during scouting + per-state garbled/scanned boundaries not checked the way the first 12
  were). A server build can extend each backward after confirming the earlier years parse clean.
- **Wisconsin's 2001-2014 depth is real but currently unrecoverable.** Those years' reports omit
  the year from their "week ending" line, so the date parser either falls back to the release
  date (which can differ from the true week-ending Sunday) or fails outright — either way the
  week fails the exact-date join against the series and gets silently dropped, not corrupted.
  Recovering it would need a 4th date-parsing path that backfills the year from the season being
  processed; not done in this pass.
- **All 12 states so far are corn (or, for Kansas, winter wheat) commodity.** The next
  incremental states to scout are likely other corn/wheat states (e.g. North/South Dakota,
  where corn vs. wheat as the *narrated* headline crop needs verifying, not assumed from series
  row counts alone — Kansas itself was chosen as wheat despite corn having a slightly higher
  PROGRESS row count). Cotton/sorghum/rice/soybean-primary states are a further step: they'd
  need a new crop-stage channel set (`_cotton_channels()` etc.), not just a new `StateConfig`.
- **`min_window_weeks` (default 20)** drops early-season weeks whose trailing window would be
  short. Combined with latest-first per-season selection, this is what keeps a capped run's
  windows long (min 20/median 31/max 38 in this demo) rather than short — lower it for more
  (shorter) records if raw count matters more than window length for a given use.

## Run

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3
python scripts/build_cpt_jsonl.py                      # demo (96, config default)
python scripts/build_cpt_jsonl.py --set output.max_records=null   # full run, all 12 states
python ../schema/validate.py output/ --strict
```

First run downloads and filters the ~1GB NASS bulk file (one-time, cached in `.cache/`).
Output: `output/nass_crop_progress_cpt.jsonl` + `output/run_report.json`. Source:
[NASS state Crop Progress hub](https://www.nass.usda.gov/Publications/State_Crop_Progress_and_Condition/),
[Quick Stats bulk datasets](https://www.nass.usda.gov/datasets/).
