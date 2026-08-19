# 58 — USDA FAS GAIN attaché reports → CPT world-knowledge records

**Status: FULL ARCHIVE BUILT + VERIFIED (2026-08-19). 1,523 records from 18,494 candidate reports,
1,523/1,523 pass `validate.py --strict` with 0 warnings.** `public-domain-us-gov` (17 U.S.C. §105),
so this ships rather than being held.

One record = a GAIN report's own **verbatim narrative** paired with the **multi-channel PSD balance
sheet(s)** that narrative discusses. GAIN is the country-granular sibling of built **WASDE #41**:
same PSD backbone, but #41 builds only `U.S. …` tables, so these foreign-post series are
structurally net-new.

> ### Retractions from the demo pass
> The demo shipped **4 records from 2 hand-pinned reports** and made three claims that the full
> archive disproves. All three are corrected below rather than quietly dropped.
>
> 1. **"Report enumeration is unsolved."** It is solved — the GAIN SPA's own search service is a
>    public API. See "Enumeration".
> 2. **"`recites` 3/4."** Wrong, and it was crashing the schema gate. GAIN is a **`describes`**
>    package: 145 of 1,523 records recite. See "Alignment retraction".
> 3. **">10k records realistic"** (from reports × commodities). Measured: **1,523**. See
>    "Why 1,523 and not 10,000".

| | |
|---|---|
| Text | GAIN attaché reports (PDF, digital — no OCR needed) |
| Series | PSD Online bulk CSV, 9 groups (`psd_<group>_csv.zip`) |
| Licence | `public-domain-us-gov` |
| Records | **1,523** from 18,494 candidate reports |
| Span | published **2000 … 2026** |
| Channels | **32,071** total; median **11** per record (p90 45, p99 130, max 247) |
| Depth | mean **7.2** points per channel (annual market years) |
| Coverage | mean **50%** of shipped series-years are named in the paired text |
| Alignment | `describes` **1,378** · `recites` **145** |
| Text | mean 3,012 chars, verbatim + one bare `<ts></ts>` |
| Duplicates | **0** duplicate texts; 1 duplicate `series_id` dropped at aggregate |
| Regions | 66 distinct, all valid ISO-2 |

## Quickstart

```bash
pip install -r requirements.txt
python scripts/census.py    --config config.example.yaml   # enumerate the archive
python scripts/harvest.py   --config config.example.yaml   # sharded, resumable
python scripts/aggregate.py --config config.example.yaml   # merge + samples + run report
python ../schema/validate.py output/fas_gain_cpt.jsonl --strict
```

`.cache/` holds PDFs (~1.8 GB for the full archive) and the PSD zips. `output/report_index.json`,
`output/shards/` and the merged JSONL are git-ignored; all three regenerate.

---

## Enumeration

The demo pinned filenames because no report-list endpoint was known. The GAIN SPA's own search
service is a public POST API, reachable with the **anonymous** bearer token the SPA itself mints:

```
POST /newgainapi/token               client_id=eAuthClient&client_secret=<fixed anon secret>
                                     &grant_type=client_credentials
POST /newgainapi/api/Search/GetSearchResults    body: SearchFilter {fromDate,toDate,...}
```

Both were read out of the SPA's own bundle (`gain.fas.usda.gov/main-es2018.js`), whose
`getFixedToken()` returns a hardcoded all-zeroes *"anonymous user token"*. This is the site's
unauthenticated read path — without the bearer the same endpoint answers
`401 {"message":"Token has expired"}`.

**Measured: 48,758 published reports, 1998–2026**, of which **18,494** sit in the 10 categories that
carry PSD balance sheets (Grain and Feed 7,566 · Oilseeds 2,726 · Livestock 2,026 · Sugar 1,500 ·
Cotton 1,335 · Poultry 1,190 · Dairy 1,134 · Coffee 654 · Tree Nuts 363).

**No silent cap.** A single `2025-01-01..2025-12-31` call returns 1,372 rows and the sum of that
year's twelve monthly calls is also **1,372**, all `reportId`s distinct — so the API is not
truncating a year window. `2025-10` legitimately returns **0** rows (the Oct-2025 US federal
shutdown), corroborated by the year total rather than assumed.

## Why 1,523 and not 10,000

The demo's ">10k realistic" came from multiplying reports by commodities. The honest figure is
**1,523**, and the losses are measured, not estimated:

| stage | n | note |
|---|--:|---|
| candidate reports | 18,494 | PSD-bearing categories |
| `no_table` | 13,557 | **73%** — no extractable PSD table (see "Template depth") |
| `download_failed` | 1,041 | |
| `empty_pdf_stub_from_api` | 200 | API returns HTTP 200 + a **10-byte** `%PDF-1.4\r\n` stub |
| `truncated_pdf` | 56 | valid header, no `%%EOF` |
| tables found | 11,492 | across the surviving reports |
| specs (candidate records) | 5,285 | |
| `no_channels` | 3,400 | tables matched but no channel survived splice/`min_points` |
| `no_psd_commodity_match` | 987 | table title is not a PSD commodity for that country |
| `no_psd_series` | 607 | attribute absent from PSD for that country/commodity |
| `no_prose` | 336 | prose after the table below `min_chars` |
| `superlative_dropped` | 24 | verbatim "highest/lowest" claim the series contradicts |
| `unresolved_country` | 1 | `Poland EU-27` — deliberately not forced onto a neighbour |
| **records** | **1,523** | |

The record rate is **~0.08 per candidate report** and varies enormously by group — livestock ≈0.9
records/report in recent years, coffee ≈0. Extrapolating from *tables per report* (as the demo did)
overstates it by roughly 8× because most tables lose their channels downstream.

## Template depth — the 73% `no_table`, measured

A stratified probe (349 reports, ~12/year) shows extraction yield is **strongly era-dependent**:

| era | reports with an extractable PSD table |
|---|--:|
| 1997–2001 | **0%** |
| 2002–2010 | 8–17% |
| 2011–2019 | 17–58% |
| 2020–2026 | 58–100% |

**A third table layout was found because of that 0%.** It looked like "the old template has no
balance sheets". It is a parser gap: pre-2004 reports (`Template Version 2.09`) carry full PSD
tables containing **none** of the three marker strings the other parsers key on. They use
`Revised 2001 / Preliminary 2002 / Forecast 2003` year columns with `Old`/`New` vintage pairs
instead of `USDA Official`/`New Post` — the same semantics, so `pick_value()`'s prefer-New logic
applies unchanged. `parse_tables_psd_legacy()` recovers them (123 tables in the final run).

Most of 1998–2001 nonetheless stays empty, and that is **real**: those years are dominated by short
one-off voluntary reports ("Spring Drought in Korea", "Grain Harvest Update") that carry no balance
sheet at all. Only ~40% of the pool is a periodic Annual/Semi-annual report.

## No hand-listing — specs are derived from the PDF

The config used to pin every report's commodities and table titles, which is why the build could
only cover the two reports someone had typed out. No hand-listing is needed: **a PSD table's title
IS its PSD `Commodity_Description`** (identical strings on all five pinned demo entries), so
`derive_specs()` reads the commodity set off the tables and confirms it against PSD's own vocabulary
for the resolved country. Two rules stop that from manufacturing duplicates, both found by auditing
the first 465 harvested records:

* **one spec per commodity** — a report can render the same commodity's table twice
  (`KS2016-2381` has two `Dairy, Cheese` tables), which produced two specs with the same
  `series_id`;
* **commodities sharing a page share their prose** — `prose_after_table()` keys on the page, so two
  commodities whose tables sit on one page got **byte-identical** text (`JA2016-1154` shipped one
  paragraph as both `Dairy, Cheese` and `Dairy, Milk, Nonfat Dry`). Re-shipping one paragraph under
  N labels is the "no fake scale" ban in SCHEMA.md, so those commodities are merged into **one**
  record whose channels are their union.

Final result: **0 duplicate texts** across 1,523 records.

Layouts in the final run: `bordered` 7,858 tables · `text` 3,511 · `legacy` 123. Shapes:
`per_commodity` 1,740 specs · `multi_commodity` 912.

## Alignment retraction

`detect_alignment()` counted an **exact unit-scale conversion** as reciting — "8.4 million hectares"
for a value of `8400` in `1000 HA`. SCHEMA §7 requires the text to state the series numbers
*literally*, and the shared gate in `schema/validate.py` (which mirrors the team's `verify_cpt.py`)
rejects it — it was raising inside `emit_record` on 2 of 24 records in the first shard. GAIN was one
of the five packages caught overclaiming this way on 2026-08-18.

`recites` now requires an **exact** surface match. The scaled hit is still recorded as auditable
evidence with an `exact: false` flag, so the alignment reasoning is inspectable rather than
discarded. Result: **145 recites, 1,378 describes** — where the demo claimed 3 of 4 reciting.

## Country join — hand-verified, because a fuzzy match is dangerous here

Three traps, all measured:

* PSD keeps **both** a legacy and a current spelling for many countries, and the legacy one is
  **frozen**: `Korea, Republic of` stops at MY2004 with 1,079 rows; `Korea, South` runs to MY2026
  with 25,796. Every candidate — *including an exact name match* — is therefore ranked by latest
  market year. An earlier version returned the exact match immediately, which made GAIN's
  `Russian Federation` resolve to PSD's frozen `Russian Federation` and silently pair recent reports
  with a series ending in 2004.
* The same label is unresolvable in one PSD group and present in another, so the alias is consulted
  even when the literal name is missing (this alone recovered 91 reports).
* A substring match would map **`Korea - Republic of` → `Korea, Democratic Peoples Rep`** before it
  matches South Korea. The fallback only strips GAIN's own ` - <qualifier>` suffix and requires a
  word-boundary match; anything still ambiguous is left **unresolved and counted** (final run: 1).

`region` is ISO-2 via a hand-verified alias table plus `pycountry`, falling back to the country name
verbatim rather than coercing two letters. An earlier pass derived it as `countryName[:2].upper()`,
which emitted `CH` for China, `ME` for Mexico, `NE` for New Zealand and `JA` for Japan — real ISO
codes for the **wrong countries**. Historical entities with no current ISO-2 (`Former Yugoslavia`,
`Union of Soviet Socialist Repu`, …) keep their names deliberately. Final run: 66 regions, all valid.

## Attribute mapping — and two aliases deliberately refused

Report row labels are GAIN's friendlier rendering of PSD attributes. Five aliases were added from
the full run's own `unmapped_labels`, each confirmed against PSD's actual vocabulary for the
commodity: `Feed and Residual` → `Feed Dom. Consumption` (1,149 rows), `Total Consumption` →
`Domestic Consumption` (906), `Milled Production` → `Production`, `Consumption and Residual` →
`Domestic Consumption`, `Total Dom. Cons.` → `Domestic Consumption`.

An **appended-unit fallback** handles rows where the unit follows the attribute with no parentheses
(`Production 1000 480 lb. Bales`), which `norm_label` cannot strip — the top of `unmapped_labels`
was ~300 cotton and dairy rows of exactly that shape. It matches on the label's **prefix** and takes
the longest attribute that is a prefix, which is deterministic and cannot invent a mapping:
`Other Imports` still does **not** match `Imports` and stays counted.

**Two plausible aliases were refused, and verification says that was right.** Checking report values
against PSD for settled years:

* `Total Use` (sugar) → matched `Total Disappearance` 3× and **mismatched 3×**; same for
  `Human Dom. Consumption`. There is no consistent mapping.
* `Total Sugar Production` → `Production` was **8/8 mismatch**.
* `Area Planted` has no PSD counterpart at all — PSD carries only `Area Harvested`.

All three stay unmapped and counted. Guessing any of them would have mislabelled channels.

## The vintage splice

PSD Online bulk carries decades of history but always reflects the **current** vintage, and USDA
keeps revising recent/forecast years after a report ships. Measured on `MX2026-0012`: settled years
matched the report exactly (8/8 for 2024), but the forecast year had drifted past **both** of the
report's own columns (Total Slaughter 2026: report 7,200 official / 7,225 New Post → PSD now 7,550).
So each series is **PSD bulk for settled market years + the report's own table values for its table
years**, with the post's revised column preferred over the previous official one (617 official
fallbacks in the final run, where New Post is a not-yet-published 0). Pairing live PSD naively would
mismatch exactly the years the prose is about.

**Forecast-not-measured:** GAIN prose is forecast-dominant, so the forecast year is the series'
terminal point. The text is the contemporaneous first-party forecast, so there is no future-value
leakage — same convention as WASDE #41. Stripping the forecast would *break* alignment, not clean it.

## Commensurability — wide, not deep

`window_mode: text_span` bounds each series to the market years the prose actually names (+2 years of
context) and drops PSD's missing-as-zero prefix. Mean coverage is **50%** of shipped series-years,
against 13% at full PSD depth. The trade-off is real: GAIN **cannot be both commensurate and deep**
at annual frequency with a 3-year narrative horizon.

The profile is genuinely **wide**: median 11 channels/record but p99 **130** and max **247** — the
tail is India oilseeds annuals whose Report Highlights paragraph pairs with 19 commodities' balance
sheets (73 records carry >60 channels). Those are one text against many series at ~4 points each;
legitimate under the schema and not text reuse, but shallow, and worth knowing before training on
them.

## Engineering notes

* **`harvest.py` shards by (PSD group, year).** Sharding on the *group* keeps exactly one PSD group
  resident — all nine together is ~1 GB of dicts. Each shard's `.report.json` is written **last**, so
  it is the completion marker: an interrupted shard is redone rather than mistaken for finished.
* **The parse runs in processes, not threads.** The work is CPU-bound in pdfplumber, which holds the
  GIL: raising threads 8 → 24 changed nothing (~2.5 s/report either way, ~14 h for a partial scope).
  Prefiltering pages by marker text before `extract_tables()` measured **1.0×**, because
  `extract_text()` is itself the cost. Moving the parse to a process pool took it to **0.18
  s/report** (~14×), which is why the full 18,494-report archive is built rather than a recent slice.
* **`aggregate.py` streams shards** rather than accumulating records in memory, and drops duplicate
  `series_id`s (1 in the final run).
* **Chart furniture is stripped.** pdfplumber reads a rotated axis label one glyph at a time, so a
  "Pesos/Kilogram" y-axis arrived as `115.00 / m / a105.00 / r / g / o 95.00 / l / i / K` and
  `join_prose` made each fragment its own paragraph — present in **5 of 18** records in the first
  shard, now 0. Bare page numbers are trimmed at prose edges.
* **`samples/example_output.jsonl` is true JSONL**, written by `aggregate.py`, one record per line
  across distinct (shape, layout, alignment) combinations. The file committed with the demo was a
  pretty-printed JSON **array**, so `json.loads` failed on line 2 and no per-line consumer could
  read it.

## Headroom (measured, not taken)

* **`no_channels` (3,400 specs)** is the largest remaining lever — tables matched but every channel
  died in the splice or under `min_points`. Worth a per-attribute autopsy.
* **Remaining `unmapped_labels`** are dominated by attributes PSD genuinely lacks for that commodity
  (`Slaughter (Reference)`, `Trees`, `Other Slaughter`, `MY Imp. from U.S.`). Some may be real
  aliases; each needs the same value-level check the sugar labels got.
* **`download_failed` (1,041)** were not retried in a second pass.
* **`CH2026-0032`-class reports** ship tables as images; they are counted in `no_table`, not
  silently dropped.

## Provenance

* Reports: `apps.fas.usda.gov/newgainapi/api/Report/DownloadReportByFileName?fileName=…`
  (`meta.report_number`, `meta.post`, `meta.country_gain`, `meta.published`).
* Series: PSD Online bulk `psd_<group>_csv.zip`; `meta.psd_group`, `meta.country_psd`,
  `meta.psd_attributes`, `meta.splice_year`, `meta.report_table_years`.
* WASDE #41 de-duplication: #41 builds only `United States` tables for six commodities; the
  `dedup` guard refuses those cells so a US-country PSD series can never be re-shipped.
