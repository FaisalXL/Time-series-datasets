# Federal Reserve Regional Business Surveys → CPT

> **Status: Built** (demo: 50 records, Philadelphia MBOS). Full MBOS build ≈ **190 months** (PDF releases ~2010→present). **Federated design** — NY Empire State, Richmond, Dallas TMOS, Kansas City, etc. slot in as additional `data.surveys` entries (see [fed_surveys_discovery.md](../../fed_surveys_discovery.md)).

**What it is:** One record = **one (survey, month)** — a Reserve Bank's monthly survey release narrative (which *recites* the diffusion-index readings: "the general activity index rose to 10.3 from -0.4") paired with a **trailing 24-month window** of those indices. Value-reciting "describes," EIA/BLS-tier, and fully **self-contained** (text + series from one survey — no external join).

**v1 survey: Philadelphia MBOS** (Manufacturing Business Outlook Survey), the flagship regional survey (series to 1968).

#### 📄 Text — monthly release narrative
| | |
|---|---|
| **What** | The release's prose: the executive summary + (for recent years) the detailed index sections. Describes general activity, new orders, shipments, employment, prices, and the 6-month outlook. |
| **Source** | Release **PDF** `…/mbos/{YYYY}/bos{MMYY}.pdf` → `pdftotext` (poppler). Real PDFs ~2010→present (older months are HTML shells → skipped). Chart-axis junk, captions, and the methodology footer are stripped; justified-text hyphenation repaired. |
| **`text_quality`** | `"real"` — a month with no release PDF or too-short narrative is dropped (no synthetic fallback). |

#### 📈 Time series — diffusion indices
| | |
|---|---|
| **What** | 7 channels, trailing 24 months ending at the release month |
| **Source** | `bos_dif.csv` (diffusion indices, **May 1968 → present**, 21 sub-indices), parsed with the **stdlib** (`csv`). |
| **Cadence** | `1M`, 24-month trailing window |

| Channel (`unit`) | MBOS code |
|---|---|
| `general_activity` | GAC |
| `new_orders` | NOC |
| `shipments` | SHC |
| `employment` | NEC |
| `prices_paid` | PPC |
| `prices_received` | PRC |
| `future_general_activity` | GAF (6-month outlook) |

Values are **diffusion indices** (% reporting increase − % reporting decrease; dimensionless, ≈ −100…+100). Series runs to 1968, so every release gets a full 24-month window.

**Record shape** (real — June 2026 MBOS; arrays/text abbreviated):
```json
{
  "text": "Manufacturing activity in the region expanded overall... The survey's indicators for general activity and new orders rebounded into positive territory... The diffusion index for current general activity rose from -0.4 in May to 10.3 in June...\n\n...Manufacturing Business Outlook Survey — monthly diffusion indices (...), trailing 24 months through June 2026: <ts></ts>",
  "timeseries": [
    {"values": ["...", 10.3], "unit": "general_activity", "freq": "1M"},
    {"values": ["...", 27.3], "unit": "new_orders", "freq": "1M"},
    {"values": ["...", 14.9], "unit": "shipments", "freq": "1M"},
    {"values": ["...", "..."], "unit": "employment", "freq": "1M"},
    {"values": ["...", "..."], "unit": "prices_paid", "freq": "1M"},
    {"values": ["...", "..."], "unit": "prices_received", "freq": "1M"},
    {"values": ["...", "..."], "unit": "future_general_activity", "freq": "1M"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "survey": "philadelphia_mbos", "bank": "Federal Reserve Bank of Philadelphia", "district": 3,
  "domain": "manufacturing", "release_month": "2026-06", "window_months": 24,
  "dataset": "fed_regional_surveys", "license": "Public domain (U.S. Government / Federal Reserve)",
  "source": "Federal Reserve Bank of Philadelphia Manufacturing Business Outlook Survey (U.S. public domain)",
  "series_id": "fedsurvey_philadelphia_mbos_2026-06"
}
```

**Key points:**
- **Alignment = describes (verified).** June 2026: narrative *"current general activity rose from -0.4 in May to 10.3 in June"* ↔ `general_activity` series terminal = 10.3. The prose recites the numbers the series holds.
- **⚠️ Real-time vs revised (minor caveat).** `bos_dif.csv` is the **latest seasonally-adjusted** data; each narrative states values **as-of release**. Seasonal-adjustment re-estimation revises history, so an older month's terminal value can differ slightly from the figure its narrative originally quoted (an ALFRED-style vintage effect). The current-month reading is essentially unrevised; deep-history points drift a little.
- **⚠️ FRED overlap (for Charon).** The diffusion-index *series* are also on FRED (Oliver's #9). The **novel element is the release-narrative pairing**, not the series — same situation as #42's XBRL reuse. Get a quick sign-off before scaling. See [NOTION_PAGE.md](NOTION_PAGE.md).
- **Text richness varies by year.** Recent releases (summary + detailed sections) run ~900–5,600 chars; older layouts fall back to the clean ~430-char executive summary. All ≥ `min_text_chars`.
- **Volume:** MBOS ≈ 190 monthly records (2010→). Federating the ~18 Tier-1 surveys (see discovery file) → an estimated ~2–4k. **No short-series problem** (opposite of #26 wildfire).
- **Dependency:** `pdftotext` (poppler) system prerequisite; per-month PDFs + the CSV cached under `.cache/`.

**Run:**
```bash
pip install -r requirements.txt          # + brew install poppler / apt-get install poppler-utils
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (~190 MBOS)
```

**Output:** `output/fed_regional_surveys_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl` = first 3 records. (`.cache/` git-ignored.)

**Adding more surveys:** append an entry to `data.surveys` (bank, `data_csv_url`, `pdf_url_template`, `channels`, `extractor`). Most banks have their own PDF layout, so a new survey usually needs its own `extract_*` function in `scripts/build_cpt_jsonl.py` (registered in `EXTRACTORS`).

**Sources:** [Philadelphia Fed MBOS](https://www.philadelphiafed.org/surveys-and-data/regional-economic-analysis/manufacturing-business-outlook-survey) — **U.S. public domain**. Discovery map of the full survey family: [fed_surveys_discovery.md](../../fed_surveys_discovery.md).
