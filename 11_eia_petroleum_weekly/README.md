# EIA Weekly Petroleum Status Report (Highlights) + Weekly Supply Series → CPT

> **Status: Built** (demo: 50 records). Full build ≈ **779 records** (every WPSR release, weekly Aug 2011 → present). Run with `output.max_records=null`.

**What it is:** One record = **one weekly report** — the EIA Weekly Petroleum Status Report (WPSR) **"Highlights" narrative**, which recites the week's crude / gasoline / distillate inventories, refinery inputs, utilization and imports, paired with a **trailing 52-week window** of the exact national supply series it describes. The prose *describes* the numbers → the tight "describes" alignment class (same as BLS CPI / earnings calls).

**Fully open:** EIA data **and** report text are **U.S. Government works → public domain**. No license gate, no key required.

#### 📄 Text — WPSR "Highlights"
| | |
|---|---|
| **What** | The official weekly Highlights narrative (~6–9 sentences): refinery inputs & utilization, gasoline/distillate production, crude & product imports, and crude/gasoline/distillate/propane inventory changes with current levels and vs. the five-year average. |
| **Source** | Highlights PDF per release, enumerated from the [WPSR archive index](https://www.eia.gov/petroleum/supply/weekly/archive/) (~779 dates, `YYYY_MM_DD`, back to 2011-08-03). URL: `…/archive/{year}/{date}/pdf/highlights.pdf`. Extracted with **`pdftotext`** (poppler) in reading order, table rows/captions stripped. |
| **`text_quality`** | `"real"` — a report whose text or window can't be assembled is dropped (no synthetic fallback). |
| **Join key** | the *"week ending {Month DD, YYYY}"* string parsed from the text = the data week-ending Friday, which anchors the series window. |

#### 📈 Time series — EIA bulk weekly supply
| | |
|---|---|
| **What** | 6 national weekly channels, trailing 52 weeks ending at the report's data week |
| **Source** | EIA bulk [`PET.zip`](https://www.eia.gov/opendata/bulk/PET.zip) (keyless, public domain) — one JSON line per series; parsed with the **stdlib** (`zipfile`/`json`). Period key = week-ending Friday. |
| **Cadence** | `1W`, 52-week trailing window |

| Channel (`unit`) | Bulk series id | Span |
|---|---|---|
| `crude_stocks_exspr` (thousand_barrels) | `PET.WCESTUS1.W` | 1982→ |
| `gasoline_stocks_total` (thousand_barrels) | `PET.WGTSTUS1.W` | 1990→ |
| `distillate_stocks` (thousand_barrels) | `PET.WDISTUS1.W` | 1982→ |
| `refinery_crude_inputs` (thousand_barrels_per_day) | `PET.WCRRIUS2.W` | 1982→ |
| `refinery_utilization` (percent) | `PET.WPULEUS3.W` | 1990→ |
| `crude_imports_exspr` (thousand_barrels_per_day) | `PET.WCEIMUS2.W` | 1982→ |

All channels run to the present and back to 1982/1990, so **every 2011→ report gets a full 52-week window** (no short-history dropouts).

**Why 52 weeks?** Measured empirically across 8 years of Highlights: every report anchors on week-over-week change, trailing **4-week** averages, and — most frequently (9–13 mentions/report) — **year-over-year** comparisons ("the same period last year"). A 52-week window grounds all of these (the year-ago point the text keeps citing is *inside* the window); only the "five-year seasonal average" stays an external derived baseline.

**Record shape** (real — 2026-07-01 report, arrays abbreviated):
```json
{
  "text": "U.S. crude oil refinery inputs averaged 17.2 million barrels per day during the week ending June 26, 2026, which was 85 thousand barrels per day more than the previous week's average. Refineries operated at 96.6% of their operable capacity... U.S. commercial crude oil inventories (excluding the SPR) ... At 408.4 million barrels ...\n\nWeekly U.S. petroleum supply and inventories ..., trailing 52 weeks through the week ending 2026-06-26: <ts></ts>",
  "timeseries": [
    {"values": ["...", 408359.0], "unit": "crude_stocks_exspr", "freq": "1W"},
    {"values": ["...", 213966.0], "unit": "gasoline_stocks_total", "freq": "1W"},
    {"values": ["...", 108599.0], "unit": "distillate_stocks", "freq": "1W"},
    {"values": ["...", 17196.0], "unit": "refinery_crude_inputs", "freq": "1W"},
    {"values": ["...", 96.6], "unit": "refinery_utilization", "freq": "1W"},
    {"values": ["...", 5279.0], "unit": "crude_imports_exspr", "freq": "1W"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "report_date": "2026-07-01", "data_week_ending": "2026-06-26", "window_weeks": 52,
  "dataset": "eia_petroleum_weekly", "source": "eia.gov (U.S. Government, public domain)",
  "report_url": "https://www.eia.gov/petroleum/supply/weekly/archive/2026/2026_07_01/pdf/highlights.pdf",
  "series_id": "eiapet_20260626"
}
```

**Key points:**
- **Alignment = describes (verified).** In the 50-record demo, **all 50** records' text-stated crude level ("At X million barrels") matched the crude-stock series' terminal value to within ±0.6 Mbbl — the number the narrative states *is* the last point of the series.
- **Terminal-value leakage is inherent and the point** (as with BLS CPI / earnings): the Highlights state the current week's level = the last TS point. Window ends at the report's data week.
- **Volume:** ~779 weekly reports (2011→). The TS is far longer (to 1982); text-archive depth is the record cap. Value is domain diversity (energy) + clean describes + zero license friction, not scale.
- **`freq: 1W`** weekly cadence.
- **Dependency:** `pdftotext` (poppler) is a system prerequisite — see requirements.txt. Bulk `PET.zip` (61 MB) and per-week PDFs are cached under `.cache/`.

**Run:**
```bash
pip install -r requirements.txt          # + brew install poppler / apt-get install poppler-utils
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (~779)
```

**Output:** `output/eia_petroleum_weekly_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl` = first 3 records. (`.cache/` git-ignored.)

**Sources:** [EIA Weekly Petroleum Status Report](https://www.eia.gov/petroleum/supply/weekly/) · [EIA Open Data bulk files](https://www.eia.gov/opendata/bulkfiles.php) — both **U.S. Government, public domain**.
