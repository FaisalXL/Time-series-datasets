# USDA NASS State Crop Progress and Condition

**Status: Built — demo, 96 records (8 per state × 12 states), 0 rejected.** Passes
`schema/validate.py --strict` clean (96/96, 0 errors/0 warnings). **Window length: min 20,
median 31, max 38 real weeks.** Domain: agriculture (US). Original 7 states scouted
2026-07-25/26 (Iowa, Kansas, Minnesota, Indiana, Nebraska, Illinois, Ohio); 5 more states
scouted and added 2026-07-26 (Wisconsin, Missouri, Michigan, Pennsylvania, Kentucky) — see
`docs/scouting_build_queue.md` and the "second rollout" notes below.

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
- **Channels per record** (corn states): `corn_pct_{planted,emerged,silking,dough,dented,
  mature,harvested}`, `corn_condition_pct_{good,excellent}`, `days_suitable_per_week` — 9
  channels, `freq: "1w"`. Kansas (winter wheat) swaps the stage set for
  `winter_wheat_pct_{planted,emerged,jointing,headed,coloring,mature,harvested}` +
  condition + days-suitable, 10 channels.
- **Per-channel nulls are real and expected — verified against the raw source, not a bug.**
  Each growth-stage channel is a cascading S-curve: NASS stops surveying "percent planted" once
  it saturates near 100% (e.g. Iowa corn 2003: real rows only exist 04-13→06-01, ending at 99% —
  no more rows published after), then "percent emerged" takes over, then "silking," etc. Any
  single channel is null outside its own real reporting window, but across a record's full
  window at least one channel has real data every week — confirmed directly against
  `.cache/progress_condition_state_weekly.tsv`, not inferred.
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
    {"values": [19.0, 64.0, 79.0, 94.0, "... 24 more real weekly values ..."], "unit": "corn_pct_planted", "freq": "1w"},
    {"values": [null, null, 38.0, 68.0, "..."], "unit": "corn_pct_emerged", "freq": "1w"},
    {"values": [2.4, 5.1, 6.2, null, "..."], "unit": "days_suitable_per_week", "freq": "1w"}
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

- **Scale not yet fully measured.** This demo caps at 8/state to show breadth; real per-state
  depth ranges 8-29 clean-text years × ~30-45 real weeks/season. A full run (`max_records:
  null`) across these 12 states, then all ~40-44 states that run a real weekly program, is needed
  to pin down the exact count (scouted estimate: ~25-28k at full 40-44-state scope — see
  `docs/scouting_build_queue.md`). Only 12 of the real ~40-44 states are wired up so far.
- **Only 12 states implemented.** Adding a sibling state is mostly a `StateConfig` entry in
  `scripts/state_sources.py` (base folder, commodity/stage channel list, clean-text start
  year) — the fetch/extract/pairing pipeline is shared. (Kentucky needed one small addition,
  `year_folder_fmt`, for its nested `cw{yy}/` folder convention — see above.)
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
