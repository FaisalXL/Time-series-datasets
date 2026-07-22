UPDATE: devset built (demo 50; full ~100) @ https://github.com/FaisalXL/Time-series-datasets/tree/main/50_richmond_nonmanufacturing/output

**Repo:** https://github.com/FaisalXL/Time-series-datasets/tree/main/50_richmond_nonmanufacturing

**Domain:** Macro / regional service-sector conditions · **Status:** Built (demo 50) · **License:** Public domain (U.S. Federal Reserve)

> One record = **one release month** — the Richmond Fed *Fifth District Survey of Service
Sector Activity* release narrative (which recites the diffusion indices) + a trailing
**24-month** window of those indices. Value-reciting "describes." Fifth District = MD/VA/NC/SC/WV/DC.
The **service-sector twin of `49`** (Richmond Manufacturing) — same bank, same site, same
`extract_richmond` plumbing, just the `non-manufacturing` segment + `nmf_`/`svc_` files.
>

---

> ✅ **Alignment verified.** June 2026: *"the revenues index fell to **-1** from 14 and the
demand index decreased to **3** from 15"* ↔ `revenues` = **-1** (May 14), `demand` = **3**
(May 15) — exact.
>
> ⚠️ **Caveats (family-wide):** (1) release-PDF text only **~2018→** (~100 records despite
series to 1993); (2) **PDF prefix rename** `svc_`→`nmf_` ~late-2025 (build probes both);
(3) **chart-heavy PDFs** → extraction best-effort; (4) **vintage drift** (SA re-benchmark)
on older months; (5) **FRED overlap** sign-off.
>

## Why this one was cheap
Pure sibling reuse: cloned `49_richmond_manufacturing`, changed the source segment
(`manufacturing`→`non-manufacturing`), the workbook (`nmf_historicaldata.xlsx`), the SA
channel columns (`sa_svc_*_c`), and taught `fetch_narrative` to probe two filename prefixes.
The chart-heavy `extract_richmond` extractor transferred **unchanged**. This is the vein
strategy working: one survey's plumbing amortises across its siblings.

## Record shape
```json
{
  "text": "Fifth District non-manufacturing activity was flat in June, according to the most recent survey by the Federal Reserve Bank of Richmond. In June, the revenues index fell to -1 from 14 and the demand index decreased to 3 from 15...\n\n...trailing 24 months through June 2026: <ts></ts>",
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
  "dataset": "richmond_nonmanufacturing", "license": "Public domain (U.S. Federal Reserve)", "series_id": "rich_svc_2026-06"
}
```

## Design decisions (resolved)
- **One record per release month**, trailing 24-month window (series to 1993 → always full).
- **6 SA current channels** (no "composite" for services → **revenues** is the headline; `average_workweek` dropped — no SA column).
- **Computed release-date enumeration** + **dual prefix probe** (`nmf_`/`svc_`).
- **XLSX parsed with stdlib**; Excel-serial dates. Shared `extract_richmond`.
- **One dataset per survey**; public domain → output committed.

## Open questions (for discussion)
- **Vintage drift (family-wide):** accept ~1–2pt on older months, use NSA columns, or source original-vintage? (Same call as `47`/`48`/`49`.)
- **Fill pre-2018 text?** FRASER/Wayback could extend below 2018.
- **FRED overlap** sign-off (Charon).
- **Next sibling:** Dallas TSSOS (service-sector twin of `48`) or Kansas City.

## Source data (Richmond Fed — U.S. public domain)
| File | Use |
| --- | --- |
| `…/non-manufacturing/data/nmf_historicaldata.xlsx` | Series — SA/NSA current + expectations, Nov-1993→ (XLSX) |
| `…/non-manufacturing/{YYYY}/pdf/{nmf,svc}_{MM}_{DD}_{YY}.pdf` | Release narrative (~2018→) |

*(Family map in `../../docs/fed_surveys_discovery.md`. Build flags in `README.md`. Needs `pdftotext`; build with repo `.venv/bin/python`.)*
