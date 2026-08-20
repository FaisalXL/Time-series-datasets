# 60 — RBNZ Monetary Policy Statements → CPT world-knowledge records

**Status: FULL ARCHIVE BUILT + VERIFIED (2026-08-20). 109 records from 119 enumerated statements,
109/109 pass `validate.py --strict` with 0 warnings.** Licence is a first-party
reproduction-with-attribution grant, so this is shippable rather than held (see "Licence").

One record = an MPS's own **verbatim** policy narrative paired with the **multi-channel macro
series** published in that same statement's data pack (OCR, inflation components, unemployment,
GDP, TWI, 90-day rate).

> **Correction to the previous pass.** This package shipped as a **1-record demo** and described
> the archive as "~110 statements, quarterly since ~1998–99", with two known URL schemes and a
> sheet-name → variable map. All three were wrong in ways that mattered: there are **four** URL
> generations reaching back to **1996-12**, the sheet-name map **mislabels series** once applied
> outside the single edition it was written against, and the narrative extractor could only read
> the current template. Details under each heading below.

| | |
|---|---|
| Text | RBNZ Monetary Policy Statement pages (HTML), via Wayback — see "Access" |
| Series | the statement's own co-published data pack (`.xls` 1997–2021, `.xlsx` 2022+) |
| Licence | `cc-by-4.0` (closest enum fit; true grant recorded in `meta.true_license`) |
| Records | **109** from 119 statements (92%) |
| Span | statements **1997-09 … 2026-02**; series data back to **1984-03** |
| Channels | mean **5.7** per record, 8 distinct variables |
| Depth | mean **33.0** points per channel (min 9, max 90) |
| Freqs | `1q` 488 channels · `1d` 83 · `1M` 44 · `1w` 10 |
| Alignment | `recites` **44** · `describes` **65** |
| Text | mean 2,186 chars, all verbatim + one bare `<ts></ts>` |
| Duplicates | 0 duplicate texts, 109/109 distinct `series_id` |

## Where the other 10 statements went, and why one run is not enough

Every drop now has a reason, and `throttled` is **0** — which took three passes to achieve, because
two of those passes were lying about what happened.

| outcome | n | what it is |
|---|--:|---|
| emitted | **109** | |
| `no_pack` | 3 | 1996-12, 1997-03, 1997-06 — before RBNZ co-published a data pack at all |
| `superlative_dropped` | 3 | 2018-11, 2020-08, 2020-11 — text makes a superlative claim the series contradicts |
| `too_few_channels` | 2 | 1998-03, 2001-11 — pack parses, fewer than `min_channels: 2` survive |
| `pack_not_archived` | 1 | **2026-05** — the statement page is archived, `mpsmay26-data.xlsx` is not: **0 captures among 1,666 archived packs** |
| `no_narrative` | 1 | **2023-11** — the archived page is a stub: 3,919 chars of visible text, a one-line decision and download links. The narrative is only in the 6 MB PDF. |

**A single pass over Wayback is not reproducible, and the failures do not repeat.** The same code
and cache emitted 108 on 2026-08-19 and 109 on 2026-08-20; each run lost a *different* statement
to a transient archive.org condition, not to anything about the data.
[`scripts/union_runs.py`](scripts/union_runs.py) unions runs by `series_id` and prints what each
one contributed, so a build is never trusted as a single sample of the archive.

### The throttle flag was sticky, and it hid a real content verdict

`fetch_page` set `throttled = True` on a connection reset and never cleared it — so if attempt 0
was reset and attempt 1 downloaded the page perfectly, the statement was still reported as
`THROTTLED`. Because a throttled return is `continue`d *without* a drop reason (deliberately: a
throttle must never be banked as a content verdict), the statement then vanished from the emitted
set **and** from the accounting.

2023-11 is the case that exposed it. Two consecutive runs called it a throttle; fetching its single
capture by hand returned **200 and 86,122 bytes**, byte-identical to what was already sitting in
the cache. It was never a throttle — its page genuinely has no narrative. A successful fetch now
retires the flag, which is the same rule as before applied in the other direction: **a fetch
condition must not be recorded as a content verdict, and a content verdict must not be recorded as
a fetch condition.**

### The frontier is the archive's lag, not the source's

RBNZ published its Feb and May 2026 statements; the Aug-2026 one is days old. Measured against CDX
on 2026-08-20: both 2026 statement *pages* are captured, May's *data pack* is not, and **no
Aug-2026 statement is captured at any of the four URL generations**. So 2026-02 is the last
buildable statement today, and the remaining gap closes itself as Wayback catches up — no code
change will close it.

## Quickstart

```bash
pip install -r requirements.txt
python scripts/census.py --config config.example.yaml          # enumerate statements + packs
python scripts/build_cpt_jsonl.py --config config.example.yaml --prefetch --workers 1
python ../schema/validate.py output/rbnz_mps_cpt.jsonl --strict
```

`--prefetch` downloads pages then packs before building; everything is cached under `.cache/`.
**Keep `--workers` at 1–4**: see "Access".

---

## Enumeration — four URL generations, not two

`scripts/census.py` walks Wayback CDX and finds **119 statements, 1996-12 … 2026-05**, with every
year **1997–2025 complete at 4/4** (MPS is quarterly; 1996 has 1 because the first MPS was
December 1996, and 2026 has 2 because the year is in progress).

| generation | pattern | covers |
|---|---|---|
| gen1 | `/monetary-policy/monetary-policy-statement/mps{YYYY}-{MM}` | 1996-12 … 2015-09 (76) |
| gen2 | `…/mps-{month}-{year}` | 2015-12 … 2022-02 (26) |
| gen3 | `…/monetary-policy-statement-filtered-listing-page/{year}/{slug}/…` | 2026 |
| gen4 | `/hub/publications/monetary-policy-statement[/{year}[/{slug}]]/…` | 2022 … 2025 |

**Statement identity is `(year, month)`, not the URL.** The same statement is archived under
several paths across redesigns, so collapsing on the URL counted ~110 statements as **213**. One
2024 slug carries no month name at all (`monetary-policy-statement-291124` — 29 Nov 2024) and is
parsed as a date.

**A CDX `200` does not mean the bytes are retrievable.** 30 of 119 statements had their newest
capture on a Jan-2026 crawl of the modern `/hub/` path that CDX lists as `statuscode 200` but that
**404s** when the original bytes are requested — while the same statement fetches fine from an older
capture of an older scheme. The census therefore keeps up to **8 candidate captures** per statement
and the builder walks them until one yields a usable narrative. Candidates are ordered
**era-native first** (the scheme that encodes the statement's own date), which makes the first
request the successful one for pre-2022 statements and matters because every failed attempt costs a
request against a rate limit.

## Narrative extraction — anchors are era-dependent, so the fallback is structural

The old extractor keyed on `"Latest OCR decision" … "Most recent outlook for the OCR"`, which exist
only on today's template. Across the census:

* **1996–2015** put the same content under `<h2>Policy assessment</h2>`.
* **2016–2021** use an "in pictures" block.
* **1996–1998 predate the OCR entirely** — they set a **Monetary Conditions Index** level
  ("We now view 725 as the appropriate level for the MCI"), so even a keyword fallback on "OCR"
  finds nothing, and those statements' narrative sits under yet another set of headings
  ("Speaking notes for briefing journalists…", then "Introduction" / "The outlook for inflation" /
  "Policy implications").

Enumerating every era's headings is the losing game that held **FHFA #59** to 28 of 46 records, so
the last resort is structural: take the **longest contiguous run of substantial paragraphs** on the
page. Which route each record used is recorded in `meta.narrative_anchor`, and the fallback is not
a rounding error — it produced **38 of 108 records**:

| route | records |
|---|--:|
| `anchor:0` "Policy assessment" | 68 |
| **`density_fallback`** | **38** |
| `anchor:4` / `anchor:1` / `anchor:2` | 8 / 3 / 1 |
| none | 1 |

RBNZ's megamenu repeats "Official Cash Rate" dozens of times, so nav/script/footer are stripped
**before** any content search — the first "OCR" hit on a 2005 page is inside the menu, ~60KB above
the real narrative.

## Series extraction — three assumptions replaced, one of them a correctness bug

**(1) Sheet numbers do not identify variables.** The config mapped sheet `"2.1"` → Production GDP.
Measured 2026-08-19: `Fig 2.1` is **"CPI inflation"** in the Jun-2005 pack and **"Employment and
investment intentions"** in the Dec-2010 pack. Figure numbering is re-cut between editions, so a
name → variable map silently mislabels series wherever it is reused. Variables are now matched on
each sheet's **own title text** (`config.variables[].title_pattern`); a title matching nothing is
counted in `run_report.unmatched_sheet_titles`, never guessed at.

**(2) Columns inside one figure are different series — this was shipping wrong data.** Taking the
leftmost value column meant the Dec-2010 block titled *"Employment and unemployment rate"*, whose
first column is **Employment**, shipped employment numbers under an `unemployment_rate_pct` label.
`pick_column()` now prefers a match on the **column's own header** and takes that column; the figure
title decides only when no header names a variable, and then the leftmost column is the release's
own central series (later columns are prior vintages, e.g. "March projection").

**(3) One sheet can hold many figures.** The 1997–2002 packs lay figures out **side by side** across
a single wide sheet — the Sep-1997 pack is **49 rows × 117 columns**, with `#1 Consumer price
inflation` at column 1 and `#2 Real and Nominal MCI` at column 7 — and their date column is a
quarter **string** (`90q4`, `  91Q1  `), not an Excel serial, so that whole era parsed to **zero
points**. `find_blocks()` splits a sheet into blocks, each with its own title and date column.

Frequency is **inferred from observed spacing**, not configured: packs mix quarterly macro series
with daily/weekly market series and the mix changes across eras. Channels are grouped by frequency
and each group trimmed to a common length, which is what the schema requires.

## Data packs — resolved by filename, not by the link on the page

Pack filenames are irregular across the archive (`aug00-data.xls`, `dec13data.xls`,
`dec2010-data.xls`, `jun11data.xls`, `mpsmar13data.xls`,
`august-2022-monetary-policy-statement-data.xlsx`), so they cannot be constructed. They also cannot
be fetched from the path the page links: **that path depends on which CMS was live when the page was
crawled.** A 2005-era capture links `/-/media/ReserveBank/Files/Publications/…`; a 2026 capture of
the same statement links `/-/media/project/sites/rbnz/files/…`, which Wayback often never crawled —
`sep97-data.xls` **404s at every timestamp** on the modern path while the pack *is* archived under
its older URL. So `census.py` indexes every archived pack from CDX (**116 found**) and the builder
joins on **filename**. Note the href carries `?revision=…` *after* the extension, so a regex
anchored at the extension finds nothing.

## Access — a bot-wall, and a global rate limit that does not say 429

`rbnz.govt.nz` answers **HTTP 403** to automated fetches, re-verified 2026-08-19 **with a full
desktop-Chrome User-Agent**; the body is RBNZ's own "Website unavailable" page, so this is a
bot-wall, not a missing header. Everything is read from Wayback.

archive.org enforces a **global** limit and does not answer with a tidy 429. Measured: after a burst
of CDX sweeps plus 4 concurrent page fetches, both CDX and `/web/` began **refusing TCP
connections** while archive.org's own root still returned 200; later the same requests returned
**503**. A refused connection is indistinguishable from "not archived" unless it is treated as a
throttle, so:

* all archive.org traffic goes through **one process-wide AIMD pacer**;
* a refused connection or 503 is counted as **`throttled`**, never as a missing narrative —
  the final run recorded `throttled: 0`;
* recovery is **multiplicative** (`gap × 0.7`). Subtracting a constant was far too timid: a handful
  of 503s pushed the gap to its ceiling and it then needed ~170 consecutive successes to recover,
  so a brief rate-limit episode cost hours of sleeping.

Also fixed here: **urllib does not decompress `Content-Encoding: gzip`**, so raw DEFLATE bytes were
being written into the page cache, where a ~20KB blob containing no tags read as *"this statement
has no narrative"* — **58 of 89** cached pages were gzip blobs. Bodies are decompressed by magic
number, and anything that does not look like HTML is never cached.

## Channels

| variable | in N records |
|---|--:|
| `inflation_headline` | 106 |
| `gdp_growth_pct` | 104 |
| `twi_index` | 100 |
| `unemployment_rate_pct` | 83 |
| `ocr_pct` | 72 |
| `ninety_day_rate_pct` | 68 |
| `inflation_non_tradables` | 64 |
| `inflation_tradables` | 22 |

`ocr_pct` appears in 72 of 108 rather than all of them, and that is correct: the **1996–1998
statements predate the OCR**, which was introduced in March 1999.

## Forecast-not-measured

A pack's own-vintage column runs from measured history straight into the Bank's own projection with
no visual break. Verified on the Feb-2026 MPS: OCR's row for the statement's own quarter is
**2.25%**, exactly the announced decision — a real current decision, not a forecast. But
unemployment/inflation's same-quarter row **is** a forward projection, and the text's quoted values
match the **prior** quarter (the last real outturn). Quarterly channels are windowed to a common end
quarter because the schema needs equal length per frequency; where that final point is the Bank's
own projection it carries the same **forecast-not-measured** caveat used for WASDE #41 / GAIN #58 —
the text is the contemporaneous first-party forecast, so there is no future-value leakage. Recorded
verbatim in `meta.series_note`.

## Skips — all 11 accounted for

| reason | n | real? |
|---|--:|---|
| `no_pack` | 3 | yes — 1996-12, 1997-03, 1997-06 link a PDF only; no data pack exists |
| `superlative_dropped` | 3 | yes — a verbatim "highest/lowest" claim the paired series contradicts |
| `too_few_channels` | 2 | yes — under `min_channels: 2` after frequency trimming |
| `pack_not_archived` | 1 | yes — linked pack has no Wayback capture |
| `pack_fetch_failed` | 1 | yes |
| `no_narrative` | 1 | yes — no substantial paragraph run on any candidate capture |
| `throttled` | 0 | — |

## Licence

RBNZ's terms grant reproduction with attribution: *"This material may be reproduced and used without
the specific permission of the Bank, subject to [acknowledge RBNZ as source; reproduce accurately;
exclude third-party copyright]"*. That is a **custom grant, not a standard CC licence**, and
`schema/cpt_record.schema.json`'s frozen v1 enum has no slot for it, so records are tagged
closest-fit **`cc-by-4.0`** with the true grant recorded verbatim in `meta.true_license` and the
required attribution in `meta.attribution_required` — the same pattern used for INSEE #57's Etalab
licence. Access via Wayback is an access-method choice forced by the bot-wall, not a redistribution
question.

## Headroom (measured, not taken)

* **More variables.** `run_report.unmatched_sheet_titles` shows real macro series present in the
  packs but not in `config.variables`: output gap, potential output, private consumption, business
  investment, export/import volumes, change in inventories. Each is a config line, not new code.
  The other frequent "unmatched" titles are vintage column headers (`Aug MPS | May MPS`) and are
  correctly not variables.
* **Chapter-level text.** Only the overview/policy-assessment narrative is paired here. The full MPS
  PDF has chapter-level content (Chapters 2–6) that could pair with the same series at finer
  granularity.
* **The 1996–1998 MCI era** produces records but has no OCR channel by construction. An `mci_index`
  variable would pair those statements with the quantity they actually discuss.

## Provenance

* Statement pages: four URL generations under `rbnz.govt.nz` (see "Enumeration"), fetched via
  Wayback; `meta.wayback_ts` records which capture each record came from.
* Data packs: the statement's own co-published `*-data.xls[x]`, resolved by filename through
  `output/pack_index.json`; `meta.data_pack_url` is the archived URL actually fetched and
  `meta.data_pack_linked_as` is the path the page linked.
* `output/statement_index.json` and `output/pack_index.json` are committed as provenance.
