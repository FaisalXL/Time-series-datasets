# USDA WASDE (World Agricultural Supply and Demand Estimates) → CPT

> **Status: Built.** **1,904 records** — 6 commodities (wheat, corn, soybean, rice, cotton, sugar),
> one **multi-channel** record per (commodity × release month) bundling the whole balance sheet
> (**11,072 channels** total), **uniform 24-month windows**, spanning **1995-01 → 2026-07** (376 reports:
> 190 `.xml` + 186 `.txt`, auto-fetched via the ESMIS API). New-domain **agriculture**. `new_datasets.md`
> #41. **US public domain** (17 U.S.C. §105).

**What it is:** One record = **(commodity × release month)** — the monthly WASDE report's per-commodity
narrative block paired, under a **single `<ts>`**, with that commodity's whole **balance sheet as multiple
channels** (beginning stocks, production, imports, domestic use / crush, exports, ending stocks — 5–6
channels). Each channel is a trailing 24-month **continuous monthly current-crop projection** of that line
(the report's own headline figure at the window's end, the then-current crop at each earlier month). This
**avoids duplicating the prose** across attributes: 1,904 records = 1,904 unique paragraphs (vs. an earlier
per-attribute design that repeated each paragraph ~2.6×). Bonus: the channels satisfy the balance identity
(begin + production + imports = supply; domestic + exports + ending stocks = supply).

**Alignment is per-record**, set by whether the prose states *any* of the record's channel endpoints:
`recites` if yes, else `describes`. Overall **1,446 recites + 458 describes** (76% recite — the
balance-sheet paragraph almost always states at least one plotted line, e.g. production or ending stocks).
Series **values are correct** everywhere (validated unit/panel).

#### 📄 Text — per-commodity prose (two eras)
Clean narrative blocks — `WHEAT:`, `COARSE GRAINS:`, `RICE:`, `OILSEEDS:`, `SUGAR:`, `COTTON:` — each
discussing that commodity's balance sheet (e.g. *"Projected 2026/27 ending stocks are reduced 22 million
bushels to 722 million"*). Prose comes from the report **PDF** (`pdftotext`) in the xml era and from the
**`.txt`** narrative in the 1995–2009 era.

#### 📈 Time series — continuous current-crop projection, two eras stitched
For a record at report month M, the series is a **continuous monthly line**: at each report we take the
*then-current* (headline "Proj.") marketing year's this-month value, stitched chronologically up to M
(`freq: 1m`, trailing 24 months), stitching two source eras seamlessly:
- **`.xml` (2010-07→present):** structured `Report[@sub_report_title]→attribute→market_year→forecast_month→Cell`,
  unambiguous (no unit guessing).
- **`.txt` (1995–2009):** fixed-width tables — this-month = the last numeric column, headline MY from the
  `"YYYY/YY Projections"` header. Machine-readable, **no OCR**.

It **deliberately crosses new-crop transitions** — a real regime step — so the model sees a long monthly
signal, not a ~12-point single-crop stub. Verified continuous across the era boundary (wheat ending stocks
`…2009-12: 900 → 2010-01: 976…`; corn stays bushels `…2009-12: 1675 → 2010-01: 1764…`). Example
(wheat ending stocks, ending 2026-07): `…938 → 762 → 744 → 722` — the 2025/26 crop's last headline (938),
the 2026/27 new-crop reset (762), revised to the prose-recited **722**.

**Record shape** (real, abridged — one multi-channel record):
```json
{
  "text": "Supplies are reduced 22 million bushels... Projected 2026/27 ending stocks are reduced 22 million bushels to 722 million...\n\nUSDA's successive monthly WASDE balance-sheet projections for U.S. wheat — beginning stocks, production, imports, domestic use, exports, and ending stocks — for the then-current marketing year (2026/27), across the trailing 24 monthly reports through 2026-07: <ts></ts>",
  "timeseries": [
    {"values": ["…24…", 920.0], "unit": "wheat_beginning_stocks_mil_bu", "freq": "1m"},
    {"values": ["…24…", 1536.0], "unit": "wheat_production_mil_bu", "freq": "1m"},
    {"values": ["…24…", 140.0], "unit": "wheat_imports_mil_bu", "freq": "1m"},
    {"values": ["…24…", 1099.0], "unit": "wheat_domestic_use_mil_bu", "freq": "1m"},
    {"values": ["…24…", 775.0], "unit": "wheat_exports_mil_bu", "freq": "1m"},
    {"values": ["…24…", 722.0], "unit": "wheat_ending_stocks_mil_bu", "freq": "1m"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "alignment": "recites", "license": "public-domain-us-gov",
  "source": "https://www.usda.gov/oce/commodity/wasde/", "dataset": "wasde",
  "domain": "agriculture", "region": "US", "period_start": "2024-07-01", "period_end": "2026-07-01",
  "meta": {"commodity": "wheat", "attributes": ["beginning stocks","production","imports","domestic use","exports","ending stocks"],
           "n_channels": 6, "marketing_year": "2026/27", "report_month": "2026-07", "source_format": "xml",
           "window": 24, "marketing_years_spanned": ["2024/25","2025/26","2026/27"], "new_crop_resets": 2}
}
```
*(Single `<ts>`, 6 index-aligned channels — the whole wheat balance sheet as one multivariate series.)*

**Key issues / caveats:**
- **⚠️ Forecast, not measured (the core caveat).** The series tracks USDA *revising its own projection*
  month to month — a forecast-revision trajectory, not a physical measurement. This is the opposite of the
  "measured signal" preference. The measured cousin is USDA **NASS Crop Production / Quick Stats**
  (surveyed actuals) — a natural sibling package.
- **Enumeration via the ESMIS REST API (no scraping, no manual download).** The build paginates
  [`/api/v1/release/findByIdentifier/wasde`](https://esmis.nal.usda.gov/api-documentation) (fault-tolerant —
  retries + skips transient 5xx) — 700 releases, each with file URLs + date — and auto-fetches per era:
  **`.xml` 2010-07→present (190 reports)**, and **machine-readable `.txt` for 1995–2009 (186 reports, no
  OCR)**. Pre-1995 releases are **pdf-only image scans** (no text layer — `pdftotext` yields ~27 chars) →
  OCR phase-2 tier. `data.use_txt_tier` toggles the txt era; `data.max_reports` caps newest-N.
- **⚠️ Two-panel tables → `month_style` / `txt_subsection` (the corn unit fix).** Some tables stack **two
  measure panels**. In XML they're keyed by forecast-month *spelling* — abbreviated (`"Jul"`) vs full
  (`"July"`): the **Feed-Grain-and-Corn** table puts feed-grain **metric tons** in the abbr panel and **corn
  bushels** in the full panel (`xml_month_style: full`). In `.txt` the same split is a **`CORN` subsection**
  below the metric panel (`txt_subsection: CORN`). Wheat/soy/rice/cotton/sugar read the abbreviated/first
  panel. **Units differ:** wheat/corn/soy = mil bu, rice = mil cwt, cotton = mil 480-lb bales, sugar = 1,000
  STRV. Verified continuous across the era boundary (corn stays bushels, no metric contamination). Still
  candidate: **sorghum/barley/oats** (combined-aggregate) + livestock/poultry/dairy/eggs (quarterly).
- **Multi-channel, one record per (commodity, report)** — the balance-sheet lines are bundled as channels
  under a single `<ts>` (index-aligned, equal length), *not* emitted as separate per-attribute records. This
  eliminates prose duplication (1,904 records = 1,904 unique paragraphs; the earlier per-attribute design
  repeated each paragraph ~2.6×) and better matches the text, which describes the whole balance sheet. A
  channel is dropped from a record only if it's missing at any window month (keeps channels equal-length);
  most records carry all 6 (rice 5).
- **Alignment is per-record** (computed by the build, not configured): `recites` if the prose states **any**
  of the record's channel endpoints, else `describes`. Overall **1,446 recites + 458 describes** (76%). The
  balance-sheet paragraph almost always states at least one plotted line (production and/or ending stocks),
  so most records recite; the `describes` minority is where the prose is purely directional (esp. the older
  `.txt` era) or administrative (sugar). Series **values are correct** everywhere.
- **⚠️ Continuous series crosses new-crop resets (by design).** To give the model a long monthly signal
  (it patches series in **32-point blocks**, so a ~12-point single-crop stub is penalised), the series
  **deliberately crosses new-crop transitions** — **real regime steps** (e.g. **938 → 762** as the new crop
  opens), not artifacts. `meta.new_crop_resets` counts them per window. Window is **24** for now
  (`data.window_max`); extend on request — Xinyue's target is ≥~32.
- **Sugar is the weakest** (~8% of channels recited, XML-only, often-administrative prose) — a drop-candidate.
- **Leakage note.** For `recites` records the prose states at least one channel's endpoint value (its last
  point). Standard for value-reciting alignment; mask the last point per channel if a stricter variant is wanted.

**Run** (fetches reports from the ESMIS API automatically):
```bash
pip install -r requirements.txt          # + poppler (pdftotext)
python scripts/build_cpt_jsonl.py --dry-run --set data.max_reports=30 --set output.max_records=3  # smoke
python scripts/build_cpt_jsonl.py                                        # full 1995→2026 (376 reports, 1,904 records)
python scripts/build_cpt_jsonl.py --set data.use_txt_tier=false          # xml era only (2010→, faster)
```

**Extend:** widen `data.window_max` (Xinyue's target is ≥~32); add commodities to `data.commodities`, or add
channels to a commodity's `channels` list (each: `xml_name`, `txt_label`, `channel`, `human`). Footnoted
attribute names are matched tolerantly; alignment is auto-tagged per record. Reports cache under
`.cache/reports/` (~2.9 MB/xml-report; ~1 GB for the full 1995→2026 span).

**Output:** `output/wasde_cpt.jsonl` + `run_report.json`; `samples/example_output.jsonl`.

**Sources:** [USDA OCE WASDE](https://www.usda.gov/oce/commodity/wasde/) (current) · [ESMIS release files](https://esmis.nal.usda.gov/publication/world-agricultural-supply-and-demand-estimates) · [Cornell mannlib archive 1973→](https://usda.library.cornell.edu/concern/publications/3t945q76s). **US public domain.** See [NOTION_PAGE.md](NOTION_PAGE.md).
