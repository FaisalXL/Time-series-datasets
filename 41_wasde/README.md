# USDA WASDE (World Agricultural Supply and Demand Estimates) → CPT

> **Status: ✅ Finalized 2026-07-25.** **1,840 records** (`public-domain-us-gov`, 17 U.S.C. §105).
> 1,840/1,840 pass `validate.py --strict` with 0 warnings. New-domain **agriculture**;
> `new_datasets.md` #41.

**What it is:** One record = **(commodity × release month)** — the monthly WASDE report's own
per-commodity narrative block paired, under a **single `<ts>`**, with that commodity's whole
**balance sheet as multiple channels** over a **trailing 32-month window ending on the month the
report covers**.

**Six commodities**, 1997-09 → 2026-07: soybean 342 · rice 339 · corn 337 · wheat 336 ·
cotton 327 · sugar 159. **10,697 channels**, **58,880 timesteps**, **342,304 datapoints**.

#### 📄 Text — the commodity's own narrative block

| | |
|---|---|
| **What** | One narrative section of the report: `WHEAT:`, `COARSE GRAINS:`, `RICE:`, `OILSEEDS:`, `SUGAR:`, `COTTON:` |
| **Source** | Report **PDF** (`pdftotext -layout`) in the xml era; the machine-readable **`.txt`** narrative for 1995–2009 |
| **Format** | Median 2,003 chars (min 198, max 5,069) |
| **`text_quality`** | `real`. **Nothing in `text` is written by this script** — `<ts></ts>` is appended directly to the extracted narrative. Distinct texts: 1,840/1,840 (100%). |

#### 📈 Time series — the continuous current-crop projection, two eras stitched

Each channel is the commodity's balance-sheet line (beginning stocks, production, imports,
domestic use / crush, exports, ending stocks — 5–6 channels) as a **continuous monthly line**: at
each report we take the *then-current* headline ("Proj.") marketing year's this-month value and
stitch chronologically. Two source eras join seamlessly:

- **`.xml` (2010-07→present):** `Report[@sub_report_title]→attribute→market_year→forecast_month→Cell` — unambiguous, no unit guessing.
- **`.txt` (1995–2009):** fixed-width tables — this-month = the last numeric column, headline MY from the `"YYYY/YY Projections"` header. Machine-readable, **no OCR**.

Verified continuous across the era boundary (wheat ending stocks `…2009-12: 900 → 2010-01: 976…`;
corn stays bushels `…1675 → 1764…`).

**Window:** `1m`, **trailing 32 months ending on the reported month** — matched to the model's
32-point patch size. Length is a guarantee: **100% exactly 32 points** (min = med = max = 32).
Months without a full 32-month history behind them are skipped rather than emitted short.

The series **deliberately crosses new-crop transitions** — a real regime step (e.g. **938 → 762**
as the new crop opens), not an artifact. `meta.new_crop_resets` counts them per window (median 3).

### Alignment — tagged per record, on the anchor channel

**Structural, and exact in 100% of records:**

| Check | Result |
|---|--:|
| Window terminal month == the month the report covers | **1,840/1,840 (100%)** |
| `period_end` == the reported month | **1,840/1,840 (100%)** |
| `period_start` == the window's first month | **1,840/1,840 (100%)** |
| `vintage_months` length == series length | **1,840/1,840 (100%)** |
| Window months strictly chronological | **1,840/1,840 (100%)** |

**Semantic — does the prose actually state the number?** A record is `recites` when the **anchor
channel's** (ending stocks) endpoint appears in its prose, else `describes` →
**1,071 recites + 769 describes**.

> ⚠️ **The previous rule was "any of the 6 channels", and it was not trustworthy.** Measured with a
> **permutation control** — the same endpoints tested against a *different month's* prose for the
> *same commodity*, which should almost never match — the any-channel rule fired **34.1%** of the
> time on mismatched text against a 75.8% true rate. **More than a third of its `recites` tags were
> coincidence.** Restricting the test to the anchor drops the control to **3.8%** against a 58.2%
> true rate:

| Commodity | n | own prose | other month's prose | lift |
|---|--:|--:|--:|--:|
| soybean | 342 | 85.1% | 2.3% | **+82.7** |
| rice | 339 | 85.3% | 12.4% | **+72.9** |
| cotton | 327 | 72.8% | 4.9% | **+67.9** |
| wheat | 336 | 40.8% | 0.6% | **+40.2** |
| corn | 337 | 30.6% | 0.6% | **+30.0** |
| sugar | 159 | 8.2% | 0.0% | +8.2 |
| **All** | **1,840** | **58.2%** | **3.8%** | **+54.4** |

The anchor is also the line the window is built on and the figure WASDE prose leads with, so it is
the honest test. The tag is reproducible from text+series in **1,840/1,840 (100%)**.

**Sugar is `describes` in substance** (8.2% anchor-recite, 0.0% control): its prose narrates
beet/cane components and NASS acreage — *"beet sugar production is projected at 4.821 million
STRV"* — not the table's totals. Real, first-party, on-topic prose about the balance sheet; it just
does not quote it. Kept, correctly tagged.

### Reconcile — balances exactly, and the build raises if it doesn't

```
376 reports (190 .xml + 186 .txt) × 6 commodities = 2,256 attempts
      1,840 emitted
    +   210 dropped: anchor line has no value that month (186 = sugar, which has no .txt tier)
    +   186 dropped: fewer than 32 months of history behind the report
    +    15 dropped: corrupted PDF text layer (see below)
    +     5 dropped: no usable prose block
    +     0 long-block / 0 invalid
    = 2,256 ✓
```

### Exhaustion — the machine-readable era is complete

The ESMIS API lists **398 WASDE releases with usable files**, which collapse to **376 distinct
months** (21 months ship a correction/re-issue; 2019-11 ships three — one report per month is
correct, not data loss). Against a full monthly calendar for 1995-01 → 2026-07 (379 months),
exactly **3 are absent — 2013-10, 2019-01 and 2025-10 — all federal government shutdowns, when no
WASDE was published.** So **376/376 available months are harvested: zero real misses.**

Pre-1995 releases are **pdf-only image scans** (no text layer — `pdftotext` yields ~27 chars) and
would need OCR; they are out of scope by design.

### Headroom — measured, and deliberately not taken

The config previously listed "sorghum-barley-oats + livestock/poultry/dairy/eggs" as candidates.
Checked against the July 2026 XML and the May 2003 `.txt`:

- **Sorghum / barley / oats — not real headroom.** A U.S. table exists in both eras, but the
  narrative has **no section of its own**: they are discussed inside `COARSE GRAINS:`, which corn
  already uses. Splitting them out would re-ship corn's paragraph under a second label —
  boilerplate reuse, banned by `SCHEMA.md` "no fake scale".
- **World tables** (wheat/corn/rice/soybean/cotton/coarse grain) — same problem: the world numbers
  are discussed **inside the same commodity paragraph** already used.
- **Livestock / poultry / dairy / eggs — this one IS real (~350 records).**
  `LIVESTOCK, POULTRY, AND DAIRY:` is a genuine **7th narrative section**, present in **both eras**,
  with its own tables. Not taken here because it needs engineering rather than a config entry:
  the `U.S. Meats Supply and Use` table exposes **no `attribute*` values in the XML** (280 cells,
  all `attribute=None`), so it needs a positional parse path; and one joint narrative would have to
  be paired with **four heterogeneous tables** (Meats + Egg balance sheets, Dairy Prices, Quarterly
  Animal Product Production) in different units — the weakest-aligned record type in the package.

### Known limits (data, not bugs)

- **⚠️ Forecast, not measured — the core caveat.** The series tracks USDA *revising its own
  projection* month to month: a forecast-revision trajectory, not a physical measurement. The
  measured cousin is USDA **NASS Crop Production / Quick Stats** (surveyed actuals) — a natural
  sibling package.
- **Corrupted PDF text layers.** 15 records from **2016-09 → 2017-02** were dropped: those releases
  duplicate glyphs — *"wheaat ending sttocks are raaised"*, *"cconsumptioon"*. It is in the PDF
  itself, not a `-layout` artifact (`-raw` and default mode show it too), so it cannot be
  re-extracted around; and repairing the characters would mean rewriting source prose, which
  `text_quality: real` forbids. `data.max_garble_ratio` (0.015) is the guard; ALL-CAPS tokens are
  skipped so acronyms like **CCC** (Commodity Credit Corporation) don't trip it.
- **One record, `wasde_rice_200001_2000-07`, opens mid-section** rather than at `RICE:` (0.05%).
- **Two-panel tables → `month_style` / `txt_subsection`.** Some tables stack **two measure panels**,
  keyed in XML by forecast-month *spelling*: the **Feed-Grain-and-Corn** table puts feed-grain
  **metric tons** in the abbreviated (`"Jul"`) panel and **corn bushels** in the full (`"July"`)
  panel (`xml_month_style: full`); in `.txt` the same split is a `CORN` subsection. **Units differ:**
  wheat/corn/soy = mil bu, rice = mil cwt, cotton = mil 480-lb bales, sugar = 1,000 STRV.
- **Leakage note.** For `recites` records the prose states the anchor channel's endpoint (its last
  point). Standard for value-reciting alignment; mask the last point per channel for a stricter variant.

### Fixed during the 2026-07-25 final inspection

1. **Window 24 → 32 months.** The old build capped at 24, below the model's 32-point patch size —
   the config's own comment flagged it as a placeholder ("≥~32 ideal; 24 for now, extend on ask").
2. **Recite tag re-based on the anchor channel** (the any-channel rule's 34% false-positive floor,
   above).
3. **Cotton prose contamination.** Cotton is the last narrative section, so its `__LAST_SECTION__`
   end marker fell back to a blind 4,000-char slice that ran through the Outlook Board sign-off,
   USDA conference advertisements (*"DOWNLOAD SPEECHES … $21.00 … Off press April"*) and into **raw
   ASCII data tables** — contaminating all 327 cotton records and **inflating their recite rate**,
   since the scraped tables contain the very numbers the recite test looks for. Blocks now end at
   the earliest of the configured marker, any other commodity's heading, and the narrative sign-off
   (`NARRATIVE_END`). One 2022 cotton block that reached **37,776 chars** by running through a
   committee roster is cut by the same rule; `data.max_block_chars` is the backstop.
4. **Cross-section bleed.** WASDE reorders its narrative sections across eras (OILSEEDS ran before
   RICE in 1998 and 2008), so 3 corn records had swallowed the following OILSEEDS block. Now **0**.
5. **Reconcile assertion** — the build now raises rather than ship an unexplained gap.

### Run

```bash
pip install -r requirements.txt          # + poppler (pdftotext)
python scripts/build_cpt_jsonl.py        # full 1995→2026 (376 reports, 1,840 records)
python scripts/verify.py                 # final-inspection pass
python ../schema/validate.py output/wasde_cpt.jsonl --strict
```

Reports are enumerated from the **ESMIS REST API** (`/api/v1/release/findByIdentifier/wasde`,
fault-tolerant — retries then skips transient 5xx) and cached under `.cache/reports/` (~527 MB for
the full span), so rebuilds are offline and free. `data.use_txt_tier` toggles the 1995–2009 era;
`data.max_reports` caps newest-N.

**Output:** `output/wasde_cpt.jsonl` + `run_report.json`; `samples/example_output.jsonl`.

**Sources:** [USDA OCE WASDE](https://www.usda.gov/oce/commodity/wasde/) ·
[ESMIS release files](https://esmis.nal.usda.gov/publication/world-agricultural-supply-and-demand-estimates) ·
[Cornell mannlib archive 1973→](https://usda.library.cornell.edu/concern/publications/3t945q76s).
**US public domain.** See [NOTION_PAGE.md](NOTION_PAGE.md).
