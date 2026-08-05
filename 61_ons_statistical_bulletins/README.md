# UK ONS Statistical Bulletins + Indicator Series → CPT

> **Status: Demo built 2026-08-04 — 28 records, 2 bulletin families, 18 + 3 channels.**
> `schema/validate.py --strict` clean (28/28, 0 errors / 0 warnings).
> Scout-lane demo. The server agent runs the full federation.

**What it is:** one record = **one analytical section of one ONS statistical bulletin edition** —
that section's own **verbatim** prose paired with the trailing **36-month multi-channel window** of
the exact indicator series the bulletin reports on.

```
"The Consumer Prices Index including owner occupiers' housing costs (CPIH) rose by 2.8% in the
 12 months to June 2026, down from 3.0% the previous month. ... The Consumer Prices Index (CPI)
 rose by 2.6% in the 12 months to June 2026, down from 2.8% the previous month. ..."
                                    ↕  evidence-checked, per claim
 cpih_annual_rate_pct  2026-06 = 2.8      cpih_annual_rate_pct  2026-05 = 3.0
 cpi_annual_rate_pct   2026-06 = 2.6      cpi_annual_rate_pct   2026-05 = 2.8
```

| | |
|---|---|
| **Domain / region** | macro / `GB` |
| **License** | Open Government Licence v3.0 → tagged `cc-by-4.0` (ONS states OGL v3 is interoperable with CC BY 4.0); true grant + required attribution in `meta.true_license` / `meta.attribution` |
| **Alignment** | `recites` 18/28, `describes` 10/28 — assigned by evidence, never by assumption |
| **Freq / depth** | `1M`, 36-month window (sources hold **450–474 months**, back to 1989) |
| **Text** | 269–493 tokens (median 427), **0 over the 500-token cap**, 100% verbatim |
| **Access** | keyless, no API key, no login. ONS rate-limits → paced fetch + 429 backoff |

## Why section-level records (this is the design decision that matters)

One ONS bulletin is **~43,800 chars ≈ 10,950 tokens** across 12 H2 sections — **22× the 500-token
cap**. One record per bulletin would discard ~95% of real first-party prose. Each H2 section is a
self-contained analytical passage about its own sub-series, so section-level records multiply scale
**without duplicating prose**.

Measured: 5 analytical sections survive per CPI edition (boilerplate excluded), so
**~5 records per bulletin edition**.

## Scale — measured with Wayback CDX, not estimated

| metric | measured |
|---|---:|
| bulletin **families** across ONS's 4 themes | **230** |
| distinct **family-editions** archived (lower bound) | **2,979** |
| families with ≥60 editions (deep monthly/quarterly) | **17** (1,846 editions) |
| deepest families | `publicsectorfinances` 130, `uklabourmarket` 129, `consumerpriceinflation` 129, `retailsales` 129, `indexofservices` 128, `uktrade` 127, `indexofproduction` 126 |
| × ~5 analytical sections/edition | **≈ 15,000 records** |

**Cross-validation of the method:** CDX measured `averageweeklyearningsingreatbritain` at **89
editions**; `docs/next_candidates.md` A7 independently estimated "~85+". The two agree, which is
why the 2,979 figure is quoted as a lower bound rather than a guess. This package **subsumes A7** —
ONS AWE is one family inside the federation, not a separate ~85-record package.

**Enumeration is solved by construction** — the edition slug is `{monthname}{year}` (`june2026`),
so a full run builds URLs directly. This is exactly what FHFA #59 lacked (unpredictable slugified
headlines, ~66 records). Verified live: `200` at `june2016`, `404` at `march2015` — the clean-URL
floor sits between those two; the server should pin it.

## The LLM question — what is and is not allowed here

The 2026-08-03 meeting made an LLM summarizer available; Xinyue separately rejected semi-synthetic
text. Both hold, so this package draws the line explicitly:

| mode | what it does | `text_quality` | status |
|---|---|---|---|
| `head` | leading paragraphs until the cap | `real` | baseline |
| `numeric_density` **(default)** | the **contiguous** paragraph run with the most verifiable claims, in source order | `real` | **ships today** |
| `llm_extractive` | an **LLM chooses which contiguous run to keep**; kept text is shipped verbatim | `real` | ready, needs `ANTHROPIC_API_KEY` |
| abstractive summary | rewrite the source into ≤500 tokens | — | **blocked, see below** |

`llm_extractive` is the same grounded-and-extractive shape as the FNSPID B1 relevance judge: the
LLM only *selects*, it never *writes*, so text stays 100% verbatim and `text_quality` stays `real`.
No sign-off needed.

**Abstractive summarization is blocked on the schema, not on this script.**
`schema/validate.py` allows `text_quality ∈ {"real","generated"}` — there is **no
`llm_summarized` value**, and `"generated"` would wrongly conflate a grounded summary of a real
document with fully synthetic text. Setting `text.abstractive_summary: true` raises a clear error
rather than silently mislabelling. Getting that vocab added is a **shared-schema decision (Defu)**,
flagged separately, not done here.

Compression achieved *without* any abstraction: sections up to **6,642 chars → ≤1,963 chars
(3.4×)**, all verbatim.

## Evidence: how `recites` is earned

A figure becomes evidence only if **all three** hold:

1. the channel's keyword appears in the figure's **own clause** — not a neighbouring one;
2. the clause carries no **disqualifying modifier** pointing at a series we do not hold
   (`core`, `excluding`, `goods`, `services`, `contribution`, …);
3. the channel's value at the month **the clause itself names** rounds to the figure.

Anything else is left unattributed and counted in `meta.evidence_rejected`. Yield on this build:
**59 attributed claims, 177 rejected** — deliberately conservative, because a polluted evidence
array inflates apparent quality.

### Per-family acceptance gate — `evidence_per_record`

`run_report.json` reports this per family. It is the number that tells you whether a family's
**CDIDs are the right variant** of the figures its prose quotes:

| family | records | recites | evidence/record | verdict |
|---|---:|---:|---:|---|
| `consumerpriceinflation` | 20 | 16 | **2.95** | channels verified |
| `retailsales` | 8 | 2 | **0.25** | ⚠️ 2 of 3 CDIDs are the wrong variant |

The retail family is left in deliberately, as a **worked example of the failure mode**: the
bulletin quotes an online-sales share of **29.4%** for June 2026 while the wired CDID `j4mc` reads
**28.3%** — a different variant of the same concept, not a data error. `j5ec` (month-on-month %) is
correct and produces the only 2 matches. **A family is not ready until `evidence_per_record ≥ 1.`**

## Six real bugs found and fixed during this build

All were found by checking output against source, not by reasoning about the design.

1. **Decimal points shredded every sentence.** A naive `[^.!?]+` sentence splitter treats the `.`
   in `2.8%` as a sentence end, so every figure was cut in half and the first evidence pass
   returned **0 matches with 0 rejections** — a silent total failure, not a crash. Sentence breaks
   now require terminator + whitespace + opening capital.
2. **4 of 9 evidence matches on the very first record were FALSE.** `0.2%` (a *CPIH* monthly claim)
   was credited to *CPI* monthly at the *wrong month*; `2.8%` was **Core** CPIH; `1.7%` was CPIH
   **goods** credited to *food*; `2.6%` was **Core** CPI. Cause: a symmetric ±140-char keyword
   radius reached into neighbouring sentences, and the bulletin discusses core/goods/services
   series no channel set holds. Fixed by clause-scoped attribution + the disqualifying-modifier
   guard. Same failure class that killed openFDA/NHTSA/CFPB and was caught pre-ship on RBNZ #60.
3. **The fix over-corrected.** Adding `owner occupiers` to the disqualifying list silently killed
   *every* headline CPIH claim, because CPIH's own full name is "…including owner occupiers'
   housing costs". **A guard that fires on a series' own name is a false-negative generator.**
4. **Chart chrome glued into the prose** — "Download this chart Figure 3: …" and bare figure
   captions. Exactly the leftover-page-chrome bug RBNZ #60 shipped and fixed. Killed by a prefix
   match **plus** a structural rule: a real prose paragraph ends in terminal punctuation.
5. **Superlative auto-drop silently deleted a good record.** Every "contradiction" was a false
   positive from mis-attribution — COICOP-04 `housing` absorbing "housing and household services",
   "owner occupiers' housing costs" and "domestic heating oil"; plus the retail wrong-variant case.
   `drop_on_superlative_contradiction` is now **off by default**, and a drop additionally requires
   unambiguous *clause*-scope attribution. GAIN #58 could drop safely because its channels were
   value-verified; ONS references far more series than any channel set holds.
6. **`previousReleases` 404s — the real path is `previousreleases`,** all lowercase. Same
   case-sensitivity class as FHFA #59's lowercase "tables and graphs" anchor.

## Known risks and open items

- **Near-duplicate tail.** Consecutive editions of the same section reach **0.806** similarity
  (ONS reuses sentence templates; the numbers differ). Nothing duplicates now, but at ~130
  editions/family the tail needs a ceiling — `text.max_similarity: 0.93` is wired and active.
- **Vintage drift.** Series come from the **current** ONS vintage; the bulletin quotes its
  contemporaneous vintage. ONS revises, so an old claim can drift from live data — the same class
  documented for FHFA #59 and `ons_awe`. A full historical run should archive per-release vintages.
  The series API returns a per-point `updateDate`, which is a real handle for this.
- **`retailsales` channels need fixing** before that family scales (see the gate table above).
- **Two-month-ambiguity edge case.** A clause naming two months ("in June 2026, compared with …
  June 2025") allows either; attribution is correct here because the values differ, but coincident
  values would be ambiguous.
- **Sub-headings are dropped** as a side effect of the terminal-punctuation rule. They are verbatim
  and carry no numbers; recoverable if wanted.
- **`freq` token note.** This package uses **`1M` = one month**. Per `schema/SCHEMA.md` §3.2 `1m`
  is one **minute**. Three existing packages (#35, #41, #59) label monthly data `1m`; `FREQ_RE`
  accepts both so `validate.py` cannot catch it. Flagged separately — not fixed here.

## Run

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run
python scripts/build_cpt_jsonl.py                                # demo (28 records, cap 12 by default)
python scripts/build_cpt_jsonl.py --set output.max_records=null   # all configured editions
python scripts/build_cpt_jsonl.py --set text.selector=llm_extractive   # needs ANTHROPIC_API_KEY
```

Adding a bulletin family is a **config block, not code** — proven by the `retailsales` entry, which
required zero script changes.

**Attribution (required by OGL v3):** *Source: Office for National Statistics licensed under the
Open Government Licence v.3.0.*
