# NOAA Fisheries Stock Assessments (Stock SMART)

**Status: Full build — 845 records, 845/845 strict-clean (0 errors, 0 warnings).** Domain:
fisheries (US). Grew from the 50-record demo (16.9×). Source is exhausted for the
single-stock-report universe; the remaining headroom is the multi-stock omnibus reports, which
are **deliberately not taken** (measured — see below).

One record = **one (stock, assessment) pair** — Stock SMART's own unit. Text is that
assessment report's own **status narrative** about that stock (its `State of Stock` /
`Executive Summary` / `Stock Status` / `Catches` / `Recruitment` / `Exploitation status`
sections, verbatim). Series is that stock's own historical **catch, fishing mortality,
recruitment and abundance/biomass**, annual, ending on the assessment's terminal data year.

```
845 records · 36,550 timesteps · 123,199 datapoints · 2.62% nulls
median 39 years of history · 71.2% >=32 points · max 149 · 1872-2025
250 recites / 595 describes (measured; permutation control 3.2% vs 29.6% true -> +26.4 pp)
```

## Record shape

```json
{
  "text": "State of Stock: Based on this updated assessment, the summer flounder (Paralichthys\ndentatus) stock is not overfished and overfishing is not occurring… Spawning stock biomass\n(SSB) in 2024 was estimated to be 40,516 (mt) which is 83% of the biomass target… The 2024\nfully selected fishing mortality was estimated to be 0.35…\n\n<ts></ts>",
  "timeseries": [
    {"values": [21409.0, "…", 8465.0],  "unit": "catch_metric_tons",             "freq": "1y"},
    {"values": [0.782,   "…", 0.35],    "unit": "fmort_fully_selected_f",        "freq": "1y"},
    {"values": [82420.0, "…", 49649.0], "unit": "recruitment_thousand_recruits", "freq": "1y"},
    {"values": [30067.0, "…", 40516.0], "unit": "abundance_metric_tons",         "freq": "1y"}
  ],
  "alignment": "recites", "period_start": "1982-01-01", "period_end": "2024-12-31",
  "meta": {"stock_name": "Summer flounder - Mid-Atlantic Coast", "n_points": 43,
           "sections": ["state_of_stock", "projections", "special_comments"], "…": "…"}
}
```

That example is a real record: 40,516 and 0.35 are the terminal points of the abundance and
fishing-mortality channels, and the prose quotes both.

## Alignment - measured, not asserted

**Structural, 100% of records:** the window's terminal year *is* `period_end`; `period_start`
is the window start; every channel has exactly `meta.n_points` values; the terminal point is
non-null in at least one channel; the window never runs past the assessment's own stated last
data year.

**Tier is measured per record.** `recites` when the prose quotes a channel's terminal value
(0.5% relative tolerance, calendar-year-looking values excluded because assessment reports are
dense with years); otherwise `describes`. A **permutation control** in the #41/#08 style - the
same terminal values against a *different* record's prose - fires **3.2%** against a **29.6%**
true rate (**+26.4 pp**). Additional measurements:

| Check | Result |
|---|---|
| Prose quotes *some* value the record's series holds | 568/845 (67.2%) |
| Window's terminal year is named in the prose | 738/845 (87.3%) |
| …and is the latest non-projection year the prose names | 448/845 (53.0%) |
| Of all non-year figures in the prose, share present in this record's series | 2,633/25,490 (10.3%) |

⚠️ **Two honest caveats on those last rows.** (a) The 10.3% is *expected and not a defect*: an
assessment narrative quotes reference points, ABC/OFL recommendations, Mohn's rho, percentages
and model diagnostics alongside the four channels, so most figures in the prose are not series
values by construction. This is the opposite trade from `47_philadelphia_mbos`, which
clause-trimmed to a ~280-char snippet to reach 100%; here the text ships verbatim. (b) A
**per-channel direction check came out at 54.0% - i.e. inconclusive, at chance**. That is a
limitation of the *measurement*, not a demonstrated misalignment: a 3.7k-char narrative
describes a whole multi-decade history with many reversals, so counting direction words near a
channel's name cannot isolate "the recent move". It is reported rather than hidden; the
load-bearing evidence is the controlled recite rate and the five structural checks.

## What the full build changed (each because the demo's assumption was measured and failed)

1. **The `max_report_pages: 80` filter rested on a false premise and is gone.** The demo
   skipped reports over 80 pages on the theory that long reports are multi-stock omnibus
   documents. Measured: **107 of 274 sampled single-stock reports exceed 80 pages** (median 35,
   p75 185, max 2,445), so the filter discarded ~39% of genuinely per-stock reports - and it
   was the demo's binding constraint (169 of the 229 rows it examined died on it). Replaced by
   the **exact signal already in the source**: 1,410 distinct report files back 3,088 candidate
   rows, so **1,842 rows (59.7%) share a report file with a *different* stock**. Zero files are
   shared by one stock across years, so file-sharing identifies omnibus documents exactly.
2. **Text is the report's own narrative, not the whole PDF** (`scripts/repex.py`). The demo
   concatenated every page, so records carried tables of contents, numeric table dumps, figure
   captions, page furniture and references - and `<ts></ts>` was spliced after a running footer
   (*"…draft working paper for peer review only\n9"*) in essentially every record. Now
   `<ts></ts>` follows a complete sentence in **835/845 (98.8%)**.
3. **The window no longer truncates the deep channel.** The demo started every window at the
   *latest* channel start (max-of-mins), discarding **13,676 real year-observations** across the
   universe - 22% of assessments lost >=10 years and the worst lost **103**. Starts are now
   tried deepest-first under a **0.20 null budget** (`data.null_budget`): median depth 29 -> 31
   years universe-wide, assessments at >=32 points 1,192 -> 1,308, at a cost of 3.0% nulls
   instead of 1.0%.
4. **`period_end` was wrong in 16% of demo records** - it was stamped from the `Last Data Year`
   metadata field while the window actually ended earlier (model-derived channels stop 1-3
   years before the freshest catch data - the terminal-year problem in stock assessment). It is
   now the window's own terminal year, 100%.
5. **Extraction switched from `pdfplumber` to `pdftotext`** - ~45x faster (verified equivalent
   on prose: long sentences match 7/8, the exception being hyphenation), which is what makes a
   full pass over ~85k pages practical.

## Why the omnibus reports are dropped (1,842 rows, measured twice)

Pairing a multi-stock document with one stock's series is boilerplate reuse, banned by
`SCHEMA.md` "no fake scale". Two attempts to isolate each stock's own chapter were measured and
both failed the quality bar:

- **Density scoping** (find the pages where the stock's name is densest) returns the **figure
  and table appendix**, not the narrative - in a 600-page SAFE the name occurs most often in
  chart legends. Sample output was model-parameter dumps
  (`'param_init_bounded_vector' 'param_init_bounded_vector' …`).
- **Heading-anchored scoping** (`repex.locate_scope`, kept in the tree and reachable via
  `data.omnibus_mode: "scoped"`) produces genuinely good text when it fires, but fires on only
  **65 of 1,839 rows (3.5%)** - and among those, Ocean quahog and Tilefish both received the
  *same* generic SAW front matter, which is exactly the failure mode being avoided.

The dominant case is structural and unfixable by better parsing: the PFMC **Review of Ocean
Salmon Fisheries** (1,296 rows) maps **64 Stock SMART stocks onto ~28 sections** - §2.6 "Puget
Sound Chinook Stocks" alone covers 25 of them. The honest multiplier there is ~1 per section,
not 1 per stock. Same call as `41_wasde`'s sorghum/barley and `31_usdm_drought`'s ~3,500
merged-label sections.

## Reconcile - balances exactly, and the build raises if it doesn't

```
3,528 summary rows = 3,088 candidates + 349 no report link + 91 jurisdiction-excluded
                     + 0 missing ids                                                   OK
3,088 candidates   = 845 emitted + 1,842 omnibus + 261 short text + 96 no timeseries
                     + 19 not-a-PDF + 19 duplicate text + 4 short window
                     + 2 no text layer + 0 invalid                                     OK
```

**Exhaustion.** 427 stock entities and **3,528 distinct assessment rows** are the complete
universe: the summary export's `segIndex` parameter is **inert** - `segIndex=0/1/2` return
byte-identical payloads - so there is no pagination truncation to recover. All **1,431 distinct
report files fetched, 0 failures** (~8 GB). Assessment years span **2001-2025**; the 19
not-a-PDF payloads and 2 text-layer-less files are the only fetch-side losses.

## Known limits

- **Regional skew.** AFSC/NPFMC is 565 of 845 records (67%), because Alaska publishes one
  report per stock while PFMC bundles salmon into omnibus reviews. 188 distinct stocks.
- **Fallback extraction, 82/845 (9.7%)** - no canonical heading matched, so the record carries
  the document's leading prose (title-page abstract + opening paragraphs) filtered to real
  sentences. Still first-party and still about this stock.
- ⚠️ **Licensing: 28 records (3.3%) name a non-federal co-author** - Alaska Dept of Fish and
  Game (21), state fish-and-wildlife agencies, Florida FWC, universities - flagged per record
  in `meta.nonfederal_affiliation` so the call is reversible by filter, without a rebuild. The
  containing document is a federal publication (a Council SAFE report or a SEDAR stock
  assessment report), which is why `public-domain-us-gov` is the package label; this is the
  same question `31_usdm_drought` answered on its bylines and it wants an owner decision.
- Reports labelled *"draft working paper for peer review only"* are normal for NOAA's
  Management Track process - the operative document, not a quality defect.
- 21 records sit between 620 and 800 chars (`text.min_text_chars: 600`). Lowering that floor
  to 400 recovers roughly a further 40-60 records of thinner narrative.
- A cover-page drop-cap rendering artifact (`"R 2025\nEVIEW OF"` for `"REVIEW OF"`) appears on
  some title pages; every page after the cover extracts clean prose.

## Run

```bash
pip install -r requirements.txt                  # + poppler-utils, for pdftotext/pdfinfo
python scripts/fetch_reports.py <cands.json>     # optional: warm the ~8 GB PDF cache
python scripts/fetch_timeseries.py <cands.json>  # optional: warm the series cache
python scripts/build_cpt_jsonl.py                # full build (~6 min warm)
python ../schema/validate.py output/ --strict
python scripts/inspect_alignment.py output/noaa_stock_assessments_cpt.jsonl
```

Output: `output/noaa_stock_assessments_cpt.jsonl` (845) · `output/run_report.json` (stats,
reconcile, alignment) · `samples/example_output.jsonl`.
Source: [NOAA Stock SMART](https://www.fisheries.noaa.gov/resource/tool-app/stock-smart).
