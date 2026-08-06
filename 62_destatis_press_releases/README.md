# Destatis (Germany) English Press Releases → CPT

> ## ⛔ STATUS: SCOUTED VIABLE — **BUILD BLOCKED on a free GENESIS API key. Nothing here is shippable.**
>
> The **text side, license and enumeration all verified GOOD.** The **series side does not work
> keyless.** Measured `evidence_per_record = 0.25`, and hand-auditing shows **both of those matches
> are false**, so the true figure is **0.00**. Output is deliberately named
> `NOT_SHIPPABLE_negative_result.jsonl` so a `datasets/*/output/*_cpt.jsonl` corpus glob cannot
> pick these 8 records up.
>
> **To unblock:** register a free Destatis GENESIS-Online account and point the channel block at
> GENESIS instead of Eurostat. That is a server-side credential task, not a dead end.

## Why this source is worth the key

Text quality is the best of any statistical office scouted so far, better than ONS #61 per token:

| measure | Destatis release | (for comparison) NIFC IMSR, killed |
|---|---:|---:|
| percent-figures per release | **61** | — |
| prose blocks | 18 | 96 |
| blocks with **zero digits** | **27%** | 56% |
| prose length | ~7,970 chars ≈ **1,991 tokens** | — |

And it recites **multiple consecutive months of its own series in one sentence**:

> *"The inflation rate in Germany … was +2.3% in June 2026. The rise in overall consumer prices
> therefore slowed again, after having stood at +2.6% in May 2026 and +2.9% in April 2026."*

That is three independently-checkable claims against three different points of one 36-month window —
a property no other package in the corpus has.

## Measured scale (Wayback CDX — the site cannot be walked)

Destatis's own press listing is JS-driven and its archive endpoints 404, so CDX is the enumerator.

| metric | measured |
|---|---:|
| distinct **English** releases, 2010→2026 | **2,845** (a floor — Wayback under-captures) |
| peak years | **562** (2021), **536** (2022) |
| recurring indicator families | **~12** (421 industrial production, 611 consumer prices, 51 foreign trade, 811 national accounts, 61241/61281 producer & import prices, 45212 turnover, 12621 unemployment, …) |
| subject 611 (consumer prices) alone | **433** releases, 2012→2026 |
| ⇒ realistic records | **~3k–8k** |

~24 CPI releases/yr because each reference month gets a **provisional flash estimate** *and* a
**final confirmation**. Note the flash releases say *"is expected to be"* — those are forecasts, so
the WASDE #41 / GAIN #58 forecast-not-measured convention applies to roughly half the stream.

## License — clean, both sides

- **Press releases (the text):** *"Reproduction and distribution, also of parts, are permitted
  provided that the source is mentioned."* Attribution-only.
- **GENESIS database (the series):** **Data licence Germany – attribution – version 2.0**
  (DL-DE-BY-2.0).

No NC clause, no permission gate. Tagged `cc-by-4.0` closest schema fit; real terms in
`meta.true_license`. Required attribution is carried in `meta.attribution`.

## The blocker, in detail — three series routes, two proven dead

**1. GENESIS-Online (Destatis's own series) — needs a free registered key. ✅ the right route.**
Keyless REST verified to return **HTTP 405**; the web UI is a JS app. Helpfully, each release
*names the exact GENESIS tables it draws on* (`61111-0004`, `61111-0006`, `61121-0002`,
`61121-0006`), so no series ID has to be guessed — a real advantage over ONS #61, where CDIDs had to
be probed one at a time.

**2. Eurostat keyless HICP for Germany — ❌ PROVEN INVALID. This is the negative result.**
Eurostat is keyless and deep (verified: 348–360 monthly points back to 1996–97, 8 channels), so it
looked like a clean substitute. It is not:

- The release's prose is about the **national CPI**, not the harmonised index. The release itself
  carries a paragraph explaining that *"the consumer price index (CPI) and the HICP differ in terms
  of coverage, methodology and weighting."* June 2026: **CPI +2.3% vs HICP +2.4%.**
- The HICP figure appears only in the machine-styled key-value bullet block, which is a data dump
  rather than prose and is dropped by the prose filter.
- Result: `evidence_per_record` **0.25**, and both matches are false on inspection —
  a CPI restaurant/accommodation figure (+5.6%) that coincidentally equalled HICP CP11, and a CPI
  food figure (−0.2%) that coincidentally equalled HICP CP01. **True ev/rec = 0.00.**
- Eurostat's German HICP **also lags**: ends 2025-12, last updated 2026-02-06 (verified — not a
  query artifact), so the newest releases have no pairable point regardless.

*The guard is what caught this.* Per-channel keywords evaluated on the figure's **own clause** plus
`reject_terms` refused to credit national-CPI figures to HICP channels. Without it this package
would have shipped a few hundred plausible-looking fake-aligned records.

**3. The release's own COICOP table — ❌ high risk, not recommended.**
Each release embeds a table, and 2022+ flash releases carry **four consecutive months** per
subindex, which is genuinely attractive. But the layout is not stable across eras — verified on six
releases:

| era | table shape |
|---|---|
| 2018 | `Index 2010 = 100 \| Change on previous year \| Change on previous month` (2 tables, 20 rows) |
| 2020 | `Weighting \| 2019 A \| December 2019` (annual + single month, 38 rows) |
| 2022 / 2024 / 2025 | `Weight \| September \| October \| November \| December` (4-month panel, ~10 rows) |

Three+ schemas, **plus base-year rebasing** (2010=100 → 2015=100 → 2020=100) that must be spliced,
plus ~10 releases stitched per 36-month window. That is the GAIN #58 layout-variance problem
compounded by a rebasing problem. Also: pre-~2016 release URLs from CDX **404 on the live site**
(2015 verified), so older editions need Wayback fetches — the FHFA #59 shape.

## Other verified findings

- **Reference month comes from the release title**, not the URL — the slug (`PE25_262_611`) carries a
  sequence number and a subject code but no date. So there is nothing date-like in the URL to
  mis-trust, unlike FHFA #59 where the URL month was the index month + 2.
- **Slugs are NOT constructible.** The subject-code suffix is unpredictable; CDX supplies full URLs.
  I initially invented three plausible slugs and all three 404'd — enumerate, never guess.
- Some releases are re-issued as `CORRECTION:` editions; a full run needs a dedup/supersede rule.
- The month-attribution logic works well on this source: Destatis names its months explicitly, and
  the matcher correctly resolved *"the first month since February 2015 (−0.2%)"* to `2015-02`.

## Run

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run
```

Abstractive summarization is refused with a clear error: `schema/validate.py` has no
`llm_summarized` `text_quality` value, and `"generated"` would mislabel a grounded summary of a real
document as synthetic. Text selection is contiguous whole-paragraph and stays 100% verbatim.

**Attribution:** Text: © Statistisches Bundesamt (Destatis). Series: Eurostat.
