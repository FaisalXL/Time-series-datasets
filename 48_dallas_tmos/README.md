# Dallas Fed Texas Manufacturing Outlook Survey (TMOS) → CPT

> **Status: Built** (demo: 50 records). Full build ≈ **~195 monthly releases** — PDF releases **2007–2020** + HTML releases **2024→present**, with a **2021–2023 gap** (see Key issues). One of the Federal Reserve regional business surveys (siblings — MBOS `47_philadelphia_mbos`, plus Dallas TSSOS/Energy/Ag/Banking — are separate packages; see [fed_surveys_discovery.md](../../docs/fed_surveys_discovery.md)).

**What it is:** One record = **one release month** — the TMOS monthly release narrative (which *recites* the diffusion-index readings: "the general business activity index inched up to -12.7… the employment index moved up two points to 5.7") paired with a **trailing 24-month window** of those indices. Value-reciting "describes," EIA/BLS-tier, self-contained.

#### 📄 Text — monthly release narrative
| | |
|---|---|
| **What** | The full release prose: lead ("Texas factory/manufacturing activity…") + paragraphs on production, business activity, company outlook, employment, hours, prices, wages, and the 6-month outlook. Rich (~1,900–2,600 chars). |
| **Source** | **Two eras, both on dallasfed.org:** release **PDF** `…/tmos/{YYYY}/tmos{YYMM}.pdf` for **2007–2020** (→ `pdftotext`), and the **HTML** release page `…/research/surveys/tmos/{YYYY}/{YYMM}` for **2024→present** (stdlib tag-strip). The build tries the PDF first, then the HTML. |
| **Extraction** | Anchored on the invariant lead phrase "…responding to the Texas Manufacturing Outlook Survey" (the subject wording varies by era), through to the "Next release / Data were collected" boilerplate. |
| **`text_quality`** | `"real"` — a month with no retrievable release is dropped. |

#### 📈 Time series — diffusion indices
| | |
|---|---|
| **What** | 7 channels, trailing 24 months ending at the release month |
| **Source** | `index_sa.xls` — seasonally-adjusted diffusion indices, **monthly Jun-2004 → present** (34 sub-indices). Despite the `.xls` name it's **XLSX**, parsed with the **stdlib** (`zipfile` + `xml.etree`) — no `openpyxl`/pandas. |
| **Cadence** | `1M`, 24-month trailing window |

| Channel (`unit`) | TMOS column |
|---|---|
| `general_business_activity` | Bact |
| `production` | Prod |
| `new_orders` | Vnwo |
| `shipments` | Vshp |
| `employment` | Nemp |
| `prices_raw_materials` | Prm |
| `company_outlook` | Colk |

Values are **diffusion indices** (% reporting increase − % reporting decrease; ≈ −100…+100). Series to 2004, so every release gets a full 24-month window.

**Record shape** (real — June 2026; arrays/text abbreviated):
```json
{
  "text": "Texas manufacturing output growth decelerated in June, according to business executives responding to the Texas Manufacturing Outlook Survey. The production index... fell five points to 4.1... The capacity utilization index ticked up to 7.3...\n\n...trailing 24 months through June 2026: <ts></ts>",
  "timeseries": [
    {"values": ["...", 0.0], "unit": "general_business_activity", "freq": "1M"},
    {"values": ["...", 4.1], "unit": "production", "freq": "1M"},
    {"values": ["...", 2.3], "unit": "new_orders", "freq": "1M"},
    {"values": ["...", 7.1], "unit": "shipments", "freq": "1M"},
    {"values": ["...", "..."], "unit": "employment", "freq": "1M"},
    {"values": ["...", "..."], "unit": "prices_raw_materials", "freq": "1M"},
    {"values": ["...", "..."], "unit": "company_outlook", "freq": "1M"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "bank": "Federal Reserve Bank of Dallas", "survey": "Texas Manufacturing Outlook Survey",
  "district": 11, "domain": "manufacturing", "release_month": "2026-06", "window_months": 24,
  "dataset": "dallas_tmos", "license": "Public domain (U.S. Government / Federal Reserve)",
  "source": "Federal Reserve Bank of Dallas Texas Manufacturing Outlook Survey (U.S. public domain)",
  "series_id": "tmos_2026-06"
}
```

**Key issues:**
- **Alignment = describes (verified).** The narrative recites the indices ("general business activity index inched up to -12.7"); recent months match the series exactly.
- **⚠️ 2021–2023 text gap.** Dallas's PDF release archive runs 2007→2020 and the HTML release pages start 2024; the 2021–2023 releases sit under some intermediate scheme I could not locate on dallasfed.org (likely a CMS-transition era). Those ~36 months are currently **dropped** (`no_text`). Fillable later via FRASER (`fraser.stlouisfed.org`, timed out from this environment) or the Wayback Machine. Full build ≈ ~195 months **with this hole**.
- **⚠️ Vintage drift (minor).** `index_sa.xls` is the **latest** seasonally-adjusted series; Dallas **re-benchmarks the whole SA history annually** (stated in each release). So an older month's series value drifts from the figure its narrative originally quoted — measured at **~0.5–2 points** (e.g. 2015-10 narrated -12.7 vs current -14.5). Direction/movements always hold; recent months match closely. Same class of caveat as the sibling surveys — a family-wide decision (accept drift vs. source original-vintage) worth raising with Charon.
- **⚠️ FRED overlap.** The indices are also on FRED (Oliver's #9); the novel element is the **release-narrative pairing** (cf. #42 XBRL reuse). Sign-off before scaling. See [NOTION_PAGE.md](NOTION_PAGE.md).
- **Dependency:** `pdftotext` (poppler) for the 2007–2020 PDFs; XLSX + HTML via stdlib. Releases + workbook cached under `.cache/`.

**Run:**
```bash
pip install -r requirements.txt          # + brew install poppler / apt-get install poppler-utils
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (~195)
```

**Output:** `output/dallas_tmos_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl` = first 3 records. (`.cache/` git-ignored.)

**Sources:** [Dallas Fed TMOS](https://www.dallasfed.org/research/surveys/tmos) — **U.S. public domain**. Family map: [fed_surveys_discovery.md](../../docs/fed_surveys_discovery.md).
