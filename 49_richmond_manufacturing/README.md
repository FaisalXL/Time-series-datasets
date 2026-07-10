# Richmond Fed Fifth District Survey of Manufacturing Activity → CPT

> **Status: Built** (demo: 50 records). Full build ≈ **~100 monthly releases** — release-PDF text runs **~2018 → present** (older releases aren't on the live site). The underlying series is far deeper (**1993→**), so every record gets a full trailing window. One of the Federal Reserve regional business surveys (siblings — MBOS `47_philadelphia_mbos`, Dallas TMOS `48_dallas_tmos`, and a Richmond Non-Manufacturing survey — are separate packages; see [../../docs/fed_surveys_discovery.md](../../docs/fed_surveys_discovery.md)).

**What it is:** One record = **one release month** — the monthly release narrative (which *recites* the diffusion indices: "the composite manufacturing index decreased to 4 in June from 13 in May") paired with a **trailing 24-month window** of those indices. Value-reciting "describes," EIA/BLS-tier. Fifth District = **MD, VA, NC, SC, WV, DC**.

#### 📄 Text — monthly release narrative
| | |
|---|---|
| **What** | The release prose: lead ("Fifth District manufacturing activity … according to … the Federal Reserve Bank of Richmond") + sentences reciting the composite and component indexes, backlog, prices, employment, local business conditions. |
| **Source** | Release **PDF** `…/manufacturing/{YYYY}/pdf/mfg_{MM}_{DD}_{YY}.pdf`, where `{MM}_{DD}_{YY}` is the **release date** (~4th Tuesday). No clean archive listing, so the build **computes the candidate release date and probes nearby days**. Real PDFs exist **~2018 → present**. |
| **Extraction** | Chart-heavy PDFs → `pdftotext` interleaves chart axis labels with prose. The extractor strips chart tokens (month-year axis, "Index, SA", runs of axis numbers), then keeps well-formed sentences from the lead. **Best-effort** — an occasional numeric value can drop out (see caveats). |
| **`text_quality`** | `"real"` — a month with no retrievable release is dropped. |

#### 📈 Time series — diffusion indices
| | |
|---|---|
| **What** | 7 SA current-month channels, trailing 24 months |
| **Source** | `mfg_historicaldata.xlsx` — composite + SA/NSA sub-indices (current + 6-mo expectations), **monthly from Nov-1993** (composite + core) / ~1997 (cap-util, wages). `.xlsx` parsed with the **stdlib** (`zipfile`+`xml.etree`); dates are Excel serials. |
| **Cadence** | `1M`, 24-month trailing window |

| Channel (`unit`) | Column |
|---|---|
| `composite_index` | `sa_mfg_composite` (weighted avg: shipments 33% + new orders 40% + employment 27%) |
| `shipments` | `sa_mfg_ship_c` |
| `new_orders` | `sa_mfg_new_orders_c` |
| `employment` | `sa_mfg_emp_c` |
| `order_backlog` | `sa_mfg_bk_logs_c` |
| `capacity_utilization` | `sa_mfg_cap_util_c` |
| `wages` | `sa_mfg_wage_c` |

Values are **diffusion indices** (% increase − % decrease; ≈ −100…+100).

**Record shape** (real — June 2026; arrays/text abbreviated):
```json
{
  "text": "Fifth District manufacturing activity was flat in June, according to the most recent survey from the Federal Reserve Bank of Richmond. The composite manufacturing index decreased to 4 in June from 13 in May. All three of its component indexes fell in June... Shipments fell to 3 from 16, new orders to 9 from 17, and employment to -1 from 3.\n\n...trailing 24 months through June 2026: <ts></ts>",
  "timeseries": [
    {"values": ["...", 4.0], "unit": "composite_index", "freq": "1M"},
    {"values": ["...", 3.0], "unit": "shipments", "freq": "1M"},
    {"values": ["...", 9.0], "unit": "new_orders", "freq": "1M"},
    {"values": ["...", -1.0], "unit": "employment", "freq": "1M"},
    {"values": ["...", "..."], "unit": "order_backlog", "freq": "1M"},
    {"values": ["...", "..."], "unit": "capacity_utilization", "freq": "1M"},
    {"values": ["...", "..."], "unit": "wages", "freq": "1M"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "bank": "Federal Reserve Bank of Richmond", "survey": "Fifth District Survey of Manufacturing Activity",
  "district": 5, "domain": "manufacturing", "release_month": "2026-06", "window_months": 24,
  "dataset": "richmond_manufacturing", "license": "Public domain (U.S. Government / Federal Reserve)",
  "source": "Federal Reserve Bank of Richmond Fifth District Survey of Manufacturing Activity (U.S. public domain)",
  "series_id": "rich_mfg_2026-06"
}
```

**Key issues:**
- **Alignment = describes (verified).** Narrative "composite … decreased to 4 in June from 13 in May" ↔ `composite_index` = 4 (May 13) — exact for recent months.
- **⚠️ Vintage drift (minor).** `sa_mfg_composite` is re-benchmarked (annual seasonal re-estimation), so older months drift from the as-published figure: **2025+ median gap 0.0 (exact); pre-2024 median ~2 pts (max ~5)**. Direction/movements hold. Same family-wide caveat — worth a call with the team (accept vs. use the NSA columns, which Richmond also ships).
- **⚠️ Text ~2018→ only.** Live-site release PDFs start ~2018; older releases (back to the 1993 data) aren't on the live site (CMS migration — FRASER/Wayback could extend). So ~100 records despite 30+ years of series.
- **⚠️ Chart-heavy PDF extraction is best-effort.** The extractor strips chart junk and recovers clean sentences, but an occasional value-tail can drop; extraction quality is lower than the clean-PDF siblings (MBOS/TMOS).
- **⚠️ FRED overlap.** Series also on FRED (Oliver's #9); the novel element is the release-narrative pairing (cf. #42 XBRL reuse). Sign-off before scaling. See [NOTION_PAGE.md](NOTION_PAGE.md).
- **Dependency:** `pdftotext` (poppler); XLSX via stdlib. Workbook + release PDFs cached under `.cache/`.

**Run:**
```bash
pip install -r requirements.txt          # + brew install poppler / apt-get install poppler-utils
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (~100)
```

**Output:** `output/richmond_manufacturing_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl` = first 3 records. (`.cache/` git-ignored.)

**Sources:** [Richmond Fed manufacturing survey](https://www.richmondfed.org/research/regional_economy/surveys_of_business_conditions/manufacturing) — **U.S. public domain**. Family map: [../../docs/fed_surveys_discovery.md](../../docs/fed_surveys_discovery.md).
