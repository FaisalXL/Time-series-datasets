# Richmond Fed Fifth District Survey of Service Sector Activity → CPT

> **Status: Built** (demo: 50 records). Full build ≈ **~100 monthly releases** — release-PDF text runs **~2018 → present** (older releases aren't on the live site). The underlying series is far deeper (**Nov-1993→**), so every record gets a full trailing window. One of the Federal Reserve regional business surveys (siblings — MBOS `47_philadelphia_mbos`, Dallas TMOS `48_dallas_tmos`, Richmond Manufacturing `49_richmond_manufacturing` — are separate packages; see [../../docs/fed_surveys_discovery.md](../../docs/fed_surveys_discovery.md)). This is the **service-sector / non-manufacturing** twin of `49`, sharing its plumbing.

**What it is:** One record = **one release month** — the monthly release narrative (which *recites* the diffusion indices: "the revenues index fell to -1 from 14 and the demand index decreased to 3 from 15") paired with a **trailing 24-month window** of those indices. Value-reciting "describes," EIA/BLS-tier. Fifth District = **MD, VA, NC, SC, WV, DC**.

#### 📄 Text — monthly release narrative
| | |
|---|---|
| **What** | The release prose: lead ("Fifth District non-manufacturing activity … according to … the Federal Reserve Bank of Richmond") + sentences reciting the revenues, demand, employment, wages, and local-business-conditions indexes. |
| **Source** | Release **PDF** under `…/non-manufacturing/{YYYY}/pdf/{prefix}_{MM}_{DD}_{YY}.pdf`, where `{MM}_{DD}_{YY}` is the **release date** (~4th Tuesday). The filename **prefix changed from `svc_` (2018–2025) to `nmf_` (late-2025→)**, so the build probes both. No clean archive listing, so the build **computes the candidate release date and probes nearby days**. Real PDFs exist **~2018 → present**. |
| **Extraction** | Chart-heavy PDFs → `pdftotext` interleaves chart axis labels with prose. The `extract_richmond` extractor (shared with `49`) strips chart tokens (month-year axis, "Index, SA", runs of axis numbers), then keeps well-formed sentences from the lead. **Best-effort.** |
| **`text_quality`** | `"real"` — a month with no retrievable release is dropped. |

#### 📈 Time series — diffusion indices
| | |
|---|---|
| **What** | 6 SA current-month channels, trailing 24 months |
| **Source** | `nmf_historicaldata.xlsx` — SA/NSA current + expectations columns, **monthly from Nov-1993**. `.xlsx` parsed with the **stdlib** (`zipfile`+`xml.etree`); dates are Excel serials. |
| **Cadence** | `1M`, 24-month trailing window |

| Channel (`unit`) | Column |
|---|---|
| `revenues` | `sa_svc_revs_sales_c` |
| `demand` | `sa_svc_demand_c` |
| `employment` | `sa_svc_emp_c` |
| `wages` | `sa_svc_ave_wage_c` |
| `local_business_conditions` | `sa_svc_local_bus_cond_c` |
| `capital_expenditures` | `sa_svc_capital_expnd_c` |

Values are **diffusion indices** (% increase − % decrease; ≈ −100…+100). The service survey has **no single "composite"** headline (unlike manufacturing) — **revenues** is the lead index. `average_workweek` is omitted (no SA column in the workbook).

**Record shape** (real — June 2026; arrays/text abbreviated):
```json
{
  "text": "Fifth District non-manufacturing activity was flat in June, according to the most recent survey by the Federal Reserve Bank of Richmond. In June, the revenues index fell to -1 from 14 and the demand index decreased to 3 from 15. Meanwhile, expectations were strong...\n\n...trailing 24 months through June 2026: <ts></ts>",
  "timeseries": [
    {"values": ["...", -1.0], "unit": "revenues", "freq": "1M"},
    {"values": ["...", 3.0], "unit": "demand", "freq": "1M"},
    {"values": ["...", "..."], "unit": "employment", "freq": "1M"},
    {"values": ["...", "..."], "unit": "wages", "freq": "1M"},
    {"values": ["...", "..."], "unit": "local_business_conditions", "freq": "1M"},
    {"values": ["...", "..."], "unit": "capital_expenditures", "freq": "1M"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "bank": "Federal Reserve Bank of Richmond", "survey": "Fifth District Survey of Service Sector Activity",
  "district": 5, "domain": "non-manufacturing", "release_month": "2026-06", "window_months": 24,
  "dataset": "richmond_nonmanufacturing", "license": "Public domain (U.S. Government / Federal Reserve)",
  "series_id": "rich_svc_2026-06"
}
```

**Key issues:**
- **Alignment = describes (verified).** Narrative "revenues index fell to -1 from 14 and the demand index decreased to 3 from 15" ↔ `revenues` = -1 (May 14), `demand` = 3 (May 15) — exact for recent months.
- **⚠️ Vintage drift (minor).** SA columns are re-benchmarked (annual seasonal re-estimation), so older months drift from the as-published figure. Same family-wide caveat as `47`/`48`/`49` (accept ~1–2 pts vs. use the NSA columns).
- **⚠️ Text ~2018→ only.** Live-site release PDFs start ~2018; older releases (back to the 1993 series) aren't on the live site. So ~100 records despite 30+ years of series.
- **⚠️ PDF filename prefix rename** (`svc_`→`nmf_`, ~late 2025); the build probes both prefixes.
- **⚠️ Chart-heavy PDF extraction is best-effort** (shared extractor with `49`).
- **⚠️ FRED overlap.** Series also on FRED (Oliver's #9); the novel element is the release-narrative pairing. Sign-off before scaling. See [NOTION_PAGE.md](NOTION_PAGE.md).
- **Dependency:** `pdftotext` (poppler); XLSX via stdlib. Workbook + release PDFs cached under `.cache/`.

**Run:**
```bash
pip install -r requirements.txt          # + brew install poppler / apt-get install poppler-utils
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (~100)
```

**Output:** `output/richmond_nonmanufacturing_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl` = first 3 records. (`.cache/` git-ignored.)

**Sources:** [Richmond Fed service-sector survey](https://www.richmondfed.org/region_communities/regional_data_analysis/surveys/service_sector) — **U.S. public domain**. Family map: [../../docs/fed_surveys_discovery.md](../../docs/fed_surveys_discovery.md).
