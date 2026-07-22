# USDA WASDE (World Agricultural Supply and Demand Estimates) → CPT

> **Status: Built.** **980 records** — 6 commodities (wheat, corn, soybean, rice, cotton, sugar) × ~167,
> **uniform 24-month windows**, full XML era **2010-07 → 2026-07** (190 reports, auto-fetched via the
> ESMIS API). New-domain **agriculture**. `new_datasets.md` #41. **US public domain** (17 U.S.C. §105).

**What it is:** One record = **(commodity × release month)** — the monthly WASDE report's per-commodity
narrative block paired with a trailing 24-month window of the **continuous monthly current-marketing-year
projection** of that commodity's ending stocks (the report's own headline figure at the window's end, the
then-current crop's projection at each earlier month). **Alignment is per-commodity**, set by the measured
endpoint-recite rate:

| alignment | commodities | endpoint recited |
|---|---|---|
| `recites` (668 recs) | soybean, rice, cotton, wheat | 89% / 86% / 74% / 62% |
| `describes` (312 recs) | corn, sugar | 36% / 8% |

Corn's narrative is change/old-crop oriented; sugar's is often administrative — both *describe* the balance
sheet but rarely restate the level. Their series **values are correct** (right unit/panel); only the
text-to-level coupling is weaker, hence the honest downgrade.

#### 📄 Text — per-commodity prose (PDF)
The report PDF (`pdftotext`) carries clean narrative blocks — `WHEAT:`, `COARSE GRAINS:`, `RICE:`,
`OILSEEDS:`, `SUGAR:`, `LIVESTOCK, POULTRY, AND DAIRY:`, `COTTON:` — each reciting its balance sheet
(e.g. *"Projected 2026/27 ending stocks are reduced 22 million bushels to 722 million"*).

#### 📈 Time series — continuous current-crop forecast projection (XML)
The report **XML** is cleanly structured (`Report[@sub_report_title]` → `attribute` → `market_year` →
`forecast_month` → `Cell[@cell_value]`), so values are unambiguous (no PDF unit/footnote guessing).
For a record at report month M, the series is a **continuous monthly line**: at each report we take the
*then-current* (headline "Proj.") marketing year's this-month value, stitched chronologically up to M
(`freq: 1m`, trailing 24 months). It **deliberately crosses new-crop transitions** — a real regime step —
so the model sees a genuine long monthly signal, not a ~12-point single-crop stub. The **endpoint** is
report M's own headline figure, which its prose recites. Example (U.S. wheat ending stocks, ending 2026-07):
`…938 → 762 → 744 → 722` — the 2025/26 crop's last headline (938), the 2026/27 new-crop reset (762),
revised down to the prose-recited **722**.

**Record shape** (real, abridged):
```json
{
  "text": "Supplies are reduced 22 million bushels on lower beginning stocks and production... Projected 2026/27 ending stocks are reduced 22 million bushels to 722 million...\n\nUSDA's successive monthly WASDE projections of U.S. wheat ending stocks for the then-current marketing year, across the trailing 24 monthly reports through 2026-07 (ending with the 2026/27 estimate): <ts></ts>",
  "timeseries": [{"values": [856.0, 828.0, "…20 more…", 938.0, 762.0, 744.0, 722.0], "unit": "wheat_ending_stocks_mil_bu", "freq": "1m"}],
  "task_type": "world_knowledge", "text_quality": "real",
  "alignment": "recites", "license": "public-domain-us-gov",
  "source": "https://www.usda.gov/oce/commodity/wasde/", "dataset": "wasde",
  "domain": "agriculture", "region": "US", "period_start": "2024-07-01", "period_end": "2026-07-01",
  "meta": {"commodity": "wheat", "attribute": "Ending Stocks", "marketing_year": "2026/27",
           "report_month": "2026-07", "window": 24,
           "marketing_years_spanned": ["2024/25","2025/26","2026/27"], "new_crop_resets": 2}
}
```

**Key issues / caveats:**
- **⚠️ Forecast, not measured (the core caveat).** The series tracks USDA *revising its own projection*
  month to month — a forecast-revision trajectory, not a physical measurement. This is the opposite of the
  "measured signal" preference. The measured cousin is USDA **NASS Crop Production / Quick Stats**
  (surveyed actuals) — a natural sibling package.
- **Enumeration via the ESMIS REST API (no scraping, no manual download).** The build paginates
  [`/api/v1/release/findByIdentifier/wasde`](https://esmis.nal.usda.gov/api-documentation) — 700 releases,
  each with file URLs + date — and auto-fetches each release's `.xml` (series) + `.pdf` (prose) into the
  cache. **Machine-readable `.xml` exists 2010-07→present (193 reports)**; older releases (1973→~2010,
  **450 PDF-only scans**, + 57 txt/errata) → OCR-feasible phase-2 tier, cross-checked prose↔table.
  `data.max_reports` caps how many newest XML-era reports to pull; `null` = all ~193.
- **⚠️ Two-panel tables → per-commodity `month_style` (the corn/soy unit fix).** Some WASDE tables stack
  **two measure panels** under one `sub_report_title`, keyed by the forecast-month *spelling*: abbreviated
  (`"Jul"`) vs full (`"July"`). The combined **Feed-Grain-and-Corn** table puts feed-grain **metric tons**
  in the abbreviated panel and **corn bushels** in the full panel; wheat/soy/rice/cotton/sugar read the
  **abbreviated** panel. Each commodity declares which to read via `month_style` + the right XML
  `title_match` (validated against the prose). **Units differ:** wheat/corn/soy = mil bu, rice = mil cwt,
  cotton = mil 480-lb bales, sugar = 1,000 short tons raw value. Still candidate: **sorghum/barley/oats**
  (combined-aggregate table, shares corn's prose — weak), plus livestock/poultry/dairy/eggs (quarterly,
  different structure).
- **⚠️ Alignment differs by commodity** (see the table above; set by measured endpoint-recite rate).
  `recites`: soybean/rice/cotton/wheat. `describes`: **corn** (~36%, change/old-crop narrative) and
  **sugar** (~8%, often-administrative prose — the **weakest link / drop-candidate**). Values are correct
  in all cases; only the text-to-level coupling varies.
- **⚠️ Continuous series crosses new-crop resets (by design).** To give the model a long monthly signal
  (it patches series in **32-point blocks**, so a ~12-point single-crop stub is penalised), the series is a
  **continuous current-crop projection** — each month's then-current headline value, stitched across
  reports. This **deliberately crosses new-crop transitions**, which are **real regime steps** (e.g. the
  2025/26 crop's last headline **938 → 762** as the 2026/27 crop opens), not artifacts. `meta.new_crop_resets`
  records how many transitions each window spans (typically 2 over 24 months). Window is **24** for now
  (`data.window_max`); extend later on request — Xinyue's target is ≥~32.
- **Endpoint recited + history as context** (`recites` commodities). For wheat & soybean the prose recites
  the **endpoint** (report M's headline figure) plus this-year-vs-last-year deltas; earlier months are honest
  context — the same endpoint-recites convention as the Fed (24-month) and EIA (52-week) packages. The minority
  of wheat/soy records that don't restate the number say the balance sheet is *"unchanged relative to last
  month"* (the figure lives only in the numeric table then); the value is recoverable from the prior recited
  figure, so those are kept as a weak `recites`.
- **Leakage note.** The prose recites the endpoint value (the last series point). Standard for value-reciting
  `recites`; mask the last point if a stricter variant is wanted.

**Run** (fetches reports from the ESMIS API automatically):
```bash
pip install -r requirements.txt          # + poppler (pdftotext)
python scripts/build_cpt_jsonl.py --dry-run --set data.max_reports=30 --set output.max_records=3  # smoke
python scripts/build_cpt_jsonl.py                                        # full XML era (~193 reports, 501 records)
python scripts/build_cpt_jsonl.py --set data.max_reports=48              # newest-48 subset (faster)
```

**Extend:** widen `data.window_max` (Xinyue's target is ≥~32); add validated commodities to
`data.commodities` (each needs its `month_style` panel + `alignment` validated against the prose).
Downloaded reports are cached under `.cache/reports/` (~2.9 MB/report, ~560 MB for the full era).

**Output:** `output/wasde_cpt.jsonl` + `run_report.json`; `samples/example_output.jsonl`.

**Sources:** [USDA OCE WASDE](https://www.usda.gov/oce/commodity/wasde/) (current) · [ESMIS release files](https://esmis.nal.usda.gov/publication/world-agricultural-supply-and-demand-estimates) · [Cornell mannlib archive 1973→](https://usda.library.cornell.edu/concern/publications/3t945q76s). **US public domain.** See [NOTION_PAGE.md](NOTION_PAGE.md).
