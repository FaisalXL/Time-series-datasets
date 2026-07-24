# ABS Australia CPI (Consumer Price Index) → CPT

> **Status: Built.** **50 records** (demo, `max_records=50`; full build = **69 records**,
> 2019-Q3 → 2026-05, zero drops). **48 recites + 2 describes** (96%). New domain
> **macro_econ**, region **AU**. **License: `cc-by-4.0`** (ABS Creative Commons Attribution
> 4.0). `datasets/53_abs_australia`.

**What it is:** One record = **one ABS CPI release** — the release's own narrative ("Key
statistics" bullets + intro, e.g. *"The Consumer Price Index (CPI) rose 4.0%, down from 4.2%
in the 12 months to April 2026. The largest contributors to annual inflation were Housing
(+6.5%)…"*) paired, under a **single `<ts>`**, with a trailing window (default 24, config
knob) of the **annual (through-the-year) % change** for 4 channels: All-groups CPI (headline),
Housing, Electricity, Food and non-alcoholic beverages — multi-channel, WASDE-style, so the
release paragraph isn't duplicated per component.

**Three real release tracks** (confirmed live against abs.gov.au, not assumed from a date
guess):
| Track | URL pattern | Span (confirmed) | Series dataflow |
|---|---|---|---|
| `quarterly` | `.../consumer-price-index-australia/{mon}[-quarter]-{year}` | 2019-Q3 → 2025-Q3 | `ABS,CPI,2.0.0` (FREQ=Q) |
| `monthly_indicator` | `.../monthly-consumer-price-index-indicator/{mon}-{year}` | 2022-10 → 2025-09 | `ABS,CPI_M,1.2.0` (FREQ=M) |
| `monthly_primary` | `.../consumer-price-index-australia/{mon}-{year}` | 2025-10 → present | `ABS,CPI,2.0.0` (FREQ=M) |

Monthly CPI became the **primary** release from the Oct-2025 reference month (published Nov
2025), replacing both the quarterly release and the separate monthly-indicator release, which
both ended then — a real cadence switch, not a fabricated stitch. (2018 releases 404 on this
URL scheme; pre-2019 needs the legacy AUSSTATS domain, out of scope per the original scouting.)

**Record shape** (real — May 2026, values abridged). The `<ts></ts>` tag is appended directly to
the real scraped release prose — **no framing/bridging sentence is generated**; every word before
the tag is verbatim ABS text:
```json
{
  "text": "The Consumer Price Index (CPI) measures household inflation... In the 12 months to May 2026: The Consumer Price Index (CPI) rose 4.0%, down from 4.2%... Housing (+6.5%)... Trimmed mean inflation was 3.6%...\n\n<ts></ts>",
  "timeseries": [
    {"values": ["...", 4.0], "unit": "cpi_all_groups_annual_pct", "freq": "1M"},
    {"values": ["...", 6.5], "unit": "cpi_housing_annual_pct", "freq": "1M"},
    {"values": ["...", 21.1], "unit": "cpi_electricity_annual_pct", "freq": "1M"},
    {"values": ["...", 3.3], "unit": "cpi_food_annual_pct", "freq": "1M"}
  ],
  "task_type": "world_knowledge", "text_quality": "real", "alignment": "recites",
  "license": "cc-by-4.0", "domain": "macro_econ", "region": "AU",
  "source": "https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia/may-2026",
  "series_id": "abs_cpi_monthly_primary_2026-05", "dataset": "abs_australia",
  "meta": {"track": "monthly_primary", "cadence": "month", "window": 24, "n_channels": 4}
}
```

**Key issues / caveats:**
- **No generated text.** An earlier version of this build appended a templated closing sentence
  (e.g. "ABS Consumer Price Index, Australia (monthly) -- the annual... trailing 24 months through
  May 2026:") to introduce the multi-channel series before the `<ts></ts>` tag. That sentence was
  **not** from ABS — it was synthesized by the build script. Fixed 2026-07-24: `<ts></ts>` is now
  appended directly to the real scraped prose with nothing generated in between. Every character
  of `text` before the tag is verbatim ABS text.
- **ABS Data API host + region-code trap (handled).** Series come from the **live**
  `data.api.abs.gov.au` SDMX-JSON API (the old `api.data.abs.gov.au` is dead — not used). An
  unqualified/wildcarded `REGION` dimension returns **every capital city bundled with
  Australia** — grabbing "the first series key" would silently substitute a city for the
  nation. Every fetch explicitly keys `REGION=50` (Australia; **not** `"AUS"` — the real
  `CL_CPI_REGION` codelist uses bare numeric codes) **and** `assert_national()` re-checks the
  response's own dimension metadata equals exactly `{"50"}` before any value is accepted —
  raises, doesn't silently continue, if that ever fails.
- **Monthly-vs-quarterly cadence (real, verified directly, not assumed).** The original brief
  guessed the monthly CPI indicator "starts April 2024" — checked live and that's wrong: the
  **standalone monthly CPI indicator** ran Oct 2022 → Sep 2025 (own dataflow `CPI_M 1.2.0`,
  back-series to 2018-09), alongside the **quarterly** release (own dataflow `CPI 2.0.0`,
  index numbers to **1948**) which ran through Sep-2025; monthly became the sole **primary**
  release only from Oct-2025 (`CPI 2.0.0` FREQ=M, itself only populated from ~2025-04). Handled
  as **three real tracks** (table above), not a fabricated 2024 seam.
- **Quarterly annual % change is derived, not fetched pre-computed.** `ABS,CPI,2.0.0` publishes
  Index Numbers (measure=1) but **not** "% change from previous year" (measure=3 → HTTP 404 for
  every index code tried) at quarterly frequency. We derive it ourselves from the real published
  index numbers (4-quarter lag) — verified exact against a live release (derived 2024-Q1 =
  **3.6%**, matching the March-2024-quarter release's own stated "3.6%"). Monthly annual % change
  **is** published directly (measure=3) and used as-is.
- **⚠️ Revision drift is real (caught by the per-record alignment check, not hidden).** CPI
  index numbers are periodically revised/reweighted, so a quarterly annual % change derived from
  **today's** index can differ slightly from what a release stated **at the time**: the
  Sep-2023-quarter release said "the CPI rose 5.4%"; deriving from the current index gives
  **5.3%** — a ~0.1pt vintage gap, not a bug. Alignment is computed **per record** (recites iff
  the prose states the channel's own endpoint, else describes) exactly to catch this — **48
  recites + 2 describes**, both `describes` cases are real vintage-drift misses, not extraction
  failures. Trimmed mean / seasonally-adjusted channels were **not** included as channels for
  this reason (Housing/Electricity/Food/headline are all `TSEST=Original`).
- **Prose extraction is HTML, not PDF** (unlike the Fed-survey/WASDE PDF packages) — `<p>`/`<li>`
  text from the release's own "Key statistics" bullets + intro, filtered for chart-embedded JSON
  (`&quot;…&quot;` Highcharts payloads inside `<p>` tags), download-button labels, and
  referent-less deep sub-section bullets ("…the group rose X%" with no heading captured).
  Best-effort, same spirit as Richmond's chart-PDF stripper.
- **License: CC BY 4.0**, confirmed at abs.gov.au's own copyright page — standard carve-outs
  (Coat of Arms, ABS logo, trademarked material, microdata, third-party content, sub-brands,
  Indigenous/Census branding); no NC surprise, no citation-only surprise.
- **Scale is modest and honest.** 69 real releases total (25 quarterly + 36 monthly-indicator +
  8 monthly-primary as of this build) — CPI is the **anchor** indicator per the original
  scouting brief, not a path to thousands alone; Labour Force (unemployment rate, also
  cross-checked live: release said "remains at 4.4%", API gives 4.43%) is a natural next sibling
  package on the same API/site pattern.
- **Domain-adjacent existing package:** `08_bls_cpi` (US BLS CPI) already exists in this repo —
  different country/source/license (US public-domain vs. AU CC-BY-4.0), so not a literal SFT/CPT
  duplicate, but flagged in [NOTION_PAGE.md](NOTION_PAGE.md) for team visibility.

**Run:**
```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (69, 2019-Q3->2026-05)
```

**Output:** `output/abs_australia_cpt.jsonl` + `output/run_report.json`;
`samples/example_output.jsonl` = first 3 records. (`.cache/` git-ignored.)

**Sources:** [ABS Consumer Price Index, Australia](https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia) ·
[ABS Monthly CPI Indicator (legacy topic)](https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/monthly-consumer-price-index-indicator) ·
[ABS Data API](https://data.api.abs.gov.au/rest/data/) ·
[ABS copyright / CC BY 4.0](https://www.abs.gov.au/website-privacy-copyright-and-disclaimer).
See [NOTION_PAGE.md](NOTION_PAGE.md).
