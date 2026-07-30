# INSEE "Informations Rapides" (France) — statistical release narrative ↔ series

**Status: Scouted + built (demo, 7 records, CPI anchor indicator), 0 rejected.** Passes
`schema/validate.py --strict` clean (7/7). Domain: economy (France). Scouted + sampled
2026-07-29. **English-only** (enforced in code — see below). Alignment = `recites`.

One record = **one INSEE "Informations Rapides" release's own verbatim English narrative** (the
reciting prose INSEE writes to accompany a statistical release) paired with a **24-month trailing
window of the exact series the narrative recites**. The anchor indicator is the **Consumer Price
Index** — the French analogue of the already-built BLS CPI (#08), StatCan "The Daily" CPI (#52),
and ABS CPI (#53). The narrative states the actual figures in sentences (e.g. *"In July 2025, the
Consumer Price Index (CPI) increased by 0.2% over one month… Year on year, consumer prices went up
by 1.0%… The Harmonised Index of Consumer Prices (HICP) increased by 0.3% over one month"*), and
those figures are exactly the movements of the paired CPI/HICP index series.

## English-only (hard requirement, enforced two ways)

INSEE publishes an English edition for **many but not all** releases. The pipeline guarantees no
French can enter the corpus and nothing is translated/synthesized:
1. **Only the `/en/` URL is ever fetched** (`insee.fr/en/statistiques/{id}`).
2. An explicit **English-language guard** (`_is_english`) runs on the extracted prose and **drops
   any release whose English edition doesn't actually exist** (verified: the French edition of the
   same release extracts to 0 English content and is rejected). Text is 100% verbatim source
   English.

## Key facts

- **Series: INSEE BDM (Banque de Données Macro-économiques) via its open SDMX endpoint** —
  `https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/{idbank}`, **keyless, no token** (the newer
  `portail-api.insee.fr` needs a free OAuth token; the legacy SDMX endpoint used here does not).
  CPI all-items index (`001759970`) back to 1990; HICP (`001759971`) back to ~1996. Alignment
  verified: recited "0.2% m/m / 1.0% y/y" for Jul-2025 = the last two index values' change
  (121.62/121.36−1 = 0.21%; 121.62/120.42−1 = 1.00%), matching to the source's own rounding.
- **Text: `insee.fr/en/statistiques/{id}`.** INSEE's site is a JS single-page app with no
  server-side listing, sitemap, or RSS, so release IDs are enumerated from the **Wayback Machine
  CDX index** of `insee.fr/en/statistiques/` (**6,814 unique English release IDs archived
  2018→**), then each candidate is fetched and kept only if its title matches the target indicator
  AND it passes the English guard. A `seed_ids` config list pins known IDs for a fast/offline demo.
- **Two releases per month, deduped.** Each month has a provisional "flash" estimate ("Over a year,
  the CPI **should** rise by 0.9%…") and a richer definitive release ("consumer prices **rose** by
  0.2% over one month and by 1.0% year on year…"). The build keeps **one record per reference
  month, preferring the definitive**; a month with only a flash still yields a record.
- **Multi-channel, dense.** Each record carries the CPI + HICP index over a 24-month window, both
  ~100% dense (monthly, no gaps). `freq: "1M"`.
- **License: Etalab Open Licence 2.0** (functionally CC-BY: free reuse incl. commercial,
  attribution "Source: Insee"). Tagged `cc-by-4.0` as the closest schema-enum fit, real license
  preserved in `meta.true_license` — same convention as StatCan #52.

## Record shape

```json
{
  "text": "In July 2025, the Consumer Price Index (CPI) increased by 0.2% over one month, after +0.4% in June...\n\n<ts></ts>",
  "timeseries": [
    {"values": ["...24 monthly CPI index values...", 121.36, 121.62], "unit": "cpi_all_items_index_base2015", "freq": "1M"},
    {"values": ["...24 monthly HICP index values...", 124.85, 125.2], "unit": "hicp_all_items_index_base2015", "freq": "1M"}
  ],
  "task_type": "world_knowledge",
  "text_quality": "real",
  "series_id": "insee_informations_rapides:cpi:2025-07",
  "dataset": "insee_informations_rapides",
  "source": "https://www.insee.fr/en/statistiques/8630008",
  "license": "cc-by-4.0",
  "text_source": "first_party_official",
  "alignment": "recites",
  "domain": "economy",
  "region": "FR",
  "period_start": "2023-08",
  "period_end": "2025-07",
  "meta": {"indicator": "cpi", "reference_month": "2025-07", "window_months": 24, "language": "en",
           "true_license": "etalab-open-license-2.0", "series_idbanks": {"cpi_all_items_index_base2015": "001759970", "hicp_all_items_index_base2015": "001759971"}}
}
```

(Real examples: `samples/example_output.jsonl`.)

## Scale

- **CPI anchor alone:** ~2 releases/month (flash + definitive, deduped to 1) × English archive
  depth (English CPI IR confirmed to at least 2018) ≈ **low hundreds** — same tier as the sibling
  BLS/StatCan/ABS CPI packages.
- **Full indicator federation (the real scale play):** INSEE publishes ~150–250 genuinely-narrative
  Informations Rapides per year across CPI, industrial production, GDP, household consumption,
  producer prices, employment/unemployment, foreign trade, business-climate surveys, etc. Each is a
  separate indicator with its own English narrative reciting its own series. Adding an indicator is
  a config entry (`title_contains` + `idbanks`), not new code. The Wayback CDX enumeration already
  proves **~6,800 English releases exist (2018→)**; scoped to the narrative indicators and counting
  FR+EN editions where wanted, the realistic corpus is **~5,000–8,000, clearing ~10k** with full
  archive depth. **This demo builds CPI only** — indicator federation is the documented next step.

## Key open issues

- **Only the CPI indicator is wired.** Others (industrial production, GDP, PPI, …) each need a
  `title_contains` phrase + BDM idbanks verified — the same federate-the-siblings pattern as the
  Fed-survey family and the NASS per-state rollout.
- **Non-monthly cadences dropped.** Annual-average CPI releases (e.g. "Consumer prices accelerated
  on average from 2016 to 2017") are correctly filtered out by the monthly reference-month parser;
  an annual variant would be a separate small indicator config if wanted.
- **Full historical depth via CDX not yet run** — the demo uses `seed_ids`; a server full run sets
  `seed_ids: null` to enumerate all ~6,800 English releases and title-filter.

## Run

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --config config.example.yaml          # demo (seed IDs, CPI)
python scripts/build_cpt_jsonl.py --config config.example.yaml --set seed_ids=null   # full CDX discovery
python ../schema/validate.py output/ --strict
```

Series and text both fetch live (keyless). Output: `output/insee_informations_rapides_cpt.jsonl`.
Source: [INSEE Informations Rapides](https://www.insee.fr/en/statistiques),
[INSEE BDM SDMX](https://bdm.insee.fr/series/sdmx/).
