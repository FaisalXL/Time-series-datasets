# UK ONS Statistical Bulletins + Indicator Series → CPT

> **Status: full-scale build shipped 2026-08-12 — 1,014 records, 1 family, 136 editions.**
> `schema/validate.py --strict` clean (1014/1014, 0 errors / 0 warnings). Banked to
> `cpt_corpus/packages/61_ons_statistical_bulletins/`.
>
> **Read [What did not work](#what-did-not-work-and-why-it-matters-more-than-what-did) first.**
> The scale estimate in the scouting note was ~15,000 records across the federation. The
> enumeration turned out to be *larger* than estimated (5,788 editions, not 2,979) and the
> alignment turned out to be *much* rarer: of 72 candidate families tested, **one** recites its
> own series in a way that survives a coincidence control. That result is the main finding of
> this build, and it is measured, not assumed.

**What it is:** one record = **one contiguous run of paragraphs** from one analytical section of
one ONS statistical bulletin edition — that run's own **verbatim** prose paired with the trailing
36-month multi-channel window of the indicator series the bulletin reports on.

```
"The Consumer Prices Index including owner occupiers' housing costs (CPIH) rose by 2.8% in the
 12 months to June 2026, down from 3.0% the previous month. ... The Consumer Prices Index (CPI)
 rose by 2.6% in the 12 months to June 2026, down from 2.8% the previous month. ..."
                                    ↕  evidence-checked, per claim
 cpih_annual_rate_00_all_items  2026-06 = 2.8    2026-05 = 3.0
 cpi_annual_rate_00_all_items   2026-06 = 2.6    2026-05 = 2.8
```

| | |
|---|---|
| **Records** | **1,014** (388 `recites` / 626 `describes`) |
| **Domain / region** | macro / `GB` |
| **License** | Open Government Licence v3.0 → tagged `cc-by-4.0` (ONS states OGL v3 is interoperable with CC BY 4.0); true grant + attribution in `meta.true_license` / `meta.attribution` |
| **Alignment** | `recites` 388 / `describes` 626 — assigned per record by evidence, never by assumption |
| **Evidence** | 1,267 attributed claims, **1.25 per record** (acceptance gate is ≥ 1.0) |
| **Freq / depth** | `1M`, 36-month window, 4 channels, **0 nulls**, 136 editions spanning 2015-03 → 2026-06 |
| **Text** | verbatim; median 408 tokens, max 499, **0 over the 500-token cap** |
| **Access** | keyless, no API key, no login. ONS rate-limits hard → shared-state paced fetch |

---

## What did not work, and why it matters more than what did

The demo shipped 28 records from two **hand-wired** families and already knew one of them was
broken: `retailsales` scored 0.25 evidence/record because two of its three hand-picked CDIDs were
the wrong variant of the right concept (the bulletin quotes an online-sales share of 29.4% while
the wired CDID `j4mc` reads 28.3%). Hand-mapping ~500 families is both infeasible and precisely
the step that was wrong, so this build replaced it with discovery from the prose's own figures.

That worked — and then it kept finding that its own output was coincidence. Each fix exposed the
next problem, and the sequence is the substance of this package:

| stage | measured result |
|---|---|
| naive value match, all ~4,000 series in the dataset | **259,303** claim–series matches; month-shifted control **189,344**. Discriminated nothing |
| typed claims only (2,295 of 2,815 "claims" were years, list indices, "12 months") + naming rule | CPI **8.3×** over control |
| generalize to 8 diverse families | `publicsectorfinances`: **0 matches from 794 claims** — units are `m`/`M`/`bn`, so `£16.0 billion` was compared against 16e9 while the series held 16,000 |
| fix unit scales; calibrate naming against the family's own prose vocabulary | recall up, but `uktrade` acquired "Trade in Goods: **Malta**: Imports" and `indexofproduction` "Manufacture of **Soap & Detergents**" |
| require **identifiability** — a figure matching 40 series identifies none of them | junk down, but "Trade in Goods: **Hong Kong**: Exports" still cleared a 6×-over-control bar |
| **Poisson test against each channel's own control, Bonferroni-corrected** by the number of series tested | **CPI only.** All junk rejected |

The last step is the one that matters. A fixed ratio bar cannot serve these datasets, because the
number of chances to get lucky differs by orders of magnitude: `uktrade` tests 1,567 monthly
series against 991 claims and its *average* series matches the shifted control **4.3 times**,
while CPI's averages **0.032**. That is a multiple-comparisons problem, and the control already
measures the null — so each candidate channel is tested against `Poisson(its own coincidence
rate)` at α = 0.001 after correcting for the number of series tested.

Two further hypotheses for recovering the other families were tested and **refuted**:

- **More editions.** Sampling 32 editions of `uktrade` instead of 8 raised claims 3.1× (991 →
  3,119) but raised the coincidence rate 3.6× (λ 0.146 → 0.525). Still zero channels. Coincidence
  scales with claims exactly as fast as signal does — so no amount of crawling fixes this. (This
  saved ~3 hours of throttled fetching.)
- **Derived series.** Adding month-on-month change, %m/m, %y/y and 3m-on-3m transforms of every
  level series (1,567 → 7,827 candidates for `uktrade`) produced **zero** passing channels for
  `uktrade`, `indexofproduction` and `publicsectorfinances`.

**Why CPI is different.** Its bulletins recite the headline series' values to one decimal at the
months they name. Trade, public-finances and GDP bulletins mostly quote *changes*, *contributions*,
*fiscal-year-to-date totals* and *£ differences* — quantities that are not the stored series'
values at those months. The near-misses show this directly: their whole-search-space real/control
ratios are **0.88–1.69**, i.e. at or below the coincidence floor, and `producerpriceinflation` is
**0.0** — zero real matches from 441 claims.

### Full discovery result

72 families in CDID-dataset subtopics (61% of all bulletin editions), all tested:

| status | families | meaning |
|---|---:|---|
| `ok` | **1** | channels verified against the control |
| `no_channels` | 33 | claims found, none survived the significance test |
| `no_monthly_series` | 26 | the family's dataset holds no monthly series (quarterly/annual only) |
| `too_few_claims` | 12 | too little typed prose to test |

The four surviving channels, with the significance actually achieved:

| CDID | title | claims | months | control | p (Bonferroni) |
|---|---|---:|---:|---:|---:|
| `l55o` | CPIH ANNUAL RATE 00: ALL ITEMS | 14 | 7 | 0.00 | < 1e-15 |
| `l564` | CPIH ANNUAL RATE: Services | 6 | 3 | 0.00 | 2.2e-09 |
| `d7nm` | CPI ANNUAL RATE: Goods | 5 | 3 | 0.00 | 5.0e-07 |
| `d7g7` | CPI ANNUAL RATE 00: ALL ITEMS | 10 | 5 | 0.75 | 3.9e-05 |

Four verified channels replace the demo's 18 hand-wired ones. That is the trade this build makes:
fewer channels, each of which the prose demonstrably recites.

---

## Scale — enumerated from the live site

| metric | scouting note (Wayback CDX) | this build (ONS search API) |
|---|---:|---:|
| bulletin **families** | 230 | **495** |
| distinct **family-editions** | 2,979 *(lower bound)* | **5,788** |
| enumeration cost | ~600 requests | **6 requests** |

CDX only sees what the crawler happened to archive; the live API is authoritative and
`previousreleases` pagination independently agrees with it (130 editions for
`consumerpriceinflation` against CDX's 129).

**Depth is not shippability.** The two deepest families on the site —
`deathsregisteredweeklyinenglandandwalesprovisional` (273 editions) and
`economicactivityandsocialchangeintheukrealtimeindicators` (249) — publish **no dataset CSV** and
therefore have no series to align to. Crawling them first cost 32 minutes for zero records, which
is why `crawl.py --triage` now checks dataset availability before fetching editions.

**Coverage actually attempted, stated plainly:** families with ≥5 editions (176 of 495, holding
91% of editions) were in scope; of those, the 72 in a CDID-dataset subtopic were triaged and
tested. The 319 single/shallow families (501 editions, 9%) and the 104 families outside those
subtopics were **not** tested, and are logged rather than silently dropped.

---

## The three design decisions

### 1. Split, don't cut

An ONS bulletin runs ~44,000 chars ≈ 11,000 tokens — 22× the 500-token cap. The demo kept only
the single densest paragraph run per section and discarded the rest (measured compression 3.4×,
so ~70% of real first-party prose thrown away). Truncating a `recites` record also orphans the
numbers that fall after the cut.

Sections are chunked into **consecutive whole-paragraph runs**, each its own record. Measured:
1,634 chunks from 136 editions → 1,014 records after gates, **7.5 records per edition** where the
demo produced ~5 *and* discarded most of the section. A paragraph longer than the cap is divided
at sentence bounds rather than truncated, so no sentence is lost and nothing exceeds the cap
(max shipped: 1,998 chars = 499 tokens).

### 2. Channels are discovered and proven, not declared

`discover_channels.py` reads every series in the family's dataset (one CSV: `mm23.csv` is 4,053
series with full history, one fetch instead of one per CDID), keeps only those whose values match
the figures the prose quotes at the months the prose names, and then has to survive the control.
The naming rule has three token classes because they behave differently:

| class | examples | rule | why |
|---|---|---|---|
| **measure** | `cpi`, `cpih`, `rpi` | must match **exactly** | CPI and CPIH are different series; `\bCPI\b` cannot match inside "CPIH", so this is decidable — and it was the #1 false match |
| **restriction** | `excluding`, `core`, `contribution` | must be named — **in both directions** | a title saying "Excluding energy" may only be credited to a clause that says so, **and** a clause saying "the **core** CPIH rate" may not be credited to the plain headline channel |
| **concept** | `housing`, `water`, `gas` | ≥ 60% coverage, calibrated by the family's prose vocabulary | requiring *any* one token let a trade bulletin acquire a vegetables-and-fruit channel off the word "goods"; requiring *all* of them rejected `Public sector net borrowing, excluding public sector banks` for a clause a reader would find unmistakable |

The reverse restriction guard is not decoration: it was missing from the first full build, and
**all 20 surviving superlative "contradictions" were caused by it** — "The core CPIH annual
inflation rate was 4.1%" being credited to headline CPIH. Adding it moved 27 records from
`recites` to `describes`, which is the honest direction.

### 3. The period comes from the document, never the slug

Edition slugs come in **14 shapes** (`june2026`, `aug2017`, `6august2026`, `weekending12may2023`,
`2019to2020`, `quarter1julytosept2021`). Each edition's reference period is read from its own h1
("Consumer price inflation, UK: June 2026") and cross-checked against the slug.

This was not theoretical. **8 shipped editions are slugged with their publication date**
(`2015-07-14`) while covering the month before (2015-06) — the FHFA #59 trap, live in this
source. Using the slug would have paired those with the wrong month's window. Final tally: 128
editions where slug and dateline agree, 8 where the slug encodes no month, **0 genuine
disagreements**.

---

## Evidence: how `recites` is earned

A figure becomes evidence only if **all** of these hold:

1. the clause **names** the series under the three-class rule above, in both directions;
2. the channel's value at the month **the clause itself names** matches, at the precision quoted
   (`2.8` → ±0.05, `2.85` → ±0.005 — a figure quoted to one decimal is not matched as if exact);
3. the unit is compatible — a `%` claim cannot match a weights series held in "Parts per 1000",
   of which `mm23` holds hundreds in the same numeric range;
4. it is **identifying** — a figure that fits several channels at once is recorded as ambiguous
   rather than credited to whichever sorted first.

Yield on this build: **1,267 attributed / 4,981 not-named / 1,216 value-mismatch / 2 ambiguous /
2 no-data**. Deliberately conservative: most figures in a CPI bulletin refer to sub-series no
four-channel set holds, and silence is cheaper than a polluted evidence array.

**Superlatives are checked, not trusted** — 383 claims, verdicts:

| verdict | n | |
|---|---:|---|
| `unchecked_no_named_channel` | 283 | the paragraph names no channel we hold |
| `consistent` | 26 | verified against held history |
| `contradicted_weak_attribution` | 19 | attributed only at paragraph scope → reported, never acted on |
| `unchecked_scope_qualifier` | 14 | claim scopes itself to a narrower series ("National Statistic") than we hold |
| `ambiguous_multi_channel` | 12 | two channels could own the claim |
| `attribution_rejected_back_reference` | 2 | the prose's own "when it was X%" disagrees with this series → the claim is about a series we do not hold |
| `contradicted` | **2** | 0.5% of claims, flagged not dropped |

Three of those verdicts exist because a first pass accused the source and was wrong each time:
the back-reference test ("lowest since October 2021, **when it was 3.1%**" against a series
reading 3.8 proves the *attribution* is wrong, not the source); the change-basis test ("the
largest ever **increase**" is a claim about the first difference, not the level); and the scope
qualifier. `drop_on_superlative_contradiction` stays **off by default**.

---

## The window and the series

Windows end at the edition's own reference period — not a stride we impose. But the anchor cannot
be the reference period unconditionally: a labour-market bulletin published *for July* reports
rolling quarters ending in **May**, so no July point exists and anchoring at July silently
emptied the window and dropped the whole family. The anchor is the latest month ≤ the reference
period at which a majority of channels hold data, recorded in `meta.window_anchor_rule` (all
1,014 shipped records anchor at `ref`). Gaps are explicit nulls, never imputed — this build has
**0 nulls across 4 channels × 36 points × 1,014 records**.

### Vintage — measured, and a non-issue here

ONS exposes **132 historical vintages** per dataset (`…/current/previous/v135/mm23.csv`), so
pairing each edition with its contemporaneous vintage is possible. It is also unnecessary for
this family: vintage **v70** differs from the current vintage in **0 of 1,172** shared monthly
points for the shipped channels; only the oldest (`v4`) differs at all (6.68%, consistent with a
rebasing). CPI annual rates are not revised in practice.

The build measures the *effect* rather than the cause, for free, by bucketing evidence yield by
edition year:

| edition year | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| evidence/record | 0.58 | 0.86 | 1.16 | 1.12 | 1.29 | 1.01 | 0.85 | 0.95 | 1.77 | 1.64 | 1.77 | 1.50 |

There is no cliff, so the current vintage is sound across the full 11-year span. The gentle
decline into the past is a **prose** effect, not a data effect: a 2019 CPI edition carries 17
typed figures across 13,301 prose chars, a 2026 edition 79 across 31,785 — and 5 of the 2019
edition's 8 sections are boilerplate. Older bulletins are shorter and put more in tables.

### Near-duplicates

ONS reuses sentence templates month to month. Exact duplicates are dropped by hash (1), and
`text.max_similarity: 0.93` bounds the rest (59 dropped). Realised similarity within a
same-section, same-chunk-index group: **p95 = 0.956** before the gate, i.e. the gate is doing
real work rather than being decorative.

---

## Access: ONS throttles hard, and a throttle is not an answer

Measured: **~5 requests then HTTP 429**, and the penalty persists — a second burst 30 s later was
still 429 on every request. Sustained 2.0 s spacing holds; effective rate under backoff ≈ 3 s.

Three things are encoded in `scripts/onsfetch.py`, each because of an observed failure:

- **The limiter is cross-process**, held in a lock file. Two politely-self-pacing processes still
  collide: running the crawler and a discovery sweep together drove the shared gap to **12.3 s**.
- **Decay must undo a spike.** At 0.98 per success it took ~90 clean fetches to walk back one
  429, so the gap ratcheted up and stayed there. It is 0.90 now.
- **A 429 is never cached.** Its body is 17 bytes; caching it would bake "the source has nothing
  here" into the corpus. Only 200s and real 404s are cached, an exhausted retry chain returns 429
  so the caller records *unknown*, and `build_census.py` / `build_dataset_index.py` refuse to
  write a partial index for the same reason.

---

## The LLM question

The 2026-08-03 meeting made an LLM summarizer available; Xinyue separately rejected semi-synthetic
text. Chunking removed the reason to want one: there is no longer a "which part do we keep"
decision, because **every** part ships as its own record. Text selection is fully deterministic
and needs no API key.

**Abstractive summarization remains blocked on the schema, not on this script.**
`schema/validate.py` allows `text_quality ∈ {"real","generated"}` — there is no
`llm_summarized` value, and `"generated"` would wrongly conflate a grounded summary of a real
document with fully synthetic text. Setting `text.abstractive_summary: true` raises a clear error.
Getting that vocab added is a **shared-schema decision (Defu)**, flagged separately.

---

## Pipeline

```bash
python scripts/build_census.py                       # 495 families / 5,788 editions (6 requests)
python scripts/build_dataset_index.py                # 43 CDID time-series datasets / 22 subtopics
python scripts/crawl.py --mode triage --min-editions 5 --dataset-subtopics-only
python scripts/discover_channels.py --dataset-subtopics-only --min-editions 5
python scripts/crawl.py --mode full                  # all editions of families that verified
python scripts/build_cpt_jsonl.py                    # -> output/ons_bulletins_cpt.jsonl
```

`build_dataset_index.py` exists because a family's series are not always where its bulletin points:
every dataset `uklabourmarket` links is **xlsx-only**, while its CDID series sit in
`labourmarketstatistics/current/lms.csv`. Probing all 3,914 ONS dataset pages would cost ~3 hours
at this throttle; ONS names the CDID datasets "… time series" and there are only 59, of which 43
carry a CSV.

The builder honours the corpus runner's config aliases (`output.max_records`,
`output.output_path`, `output.report_path`), so `cpt_corpus/run_full.py` drives it unchanged.
Workers write JSONL shards and return counts — returning records through process IPC would move
gigabytes on a full federation build.

---

## Open items

- **Only 1 of 72 tested families verified.** The remaining volume is not reachable by value
  verification. Two options exist and both are scope decisions, not bugs: (a) pair the
  unverified families' prose with structurally-chosen headline channels as `describes`-tier
  records — legitimate under SCHEMA §7 but weaker alignment, and it needs a defensible rule for
  "headline"; (b) leave them out. This build does (b).
- **26 families were rejected for having no monthly series** — their datasets are quarterly or
  annual. `onslib.window` already takes a bucket, and `coverage_period` already returns
  `YYYY-Qn`, so quarterly support is a contained extension that would put those families back in
  scope. Not done here.
- **104 families outside CDID-dataset subtopics were never tested**, and neither were the 319
  families with <5 editions. Both are enumerated in `census.json`.
- **Two superlative contradictions remain** (0.5% of claims), flagged in
  `meta.superlative_flags`, not dropped.
