# Statistics Canada "The Daily" — Consumer Price Index → CPT

> **Status: Built** (demo: 50 records, data months 2022-04 → 2026-05). Full build (config `output.max_records:
> null`) walks back to `min_release_date` (default **2015-01-01**, ~135 releases) — new-domain
> **macro_econ / Canada**, sibling of `08_bls_cpi` (US) but a distinct country/source. `alignment`
> is computed per record; demo run so far is **100% `recites`**.

**What it is:** One record = **one CPI release (one data month)** — the release's own lead +
major-component narrative paragraphs from *The Daily* (StatCan's daily bulletin, published
08:30 ET), paired under a **single `<ts>`** with a trailing **24-month** window of the
**not-seasonally-adjusted (NSA) 12-month (year-over-year) % change** for the all-items index +
4 major components (gasoline, food purchased from stores, shelter, transportation) — a
**multi-channel** record, WASDE-style, so the same paragraph isn't duplicated per component.

#### 📄 Text — "The Daily" bulletin
| | |
|---|---|
| **What** | Lead paragraph(s) stating the headline YoY/MoM change, then component-driver sections ("Gasoline prices continue to drive...", "Shelter prices continue to decelerate...", etc.) — real StatCan prose, not boilerplate. |
| **Source** | `https://www150.statcan.gc.ca/n1/daily-quotidien/{YYMMDD}/dq{YYMMDD}{letter}-eng.htm`. **Not guessable** — dates/letters aren't stable per subject (StatCan publishes several unrelated Daily articles most days). Instead, every CPI release page links its own **"Previous release"** (the prior month's CPI article specifically); the build starts at a verified seed URL and **walks that link backward**, discovering each earlier URL from the page itself. Verified stable back to 2016 in this session (structure/markers unchanged); StatCan states the archive has been online since June 1995. |
| **Extraction** | stdlib-only: strip the chart-image/indicator-dashboard `<div class="...sd-thumbnail...">` blocks (nesting-aware scan), then keep `<p>/<h2>/<h3>` blocks in order from just after "Released: …" until a boilerplate heading ("Regional highlights", "Note to readers", …). No HTML parser dependency. |
| `text_quality` | `"real"`. |

#### 📈 Time series — NSA 12-month % change, 5 channels
| | |
|---|---|
| **What** | All-items (anchor) + gasoline, food purchased from stores, shelter, transportation — each the 12-month %-change, trailing 24 months, index-aligned. |
| **Source** | StatCan **Web Data Service (WDS)** REST API (keyless JSON) — table **18-10-0004** "CPI, monthly, not seasonally adjusted", per-channel **vector ID** resolved once via `getSeriesInfoFromCubePidCoord` (recorded in `config.example.yaml`), values fetched via `getDataFromVectorByReferencePeriodRange`. |
| **Computed, not raw** | We pull raw monthly **index levels** and compute the 12-month %-change ourselves: `(level[m] / level[m-12] - 1) * 100`, rounded to 1 decimal. **Verified exact**: computed All-items YoY reproduces the release's own stated 3.2% (May 2026) / 2.8% (April 2026); Gasoline reproduces 33.2% / 28.6%. |
| **Cadence** | `1M`, 24-month trailing window (config knob `data.window_months`, widen toward 32 on request). |

**Record shape** (real — May 2026 release; arrays abbreviated). The `<ts></ts>` tag is appended
directly to the real scraped narrative — **no framing/bridging sentence is generated**; every word
before the tag is verbatim StatCan prose:
```json
{
  "text": "The Consumer Price Index (CPI) increased 3.2% year over year in May, up from a 2.8% gain in April.\n\nHigher prices for gasoline continued to drive the acceleration in the headline CPI in May...\n\nGasoline prices continue to drive acceleration of Consumer Price Index\n\nOn a year-over-year basis, gasoline prices rose at a faster pace in May (+33.2%)...\n\n<ts></ts>",
  "timeseries": [
    {"values": ["...22 more...", 2.8, 3.2], "unit": "cpi_all_items_yoy_pct", "freq": "1M"},
    {"values": ["...22 more...", 28.6, 33.2], "unit": "cpi_gasoline_yoy_pct", "freq": "1M"},
    {"values": ["...22 more...", 3.8, 4.3], "unit": "cpi_food_from_stores_yoy_pct", "freq": "1M"},
    {"values": ["...22 more...", 1.8, 1.7], "unit": "cpi_shelter_yoy_pct", "freq": "1M"},
    {"values": ["...22 more...", "...", "..."], "unit": "cpi_transportation_yoy_pct", "freq": "1M"}
  ],
  "task_type": "world_knowledge", "text_quality": "real", "alignment": "recites",
  "license": "cc-by-4.0", "dataset": "statcan_daily", "domain": "macro_econ", "region": "CA",
  "series_id": "statcan_cpi:2026-05", "period_start": "2024-06-01", "period_end": "2026-05-01",
  "source": "https://www150.statcan.gc.ca/n1/daily-quotidien/260622/dq260622a-eng.htm",
  "meta": {"release_date": "2026-06-22", "data_month": "2026-05",
           "table": "18-10-0004 (CPI, monthly, not seasonally adjusted)",
           "true_license": "Statistics Canada Open Licence",
           "true_license_url": "https://www.statcan.gc.ca/en/reference/licence"}
}
```

**Key issues / caveats:**
- **No generated text.** An earlier version of this build appended a templated closing sentence
  (e.g. "Statistics Canada's Consumer Price Index... trailing 24 months through May 2026:") to
  introduce the multi-channel series before the `<ts></ts>` tag. That sentence was **not** from
  StatCan — it was synthesized by the build script. Fixed 2026-07-24: `<ts></ts>` is now appended
  directly to the real scraped narrative with nothing generated in between. Every character of
  `text` before the tag is verbatim StatCan prose.
- **⚠️ NSA vs. SA ambiguity (handled).** A single release paragraph states BOTH an NSA %-change
  (table 18-10-0004, what we use) and a seasonally-adjusted monthly figure (table 18-10-0006, only
  back to 1992) — e.g. "CPI was up 1.0% month over month... on a seasonally adjusted basis... rose
  0.5%." We anchor **only** on the NSA **12-month** change (deepest history, the number that leads
  almost every release) and never build an SA or monthly-change channel, so there is no ambiguity
  about which table a given series number comes from.
- **⚠️ Reference-base rebasing (handled).** CPI's index base has shifted historically
  (1986=100→1992=100→2002=100...), so raw index *levels* from old vs. new releases aren't
  comparable. We build on the **%-change**, computed fresh from same-vector index levels 12 months
  apart — base-invariant by construction, and literally the number the prose states.
- **⚠️ Scope: 2015-present by default** (`data.min_release_date`), a deliberate conservative choice
  per team guidance even though the %-change design is already base-invariant. Archive is
  machine-readable HTML at least back to the late 1990s; widen by lowering `min_release_date`.
- **License-enum gap.** True license is the **Statistics Canada Open Licence** (free
  reproduce/redistribute/adapt, commercial use OK, attribution required) — not literally one of
  the 5 `cpt_record.schema.json` enum values. We record the closest functional fit,
  `"cc-by-4.0"`, and keep the real name/URL in `meta.true_license(_url)`. Flagged in
  [NOTION_PAGE.md](NOTION_PAGE.md).
- **CPT overlap, not SFT overlap.** `08_bls_cpi` already covers **US** CPI with the same
  release+series pattern; this package is the **Canadian** analogue (different country, source,
  license, API) — additive, not duplicate. No SFT-corpus hit found for StatCan/Canadian CPI.
- **Enumeration depends on the "Previous release" link staying present** on every release page
  (verified across 2016→2026 in this session). If StatCan ever redesigns the template and drops
  it, the walk stops cleanly (`stopped_no_prev_link` / `no_title_match` in `run_report.json`)
  rather than guessing a URL.
- **Alignment is per-record** (computed, not configured): `recites` if the prose states ANY
  channel's current-month YoY value verbatim (e.g. "33.2%"), else `describes`. Demo run: 100%
  recites (headline All-items YoY is essentially always the lead sentence).

**Run:**
```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (~2015->present)
```

**Output:** `output/statcan_daily_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl`
= first 3 records. (`.cache/` git-ignored: cached release HTML + WDS JSON.)

**Sources:** [Statistics Canada "The Daily"](https://www150.statcan.gc.ca/n1/dai-quo/index-eng.htm) ·
[WDS API docs](https://www.statcan.gc.ca/en/developers/wds) · Table
[18-10-0004](https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1810000401) · **Statistics
Canada Open Licence** (https://www.statcan.gc.ca/en/reference/licence). See
[NOTION_PAGE.md](NOTION_PAGE.md).
