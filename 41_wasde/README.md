# USDA WASDE (World Agricultural Supply and Demand Estimates) → CPT

> **Status: Built (v1 — wheat).** Demo: 7 records from the 9 most-recent reports (short windows).
> New-domain **agriculture**. `new_datasets.md` #41. **US public domain** (17 U.S.C. §105).

**What it is:** One record = **(commodity × release month)** — the monthly WASDE report's per-commodity
narrative block (which *recites* the balance-sheet figures) paired with a trailing window of that
attribute's **monthly forecast vintages** for the report's headline marketing year. `alignment: recites`.

#### 📄 Text — per-commodity prose (PDF)
The report PDF (`pdftotext`) carries clean narrative blocks — `WHEAT:`, `COARSE GRAINS:`, `RICE:`,
`OILSEEDS:`, `SUGAR:`, `LIVESTOCK, POULTRY, AND DAIRY:`, `COTTON:` — each reciting its balance sheet
(e.g. *"Projected 2026/27 ending stocks are reduced 22 million bushels to 722 million"*).

#### 📈 Time series — forecast-vintage revisions (XML)
The report **XML** is cleanly structured (`Report[@sub_report_title]` → `attribute` → `market_year` →
`forecast_month` → `Cell[@cell_value]`), so values are unambiguous (no PDF unit/footnote guessing).
For a record at report month M, the series is that attribute's **this-month projection for the headline
marketing year, stitched across reports** up to M (chronological, `freq: 1m`). Example (2026/27 U.S.
wheat ending stocks): **762 → 744 → 722** (May→Jun→Jul 2026).

**Record shape** (real):
```json
{
  "text": "WHEAT: The outlook for 2026/27 U.S. wheat this month is for lower supplies... Projected 2026/27 ending stocks are reduced 22 million bushels to 722 million...\n\nSuccessive USDA WASDE monthly projections of 2026/27 U.S. wheat ending stocks across the trailing 3 reports (through 2026-07): <ts></ts>",
  "timeseries": [{"values": [762.0, 744.0, 722.0], "unit": "wheat_ending_stocks_mil_bu", "freq": "1m"}],
  "task_type": "world_knowledge", "text_quality": "real",
  "alignment": "recites", "license": "public-domain-us-gov",
  "source": "https://www.usda.gov/oce/commodity/wasde/", "dataset": "wasde",
  "domain": "agriculture", "region": "US", "period_start": "2026-05-01", "period_end": "2026-07-01",
  "meta": {"commodity": "wheat", "attribute": "Ending Stocks", "marketing_year": "2026/27",
           "report_month": "2026-07", "vintage_months": ["2026-05","2026-06","2026-07"], "window": 3}
}
```

**Key issues / caveats:**
- **⚠️ Forecast, not measured (the core caveat).** The series tracks USDA *revising its own projection*
  month to month — a forecast-revision trajectory, not a physical measurement. Alignment is strong
  (`recites`), but this is the opposite of the "measured signal" preference. The measured cousin is USDA
  **NASS Crop Production / Quick Stats** (surveyed actuals) — a natural sibling package.
- **⚠️ Enumeration is gated → local-file build.** The WASDE archive *list* is JS/AJAX-gated on both
  usda.gov/ESMIS and Cornell mannlib, and release IDs are irregular, so **headless full-history harvest is
  blocked**. This build reads reports from a **local `data/` folder**. Supply **both** files per report:
  `wasde{MMYY}.xml` (series) **and** `wasde{MMYY}.pdf` (prose). ~9 recent months are pre-fetched;
  bulk-download older ones in-browser to grow windows toward ~24–36. Full 2010→ (~195) needs a browser
  scrape; 1973→ adds ~440 **scanned** reports (clean scans → OCR-feasible; prose↔table cross-check).
- **⚠️ v1 is wheat only.** Wheat's U.S. table gives ending stocks in bushels, matching the prose — validated
  exactly. **Corn & soybeans read in *metric tons*** in their combined/products tables (Feed-Grain-and-Corn,
  Soybeans-and-Products), so they don't match the bushels prose → they need their bushels-table mapping
  before enabling. Rice/cotton/sugar/sorghum are candidates pending the same per-commodity validation. Add
  validated commodities to `data.commodities` in the config.
- **⚠️ Marketing-year bookkeeping.** Each report shows up to 3 marketing years (last-month + this-month
  columns). We track a **single headline marketing year** across reports to avoid a sawtooth at new-crop
  transitions.
- **Leakage note.** The prose recites the endpoint value (the last series point). Standard for value-reciting
  `recites`; mask the last point if a stricter variant is wanted.

**Run:**
```bash
pip install -r requirements.txt          # + poppler (pdftotext)
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full (all local reports × commodities)
```

**Extend:** drop more `wasde{MMYY}.xml` + `wasde{MMYY}.pdf` into `data/` (windows grow automatically); add
validated commodities to `config.example.yaml`.

**Output:** `output/wasde_cpt.jsonl` + `run_report.json`; `samples/example_output.jsonl`.

**Sources:** [USDA OCE WASDE](https://www.usda.gov/oce/commodity/wasde/) (current) · [ESMIS release files](https://esmis.nal.usda.gov/publication/world-agricultural-supply-and-demand-estimates) · [Cornell mannlib archive 1973→](https://usda.library.cornell.edu/concern/publications/3t945q76s). **US public domain.** See [NOTION_PAGE.md](NOTION_PAGE.md).
