# Richmond Fed Fifth District Survey of Manufacturing Activity → CPT

> **Status: ✅ Finalized — 1,003 records** (was a 93-record bank, 10.8×). One of the Federal
> Reserve regional business surveys; siblings are Philadelphia MBOS `47_philadelphia_mbos`,
> Dallas TMOS `48_dallas_tmos` and the service-sector twin `50_richmond_nonmanufacturing`,
> which shares these scripts byte-for-byte. Family map:
> [../../docs/fed_surveys_discovery.md](../../docs/fed_surveys_discovery.md).

**What it is:** one record = **(data month × the release's own narrative block)** — that block
of the release prose, *verbatim*, paired with a **trailing 36-month window of exactly the
indicators the block names**, ending on the month the release reports. Fifth District = **MD,
VA, NC, SC, WV, DC**.

| | |
|---|---:|
| Records | **1,003** |
| Timesteps / datapoints | 36,108 / 196,164 |
| Points per channel | **100% exactly 36** |
| Channels per record | median 4, max 20 (31 distinct) |
| Nulls | 0.58% (all leading runs — see below) |
| Data-month span | **2001-04 → 2026-06** (226 distinct months) |
| Text | median **478 chars**, 1,003/1,003 distinct |
| `alignment` | 256 `recites` / 747 `describes` (measured per record) |

---

## The two findings that define this build

### 1. The published workbook is the wrong series — the release says so itself

`mfg_historicaldata.xlsx` is the **revised** series. The release's own Technical Notes:

> "Seasonal adjustment factors are recalculated every July and the entire series is revised
> to better reflect current economic trends."

Measured across the whole harvest (`--audit-vintage`), the value a release printed still
equals today's workbook in **4,584 of 23,463 cells = 19.5%**, median drift **2.0 index
points** (max 64). The agreement rate tracks the revision schedule exactly:

| data year | 1997 | 2000 | 2004 | 2010 | 2018 | 2024 | 2025 | 2026 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| as-published == workbook | 7% | 13% | 14% | 9% | ~14% | 27% | 69% | **100%** |

So the banked build paired real Richmond prose with numbers that prose never quoted, in
roughly four cells out of five. **The series is now the as-first-published vintage, stitched
from each release's own results table** — the same conclusion `47_philadelphia_mbos` and
`48_dallas_tmos` reached. Third package in this family; treat it as the default expectation.

**The stitch is checkable, and it checks out.** Every release prints three consecutive
months, so consecutive releases overlap on two of them. Agreement by release month
(`--audit-overlap`):

| release month | 02 | 03 | 04 | 05 | 06 | **07** | 08 | 09 | 10 | 11 | 12 | 01 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| overlap agreement | 99.8% | 99.9% | 100% | 97.9% | 100% | **41.4%** | 100% | 100% | 99.9% | 99.1% | 97.5% | 95.9% |

100% (or near) in eleven months and **41% in July** — precisely the annual re-benchmark the
Technical Notes describe. The independently-printed 3-month-average column checksums
**1,404/1,426** of the rows that carry it.

Each record's window is the vintage **as of its own release**: for a month printed by more
than one release, the value from the latest release at or before this one. That is what makes
the prose's own comparison check out — a release restates the two prior months under its
current factors, and "shipments fell to 3 from 16" quotes that restatement.

### 2. The release universe is three times what the live site shows

The banked build read the live media tree, which starts 2018. The releases actually run
**1997-01 → 2026-06** across three site layouts (enumerated from the Wayback CDX by
`scripts/richsrc.py`, never guessed from a URL template):

| era | location | formats | months |
|---|---|---|--:|
| 1997-01 … 2004-12 | `rich.frb.org/{research/,research/regional/,}surveys/archive/` | `mfg{MM}{YY}.html` narrative + `m{MM}{YY}tbl.html` table (fixed-width `<pre>` before ~1997-06) | 96 |
| 2005-01 … 2008-09 | — | **no web capture in any layout** | 0 |
| 2008-10 … 2026-06 | `richmondfed.org/…/manufacturing/{YYYY}/` and the `-/media/…` tree | 2-column PDF, `*_busindex_*.csv`, release page | 213 |

**309 release months harvested, 0 unresolved fetches.** The 2005-2007 gap is real: the Bank's
own 2009 archive page lists year folders 2006-2008, so the pages existed, but Wayback holds
none of the release documents (checked against every naming variant). 1998 has narratives but
**no table page anywhere** — verified across all `tbl`-shaped names in the archive — which is
what pushes the retired era's first record to 2001-04 rather than 1999-12.

---

## Record shape

The 1997-2017 releases print their own section headings, so the block is the source's own
section; the 2018+ releases print none, so the block is the paragraph — the unit `48` shipped.

| era | blocks/release | heading vocabulary |
|---|--:|---|
| 1997-2004 (archive HTML) | 6.2 | Current Activity · Employment · Expectations · Prices |
| 2008-2017 (2-column PDF) | 4.9 | Overview · Current Activity · Employment · Expectations · Prices |
| 2018-2026 (chart PDF) | 4.0 | *(none — paragraphs)* |

```json
{
  "text": "The composite manufacturing index decreased to 4 in June from 13 in May. All three of its component indexes fell in June… Shipments fell to 3 from 16, new orders to 9 from 17, and employment to -1 from 3.\n\n<ts></ts>",
  "timeseries": [
    {"values": ["…", 3.0],  "unit": "shipments",       "freq": "1M"},
    {"values": ["…", 9.0],  "unit": "new_orders",      "freq": "1M"},
    {"values": ["…", -1.0], "unit": "employment",      "freq": "1M"},
    {"values": ["…", 4.0],  "unit": "composite_index", "freq": "1M"}
  ],
  "alignment": "recites", "text_quality": "real", "license": "public-domain-us-gov",
  "series_id": "rich_mfg_2026-06_b0", "period_start": "2023-07-01", "period_end": "2026-06-01",
  "meta": {"data_month": "2026-06", "release_date": "2026-06-23", "section_heading": null,
           "series_vintage": "as-published (stitched from each release's own table, real-time as of this release)"}
}
```

Channels are the table's own indicators; an expectations channel is suffixed `_fut`
(`shipments` = current, `shipments_fut` = six months ahead).

**No generated text.** `<ts></ts>` is appended directly to the Bank's own words — no
provenance header, no channel-naming sentence. Provenance lives in `meta`.

---

## Alignment — measured, not asserted (`--audit-alignment`)

**Structural, 1,003/1,003 on all five:** `period_end` == the reported month · `period_start` ==
the window start · every channel length == `meta.n_points` · terminal point non-null ·
exactly one `<ts></ts>`.

**Quotation.** Of the 2,913 figures the prose states, **55 (1.89%) are not values in that
record's own series**; **965/1,003 records have every quoted figure in their own series**. This
is the check that caught a 15.8% cross-indicator contamination in `47`.

**Tier, with a permutation control:**

| test | true rate | control (same channel set, *another* month) | lift |
|---|--:|--:|--:|
| **ordered pair** — prose quotes a channel's last two points in order (shipped) | 25.5% | **0.7%** | **+24.9 pp** |
| any quoted number equals any terminal value (rejected) | 60.9% | 22.4% | +38.5 pp |

The single-value form is *reported and rejected*: on bounded small integers it fires on 22%
of deliberately mismatched pairs, so more than a third of those tags would be coincidence.
The releases state the move as an ordered pair ("decreased to 4 in June from 13 in May"), so
that is what the test requires.

**The stated move reproduces from the series:** a stated "N points" change matches a channel's
own last-two-point delta in **1,667/1,709 = 97.5%**. Dominant direction agrees in 78.7%.

⚠️ **What this is not.** The text pins a median of ~3 numbers against 144 attached values. The
window is context for the terminal point, not a transcript — the same contract as `11_eia`
(260 weekly points for one week's sentence) and `08_bls_cpi` (120 months).

---

## Why some channels have leading nulls, and can it go further?

**All nulls are a leading run — there are no interior or trailing gaps.** Measured on the
built file: 0.58% of datapoints, in 7.0% of channels, and **100% of the null runs sit at the
oldest end of the window** (max run 5 of 36; the terminal point is non-null in 1,003/1,003).
They cluster on exactly two stretches of calendar time — **1998-06…09 and 2008-04…07** — which
are the two source walls. A window is allowed to overhang a wall by up to
`data.max_null_fraction` (0.15 → 5 months), which is what those records are.

**They are not fillable.** Both walls were probed, not assumed: 1998 has narratives but **no
table page under any naming variant** in the archive, and **2005-01…2008-09 has no release
document at all** in any of the three site layouts. With no release printing those months,
there is no as-published value to attach.

**What the knob buys (real builds, not estimates):**

| `window_months` | `max_null_fraction` | records | note |
|---:|---:|---:|---|
| 36 | 0.00 | 947 | zero nulls anywhere, costs 56 records |
| **36** | **0.15** | **1,003** | **shipped** — 0.58% nulls, all leading |
| 36 | 0.30 | 1,103 | +100 records, but a window may be ~28% null |
| 32 | 0.15 | 1,030 | +27 records, still ≥ the model's 32-point patch |

Zero-null output is one config flag away (`--set data.max_null_fraction=0.0`) and costs 5.6%
of records; the shipped setting keeps them because a 5-month gap at the far end of a 36-month
window is context, not the claim the text makes. For `50_richmond_nonmanufacturing` the same
sweep gives 711 / **757** / 825 / 780.

---

## Reconcile (the build raises if it does not balance)

309 releases → 295 with a parseable table, 309 with narrative.
**1,455 block units = 1,003 emitted + 94 no-indicator-named + 25 short-text (<120 chars) +
333 sparse-or-short-window ✓** (plus 73 blocks in the 14 releases with no table).

---

## Defects fixed at inspection

| # | Defect | Effect |
|---|---|---|
| 1 | Series was the revised workbook | 80.5% of cells disagreed with the prose |
| 2 | Text era assumed to start 2018 | 102 → 309 release months |
| 3 | Chart axis labels outnumber narrative words on page 1, so `colex._body_size` picked 9.0pt (axis) over 11.5pt (prose) | the whole 2018+ narrative was discarded and the axis labels kept |
| 4 | Prose furniture filter matched on prefix only | `"Fifth District manufacturing activity was flat in June, according to…"` matches the running-header pattern → **the lede of every 2018+ release was deleted** |
| 5 | `mfg0709.html` decodes as July 2009 under the archive's own `{MM}{YY}` scheme but **is the July 9 2002 release** (that one file is `{MM}{DD}`) | 2002 values injected into the 2009 vintage; July 2002 missing. Release dates now come from the document's own dateline |
| 6 | 1997 tables are a **per-state** layout (`< = >` shares + District/state Index columns) read by the generic three-month reader | the response *percentages* were being stored as three months of index values, with no checksum to catch it. Now read separately and arbitrated by the diffusion identity (`index == %inc − %dec`) |
| 7 | Blanket `<[^>]+>` tag-stripping on the 1997 press releases | those pages print `<, =, and >` unescaped, so the stripper deleted the very columns that identify the layout |
| 8 | 21 releases emit page 2 with **per-glyph positioning** | `extract_words` returns `'B','u','s',…`; the header line never forms and the table reads as absent. Words are now assembled from character geometry |
| 9 | Two table halves print the same three month labels | a month-keyed cell dict collided → every row of every release silently rejected |
| 10 | `prices (?:they )?pay` never matches "prices they **paid**"; "the finished goods **index**" was unmapped; "employees with the skills they needed" attached *employment* | 17.6% → 1.9% of quoted figures orphaned |
| 11 | Retired tables print current inventories trailing the six-months block with no heading | inventories records carried the expectations window beside current-level prose |
| 12 | Release CSVs head their current-price block **"Currrent trends"** (three r's) | both current-price rows dropped from all 99 CSV releases |
| 13 | Chart year-axis occasionally appended to a paragraph | numbers in the text belonging to no series |
| 14 | The harvester skipped the release *page* wherever a PDF existed for that month | some releases print their table **only** on the page. Cost 6 months of table in the sibling package, and a month with no table drops all of its blocks |
| 15 | 22 of the 198 cached CSVs are a **spreadsheet dump** of the table, not the `section_type,item_title,…` export | the CSV reader returned nothing for them, so those releases looked table-less. Both layouts now share one cell-row reader |
| 16 | One rendering of a release's table was *chosen* over the other | the CSV and PDF are the same table and neither is a superset, so choosing lost rows. They are now **merged**, and where both print a cell it is a free cross-check — **0 conflicts in either package** |
| 17 | A single thin channel vetoed its whole record | recovering a table could *lose* records. A channel without a usable window is now dropped on its own and the record survives on the rest: **+44 records here, +77 in the sibling** |

---

## Caveats and headroom

- ⚠️ **2005-01 … 2008-09 (45 months) is a hard gap** — no web capture in any layout. Because
  a 36-month window cannot span it, the modern era's first record is **2011-07**, and
  2008-10…2011-06 is warm-up. Same for 1998: no table page exists, so the retired era starts
  2001-04.
- ⚠️ **Forecast channels are included** (`*_fut`, six months ahead). They are the survey's own
  question, not a projection this build invented, and the prose discusses them explicitly.
- **FRED overlap is retired as a concern.** The revised indices are on FRED; a *real-time*
  Fifth District vintage is published nowhere else, so the pairing and the series are both
  novel — the same conclusion `47` reached.
- **Headroom is the pre-1997 era** (the survey's data start is Nov 1993) and needs documents
  that are not on the web in any layout; and the ~45 months of 2005-2007, which would need a
  non-Wayback source.

---

## Run

```bash
pip install -r requirements.txt          # + apt-get install poppler-utils
python scripts/harvest.py                # enumerate + fetch releases (cached; ~390 docs)
python scripts/harvest.py --report       # coverage of what is cached
python scripts/build_cpt_jsonl.py        # full build
python scripts/build_cpt_jsonl.py --audit-vintage     # as-published vs published workbook
python scripts/build_cpt_jsonl.py --audit-overlap     # the stitch's own consistency check
python scripts/build_cpt_jsonl.py --audit-convention  # data-month rule, measured per era
python scripts/build_cpt_jsonl.py --audit-alignment   # everything in the section above
```

**Scripts** (identical in `50_richmond_nonmanufacturing`; everything that differs between the
two surveys is in `config.example.yaml`):
`richsrc.py` universe enumeration · `harvest.py` polite cached fetch ·
`richtab.py` as-published table (5 formats) · `richnarr.py` narrative + sectioning ·
`colex.py` multi-column PDF prose (ported from `47`, one change — see `_body_size`) ·
`polite_fetch.py` (ported from `56`).

**Sources:** [Richmond Fed manufacturing survey](https://www.richmondfed.org/research/regional_economy/surveys_of_business_conditions/manufacturing)
— **U.S. public domain**.
