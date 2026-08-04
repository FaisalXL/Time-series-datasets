# Richmond Fed Fifth District Survey of Service Sector Activity (Non-Manufacturing) → CPT

> **Status: ✅ Finalized — 757 records** (was a 76-record bank, 10.0×). Twin of
> `49_richmond_manufacturing`: same bank, same document shapes, **byte-identical scripts** —
> everything that differs between the two surveys lives in `config.example.yaml`. Read
> [49's README](../49_richmond_manufacturing/README.md) for the full account of the two
> findings that define both builds; this file records what is specific to the service sector.
> Family map: [../../docs/fed_surveys_discovery.md](../../docs/fed_surveys_discovery.md).

**What it is:** one record = **(data month × the release's own narrative block)** — that block
of the release prose, *verbatim*, paired with a **trailing 36-month window of exactly the
indicators the block names**, ending on the month the release reports. Fifth District = **MD,
VA, NC, SC, WV, DC**.

| | |
|---|---:|
| Records | **757** |
| Timesteps / datapoints | 27,252 / 106,128 |
| Points per channel | **100% exactly 36** |
| Channels per record | median 4, max 12 (36 distinct) |
| Nulls | 0.63% (all leading runs — see 49's README) |
| Data-month span | **2001-04 → 2026-06** (224 distinct months) |
| Text | median **482 chars**, 757/757 distinct |
| `alignment` | 246 `recites` / 511 `describes` (measured per record) |

---

## ✅ The "suspected wrong workbook" flag is disproven

The audit flagged that this package's config names `nmf_historicaldata.xlsx` while its cached
workbook was called `mfg_historicaldata.xlsx` — the same name as #49's — and warned that if it
was silently building off the *manufacturing* workbook, its series were wrong and the package
invalid.

**It was not.** The cached file's content is the non-manufacturing data: sheet
`Non-Mfg Historical Series`, 45 columns, all `*_svc_*`, matching this config's channels (and the
two files differ by md5). The filename came from a hardcoded cache path in the ported
`load_series()`, shared by both packages — a cosmetic cache-naming bug, not a data bug. The
cache is now `nmf_historicaldata.xlsx`, named from `data.workbook_cache_name` in config.

## The vintage finding, for this survey

The workbook is now used **only** by `--audit-vintage`, because it is the *revised* series — the
release's own Technical Notes say the seasonal factors are recalculated every July and the whole
history revised. Measured over the harvest, the value a release printed still equals today's
`nmf_historicaldata.xlsx` in **2,054 of 8,964 cells = 22.9%**, median drift **2.0 index points**
(max 48): 11-20% through 2018-23, 70% in 2025, **100% in 2026**. Cross-release overlap agreement
is **94.6-100% in eleven release months and 42.9% in July** — the re-benchmark — and the
independently-printed 3-month-average column checksums **2,312/2,321**.

**A second, independent reason the workbook cannot supply this series:** the Bank restructured
the survey and **dropped the retail / non-retail split entirely**. The 1997-2018 releases publish
`retail_*` and `nonretail_*` indicators (sales revenues, big-ticket sales, shopper traffic,
inventories, per-sector prices) that have **no column in today's workbook at all**. Those
channels exist only in the releases.

---

## What is specific to this survey

- **Sector-split sections.** The 1997-2018 releases section their narrative by sector —
  `Overall` / `Retail` / `Non-retail Services` (called `Services Firms` from ~2005) — alongside
  topic sections. A channel's sector prefix is taken from the **sector heading** where the release
  prints one, and only falls back to sentence wording for topic sections (see defect B).
- **One price question per sector, not the paid/received pair.** The older tables label price rows
  by *sector* ("Service Sector" / "Retail" / "Services Firms"; just "Services" in the retired
  layout). Those become a distinct `prices` channel (with `retail_`/`nonretail_` prefixes) rather
  than being guessed into `prices_paid` or `prices_received` — nothing in the source says which,
  and inventing the distinction would put the wrong series beside the prose.
- **Blocks per release:** 5.4 (1997-2004) · 4.0 (2008-2017) · 4.0 (2018-2026).

---

## Alignment — measured (`--audit-alignment`)

**Structural: 757/757 on all five checks** (`period_end` == reported month · `period_start` ==
window start · every channel length == `meta.n_points` · terminal non-null · exactly one
`<ts></ts>`).

| test | true rate | permutation control | lift |
|---|--:|--:|--:|
| **ordered pair** — prose quotes a channel's last two points in order (shipped) | **32.5%** | **0.8%** | **+31.7 pp** |

**Stated "N points" change matches a channel's own last-two-point delta: 396/537 = 73.7%.**
Dominant direction agrees in **79.6%**.

⚠️ **Quotation is measurably weaker than the manufacturing twin: 271 of 2,870 quoted figures
(9.44%) are not values in that record's own series, against 1.89% for #49** (624/757 records are
fully clean). Reported rather than filtered, and the cause is characterised: the residual
concentrates in the retired-era `Prices` sections, which walk through the district-wide, retail
and non-retail price series in consecutive sentences while the release's own table publishes fewer
than three price rows in some years — so a sentence's figure has no channel to land in. That is a
source-structure limit, not a demonstrated misalignment; the same call `48_dallas_tmos` made at
3.8%. Tightening it would mean clause-level trimming of the Bank's own sentences, as `47` did.

---

## Reconcile (the build raises if it does not balance)

308 releases → 292 with a parseable table, 308 with narrative.
**1,338 block units = 757 emitted + 177 no-indicator-named + 22 short-text (<120 chars) +
301 sparse-or-short-window + 81 in the 16 releases with no table ✓**

---

## Defects fixed at inspection — beyond the thirteen shared with #49

| # | Defect | Effect |
|---|---|---|
| A2 | **22 of the cached CSVs are a spreadsheet dump of the table**, not the `section_type` export — a dateline row, a `September / August / July / 3-Month Average` header, group rows, and the label in whichever column the sheet used | the CSV reader returned nothing for them, so 6 more releases looked table-less. Both layouts now share one cell-row reader, and this one carries its own month names (no era-convention fallback) plus a free average checksum |
| A | **The service CSVs key `section_type` by indicator *group*** ("Service-Sector Indicators", "Retail Indicators"), where the manufacturing CSVs use a horizon caption ("Compared to the previous month" / "Six months from now"). Requiring a horizon caption rejected **every row of all 99 service CSVs** | this survey had *no table at all* for 2008-10…2009-06, 2011-09…2012-08 and 2014-01. Fixed: **+114 records** at the time it was found |
| B | Retail/non-retail prefix guessed per sentence instead of taken from the section heading | under `Retail`, "Shopper traffic increased 24 points" names no sector, so it resolved to the district-wide channel the retired table does not carry; under `Overall`, "an upsurge in revenues at services firms" pulled in the non-retail series while the sentence's subject was the aggregate index. Stated-points match **44.4% → 75.7%** |
| C | The retired table prints "Product demand during next six months" **inside the current-conditions block** — that layout has no expectations block at all | prose about the forward-looking demand index requested a channel the record did not carry, the largest single group of unmatched figures. A row whose own label names its horizon is now filed by that label, not by the block it sits in |
| D | The 2018-01-and-earlier layout prints a trailing **"3-Month Average"** column past the last month column | the value could not be assigned to any column, so **every row of the retail/services-firms layout was rejected**. Captured instead, it also supplies a free within-row checksum |

---

## Caveats and headroom

- ⚠️ **Modern-era coverage is thinner around 2017-19** (18 / 22 / 24 records against 36-48/yr
  elsewhere): 16 of 308 releases still print no results table and have no usable CSV. The release
  *page* was checked for all of them — it is a navigation shell with no table in that era, so
  this is a source limit, not an unfetched document.
- ⚠️ **2005-01 … 2008-09 is a hard gap** (no web capture in any layout) and **1998 has no table
  page anywhere** — identical to #49. Because a 36-month window cannot span either, the retired
  era's first record is 2001-04 and the modern era's is 2011+.
- **The channel vocabulary changes at ~2018-02**, when the Bank dropped the retail/non-retail
  split. Windows never span the change because the sector channels simply stop; the aggregate
  channels (`revenues`, `employment`, `wages`, `demand`) run through it.
- ⚠️ **Forecast channels are included** (`*_fut`, six months ahead) — the survey's own question,
  which the prose discusses explicitly, not a projection this build invented.

---

## Run

```bash
pip install -r requirements.txt          # + apt-get install poppler-utils
python scripts/harvest.py                # enumerate + fetch releases (cached; ~384 docs)
python scripts/harvest.py --report       # coverage of what is cached
python scripts/build_cpt_jsonl.py        # full build
python scripts/build_cpt_jsonl.py --audit-vintage     # as-published vs published workbook
python scripts/build_cpt_jsonl.py --audit-overlap     # the stitch's own consistency check
python scripts/build_cpt_jsonl.py --audit-convention  # data-month rule, measured per era
python scripts/build_cpt_jsonl.py --audit-alignment   # everything in the section above
```

**Output:** `output/richmond_nonmanufacturing_cpt.jsonl` + `output/run_report.json`;
`samples/example_output.jsonl` = first 3 records. (`.cache/` git-ignored.)

**Null values:** 0.63% of datapoints, **all leading runs at the oldest end of the window** and
clustered on the two source walls (1998-06…09, 2008-04…07). The full analysis, including what a
zero-null build would cost (757 → 711 here), is in
[49's README](../49_richmond_manufacturing/README.md#why-some-channels-have-leading-nulls-and-can-it-go-further).

**Sources:** [Richmond Fed service sector survey](https://www.richmondfed.org/research/regional_economy/surveys_of_business_conditions/service_sector)
— **U.S. public domain**.
