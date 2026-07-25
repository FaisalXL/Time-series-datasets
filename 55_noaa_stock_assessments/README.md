# NOAA Fisheries Stock Assessments (Stock SMART)

**Status: Built — 50 demo records, 0 rejected.** Domain: fisheries (US). Passes
`schema/validate.py --strict`, 0 errors/0 warnings. Full build not yet run (see below).

One record = **one (stock, assessment) pair**. Text is that assessment's own real final
report — a NOAA/regional-council scientific document narrating stock status, biomass trends,
exploitation history, and management reference points. Series is that stock's own historical
**catch, fishing mortality, recruitment, and abundance/biomass** time series, all channels
aligned to a shared year range through the assessment's own last-data-year. Alignment =
`describes` (reports narrate stock trends/dynamics directly off their own data, not a pure
recite-every-number style). **Real per-record depth is deep — median 43.5 years, up to 149 —
far beyond the 24-32-period baseline other packages in this project target.**

## Key facts

- **Source: NOAA's Stock SMART public JSON API**, reverse-engineered from the NOAA-EDAB/
  stocksmart R package's own data-pull script (no auth, no key). Confirmed live: 427 stock
  entities, 3,527 real (stock, assessment) rows with jurisdiction + report-link metadata.
- **License filter applied and verified against the real jurisdiction distribution** (not
  assumed): excludes `Atlantic HMS` (ICCAT-linked international tuna/billfish/shark species,
  79 rows), `IPHC` (a real US/Canada joint international commission, 10 rows), and `ASMFC`
  (an interstate compact, not a federal NOAA jurisdiction, 2 rows). Everything kept is a
  genuine US federal regional fishery management council (PFMC, NPFMC, NEFMC, GMFMC, SAFMC,
  MAFMC, WPFMC, CFMC, or a joint pairing of these) — `public-domain-us-gov`.
- **Reports over 80 pages are skipped** — large reports are almost always multi-stock omnibus
  documents (found one at 362 pages covering dozens of salmon stocks); pairing the WHOLE
  document with one stock's series would dilute alignment (most of the text would be about
  other stocks). This trades some scale for tighter, more genuinely-per-stock text.
- Some reports are explicitly labeled "draft working paper for peer review only" in their own
  header — this is normal for NOAA's Management Track Assessment process, not a quality defect;
  these are the actual operative documents, not superseded by a separate final version.
- A cover-page drop-cap rendering artifact ("R 2025\nEVIEW OF" instead of "REVIEW OF") appears
  on some reports' title pages only — every page checked after the cover extracts clean prose.

## Run

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3
python scripts/build_cpt_jsonl.py
python ../schema/validate.py output/ --strict
```

Output: `output/noaa_stock_assessments_cpt.jsonl` (50) · `output/run_report.json` ·
`samples/example_output.jsonl`. Source: [NOAA Stock SMART](https://www.fisheries.noaa.gov/resource/tool-app/stock-smart).
Full-scale count not yet measured — the >80-page filter and jurisdiction filter both reduce
the raw 3,527-row universe by an amount only a full run will pin down exactly; see NOTION_PAGE.md.
